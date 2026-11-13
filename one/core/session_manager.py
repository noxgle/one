# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
import math
import os
import shutil
import stat
import unicodedata
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from one.config import get_agent_dir
from one.core.persistence import (
    append_private_text,
    atomic_write_text,
    ensure_private_dir,
    ensure_private_file,
)
from one.providers.native_compaction import NativeCompactionProtocolError, validate_native_items

CURRENT_SESSION_VERSION = 4
# Evidence is deliberately a sidecar rather than a session entry: it must not
# become provider context when a session is restored.  Limits bound disk use.
EVIDENCE_VERSION = 1
EVIDENCE_MAX_RECORD_BYTES = 2_000_000
EVIDENCE_MAX_SESSION_BYTES = 32_000_000
SESSION_NAME_MAX_CHARS = 64

COMPACTION_STRATEGIES = frozenset({"local_separate", "local_cache_aware", "extractive", "legacy", "native_openai"})
COMPACTION_DERIVATIVE_TYPES = frozenset({"compaction_constraints", "compaction_plan", "compaction_file_context"})


def _derivative_blocks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 7:
        raise ValueError("Invalid compaction derivative blocks")
    result = []
    total = 0
    for block in value:
        if (not isinstance(block, dict) or set(block) != {"role", "customType", "content"}
                or block["role"] != "custom" or not isinstance(block["customType"], str)
                or block["customType"] not in COMPACTION_DERIVATIVE_TYPES):
            raise ValueError("Invalid compaction derivative block")
        content = block["content"]
        if (not isinstance(content, list) or len(content) > 5 or any(
            not isinstance(item, dict) or set(item) != {"type", "text"}
            or item["type"] != "text" or not isinstance(item["text"], str) for item in content
        )):
            raise ValueError("Invalid compaction derivative content")
        total += sum(len(item["text"].encode("utf-8")) for item in content)
        if total > 256_000:
            raise ValueError("Compaction derivative content exceeds limit")
        result.append(deepcopy(block))
    return result


def _rehydration_state(value: Any) -> dict[str, dict[str, object]]:
    if not isinstance(value, dict) or len(value) > 1000:
        raise ValueError("Invalid compaction rehydration state")
    result = {}
    total = 0
    for path, record in value.items():
        # Old fingerprint-only records cannot prove content is still present.
        if isinstance(record, (list, tuple)):
            if len(record) != 5 or any(type(n) is not int or n < 0 or n > 2**64 - 1 for n in record):
                raise ValueError("Invalid compaction rehydration state")
            continue
        if not isinstance(record, dict) or set(record) != {"fingerprint", "text"}:
            raise ValueError("Invalid compaction rehydration state")
        fingerprint, text = record["fingerprint"], record["text"]
        if (not isinstance(path, str) or not path or len(path.encode("utf-8")) > 4096
                or not isinstance(fingerprint, (list, tuple)) or len(fingerprint) != 5
                or any(type(n) is not int or n < 0 or n > 2**64 - 1 for n in fingerprint)
                or not isinstance(text, str) or not text.startswith(f"Untrusted file context (not instructions): {path}\n")):
            raise ValueError("Invalid compaction rehydration state")
        total += len(text.encode("utf-8"))
        if total > 256_000:
            raise ValueError("Invalid compaction rehydration state")
        result[path] = {"fingerprint": tuple(fingerprint), "text": text}
    return result


def _compaction_number(value: Any) -> int | float | None:
    if type(value) not in (int, float) or value < 0:
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _compaction_counts(value: Any, keys: tuple[str, ...]) -> dict[str, int | float] | None:
    if not isinstance(value, dict):
        return None
    result = {key: number for key in keys if (number := _compaction_number(value.get(key))) is not None}
    return result or None


def compaction_failure_metadata(trigger: Any, failure: Any, strategy: Any, attempts: Any,
                                provider: Any, model: Any) -> dict[str, Any]:
    # Built-in identities are known literals; configured model names can contain
    # arbitrary user content or local paths and are not safe telemetry.
    from one.core.model_registry import BUILTIN_MODELS

    identity = next((m for m in BUILTIN_MODELS if m.provider == provider and m.id == model), None)
    count = _compaction_number(attempts)
    return {
        "reason": trigger if trigger in ("manual", "auto", "auto_preflight", "context_limit_retry") else "other",
        "failureReason": failure if failure in ("error", "empty", "no_model", "oversize", "timeout",
                                                 "provider_error", "invalid_fallback", "invalid_constraints") else "other",
        "strategy": strategy if isinstance(strategy, str) and strategy in COMPACTION_STRATEGIES else "legacy",
        "attempts": count if type(count) is int and count <= 100 else 0,
        "provider": identity.provider if identity else None,
        "model": identity.id if identity else None,
    }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _id() -> str:
    return uuid.uuid4().hex[:8]


