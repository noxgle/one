from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from one.config import get_agent_dir
from one.core.persistence import (
    append_private_text,
    atomic_write_text,
    ensure_private_dir,
    ensure_private_file,
)

CURRENT_SESSION_VERSION = 4


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _id() -> str:
    return uuid.uuid4().hex[:8]


def get_default_session_dir(cwd: str, agent_dir: str | None = None) -> str:
    a = Path(agent_dir or get_agent_dir())
    safe = "--" + cwd.strip("/\\").replace("/", "-").replace("\\", "-").replace(":", "-") + "--"
    d = a / "sessions" / safe
    ensure_private_dir(d)
    return str(d)


@dataclass
class SessionInfo:
    path: str
    id: str
    cwd: str
    name: str | None
    created: datetime
    modified: datetime
    message_count: int


class SessionManager:
    def __init__(self, cwd: str, session_dir: str, session_file: str | None, persist: bool) -> None:
        self._cwd = cwd
        self._session_dir = session_dir
        self._persist = persist
        self._session_file = str(Path(session_file).resolve()) if session_file else None
        self._entries: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._leaf_id: str | None = None
        if persist and session_dir:
            ensure_private_dir(Path(session_dir))
        if self._session_file and Path(self._session_file).exists():
            self._load(self._session_file)
        else:
            self._new_session()

    def _new_session(self) -> None:
        sid = str(uuid.uuid4())
        header = {
            "type": "session",
            "version": CURRENT_SESSION_VERSION,
            "id": sid,
            "timestamp": _now_iso(),
            "cwd": self._cwd,
        }
        self._entries = [header]
        self._by_id = {}
        self._leaf_id = None
        if self._persist:
            ts = datetime.utcnow().isoformat().replace(":", "-").replace(".", "-")
            self._session_file = str(Path(self._session_dir) / f"{ts}_{sid}.jsonl")

    def _load(self, path: str) -> None:
        p = Path(path)
        # Tighten existing opened session file and its parent dir.
        try:
            ensure_private_dir(p.parent)
            ensure_private_file(p, 0o600)
        except Exception:
            pass
        lines = p.read_text(encoding="utf-8").splitlines()
        parsed: list[dict[str, Any]] = []
        for line in lines:
            try:
                parsed.append(json.loads(line))
            except Exception:
                continue
        if not parsed or parsed[0].get("type") != "session":
            self._new_session()
            return

        self._entries = parsed
        self._migrate_if_needed()
        self._reindex()

    def _migrate_if_needed(self) -> None:
        header = self._entries[0]
        original_version = int(header.get("version", 1))
        version = original_version
        if version < 2:
            prev = None
            for e in self._entries[1:]:
                e["id"] = e.get("id") or _id()
                e["parentId"] = prev
                prev = e["id"]
            header["version"] = 2
            version = 2
        if version < 3:
            for e in self._entries[1:]:
                if e.get("type") == "message":
                    m = e.get("message", {})
                    if m.get("role") == "hookMessage":
                        m["role"] = "custom"
            header["version"] = 3
            version = 3
        if version < 4:
            # v4: normalize timestamps to ISO "Z" format and backfill missing
            # ids/parentIds so partially-corrupted legacy files stay loadable.
            for e in self._entries[1:]:
                e["id"] = e.get("id") or _id()
                e["parentId"] = e.get("parentId")
                ts = e.get("timestamp")
                if ts is None:
                    e["timestamp"] = _now_iso()
                elif isinstance(ts, (int, float)):
                    e["timestamp"] = datetime.fromtimestamp(ts / 1000).isoformat() + "Z"
            header.setdefault("timestamp", _now_iso())
            header["version"] = 4
            version = 4

        if header.get("version") != original_version:
            # Version was upgraded: persist the migration so it runs once.
            self._rewrite()

    def _reindex(self) -> None:
        self._by_id = {}
        self._leaf_id = None
        for e in self._entries[1:]:
            self._by_id[e["id"]] = e
            self._leaf_id = e["id"]

    def _append(self, entry: dict[str, Any]) -> str:
        self._entries.append(entry)
        self._by_id[entry["id"]] = entry
        self._leaf_id = entry["id"]
        self._persist_entry(entry)
        return entry["id"]

    def _persist_entry(self, entry: dict[str, Any]) -> None:
        if not self._persist or not self._session_file:
            return
        path = Path(self._session_file)
        if not path.exists():
            # First write: write the full session header + entries atomically.
            atomic_write_text(
                path, "\n".join(json.dumps(x) for x in self._entries) + "\n"
            )
            return
        # Append-only for subsequent entries.
        append_private_text(path, json.dumps(entry) + "\n")

    def _rewrite(self) -> None:
        if not self._persist or not self._session_file:
            return
        atomic_write_text(
            Path(self._session_file),
            "\n".join(json.dumps(x) for x in self._entries) + "\n",
        )

    def append_message(self, message: dict[str, Any]) -> str:
        return self._append(
            {
                "type": "message",
                "id": _id(),
                "parentId": self._leaf_id,
                "timestamp": _now_iso(),
                "message": message,
            }
        )

    def append_thinking_level_change(self, level: str) -> str:
        return self._append(
            {
                "type": "thinking_level_change",
                "id": _id(),
                "parentId": self._leaf_id,
                "timestamp": _now_iso(),
                "thinkingLevel": level,
            }
        )

    def append_model_change(self, provider: str, model_id: str) -> str:
        return self._append(
            {
                "type": "model_change",
                "id": _id(),
                "parentId": self._leaf_id,
                "timestamp": _now_iso(),
                "provider": provider,
                "modelId": model_id,
            }
        )

    def append_compaction(
        self,
        summary: str,
        first_kept_entry_id: str,
        tokens_before: int,
        details: Any = None,
        from_hook: bool | None = None,
    ) -> str:
        return self._append(
            {
                "type": "compaction",
                "id": _id(),
                "parentId": self._leaf_id,
                "timestamp": _now_iso(),
                "summary": summary,
                "firstKeptEntryId": first_kept_entry_id,
                "tokensBefore": tokens_before,
                "details": details,
                "fromHook": from_hook,
            }
        )

    def append_branch_summary(
        self,
        from_id: str | None,
        summary: str,
        details: Any = None,
        from_hook: bool | None = None,
    ) -> str:
        return self._append(
            {
                "type": "branch_summary",
                "id": _id(),
                "parentId": self._leaf_id,
                "timestamp": _now_iso(),
                "fromId": from_id or "root",
                "summary": summary,
                "details": details,
                "fromHook": from_hook,
            }
        )

    def append_custom(self, custom_type: str, data: Any = None) -> str:
        return self._append(
            {
                "type": "custom",
                "id": _id(),
                "parentId": self._leaf_id,
                "timestamp": _now_iso(),
                "customType": custom_type,
                "data": data,
            }
        )

    def append_custom_message(self, custom_type: str, content: Any, display: bool, details: Any = None) -> str:
        return self._append(
            {
                "type": "custom_message",
                "id": _id(),
                "parentId": self._leaf_id,
                "timestamp": _now_iso(),
                "customType": custom_type,
                "content": content,
                "display": display,
                "details": details,
            }
        )

    def append_label_change(self, target_id: str, label: str | None) -> str:
        if target_id not in self._by_id:
            raise ValueError(f"Entry {target_id} not found")
        return self._append(
            {
                "type": "label",
                "id": _id(),
                "parentId": self._leaf_id,
                "timestamp": _now_iso(),
                "targetId": target_id,
                "label": label,
            }
        )

    def append_session_info(self, name: str | None) -> str:
        return self._append(
            {
                "type": "session_info",
                "id": _id(),
                "parentId": self._leaf_id,
                "timestamp": _now_iso(),
                "name": (name or "").strip() or None,
            }
        )

    def get_session_name(self) -> str | None:
        for e in reversed(self._entries[1:]):
            if e.get("type") == "session_info":
                return e.get("name") or None
        return None

    def get_last_compaction(self) -> dict[str, Any] | None:
        for e in reversed(self._entries[1:]):
            if e.get("type") == "compaction":
                return e
        return None

    def get_entry(self, entry_id: str) -> dict[str, Any] | None:
        return self._by_id.get(entry_id)

    def get_entries(self) -> list[dict[str, Any]]:
        return [x for x in self._entries[1:]]

    def get_header(self) -> dict[str, Any]:
        return self._entries[0]

    def get_leaf_id(self) -> str | None:
        return self._leaf_id

    def branch(self, branch_from_id: str) -> None:
        if branch_from_id not in self._by_id:
            raise ValueError(f"Entry {branch_from_id} not found")
        self._leaf_id = branch_from_id

    def reset_leaf(self) -> None:
        self._leaf_id = None

    def branch_with_summary(self, branch_from_id: str | None, summary: str, details: Any = None, from_hook: bool = False) -> str:
        self._leaf_id = branch_from_id
        return self.append_branch_summary(branch_from_id, summary, details, from_hook)

    def get_branch(self, from_id: str | None = None) -> list[dict[str, Any]]:
        start = from_id if from_id is not None else self._leaf_id
        out: list[dict[str, Any]] = []
        cur = self._by_id.get(start) if start else None
        while cur:
            out.insert(0, cur)
            pid = cur.get("parentId")
            cur = self._by_id.get(pid) if pid else None
        return out

    _MESSAGE_ENTRY_TYPES = {"message", "custom_message", "branch_summary"}

    def get_message_entry_ids(self, from_id: str | None = None) -> list[str]:
        """Entry ids of message-producing entries in branch order (root -> leaf).

        Every such entry appends exactly one message to `build_session_context`,
        so this list is index-aligned with `AgentSession.messages`.
        """
        return [e["id"] for e in self.get_branch(from_id) if e.get("type") in self._MESSAGE_ENTRY_TYPES]

    def get_tree(self) -> list[dict[str, Any]]:
        nodes = {e["id"]: {"entry": e, "children": []} for e in self._entries[1:]}
        roots: list[dict[str, Any]] = []
        for e in self._entries[1:]:
            pid = e.get("parentId")
            if pid and pid in nodes and pid != e["id"]:
                nodes[pid]["children"].append(nodes[e["id"]])
            else:
                roots.append(nodes[e["id"]])
        return roots

    def build_session_context(self) -> dict[str, Any]:
        path = self.get_branch()
        messages: list[dict[str, Any]] = []
        thinking_level = "off"
        model = None
        compaction = None

        for e in path:
            t = e.get("type")
            if t == "thinking_level_change":
                thinking_level = e.get("thinkingLevel", thinking_level)
            elif t == "model_change":
                model = {"provider": e.get("provider"), "modelId": e.get("modelId")}
            elif t == "message":
                m = e.get("message", {})
                if m.get("role") == "assistant":
                    model = {"provider": m.get("provider"), "modelId": m.get("model")}
            elif t == "compaction":
                compaction = e

        def append_entry(ent: dict[str, Any]) -> None:
            t = ent.get("type")
            if t == "message":
                messages.append(ent["message"])
            elif t == "custom_message":
                messages.append({
                    "role": "custom",
                    "customType": ent.get("customType"),
                    "content": ent.get("content"),
                    "display": ent.get("display"),
                    "details": ent.get("details"),
                    "timestamp": ent.get("timestamp"),
                })
            elif t == "branch_summary":
                messages.append({
                    "role": "custom",
                    "customType": "branch_summary",
                    "content": ent.get("summary", ""),
                    "timestamp": ent.get("timestamp"),
                    "fromId": ent.get("fromId"),
                })

        if compaction:
            messages.append(
                {
                    "role": "custom",
                    "customType": "compaction_summary",
                    "content": compaction.get("summary", ""),
                    "tokensBefore": compaction.get("tokensBefore", 0),
                    "timestamp": compaction.get("timestamp"),
                }
            )
            cidx = next((i for i, x in enumerate(path) if x.get("id") == compaction.get("id")), -1)
            first_kept = compaction.get("firstKeptEntryId")
            seen = False
            for i in range(cidx):
                if path[i].get("id") == first_kept:
                    seen = True
                if seen:
                    append_entry(path[i])
            for i in range(cidx + 1, len(path)):
                append_entry(path[i])
        else:
            for e in path:
                append_entry(e)

        return {
            "messages": messages,
            "thinkingLevel": thinking_level,
            "model": model,
        }

    def create_branched_session(self, leaf_id: str) -> str | None:
        if leaf_id not in self._by_id:
            raise ValueError(f"Entry {leaf_id} not found")
        path = self.get_branch(leaf_id)
        sid = str(uuid.uuid4())
        ts = _now_iso()
        header = {
            "type": "session",
            "version": CURRENT_SESSION_VERSION,
            "id": sid,
            "timestamp": ts,
            "cwd": self._cwd,
            "parentSession": self._session_file,
        }
        self._entries = [header] + path
        self._reindex()
        if self._persist:
            file_ts = ts.replace(":", "-").replace(".", "-")
            self._session_file = str(Path(self._session_dir) / f"{file_ts}_{sid}.jsonl")
            self._rewrite()
            # Tighten the new branch file.
            try:
                ensure_private_file(Path(self._session_file), 0o600)
            except Exception:
                pass
            return self._session_file
        return None

    def export_to_jsonl(self, output_path: str | None = None) -> str:
        p = Path(output_path or f"session-{datetime.utcnow().isoformat().replace(':','-').replace('.','-')}.jsonl").resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(json.dumps(e) for e in self._entries) + "\n", encoding="utf-8")
        return str(p)

    @property
    def session_file(self) -> str | None:
        return self._session_file

    @property
    def session_id(self) -> str:
        return self._entries[0]["id"]

    @property
    def cwd(self) -> str:
        return self._cwd

    @property
    def session_dir(self) -> str:
        return self._session_dir

    @classmethod
    def create(cls, cwd: str, session_dir: str | None = None) -> "SessionManager":
        return cls(cwd, session_dir or get_default_session_dir(cwd), None, True)

    @classmethod
    def open(cls, path: str, session_dir: str | None = None) -> "SessionManager":
        p = Path(path).resolve()
        lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
        cwd = None
        if lines:
            try:
                h = json.loads(lines[0])
                cwd = h.get("cwd")
            except Exception:
                cwd = None
        return cls(cwd or str(Path.cwd()), session_dir or str(p.parent), str(p), True)

    @classmethod
    def continue_recent(cls, cwd: str, session_dir: str | None = None) -> "SessionManager":
        d = Path(session_dir or get_default_session_dir(cwd))
        ensure_private_dir(d)
        files = sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)
        if files:
            return cls.open(str(files[0]), str(d))
        return cls.create(cwd, str(d))

    @classmethod
    def in_memory(cls, cwd: str | None = None) -> "SessionManager":
        return cls(cwd or str(Path.cwd()), "", None, False)

    @classmethod
    def fork_from(cls, source_path: str, target_cwd: str, session_dir: str | None = None) -> "SessionManager":
        source = cls.open(source_path)
        target = cls.create(target_cwd, session_dir or get_default_session_dir(target_cwd))
        target._entries = [
            {
                "type": "session",
                "version": CURRENT_SESSION_VERSION,
                "id": str(uuid.uuid4()),
                "timestamp": _now_iso(),
                "cwd": target_cwd,
                "parentSession": str(Path(source_path).resolve()),
            }
        ] + source.get_entries()
        target._reindex()
        target._rewrite()
        return target

    @classmethod
    def list(cls, cwd: str, session_dir: str | None = None) -> list[SessionInfo]:
        d = Path(session_dir or get_default_session_dir(cwd))
        if not d.exists():
            return []
        out: list[SessionInfo] = []
        for p in d.glob("*.jsonl"):
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
                if not lines:
                    continue
                h = json.loads(lines[0])
                entries = [json.loads(x) for x in lines[1:] if x.strip()]
                name = None
                for e in reversed(entries):
                    if e.get("type") == "session_info":
                        name = e.get("name")
                        break
                out.append(
                    SessionInfo(
                        path=str(p),
                        id=h.get("id", ""),
                        cwd=h.get("cwd", ""),
                        name=name,
                        created=datetime.fromisoformat(h.get("timestamp", _now_iso()).replace("Z", "")),
                        modified=datetime.utcfromtimestamp(p.stat().st_mtime),
                        message_count=sum(1 for e in entries if e.get("type") == "message"),
                    )
                )
            except Exception:
                continue
        out.sort(key=lambda x: x.modified, reverse=True)
        return out

    @classmethod
    def list_all(cls) -> list[SessionInfo]:
        root = Path(get_agent_dir()) / "sessions"
        if not root.exists():
            return []
        out: list[SessionInfo] = []
        for d in root.iterdir():
            if d.is_dir():
                out.extend(cls.list(cwd="", session_dir=str(d)))
        out.sort(key=lambda x: x.modified, reverse=True)
        return out
