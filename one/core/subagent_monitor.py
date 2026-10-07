"""Bounded, in-memory observation state for a session's direct subagents."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one

from __future__ import annotations

import copy
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from one.core.provider_login import _redact_credentials
from one.tools.common import sanitize_display_text

MAX_OUTPUT_BYTES = 32 * 1024
MAX_TIMELINE_ENTRIES = 64
MAX_TOOL_NAME_CHARS = 128
MAX_TOOL_ARGS_CHARS = 1024
MAX_COMPLETED_CHILDREN = 20
MAX_LABEL_CHARS = 1024

_SENSITIVE_FIELD_ALIASES = frozenset({
    "password", "passwd", "secret", "token", "authorization", "api_key", "access_token",
})
_CONTENT_FIELD_ALIASES = frozenset({
    "content", "text", "payload", "image", "images", "evidence", "evidence_data", "raw_result", "patch_text", "binary", "blob",
})


class SubagentMonitor:
    """Thread-safe snapshots of direct children, with bounded output tails."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._children: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._subscribers: dict[int, Callable[[dict[str, Any]], None]] = {}
        self._next_subscription = 0
        self._revision = 0

    @staticmethod
    def _label(value: Any) -> str:
        return sanitize_display_text(str(value or ""))[:MAX_LABEL_CHARS]

    @staticmethod
    def _safe_text(value: Any) -> str:
        return _redact_credentials(sanitize_display_text(str(value or "")))

    @staticmethod
    def _normalize_field_name(value: str) -> str:
        """Convert camelCase and hyphenated argument names to snake_case."""
        value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
        return re.sub(r"[-\s]+", "_", value).lower()

    @staticmethod
    def _tail(text: str) -> tuple[str, bool]:
        raw = text.encode("utf-8")
        if len(raw) <= MAX_OUTPUT_BYTES:
            return text, False
        return raw[-MAX_OUTPUT_BYTES:].decode("utf-8", errors="ignore"), True

    @staticmethod
    def display_tool_args(args: Any) -> str:
        """Make a bounded, display-only projection of a tool's arguments.

        This intentionally returns text, rather than a filtered argument mapping:
        monitor snapshots must never retain raw tool-call arguments.
        """
        if not isinstance(args, dict):
            return ""
        pieces: list[str] = []
        for key, value in args.items():
            label = sanitize_display_text(str(key))[:80]
            normalized = SubagentMonitor._normalize_field_name(label)
            if not label:
                continue
            if normalized in _SENSITIVE_FIELD_ALIASES:
                shown = "[REDACTED]"
            elif normalized in _CONTENT_FIELD_ALIASES:
                shown = f"<{len(value)} chars>" if isinstance(value, str) else "<content omitted>"
            elif isinstance(value, str):
                shown = SubagentMonitor._safe_text(value)
                if len(shown) > 240:
                    shown = shown[:237] + "..."
            elif isinstance(value, (int, float, bool)) or value is None:
                shown = str(value).lower() if isinstance(value, bool) else str(value)
            elif isinstance(value, (list, tuple, set, dict)):
                shown = f"<{len(value)} items>"
            else:
                shown = "<value omitted>"
            pieces.append(f"{label}={shown}")
        return sanitize_display_text(", ".join(pieces))[:MAX_TOOL_ARGS_CHARS]

    @staticmethod
    def visible_tool_result(event: dict[str, Any]) -> str:
        """Return precisely the safe, main-TUI-visible result projection.

        This is deliberately an allowlist: callers must not hand monitor state a
        result object, because those objects can contain evidence and image data.
        """
        payload = event.get("result")
        if not isinstance(payload, dict):
            return ""
        if event.get("ok"):
            value = payload.get("outputText") or payload.get("result") or ""
        else:
            value = payload.get("error") or ""
        return SubagentMonitor._safe_text(value).rstrip()

    @staticmethod
    def _response_text(child: dict[str, Any]) -> str:
        """Compatibility view of response blocks, without retaining a copy."""
        return "".join(str(item.get("text") or "") for item in child["timeline"] if item.get("kind") == "response")

    def _enforce_preview_budget(self, child: dict[str, Any]) -> None:
        """Keep all visible response/result bodies in one per-child budget."""
        def total() -> int:
            return sum(
                len(str(item.get("text") if item.get("kind") == "response" else item.get("output") or "").encode("utf-8"))
                + (len(str(item.get("args") or "").encode("utf-8")) if item.get("kind") == "tool" else 0)
                for item in child["timeline"]
            )

        # Discard oldest visible bodies first while retaining their lifecycle
        # blocks. This preserves recent chronology and makes the truncation
        # marker meaningful rather than silently reordering prose and tools.
        visible: list[tuple[int, str]] = []
        for index, item in enumerate(child["timeline"]):
            field = "text" if item.get("kind") == "response" else "output"
            if item.get(field):
                visible.append((index, field))
        last_visible = visible[-1][0] if visible else -1
        for index, item in enumerate(child["timeline"]):
            if total() <= MAX_OUTPUT_BYTES:
                break
            field = "text" if item.get("kind") == "response" else "output"
            value = str(item.get(field) or "")
            if value:
                if index == last_visible:
                    other = total() - len(value.encode("utf-8"))
                    value, _ = self._tail(value)
                    allowed = max(0, MAX_OUTPUT_BYTES - other)
                    item[field] = value.encode("utf-8")[-allowed:].decode("utf-8", errors="ignore") if allowed else ""
                else:
                    item[field] = ""
                child["previewTruncated"] = True
                if field == "text":
                    child["outputTruncated"] = True

        # Argument projections are independently bounded at creation. Drop the
        # oldest ones only if bodies alone did not recover the aggregate cap.
        for item in child["timeline"]:
            if total() <= MAX_OUTPUT_BYTES:
                break
            if item.get("kind") == "tool" and item.get("args"):
                item["args"] = ""
                child["previewTruncated"] = True

    def _append_timeline(self, child: dict[str, Any], item: dict[str, Any]) -> None:
        child["timeline"].append(item)
        while len(child["timeline"]) > MAX_TIMELINE_ENTRIES:
            child["timeline"].pop(0)
            child["timelineTruncated"] = True
        self._enforce_preview_budget(child)

    def _append_response(self, child: dict[str, Any], text: Any) -> bool:
        """Append a visible delta, coalescing only adjacent response blocks."""
        delta = sanitize_display_text(str(text or ""))
        if not delta:
            return False
        timeline = child["timeline"]
        if timeline and timeline[-1].get("kind") == "response":
            # Redact after concatenation so split credentials are not retained.
            timeline[-1]["text"] = _redact_credentials(str(timeline[-1].get("text") or "") + delta)
            self._enforce_preview_budget(child)
        else:
            self._append_timeline(child, {"kind": "response", "text": self._safe_text(delta)})
        return True

    def _changed(self, session_id: str, event_type: str) -> tuple[dict[str, Any], list[Callable[[dict[str, Any]], None]]]:
        self._revision += 1
        self._children[session_id]["revision"] = self._revision
        return ({"type": event_type, "sessionId": session_id, "revision": self._revision}, list(self._subscribers.values()))

    @staticmethod
    def _notify(notice: dict[str, Any], listeners: list[Callable[[dict[str, Any]], None]]) -> None:
        for listener in listeners:
            try:
                listener(dict(notice))
            except Exception:
                pass

    def register(self, session_id: str, task: Any, model: Any, *, started_at: float | None = None) -> None:
        now = time.monotonic() if started_at is None else started_at
        with self._lock:
            self._children[session_id] = {
                "sessionId": session_id, "task": self._label(task), "model": self._label(model),
                "startedAt": now, "startedMonotonic": now, "completedAt": None, "status": "running",
                "currentTool": None, "activity": "started", "outputTruncated": False,
                "timeline": [], "timelineTruncated": False, "previewTruncated": False,
                "outcome": None, "error": None, "errorType": None, "externalState": None, "revision": 0,
            }
            notice, listeners = self._changed(session_id, "registered")
        self._notify(notice, listeners)

    def update(self, session_id: str, *, text: Any | None = None, tool: Any | None = None, activity: Any | None = None) -> None:
        with self._lock:
            child = self._children.get(session_id)
            if child is None:
                return
            changed = False
            if text is not None:
                changed = self._append_response(child, text)
            if tool is not None and child["currentTool"] != self._label(tool):
                child["currentTool"] = self._label(tool)
                changed = True
            if activity is not None and child["activity"] != self._label(activity):
                child["activity"] = self._label(activity)
                changed = True
            if not changed:
                return
            notice, listeners = self._changed(session_id, "updated")
        self._notify(notice, listeners)

    def observe_final(self, session_id: str, text: Any) -> None:
        safe = self._safe_text(text)
        if not safe:
            return
        with self._lock:
            child = self._children.get(session_id)
            if child is None:
                return
            responses = [item for item in child["timeline"] if item.get("kind") == "response"]
            tail = str(responses[-1].get("text") or "") if responses else ""
            # Providers may stream the same prose before reporting it in a finish
            # summary. Treat a final newline as equivalent, and never append a
            # full summary when the retained tail is already its suffix.
            comparable_tail = tail.rstrip("\r\n")
            comparable_safe = safe.rstrip("\r\n")
            if (
                not comparable_safe
                or comparable_safe in comparable_tail
                or (comparable_tail and comparable_safe.endswith(comparable_tail))
            ):
                return
            # Preserve only the part of a summary that was not already emitted as
            # a visible delta. This also handles summaries that extend the tail.
            overlap = min(len(comparable_tail), len(safe))
            while overlap and not comparable_tail.endswith(safe[:overlap]):
                overlap -= 1
            addition = safe[overlap:]
            if not addition:
                return
            # A streamed line break separates prose from the provider's finish
            # summary, rather than being part of the prose to reconcile. Keep
            # it when there is no overlap, but replace it when the summary
            # continues an already-streamed suffix.
            combined = _redact_credentials((comparable_tail if overlap and comparable_tail != tail else tail) + addition)
            if child["timeline"] and child["timeline"][-1].get("kind") == "response":
                child["timeline"][-1]["text"] = combined
                self._enforce_preview_budget(child)
            else:
                # A tool since the last prose block is a real ordering boundary.
                # Keep the final suffix after that tool rather than rewriting
                # earlier response text out of chronological position.
                self._append_response(child, addition)
            child["activity"] = "final response"
            notice, listeners = self._changed(session_id, "updated")
        self._notify(notice, listeners)

    def tool_started(self, session_id: str, tool: Any, *, tool_call_id: Any = None, args: Any = None,
                     effective_timeout: Any = None) -> None:
        with self._lock:
            child = self._children.get(session_id)
            if child is None:
                return
            name = (self._label(tool) or "tool")[:MAX_TOOL_NAME_CHARS]
            child["currentTool"] = name
            child["activity"] = "using tool"
            timeout = effective_timeout if isinstance(effective_timeout, (int, float)) and not isinstance(effective_timeout, bool) and effective_timeout > 0 else None
            self._append_timeline(child, {
                "kind": "tool", "tool": name, "toolCallId": self._label(tool_call_id) or None,
                "args": self._safe_text(args)[:MAX_TOOL_ARGS_CHARS] if isinstance(args, str) else self.display_tool_args(args), "effectiveTimeout": timeout,
                "state": "running", "ok": None, "elapsedMs": None, "output": "",
            })
            notice, listeners = self._changed(session_id, "updated")
        self._notify(notice, listeners)

    def tool_finished(
        self, session_id: str, tool: Any, ok: Any, elapsed_ms: Any, output: Any = None, *, retain: bool = True,
        tool_call_id: Any = None
    ) -> None:
        with self._lock:
            child = self._children.get(session_id)
            if child is None:
                return
            name = (self._label(tool) or "tool")[:MAX_TOOL_NAME_CHARS]
            elapsed = elapsed_ms if isinstance(elapsed_ms, (int, float)) and not isinstance(elapsed_ms, bool) else None
            text = self._safe_text(output).rstrip() if output else ""
            child["currentTool"] = None
            child["activity"] = "responding" if child["status"] == "running" else child["activity"]
            if retain:
                call_id = self._label(tool_call_id) or None
                match = next((item for item in child["timeline"] if item.get("kind") == "tool" and item.get("state") == "running" and item.get("toolCallId") == call_id), None) if call_id else None
                if match is None:
                    # Legacy events without IDs pair the oldest matching active
                    # call (FIFO), never merely the most recent same-name call.
                    match = next((item for item in child["timeline"] if item.get("kind") == "tool" and item.get("state") == "running" and item.get("tool") == name), None)
                if match is None:
                    match = {"kind": "tool", "tool": name, "toolCallId": call_id, "args": "", "effectiveTimeout": None}
                    self._append_timeline(child, match)
                match.update({"state": "completed", "ok": bool(ok), "elapsedMs": elapsed, "output": text})
                self._enforce_preview_budget(child)
            notice, listeners = self._changed(session_id, "updated")
        self._notify(notice, listeners)

    def complete(self, session_id: str, status: str, *, outcome: str | None = None, error: Any | None = None,
                 error_type: Any | None = None, external_state: Any | None = None) -> None:
        with self._lock:
            child = self._children.get(session_id)
            if child is None or child["status"] != "running":
                return
            child.update({"status": self._label(status) or "failed", "outcome": self._label(outcome) if outcome else status,
                          "completedAt": time.monotonic(), "activity": "completed",
                          "error": self._safe_text(error)[:512] if error else None,
                          "errorType": self._label(error_type) if error_type else None,
                          "externalState": self._label(external_state) if external_state else None})
            completed = [
                (key, item["completedAt"])
                for key, item in self._children.items()
                if item["status"] != "running"
            ]
            while len(completed) > MAX_COMPLETED_CHILDREN:
                oldest, _ = min(completed, key=lambda item: item[1])
                self._children.pop(oldest, None)
                completed = [item for item in completed if item[0] != oldest]
            notice, listeners = self._changed(session_id, "completed")
        self._notify(notice, listeners)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            children = copy.deepcopy(list(self._children.values()))
            for child in children:
                # outputTail remains a read-compatible snapshot field, derived
                # from timeline response blocks rather than stored twice.
                child["outputTail"] = self._response_text(child)
            return {"revision": self._revision, "children": children}

    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        with self._lock:
            token = self._next_subscription
            self._next_subscription += 1
            self._subscribers[token] = listener

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers.pop(token, None)

        return unsubscribe