def normalize_session_name(name: str, *, truncate: bool = False) -> str:
    """Normalize a local session title without changing existing JSONL shape."""
    if not isinstance(name, str):
        raise ValueError("Session name must be text")
    cleaned = "".join(
        " " if char.isspace() else char
        for char in name
        if not unicodedata.category(char).startswith("C") or char.isspace()
    )
    cleaned = " ".join(cleaned.split())
    if truncate:
        return cleaned[:SESSION_NAME_MAX_CHARS]
    if not cleaned:
        raise ValueError("Session name cannot be empty")
    if len(cleaned) > SESSION_NAME_MAX_CHARS:
        raise ValueError(f"Session name must be at most {SESSION_NAME_MAX_CHARS} characters")
    return cleaned


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
            ts = datetime.now(UTC).isoformat().replace(":", "-").replace(".", "-")
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

    def checkpoint_path(self) -> Path | None:
        """Private, non-discoverable per-session storage; forks do not copy it."""
        if not self._persist or not self._session_file:
            return None
        return Path(self._session_file + ".checkpoints")

    def run_history(self):
        """Return detached historical run storage, if this session is persistent."""
        if not self._persist or not self._session_file:
            return None
        from one.core.run_history import RunHistory

        return RunHistory(self._session_file, self.session_id)

    def ensure_run_session(self) -> None:
        """Persist the header before creating a run sidecar for an empty session."""
        if self._persist and self._session_file and not Path(self._session_file).exists():
            self._rewrite()

    def create_checkpoint_manager(self):
        from one.core.checkpoints import CheckpointManager

        path = self.checkpoint_path()
        if path is None:
            raise ValueError("Checkpoints require a persistent session")
        return CheckpointManager(path, Path(self.cwd), self.session_id)

    def _evidence_path(self) -> Path | None:
        if not self._persist or not self._session_file:
            return None
        # Do not use a .jsonl suffix: session discovery intentionally globs it.
        return Path(self._session_file).with_suffix(Path(self._session_file).suffix + ".evidence")

    def append_evidence(self, evidence: dict[str, Any]) -> tuple[str | None, str | None]:
        """Append a complete, already-sanitised tool result outside chat history.

        Returns ``(id, None)`` on success, or ``(None, reason)``.  Failures are
        intentionally returned to callers so they never advertise unavailable
        evidence.  In-memory/no-session runs cannot offer durable evidence.
        """
        path = self._evidence_path()
        if path is None:
            return None, "durable session storage is unavailable"
        record = {"type": "tool_evidence", "version": EVIDENCE_VERSION, "sessionId": self.session_id, **evidence}
        try:
            encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            return None, f"evidence serialization failed: {exc}"
        if len(encoded) > EVIDENCE_MAX_RECORD_BYTES:
            return None, f"evidence exceeds {EVIDENCE_MAX_RECORD_BYTES} byte record limit"
        try:
            if path.exists() and path.stat().st_size + len(encoded) + 1 > EVIDENCE_MAX_SESSION_BYTES:
                return None, f"evidence exceeds {EVIDENCE_MAX_SESSION_BYTES} byte session limit"
            if not path.exists():
                header = {"type": "evidence", "version": EVIDENCE_VERSION, "sessionId": self.session_id, "timestamp": _now_iso()}
                # The initial header and record must appear together: a crash
                # before either append would otherwise leave an incomplete sidecar.
                atomic_write_text(path, json.dumps(header, separators=(",", ":")) + "\n" + encoded.decode("utf-8") + "\n")
            else:
                # Keep later evidence records append-only.
                append_private_text(path, encoded.decode("utf-8") + "\n")
        except Exception as exc:
            return None, f"evidence write failed: {exc}"
        return str(record.get("id")), None

    def read_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        """Return one evidence record for this session, tolerating bad sidecars."""
        path = self._evidence_path()
        if path is None or not path.exists() or not isinstance(evidence_id, str):
            return None
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if item.get("type") == "tool_evidence" and item.get("sessionId") == self.session_id and item.get("id") == evidence_id:
                    return item
        except Exception:
            return None
        return None

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
            atomic_write_text(path, "\n".join(json.dumps(x) for x in self._entries) + "\n")
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

    def append_preset_state(self, state: dict[str, Any]) -> str:
        """Append a versioned effective preset snapshot to this branch."""
        return self._append({"type": "preset_state", "id": _id(), "parentId": self._leaf_id, "timestamp": _now_iso(), "state": state})

    def append_timing(
        self,
        scope: str,
        elapsed_ms: float | int,
        outcome: str,
        **metadata: Any,
    ) -> str:
        """Append non-provider-context lifecycle timing metadata.

        Timing is deliberately its own entry type: it remains available in the
        JSONL graph without becoming a message supplied to a provider.
        """
        measured = float(elapsed_ms)
        elapsed = int(measured) if math.isfinite(measured) and measured > 0 else 0
        scopes = {"turn", "attempt", "provider_request", "tool"}
        outcomes = {"success", "error", "timeout", "cancelled", "rejected", "budget_exceeded"}
        if scope not in scopes:
            raise ValueError(f"Invalid timing scope: {scope}")
        if outcome not in outcomes:
            raise ValueError(f"Invalid timing outcome: {outcome}")
        entry: dict[str, Any] = {
            "type": "timing",
            "id": _id(),
            "parentId": self._leaf_id,
            "timestamp": _now_iso(),
            "scope": scope,
            "elapsedMs": elapsed,
            "outcome": outcome,
        }
        # Keep durable telemetry correlation-only. Do not permit request bodies,
        # prompts, results, errors, credentials, or arbitrary event payloads.
        allowed = {
            "turnId", "providerRequestId", "parentRequestId",
            "provider", "model", "purpose", "tool", "toolCallId",
        }
        for key in allowed:
            value = metadata.get(key)
            if value is not None:
                entry[key] = str(value)[:128]
        attempt = metadata.get("attempt")
        if attempt is not None:
            entry["attempt"] = int(attempt)
        # Provider-reported token counters are additive diagnostics.  They are
        # stored as integers so later aggregation can distinguish a real zero
        # from an unknown (missing) counter.  Never accept arbitrary numbers.
        for key in ("inputTokens", "cachedTokens", "outputTokens", "reasoningTokens"):
            value = metadata.get(key)
            if type(value) is int and value >= 0:
                entry[key] = value
        return self._append(entry)

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

    def append_temperature_change(self, temperature: float) -> str:
        return self._append({"type": "temperature_change", "id": _id(), "parentId": self._leaf_id,
                             "timestamp": _now_iso(), "temperature": temperature})

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
        reason: str = "manual",
        duration_ms: float | None = None,
        *,
        tokens_after: Any = None,
        tokens_kept: Any = None,
        strategy: Any = None,
        summary_schema: Any = None,
        attempts: Any = None,
        fallback: Any = None,
        usage: Any = None,
        constraints: Any = None,
        rehydrated: Any = None,
        transactional_v2: bool = False,
        derivative_blocks: Any = None,
        rehydration_state: Any = None,
    ) -> str | dict[str, Any]:
        """Append a compaction; V2 transactions return a detached committed entry.

        The transactional option is reserved for V2 and leaves memory unchanged
        if serialization or persistence fails. In-memory sessions commit locally.
        """
        if transactional_v2 and summary_schema != "v2":
            raise ValueError("transactional_v2 requires summary_schema='v2'")
        derivatives = _derivative_blocks(derivative_blocks) if derivative_blocks is not None else None
        state = _rehydration_state(rehydration_state) if rehydration_state is not None else None
        entry = {
            "type": "compaction",
            "id": _id(),
            "parentId": self._leaf_id,
            "timestamp": _now_iso(),
            "summary": summary,
            "firstKeptEntryId": first_kept_entry_id,
            "tokensBefore": tokens_before,
            "details": details,
            "fromHook": from_hook,
            "reason": reason,
        }
        if derivatives is not None:
            entry["derivativeBlocks"] = derivatives
        if state is not None:
            entry["rehydrationState"] = state
        # Duration is optional so historical entries retain their exact shape.
        # New completed compactions write this durable monotonic measurement.
        for key, value in (("durationMs", duration_ms), ("tokensAfter", tokens_after),
                           ("tokensKept", tokens_kept), ("attempts", attempts)):
            number = _compaction_number(value)
            if number is not None:
                entry[key] = number
        if isinstance(strategy, str) and strategy in COMPACTION_STRATEGIES:
            entry["strategy"] = strategy
        if isinstance(summary_schema, str) and summary_schema in ("legacy", "v2"):
            entry["summarySchema"] = summary_schema
        if type(fallback) is bool:
            entry["fallback"] = fallback
        for key, value, keys in (
            ("usage", usage, ("inputTokens", "cachedTokens", "outputTokens", "reasoningTokens")),
            ("constraints", constraints, ("extracted", "preserved", "dropped")),
            ("rehydrated", rehydrated, ("files", "tokens")),
        ):
            cleaned = _compaction_counts(value, keys)
            if cleaned is not None:
                entry[key] = cleaned
            elif key == "usage":
                entry[key] = None
        if not transactional_v2:
            return self._append(entry)

        return self._append_transactional(entry)

    def _append_transactional(self, entry: dict[str, Any]) -> dict[str, Any]:
        # Prepare everything that can fail before replacing the file or publishing
        # the new leaf. Do not share nested input or returned data with the index.
        entry = deepcopy(entry)
        committed = deepcopy(entry)
        entries = [*self._entries, entry]
        by_id = {**self._by_id, entry["id"]: entry}
        if self._persist and self._session_file:
            path = Path(self._session_file)
            if path.exists():
                # Preserve even unparsed/unknown lines in the original session.
                original = path.read_bytes().decode("utf-8")
                prefix = original + ("\n" if original and not original.endswith("\n") else "")
                text = prefix + json.dumps(entry) + "\n"
            else:
                text = "\n".join(json.dumps(item) for item in entries) + "\n"
            atomic_write_text(path, text)
        self._entries = entries
        self._by_id = by_id
        self._leaf_id = entry["id"]
        return committed

    def append_native_compaction(
        self, provider: str, model: str, endpoint: str, native_input_items: list[dict[str, Any]],
        first_covered_entry_id: str, last_covered_entry_id: str,
    ) -> str:
        """Commit a private ordered replay window covering a contiguous message range."""
        if any(not isinstance(value, str) or not value for value in (provider, model, endpoint)):
            raise ValueError("Native compaction requires provider, model and endpoint")
        ids = self.get_message_entry_ids()
        if first_covered_entry_id not in ids or last_covered_entry_id not in ids:
            raise ValueError("Native compaction anchors must be branch messages")
        if ids.index(first_covered_entry_id) > ids.index(last_covered_entry_id):
            raise ValueError("Native compaction anchors are out of order")
        items = validate_native_items(native_input_items)
        if not any(item["type"] == "compaction" for item in items):
            raise ValueError("Native compaction requires a compaction item")
        entry = {
            "type": "native_compaction", "id": _id(), "parentId": self._leaf_id, "timestamp": _now_iso(),
            "provider": provider, "model": model, "endpoint": endpoint, "nativeInputItems": items,
            "coveredEntryIds": ids[ids.index(first_covered_entry_id):ids.index(last_covered_entry_id) + 1],
        }
        return self._append_transactional(entry)["id"]

    def get_native_compaction(self, provider: str, model: str, endpoint: str) -> dict[str, Any] | None:
        """Return the latest valid branch replay window, detached from storage.

        The covered IDs map the window to canonical messages; later messages
        remain available through the ordinary context. No older window is used
        when the latest one is stale or bound to another endpoint.
        """
        path = self._get_branch()
        for index in range(len(path) - 1, -1, -1):
            entry = path[index]
            if entry.get("type") == "compaction":
                return None
            if entry.get("type") != "native_compaction":
                continue
            if (entry.get("provider"), entry.get("model"), entry.get("endpoint")) != (provider, model, endpoint):
                return None
            ids = [e.get("id") for e in path[:index] if e.get("type") in self._MESSAGE_ENTRY_TYPES]
            covered = entry.get("coveredEntryIds")
            if (not isinstance(covered, list) or not covered or any(not isinstance(i, str) for i in covered)
                    or covered[0] not in ids):
                return None
            start = ids.index(covered[0])
            if ids[start:start + len(covered)] != covered:
                return None
            try:
                items = validate_native_items(entry.get("nativeInputItems"))
            except NativeCompactionProtocolError:
                return None
            if not any(item["type"] == "compaction" for item in items):
                return None
            return {"nativeInputItems": items, "coveredEntryIds": list(covered), "entryId": entry["id"]}
        return None

    def append_compaction_skipped(self, reason: str, *, busy: bool = False,
                                  transactional_v2: bool = False) -> str | dict[str, Any]:
        """Record a compaction request that deliberately made no summary."""
        entry = {
            "type": "compaction_skipped",
            "id": _id(),
            "parentId": self._leaf_id,
            "timestamp": _now_iso(),
            "reason": reason,
            "busy": busy,
        }
        return self._append_transactional(entry) if transactional_v2 else self._append(entry)

    def append_compaction_failed(self, trigger: str, failure_reason: str, *, strategy: Any = None,
                                 attempts: Any = 0, provider: Any = None, model: Any = None,
                                 transactional_v2: bool = False) -> str | dict[str, Any]:
        """Persist bounded failure metadata without provider diagnostics."""
        entry = {
            "type": "compaction_failed", "id": _id(), "parentId": self._leaf_id,
            "timestamp": _now_iso(),
            **compaction_failure_metadata(trigger, failure_reason, strategy, attempts, provider, model),
        }
        return self._append_transactional(entry) if transactional_v2 else self._append(entry)

    def get_compaction_stats(self) -> dict[str, Any]:
        """Return durable compaction metrics for the currently loaded session."""
        breakdown = {"manual": 0, "automatic": 0, "recovery": 0}

        def category(reason: Any) -> str:
            if reason == "manual":
                return "manual"
            if reason == "context_limit_retry":
                return "recovery"
            return "automatic"

        completed = skipped = failed = tokens_before = 0
        failure_reasons: dict[str, int] = {}
        by_strategy: dict[str, int] = {}
        cache_entries: list[dict[str, Any]] = []
        durations: list[float] = []
        aggregates: dict[str, dict[str, int | float]] = {"constraints": {}, "rehydrated": {}}
        last: dict[str, Any] | None = None
        for entry in self._entries[1:]:
            if entry.get("type") == "compaction":
                completed += 1
                reason = str(entry.get("reason") or "manual")
                breakdown[category(reason)] += 1
                strategy = entry.get("strategy")
                strategy = strategy if isinstance(strategy, str) and strategy in COMPACTION_STRATEGIES else "legacy"
                by_strategy[strategy] = by_strategy.get(strategy, 0) + 1
                usage = _compaction_counts(entry.get("usage"), self._PROVIDER_CACHE_COUNTERS)
                cache_entries.append(usage or {})
                duration = _compaction_number(entry.get("durationMs"))
                if duration is not None:
                    durations.append(duration)
                for key, keys in (("constraints", ("extracted", "preserved", "dropped")),
                                  ("rehydrated", ("files", "tokens"))):
                    for name, value in (_compaction_counts(entry.get(key), keys) or {}).items():
                        aggregates[key][name] = aggregates[key].get(name, 0) + value
                before = _compaction_number(entry.get("tokensBefore"))
                before = int(before) if before is not None else 0
                tokens_before += before
                last = {
                    "id": entry.get("id"), "timestamp": entry.get("timestamp"),
                    "reason": reason, "tokensBefore": before,
                    "summary": entry.get("summary", ""),
                }
            elif entry.get("type") == "compaction_skipped":
                skipped += 1
            elif entry.get("type") == "compaction_failed":
                failed += 1
                failure = entry.get("failureReason")
                failure = failure if failure in ("error", "empty", "no_model") else "other"
                failure_reasons[failure] = failure_reasons.get(failure, 0) + 1
        return {
            "completed": completed, "skipped": skipped, "tokensBefore": tokens_before,
            "byReason": breakdown, "last": last, "failed": failed, "failureReasons": failure_reasons,
            "byStrategy": by_strategy, "cache": self._provider_cache_summary(cache_entries),
            "avgDurationMs": sum(durations) / len(durations) if durations else None,
            "constraints": aggregates["constraints"], "rehydrated": aggregates["rehydrated"],
        }

    def get_compaction_timing(self) -> dict[str, Any]:
        """Return durable completed-compaction timings, tolerating old JSONL."""
        durations: list[float] = []
        last_duration: float | None = None
        for entry in self._entries[1:]:
            if entry.get("type") != "compaction":
                continue
            value = entry.get("durationMs")
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value >= 0
            ):
                last_duration = float(value) / 1000
                durations.append(last_duration)
            else:
                # The latest completed entry may be an old entry with no timing.
                last_duration = None
        return {"durationsSec": durations, "lastSec": last_duration}

    def get_lifecycle_timing_stats(self) -> dict[str, dict[str, Any]]:
        """Summarize valid durable lifecycle timings on the current branch.

        Unknown outcomes are deliberately ignored: the public outcome contract
        stays stable when a newer writer adds an outcome this reader does not
        understand.  Timing entries are correlation metadata, so walking the
        active ancestry retains pre-compaction measurements while excluding
        sibling branch work.
        """
        outcomes = ("success", "error", "timeout", "cancelled", "rejected", "budget_exceeded")
        scopes = ("turn", "attempt", "provider_request", "tool")
        values: dict[str, list[float]] = {scope: [] for scope in scopes}
        by_outcome: dict[str, dict[str, int]] = {
            scope: {outcome: 0 for outcome in outcomes} for scope in scopes
        }
        totals: dict[str, float] = {scope: 0.0 for scope in scopes}
        for entry in self.get_branch():
            scope = entry.get("scope")
            if entry.get("type") != "timing" or not isinstance(scope, str) or scope not in values:
                continue
            elapsed = entry.get("elapsedMs")
            outcome = entry.get("outcome")
            try:
                valid_elapsed = (
                    not isinstance(elapsed, bool)
                    and isinstance(elapsed, (int, float))
                    and math.isfinite(elapsed)
                    and elapsed >= 0
                )
            except OverflowError:
                valid_elapsed = False
            if (
                not valid_elapsed
                or outcome not in outcomes
            ):
                continue
            assert isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
            # Keep JSON output finite when malformed legacy records contain
            # individually finite, near-maximum floats.
            if not math.isfinite(totals[scope] + float(elapsed)):
                continue
            values[scope].append(float(elapsed))
            totals[scope] += float(elapsed)
            by_outcome[scope][outcome] += 1
        result: dict[str, dict[str, Any]] = {}
        for scope in scopes:
            measured = values[scope]
            total = totals[scope]
            result[scope] = {
                "measurements": len(measured),
                "totalMs": total,
                "averageMs": total / len(measured) if measured else None,
                "minMs": min(measured) if measured else None,
                "maxMs": max(measured) if measured else None,
                "lastMs": measured[-1] if measured else None,
                "byOutcome": by_outcome[scope],
            }
        return result

    _PROVIDER_CACHE_COUNTERS = ("inputTokens", "cachedTokens", "outputTokens", "reasoningTokens")

    def _provider_cache_summary(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Aggregate provider cache counters without inventing zeroes.

        Totals sum only counters the provider actually reported; a counter
        nobody reported stays ``None`` (unknown), never zero.  The cache ratio
        uses only requests that reported both input and cached counts with a
        consistent ``cached <= input`` relationship, and ``ratioRequests``
        exposes that coverage.  ``usageComplete`` means every counted request
        contributed to the ratio.
        """
        requests = len(entries)
        totals = {key: 0 for key in self._PROVIDER_CACHE_COUNTERS}
        reported = {key: 0 for key in self._PROVIDER_CACHE_COUNTERS}
        with_usage = 0
        ratio_requests = 0
        ratio_input = 0
        ratio_cached = 0
        for entry in entries:
            present: set[str] = set()
            for key in self._PROVIDER_CACHE_COUNTERS:
                value = entry.get(key)
                if type(value) is int and value >= 0:
                    reported[key] += 1
                    totals[key] += value
                    present.add(key)
            if present:
                with_usage += 1
            input_value = entry.get("inputTokens")
            cached_value = entry.get("cachedTokens")
            if (
                type(input_value) is int
                and type(cached_value) is int
                and 0 <= cached_value <= input_value
            ):
                ratio_requests += 1
                ratio_input += input_value
                ratio_cached += cached_value
        cache_ratio = (
            ratio_cached / ratio_input
            if ratio_requests and ratio_input > 0
            else None
        )

        def total(key: str) -> int | None:
            return totals[key] if reported[key] else None

        return {
            "requests": requests,
            "requestsWithUsage": with_usage,
            "requestsMissingUsage": requests - with_usage,
            "ratioRequests": ratio_requests,
            "usageComplete": requests > 0 and ratio_requests == requests,
            "input": total("inputTokens"),
            "cached": total("cachedTokens"),
            "output": total("outputTokens"),
            "reasoning": total("reasoningTokens"),
            "cacheRatio": cache_ratio,
        }

    def get_provider_cache_stats(self) -> dict[str, Any]:
        """Summarize durable provider cache counters on the active branch.

        Each physical provider request appends exactly one ``provider_request``
        timing entry, including retries, timeouts, cancellations and failed
        attempts.  Unknown or legacy entries carry no counters and therefore
        make the corresponding aggregate unknown rather than zero.
        """
        entries = [
            entry
            for entry in self.get_branch()
            if entry.get("type") == "timing" and entry.get("scope") == "provider_request"
        ]
        summary = self._provider_cache_summary(entries)

        def grouped(key: str) -> dict[str, dict[str, Any]]:
            groups: dict[str, list[dict[str, Any]]] = {}
            for entry in entries:
                value = entry.get(key)
                name = value if isinstance(value, str) and value else "unknown"
                groups.setdefault(name, []).append(entry)
            return {name: self._provider_cache_summary(groups[name]) for name in sorted(groups)}

        summary["byProvider"] = grouped("provider")
        summary["byPurpose"] = grouped("purpose")
        return summary

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
        # Keep the historical session_info entry format.  ``None`` remains
        # accepted for old callers, while user-provided names are validated.
        normalized = None if name is None else normalize_session_name(name)
        return self._append(
            {
                "type": "session_info",
                "id": _id(),
                "parentId": self._leaf_id,
                "timestamp": _now_iso(),
                "name": normalized,
            }
        )

    def get_session_name(self) -> str | None:
        for e in reversed(self._entries[1:]):
            if e.get("type") == "session_info":
                return e.get("name") or None
        return None

    def set_session_name(self, name: str) -> str:
        return self.append_session_info(name)

    def set_automatic_name_from_prompt(self, prompt: str) -> bool:
        """Persist a local title from the first user prompt, once only."""
        if self.get_session_name() is not None:
            return False
        name = normalize_session_name(prompt, truncate=True)
        if not name:
            return False
        self.append_session_info(name)
        return True

    def get_last_compaction(self) -> dict[str, Any] | None:
        for e in reversed(self._entries[1:]):
            if e.get("type") == "compaction":
                return self._public_entry(e)
        return None

    def get_rehydration_state(self) -> dict[str, dict[str, object]]:
        """Return private deduplication evidence from the latest branch compaction."""
        for entry in reversed(self._get_branch()):
            if entry.get("type") == "compaction":
                try:
                    return _rehydration_state(entry.get("rehydrationState", {}))
                except (ValueError, UnicodeError):
                    return {}
        return {}

    def get_entry(self, entry_id: str) -> dict[str, Any] | None:
        entry = self._by_id.get(entry_id)
        return self._public_entry(entry) if entry else None

    @staticmethod
    def _public_entry(entry: dict[str, Any]) -> dict[str, Any]:
        if entry.get("type") == "native_compaction":
            return {key: entry[key] for key in ("type", "id", "parentId", "timestamp") if key in entry}
        if entry.get("type") == "compaction" and "rehydrationState" in entry:
            return {key: value for key, value in entry.items() if key != "rehydrationState"}
        return entry

    def get_entries(self) -> list[dict[str, Any]]:
        return [self._public_entry(x) for x in self._entries[1:]]

    def get_header(self) -> dict[str, Any]:
        return self._entries[0]

    def get_effective_affinity_id(self) -> str:
        """Stable cache-affinity identity for this session lineage.

        Normal sessions use their own id. Forks and subagents persist the
        inherited lineage id in their header so reopening, forking, or spawning
        again keeps the same provider-side cache affinity.
        """
        value = self.get_header().get("affinityId")
        if isinstance(value, str) and value:
            return value
        return self.session_id

    def get_leaf_id(self) -> str | None:
        return self._leaf_id

    def branch(self, branch_from_id: str) -> None:
        if branch_from_id not in self._by_id:
            raise ValueError(f"Entry {branch_from_id} not found")
        self._leaf_id = branch_from_id

    def reset_leaf(self) -> None:
        self._leaf_id = None

    def branch_with_summary(
        self, branch_from_id: str | None, summary: str, details: Any = None, from_hook: bool = False
    ) -> str:
        self._leaf_id = branch_from_id
        return self.append_branch_summary(branch_from_id, summary, details, from_hook)

    def get_branch(self, from_id: str | None = None) -> list[dict[str, Any]]:
        return [self._public_entry(entry) for entry in self._get_branch(from_id)]

    def _get_branch(self, from_id: str | None = None) -> list[dict[str, Any]]:
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

        Derivative blocks and synthetic summaries have no entry IDs.
        """
        return [e["id"] for e in self.get_branch(from_id) if e.get("type") in self._MESSAGE_ENTRY_TYPES]

    def get_tree(self) -> list[dict[str, Any]]:
        nodes = {e["id"]: {"entry": self._public_entry(e), "children": []} for e in self._entries[1:]}
        roots: list[dict[str, Any]] = []
        for e in self._entries[1:]:
            pid = e.get("parentId")
            if pid and pid in nodes and pid != e["id"]:
                nodes[pid]["children"].append(nodes[e["id"]])
            else:
                roots.append(nodes[e["id"]])
        return roots

    def build_session_context(self, *, raw_messages: bool = False) -> dict[str, Any]:
        path = self._get_branch()
        messages: list[dict[str, Any]] = []
        thinking_level = "off"
        temperature = None
        model = None
        compaction = None
        preset_state = None
        preset_state_present = False

        for e in path:
            t = e.get("type")
            if t == "thinking_level_change":
                thinking_level = e.get("thinkingLevel", thinking_level)
            elif t == "temperature_change":
                temperature = e.get("temperature", temperature)
            elif t == "model_change":
                model = {"provider": e.get("provider"), "modelId": e.get("modelId")}
            elif t == "message":
                m = e.get("message", {})
                if m.get("role") == "assistant":
                    model = {"provider": m.get("provider"), "modelId": m.get("model")}
            elif t == "compaction":
                compaction = e
            elif t == "preset_state":
                preset_state_present = True
                preset_state = e.get("state")

        def append_entry(ent: dict[str, Any]) -> None:
            t = ent.get("type")
            if t == "message":
                messages.append(ent["message"])
            elif t == "custom_message":
                messages.append(
                    {
                        "role": "custom",
                        "customType": ent.get("customType"),
                        "content": ent.get("content"),
                        "display": ent.get("display"),
                        "details": ent.get("details"),
                        "timestamp": ent.get("timestamp"),
                    }
                )
            elif t == "branch_summary":
                messages.append(
                    {
                        "role": "custom",
                        "customType": "branch_summary",
                        "content": ent.get("summary", ""),
                        "timestamp": ent.get("timestamp"),
                        "fromId": ent.get("fromId"),
                    }
                )

        if compaction and not raw_messages:
            messages.append(
                {
                    "role": "custom",
                    "customType": "compaction_summary",
                    "content": compaction.get("summary", ""),
                    "tokensBefore": compaction.get("tokensBefore", 0),
                    "timestamp": compaction.get("timestamp"),
                }
            )
            messages.extend(_derivative_blocks(compaction.get("derivativeBlocks", [])))
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
            "temperature": temperature,
            "model": model,
            "presetState": preset_state,
            "presetStatePresent": preset_state_present,
        }

    def create_branched_session(self, leaf_id: str) -> str | None:
        if leaf_id not in self._by_id:
            raise ValueError(f"Entry {leaf_id} not found")
        path = self._get_branch(leaf_id)
        sid = str(uuid.uuid4())
        ts = _now_iso()
        header = {
            "type": "session",
            "version": CURRENT_SESSION_VERSION,
            "id": sid,
            "timestamp": ts,
            "cwd": self._cwd,
            "parentSession": self._session_file,
            # A fork shares the source lineage's provider cache affinity.
            "affinityId": self.get_effective_affinity_id(),
        }
        history = self.run_history()
        runs = history.read() if history else []
        if runs:
            header["parentRunId"] = runs[-1]["runId"]
        if "subagentDepth" in self.get_header():
            header["subagentDepth"] = self.get_header()["subagentDepth"]
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
        p = Path(
            output_path or f"session-{datetime.now(UTC).isoformat().replace(':', '-').replace('.', '-')}.jsonl"
        ).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(json.dumps(self._public_entry(e)) for e in self._entries) + "\n", encoding="utf-8")
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

    def get_session_created_at(self) -> float | None:
        """Return the durable session-header creation time as epoch seconds.

        Session age intentionally uses the JSONL header rather than process
        startup, so it includes idle time and survives a session reload.
        """
        timestamp = self._entries[0].get("timestamp") if self._entries else None
        if not isinstance(timestamp, str):
            return None
        try:
            return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        except (OverflowError, OSError, ValueError):
            return None

    @classmethod
    def create(cls, cwd: str, session_dir: str | None = None) -> SessionManager:
        return cls(cwd, session_dir or get_default_session_dir(cwd), None, True)

    @classmethod
    def open(cls, path: str, session_dir: str | None = None) -> SessionManager:
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
    def continue_recent(cls, cwd: str, session_dir: str | None = None) -> SessionManager:
        d = Path(session_dir or get_default_session_dir(cwd))
        ensure_private_dir(d)
        files = sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)
        if files:
            return cls.open(str(files[0]), str(d))
        return cls.create(cwd, str(d))

    @classmethod
    def in_memory(cls, cwd: str | None = None) -> SessionManager:
        return cls(cwd or str(Path.cwd()), "", None, False)

    @classmethod
    def fork_from(cls, source_path: str, target_cwd: str, session_dir: str | None = None) -> SessionManager:
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
                # Forked work shares the source lineage's cache affinity.
                "affinityId": source.get_effective_affinity_id(),
            }
        ] + deepcopy(source._entries[1:])
        history = source.run_history()
        runs = history.read() if history else []
        if runs:
            target._entries[0]["parentRunId"] = runs[-1]["runId"]
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
                        modified=datetime.fromtimestamp(p.stat().st_mtime, tz=UTC),
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

    @classmethod
    def delete(cls, path: str, session_dir: str) -> None:
        """Delete one managed session and its associated evidence sidecar.

        This deliberately accepts no arbitrary path outside *session_dir*.
        Filesystem deletion of two paths is not atomic, so all validation occurs
        first and the sidecar is removed before the owning JSONL; a failure can
        therefore never leave an orphaned sidecar for a deleted session.
        """
        directory = Path(session_dir).resolve()
        candidate = Path(path).resolve()
        if candidate.parent != directory or candidate.suffix != ".jsonl":
            raise ValueError("Session path is outside the managed session directory")
        if not candidate.exists() or not candidate.is_file():
            raise FileNotFoundError("Session no longer exists")
        history = candidate.with_suffix(candidate.suffix + ".agents")
        if history.is_symlink() or (history.exists() and not history.is_file()):
            raise ValueError("Invalid subagent history sidecar")
        runs = candidate.with_suffix(candidate.suffix + ".runs")
        if runs.is_symlink() or (runs.exists() and not runs.is_file()):
            raise ValueError("Invalid run history sidecar")
        checkpoints = candidate.with_suffix(candidate.suffix + ".checkpoints")
        if checkpoints.is_symlink():
            raise ValueError("Invalid checkpoint sidecar")
        if checkpoints.exists():
            info = checkpoints.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Invalid checkpoint sidecar")
            if not shutil.rmtree.avoids_symlink_attacks:
                raise ValueError("Safe checkpoint deletion unavailable")
            shutil.rmtree(checkpoints)
        evidence = candidate.with_suffix(candidate.suffix + ".evidence")
        if evidence.exists():
            if not evidence.is_file() or evidence.parent != directory:
                raise ValueError("Invalid session evidence sidecar")
            evidence.unlink()
        history.unlink(missing_ok=True)
        runs.unlink(missing_ok=True)
        candidate.unlink()
