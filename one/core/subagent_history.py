"""Private, disk-backed display journal. Never enters provider context."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any

from one.core.persistence import ensure_private_file
from one.core.provider_login import _redact_credentials
from one.tools.common import sanitize_display_text

PAGE_CHARS = 6000
PAGE_ROWS = 4


class SubagentHistory:
    """A bounded-memory journal of allowlisted display data, owned by a parent session.

    SQLite avoids replaying or copying an ever-growing JSONL on every UI refresh.
    Ephemeral sessions use a private temporary database, not unbounded RAM.
    """

    def __init__(self, session_file: str | None = None) -> None:
        self._closed = False
        self._temporary = None if session_file else tempfile.TemporaryDirectory(prefix="one-agents-")
        self.path = Path(session_file + ".agents") if session_file else Path(self._temporary.name) / "history"
        if self.path.is_symlink():
            raise ValueError("Invalid subagent history sidecar")
        self._lock = threading.RLock()
        ensure_private_file(self.path)
        self.path.touch(mode=0o600, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        ensure_private_file(self.path)
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self._db.close()
            self._closed = True
            raise ValueError("Unsupported subagent history version")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS children (id TEXT PRIMARY KEY, metadata TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS prompts (child TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS blocks (
                id INTEGER PRIMARY KEY, child TEXT NOT NULL, kind TEXT NOT NULL,
                call_id TEXT, body TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations (
                block INTEGER PRIMARY KEY, child TEXT NOT NULL, call_id TEXT,
                tool TEXT NOT NULL, state TEXT NOT NULL, output TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS blocks_child ON blocks(child, id);
            PRAGMA user_version=1;
        """)
        self._db.commit()
        self.durable = session_file is not None

    @staticmethod
    def safe(value: Any) -> str:
        return _redact_credentials(sanitize_display_text(str(value or "")))

    def metadata(self, child: dict[str, Any]) -> None:
        record = {key: value for key, value in child.items() if key not in {"timeline", "outputTail"}}
        with self._lock, self._db:
            self._db.execute("INSERT INTO children VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata",
                             (child["sessionId"], json.dumps(record, allow_nan=False)))

    def children(self, offset: int = 0, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT metadata FROM children ORDER BY rowid LIMIT ? OFFSET ?",
                                    (limit, offset)).fetchall()
        children = [json.loads(row[0]) for row in rows]
        for child in children:
            child["historyDurable"] = self.durable
            # Monotonic timestamps from a previous process are not portable.
            if child.get("status") == "running":
                child["status"] = "interrupted"
            child["elapsedSeconds"] = max(0, (child.get("completedAt") or child.get("startedMonotonic") or 0) - (child.get("startedMonotonic") or 0))
            child["startedMonotonic"] = None
            child["completedAt"] = None
            child["timeline"] = []
        return children

    def save_prompt(self, child: str, text: Any) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO prompts VALUES (?, ?)", (child, self.safe(text)))

    def prompt_page(self, child: str, offset: int = 0) -> dict[str, Any]:
        if not isinstance(child, str) or not child or len(child) > 1024:
            raise ValueError("Invalid agent ID")
        if type(offset) is not int or not 0 <= offset <= 2**63 - PAGE_CHARS:
            raise ValueError("Invalid prompt offset")
        with self._lock:
            metadata = self._db.execute("SELECT metadata FROM children WHERE id=?", (child,)).fetchone()
            if metadata is None:
                raise ValueError("Agent history is unavailable")
            row = self._db.execute("SELECT length(body),substr(body,?,?) FROM prompts WHERE child=?",
                                   (offset + 1, PAGE_CHARS, child)).fetchone()
            if row is None:
                text = json.loads(metadata[0]).get("task", "")
                return {"sessionId": child, "text": text, "offset": 0, "nextOffset": len(text),
                        "hasMore": False, "complete": False, "durable": self.durable}
            return {"sessionId": child, "text": row[1], "offset": offset,
                    "nextOffset": offset + len(row[1]), "hasMore": offset + len(row[1]) < row[0],
                    "complete": True, "durable": self.durable}

    def append(self, child: str, kind: str, text: str, call_id: str | None = None) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO blocks(child,kind,call_id,body) VALUES (?,?,?,?)",
                             (child, kind, call_id, self.safe(text)))

    def tool_start(self, child: str, tool: str, call_id: str, args: str) -> None:
        with self._lock, self._db:
            row = self._db.execute("INSERT INTO blocks(child,kind,call_id,body) VALUES (?,?,?,?)",
                                   (child, "tool", call_id, self.safe(f"Tool: ○ {tool}\nArguments: {args}")))
            self._db.execute("INSERT INTO operations(block,child,call_id,tool,state) VALUES (?,?,?,?,?)",
                             (row.lastrowid, child, call_id, tool, "running"))

    def tool_end(self, child: str, tool: str, call_id: str, ok: bool, elapsed: Any, output: str) -> None:
        with self._lock, self._db:
            query = "SELECT block FROM operations WHERE child=? AND state='running' AND "
            query += "call_id=?" if call_id else "tool=?"
            row = self._db.execute(query + " ORDER BY block LIMIT 1", (child, call_id or tool)).fetchone()
            if row is None:
                if call_id and self._db.execute("SELECT 1 FROM operations WHERE child=? AND call_id=?", (child, call_id)).fetchone():
                    return
                self.tool_start(child, tool, call_id, "")
                row = self._db.execute(query + " ORDER BY block LIMIT 1", (child, call_id or tool)).fetchone()
            block = row[0]
            body = self._db.execute("SELECT body FROM blocks WHERE id=?", (block,)).fetchone()[0]
            args = body.partition("\n")[2]
            heading = f"Tool: {'✓' if ok else '×'} {tool} · {elapsed} ms\n{args}"
            self._db.execute("UPDATE blocks SET body=? WHERE id=?", (self.safe(heading), block))
            self._db.execute("UPDATE operations SET state=?,output=? WHERE block=?",
                             ("completed" if ok else "failed", self.safe(("\nResult:\n" if ok else "\nError:\n") + output), block))

    def close_operations(self, child: str, status: str) -> None:
        with self._lock, self._db:
            rows = self._db.execute("SELECT block,tool FROM operations WHERE child=? AND state='running'", (child,)).fetchall()
            for block, tool in rows:
                self._db.execute("UPDATE blocks SET body=replace(body,?,?) WHERE id=?",
                                 (f"Tool: ○ {tool}", f"Tool: × {tool} · {status}", block))
            self._db.execute("UPDATE operations SET state=? WHERE child=? AND state='running'", (status, child))

    def inspector_page(self, child: str, cursor: int = 0, part: int = 0, *, show_results: bool = True) -> dict[str, Any]:
        if any(type(value) is not int or value < 0 or value > 2**63 - PAGE_CHARS for value in (cursor, part)):
            raise ValueError("Invalid agent history cursor")
        if cursor == 0 and part != 1:
            prompt = self.prompt_page(child, part)
            entries = [{"kind": "prompt", "text": prompt["text"], "complete": prompt["complete"]}]
            if prompt["hasMore"]:
                return {"sessionId": child, "entries": entries, "cursor": 0,
                        "part": prompt["nextOffset"], "hasMore": True, "durable": self.durable}
            history = self.page(child, 1, 0, show_results=show_results)
            history["entries"] = entries + history["entries"]
            return history
        return self.page(child, cursor, part, show_results=show_results)

    def response(self, child: str, delta: str, kind: str = "response") -> None:
        with self._lock, self._db:
            row = self._db.execute("SELECT id,kind,length(body),substr(body,-4096) FROM blocks "
                                   "WHERE child=? ORDER BY id DESC LIMIT 1", (child,)).fetchone()
            if row and row[1] == kind:
                # Re-redact across delta boundaries; keep only a bounded suffix in RAM.
                prefix = max(0, row[2] - 4096)
                safe = self.safe(row[3] + delta)
                self._db.execute("UPDATE blocks SET body=substr(body,1,?) || ? WHERE id=?",
                                 (prefix, safe, row[0]))
            else:
                self.append(child, kind, delta)

    def page(self, child: str, cursor: int = 0, part: int = 0, *, show_results: bool = True) -> dict[str, Any]:
        """Forward cursor over IDs and character slices, with a bounded DTO.

        (0, 0) starts at the beginning; (0, 1) selects the latest display page.
        The last response stays open so a returned cursor can consume new deltas.
        """
        if not isinstance(child, str) or not child or len(child) > 1024:
            raise ValueError("Invalid agent ID")
        if any(type(value) is not int or value < 0 for value in (cursor, part)):
            raise ValueError("Invalid agent history cursor")
        with self._lock:
            metadata = self._db.execute("SELECT metadata FROM children WHERE id=?", (child,)).fetchone()
            if metadata is None:
                raise ValueError("Agent history is unavailable")
            entries = []
            projection = "body || coalesce((SELECT output FROM operations WHERE block=blocks.id), '')" if show_results else "body"
            if cursor == 0 and part == 1:
                recent = self._db.execute("SELECT id FROM blocks WHERE child=? ORDER BY id DESC LIMIT ?", (child, PAGE_ROWS)).fetchall()
                cursor = recent[-1][0] if recent else 0
                part = 0
                last = self._db.execute(f"SELECT id,length({projection}) FROM blocks WHERE child=? ORDER BY id DESC LIMIT 1", (child,)).fetchone()
                if last and last[1] > PAGE_CHARS:
                    cursor, part = last[0], last[1] - PAGE_CHARS
                first = self._db.execute(f"SELECT length({projection}) FROM blocks WHERE child=? AND id=?", (child, cursor)).fetchone()
                if not part and first and first[0] > PAGE_CHARS:
                    part = first[0] - PAGE_CHARS
            projection = "body || coalesce((SELECT output FROM operations WHERE block=blocks.id), '')" if show_results else "body"
            for _ in range(PAGE_ROWS):
                row = self._db.execute(
                    f"SELECT id,kind,length({projection}),substr({projection},?,?) FROM blocks "
                    "WHERE child=? AND id>=? ORDER BY id LIMIT 1",
                    (part + 1, PAGE_CHARS, child, cursor),
                ).fetchone()
                if row is None:
                    break
                entries.append({"id": row[0], "kind": row[1], "text": row[3] if show_results or row[1] != "result" else "[result hidden; details off]"})
                if part + len(row[3]) < row[2]:
                    cursor, part = row[0], part + len(row[3])
                else:
                    following = self._db.execute("SELECT 1 FROM blocks WHERE child=? AND id>? LIMIT 1", (child, row[0])).fetchone()
                    if row[1] in {"response", "thinking"} and not following:
                        cursor, part = row[0], row[2]
                        break
                    cursor, part = row[0] + 1, 0
            more = self._db.execute(f"SELECT 1 FROM blocks WHERE child=? AND (id>? OR (id=? AND length({projection})>?)) LIMIT 1",
                                    (child, cursor, cursor, part)).fetchone() is not None
        return {"sessionId": child, "entries": entries, "cursor": cursor, "part": part,
                "hasMore": more, "durable": self.durable}

    def __del__(self) -> None:
        if hasattr(self, "_db") and not self._closed:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._db.close()
        if self._temporary is not None:
            self._temporary.cleanup()
