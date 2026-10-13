"""Transport-neutral, UI-loop-safe facade for an agent-owned runtime thread."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from one.core.subagent_monitor import SubagentMonitor
from one.core.threaded_agent_runtime import (
    DEFAULT_COMMAND_DTO_MAX_BYTES,
    RuntimeBootstrapSpec,
    RuntimeClosedError,
    RuntimeFactory,
    ThreadedAgentRuntime,
)

_LOG = logging.getLogger(__name__)
_COALESCIBLE = frozenset({"message_update", "text_delta", "thinking_delta"})
_SENSITIVE = frozenset({"api_key", "apikey", "authorization", "token", "secret", "password", "rawresult", "raw_result", "evidence"})
DEFAULT_EVENT_DTO_MAX_BYTES = 64 * 1024
DEFAULT_EVENT_QUEUE_MAX_BYTES = 512 * 1024
DEFAULT_EVENT_DRAIN_MAX_ITEMS = 32
DEFAULT_EVENT_DRAIN_MAX_SECONDS = 0.005
MAX_COALESCED_DISPLAY_DELTA_BYTES = 512
_DISPLAY_TEXT_MAX_BYTES = 32 * 1024
_IDENTITY_FIELDS = ("sessionId", "session_id", "messageId", "message_id", "turnId", "id")
_DISPLAY_TEXT_FIELDS = frozenset({
    "text", "delta", "value", "content", "task", "question", "error", "reason",
    "warning", "answer", "displayArgs", "outputText",
})
_PLAN_ITEM_FIELDS = ("title", "step", "status")


def _bounded_text(value: Any, maximum: int = _DISPLAY_TEXT_MAX_BYTES) -> str:
    """Return literal text capped by bytes, without interpreting it for a UI."""
    if not isinstance(value, str):
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    return encoded[:maximum].decode("utf-8", "ignore") + "<truncated>"


def _display_text(value: Any, maximum: int = _DISPLAY_TEXT_MAX_BYTES) -> str:
    """Sanitize and redact untrusted display prose before bounding it."""
    return _bounded_text(SubagentMonitor._safe_text(value), maximum)


def _fields(event: Mapping[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    """Copy only scalar display fields; structured runtime state never crosses here."""
    result: dict[str, Any] = {}
    for name in names:
        value = event.get(name)
        if value is None or isinstance(value, (bool, int, float, str)):
            if isinstance(value, str):
                value = _display_text(value) if name in _DISPLAY_TEXT_FIELDS else _bounded_text(value)
            if value is not None:
                result[name] = value
    return result


def _event_projection(event: Mapping[str, Any]) -> dict[str, Any]:
    """Make the detached, allowlisted UI DTO before generic bounding/encoding."""
    event_type = _bounded_text(event.get("type"), 128) or "event"
    result: dict[str, Any] = {"type": event_type}
    # This is stamped by TuiRuntimeHost, rather than guessed from the last UI
    # command reply.  It identifies the owner session while leaving event-level
    # sessionId available for child/monitor identities.
    if isinstance(event.get("sourceSessionId"), str):
        result["sourceSessionId"] = _bounded_text(event["sourceSessionId"], 256)

    if event_type == "tool_call_start":
        result.update(_fields(event, ("tool", "toolCallId", "effectiveTimeout")))
        result["displayArgs"] = _bounded_text(SubagentMonitor.display_tool_args(event.get("args")))
        return result

    if event_type == "tool_call_end":
        result.update(_fields(event, (
            "tool", "toolCallId", "ok", "elapsedMs", "exitCode", "timedOut", "timeout",
            "cancelled", "aborted",
        )))
        raw_result = event.get("result")
        # visible_tool_result is an allowlisted display projection.  Give it only
        # its supported visible field so a fallback cannot stringify a raw object.
        visible_payload: dict[str, Any] = {}
        if isinstance(raw_result, Mapping):
            key = "outputText" if event.get("ok") else "error"
            if isinstance(raw_result.get(key), str):
                visible_payload[key] = raw_result[key]
        visible = SubagentMonitor.visible_tool_result({"ok": bool(event.get("ok")), "result": visible_payload})
        if visible:
            result["outputText" if event.get("ok") else "error"] = _display_text(visible)
        if isinstance(raw_result, Mapping):
            evidence_id = raw_result.get("evidenceId")
            if isinstance(evidence_id, str) and evidence_id:
                result["evidenceId"] = _bounded_text(evidence_id, 256)
            if raw_result.get("evidenceUnavailable"):
                result["evidenceUnavailable"] = True
        return result

    if event_type in {"message_update", "text_delta"}:
        result.update(_fields(event, _IDENTITY_FIELDS + ("text", "delta", "value")))
        nested = event.get("assistantMessageEvent")
        if isinstance(nested, Mapping) and nested.get("type") == "text_delta" and isinstance(nested.get("delta"), str):
            result["assistantMessageEvent"] = {"type": "text_delta", "delta": _display_text(nested["delta"])}
        return result

    if event_type == "thinking_delta":
        result.update(_fields(event, _IDENTITY_FIELDS + ("delta", "text")))
        return result

    if event_type == "plan_update":
        # Plans are state, rather than arbitrary tool output.  Keep an explicit
        # presence/clear marker: an omitted plan must never be interpreted as a
        # request to clear the UI projection.
        result["planPresent"] = "plan" in event or "planItems" in event
        result["cleared"] = event.get("plan") == "" if "plan" in event else False
        if isinstance(event.get("plan"), str):
            result["plan"] = _display_text(event["plan"], 24 * 1024)
        items = event.get("planItems")
        if isinstance(items, list):
            safe_items: list[dict[str, str]] = []
            budget = 24 * 1024
            omitted = 0
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                safe = {
                    name: _display_text(item.get(name), 512)
                    for name in _PLAN_ITEM_FIELDS if isinstance(item.get(name), str)
                }
                if safe:
                    size = len(json.dumps(safe, ensure_ascii=False).encode("utf-8"))
                    if size <= budget:
                        safe_items.append(safe)
                        budget -= size
                    else:
                        omitted += 1
            result["planItems"] = safe_items
            if omitted:
                result["planItemsOmitted"] = omitted
        return result

    if event_type == "subagent_monitor_invalidated":
        result.update(_fields(event, ("sessionId", "revision", "changeType")))
        return result

    if event_type == "mcp_availability":
        result.update(_fields(event, ("name", "state", "toolCount", "reasonCode")))
        return result

    if event_type == "tui_approval_request":
        result.update(_fields(event, ("approvalId", "tool", "origin", "reason", "displayArgs")))
        return result

    if event_type in {"extension_ui_request", "extension_ui_response"}:
        result.update(_fields(event, ("id", "requestId", "extension", "uiType", "cancelled", "error")))
        payload = event.get("payload")
        if isinstance(payload, Mapping):
            # Extension UI is explicitly opt-in but remains bounded and detached.
            result["payload"] = _safe(payload, budget=[8 * 1024])
        return result

    if event_type in {"message_start", "message_end"}:
        result.update(_fields(event, _IDENTITY_FIELDS + ("role", "model", "suppressed")))
        message = event.get("message")
        if isinstance(message, Mapping):
            result.update(_fields(message, ("role", "model")))
        return result

    lifecycle_fields = (
        "ok", "reason", "error", "errorType", "attempt", "aborted", "cancelled", "timedOut",
        "willRetry", "used", "count", "kind", "enabled", "fireCount", "elapsedMs", "exitCode",
        "tool", "toolCallId", "provider", "model", "temperature", "temperatureMode", "warning",
        "question", "answer", "task",
    )
    known_lifecycle = {
        "agent_start", "agent_end", "turn_start", "turn_end", "ask_user", "ask_user_answered",
        "ask_user_released", "tool_approval_rejected", "tool_call_error", "tool_call_parse_failed",
        "subagent_start", "subagent_end", "warning", "error", "budget_exceeded", "compaction_start",
        "compaction_end", "auto_retry_start", "auto_retry_end", "tool_call_nudge_start", "tool_call_nudge_end",
        "tool_response_repair_start", "tool_response_repair_end", "cooperation_changed", "temperature_change",
        "provider_request_start", "provider_request_end",
    }
    if event_type in known_lifecycle:
        result.update(_fields(event, _IDENTITY_FIELDS + lifecycle_fields))
        if event_type == "tool_call_parse_failed":
            # Safe parser diagnostics only: never forward candidate text/args.
            result.update(_fields(event, ("source", "category", "reason", "tool", "toolCallId")))
    return result


def _safe(value: Any, *, budget: list[int], depth: int = 0) -> Any:
    """Project an event before copying it, keeping the result bounded and detached."""
    if budget[0] <= 0 or depth > 5:
        return "<omitted>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "<non-finite>"
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        take = min(len(encoded), budget[0])
        budget[0] -= take
        return encoded[:take].decode("utf-8", "ignore") + ("<truncated>" if take < len(encoded) else "")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index == 64:
                break
            name = str(key)[:256]
            if name.lower().replace("-", "_") in _SENSITIVE:
                continue
            result[name] = _safe(item, budget=budget, depth=depth + 1)
            if budget[0] <= 0:
                break
        return result
    if isinstance(value, (list, tuple)):
        list_result: list[Any] = []
        for item in value[:64]:
            list_result.append(_safe(item, budget=budget, depth=depth + 1))
            if budget[0] <= 0:
                break
        return list_result
    return f"<{type(value).__name__}>"


def _event_snapshot(event: Mapping[str, Any], maximum: int) -> dict[str, Any]:
    snapshot = _safe(event, budget=[maximum])
    assert isinstance(snapshot, dict)
    # Structural JSON encoding confirms detached transport compatibility and hard cap.
    encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > maximum:
        return {"type": str(snapshot.get("type", "event"))[:128], "detail": "<event omitted: too large>"}
    return snapshot


def _encoded_size(value: Mapping[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))


def _fit_event_snapshot(snapshot: dict[str, Any], maximum: int) -> dict[str, Any]:
    """Fit an already-projected event, retaining lifecycle identity on clipping.

    Epoch and sequence are part of the transport envelope, so apply this after
    adding them.  Unlike a generic omission fallback, this keeps terminal and
    approval/question event types and IDs observable when display prose is bad
    or unexpectedly large.
    """
    if _encoded_size(snapshot) <= maximum:
        return snapshot
    retained: dict[str, Any] = {"type": _bounded_text(snapshot.get("type"), 128)}
    for name in (
        "epoch", "sequence", "sessionId", "session_id", "turnId", "messageId",
        "message_id", "id", "approvalId", "toolCallId", "tool", "ok",
    ):
        value = snapshot.get(name)
        if isinstance(value, (bool, int, float)) or value is None:
            if value is not None:
                retained[name] = value
        elif isinstance(value, str):
            retained[name] = _bounded_text(value, 256)
    retained["displayTruncated"] = True
    # Add fields one at a time. Strings are binary-clipped to leave room for
    # the explicit marker; nested payloads have no place in a display DTO.
    for name, value in snapshot.items():
        if name in retained or isinstance(value, (dict, list, tuple)):
            continue
        candidate = dict(retained)
        candidate[name] = value
        if _encoded_size(candidate) <= maximum:
            retained = candidate
            continue
        if not isinstance(value, str):
            continue
        raw = value.encode("utf-8")
        low, high, best = 0, len(raw), ""
        while low <= high:
            middle = (low + high) // 2
            clipped = raw[:middle].decode("utf-8", "ignore") + "<truncated>"
            candidate[name] = clipped
            if _encoded_size(candidate) <= maximum:
                best = clipped
                low = middle + 1
            else:
                high = middle - 1
        if best:
            retained[name] = best
    # A pathological tiny configured limit cannot contain the normal envelope;
    # production's 64 KiB limit always can. Keep the type/marker rather than
    # raising in the producer thread.
    return retained


class AgentRuntimeFacade:
    """Schedules bounded event DTOs and allowed commands on the caller's loop."""

    def __init__(
        self,
        spec: RuntimeBootstrapSpec,
        factory: RuntimeFactory,
        *,
        event_capacity: int = 128,
        event_dto_max_bytes: int = DEFAULT_EVENT_DTO_MAX_BYTES,
        event_queue_max_bytes: int = DEFAULT_EVENT_QUEUE_MAX_BYTES,
        event_drain_max_items: int = DEFAULT_EVENT_DRAIN_MAX_ITEMS,
        event_drain_max_seconds: float = DEFAULT_EVENT_DRAIN_MAX_SECONDS,
        command_allowlist: Iterable[str] = (),
        command_dto_max_bytes: int = DEFAULT_COMMAND_DTO_MAX_BYTES,
    ) -> None:
        if event_capacity < 2:
            raise ValueError("event_capacity must be at least two")
        if event_dto_max_bytes < 1 or event_queue_max_bytes < event_dto_max_bytes:
            raise ValueError("event byte limits must be positive and queue limit must fit one event")
        if event_drain_max_items < 1 or event_drain_max_seconds <= 0:
            raise ValueError("event drain limits must be positive")
        self._lock = threading.Lock()
        self._space = threading.Condition(self._lock)
        self._events: deque[tuple[dict[str, Any], int]] = deque()
        self._event_bytes = 0
        self._capacity = event_capacity
        self._event_dto_max_bytes = event_dto_max_bytes
        self._event_queue_max_bytes = event_queue_max_bytes
        self._event_drain_max_items = event_drain_max_items
        self._event_drain_max_seconds = event_drain_max_seconds
        # A listener may provide a readiness predicate.  In particular, the
        # Textual ingress uses this to leave an event in this queue until its
        # bounded UI accumulator has room for it.
        self._listeners: list[tuple[Callable[[dict[str, Any]], None], Callable[[dict[str, Any]], bool] | None]] = []
        self._ui_loop: asyncio.AbstractEventLoop | None = None
        self._drain_scheduled = False
        self._accepting_events = True
        self._epoch = 0
        self._sequence = 0
        self._backend = ThreadedAgentRuntime(
            spec, factory, self._on_worker_event,
            command_allowlist=command_allowlist,
            command_dto_max_bytes=command_dto_max_bytes,
        )

    async def start(self) -> None:
        await asyncio.wrap_future(self._backend.start())

    def subscribe(
        self,
        listener: Callable[[dict[str, Any]], None],
        *,
        can_accept_event: Callable[[dict[str, Any]], bool] | None = None,
    ) -> Callable[[], None]:
        loop = asyncio.get_running_loop()
        with self._space:
            self._ui_loop = loop
            self._listeners.append((listener, can_accept_event))
            self._schedule_drain_locked()

        def off() -> None:
            with self._space:
                self._listeners[:] = [item for item in self._listeners if item[0] is not listener]

        return off

    def resume_events(self) -> None:
        """Resume UI delivery after a bounded consumer frees ingress space."""
        with self._space:
            self._schedule_drain_locked()

    async def command(self, command: str, payload: Mapping[str, Any] | None = None, *, correlation_id: str | None = None) -> dict[str, Any]:
        future = self._backend.submit(correlation_id or uuid.uuid4().hex, command, payload or {})
        reply = await asyncio.wrap_future(future)
        # The host is authoritative for session epochs.  Carry that epoch into
        # subsequent event DTOs so an ID-bound UI reply cannot target a newer
        # session after a switch.
        result = reply.get("result") if isinstance(reply, Mapping) else None
        if isinstance(result, Mapping) and isinstance(result.get("epoch"), int):
            with self._space:
                self._epoch = result["epoch"]
        return reply

    async def close(self) -> None:
        with self._space:
            self._accepting_events = False
            self._space.notify_all()
        await asyncio.wrap_future(self._backend.close())

    def finalize(self, timeout: float = 2.0) -> bool:
        """Only call after the UI loop has stopped."""
        return self._backend.join(timeout)

    @staticmethod
    def _text_key(event: Mapping[str, Any]) -> str | None:
        for key in ("text", "delta", "content", "value"):
            if isinstance(event.get(key), str):
                return key
        return None

    @staticmethod
    def _delta(event: Mapping[str, Any]) -> tuple[str, tuple[str, ...], str] | None:
        """Return the delta kind, its DTO path, and text for supported stream events."""
        event_type = event.get("type")
        nested = event.get("assistantMessageEvent")
        if event_type == "message_update" and isinstance(nested, Mapping):
            kind = nested.get("type")
            delta = nested.get("delta")
            if isinstance(kind, str) and isinstance(delta, str):
                return kind, ("assistantMessageEvent", "delta"), delta
        if event_type in {"text_delta", "thinking_delta"}:
            key = AgentRuntimeFacade._text_key(event)
            if key is not None:
                return event_type, (key,), str(event[key])
        return None

    @staticmethod
    def _identities_compatible(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
        """Only reject identities that are explicitly present on both events."""
        pairs = (("sessionId", "session_id"), ("messageId", "message_id"), ("turnId",), ("id",))
        for names in pairs:
            old = next((previous.get(name) for name in names if previous.get(name) is not None), None)
            new = next((current.get(name) for name in names if current.get(name) is not None), None)
            if old is not None and new is not None and old != new:
                return False
        return True

    @staticmethod
    def _set_delta(snapshot: dict[str, Any], path: tuple[str, ...], value: str) -> None:
        if len(path) == 1:
            snapshot[path[0]] = value
        else:
            nested = dict(snapshot[path[0]])
            nested[path[1]] = value
            snapshot[path[0]] = nested

    def _fit_delta_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Make delta cap loss explicit instead of relying on generic snapshot clipping."""
        encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) <= self._event_dto_max_bytes:
            return snapshot
        delta = self._delta(snapshot)
        if delta is None:
            return _fit_event_snapshot(snapshot, self._event_dto_max_bytes)
        _, path, text = delta
        candidate = dict(snapshot)
        candidate["deltaTruncated"] = True
        low, high, best = 0, len(text.encode("utf-8")), ""
        raw = text.encode("utf-8")
        while low <= high:
            middle = (low + high) // 2
            clipped = raw[:middle].decode("utf-8", "ignore")
            self._set_delta(candidate, path, clipped)
            if len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= self._event_dto_max_bytes:
                best = clipped
                low = middle + 1
            else:
                high = middle - 1
        self._set_delta(candidate, path, best)
        return _fit_event_snapshot(candidate, self._event_dto_max_bytes)

    def _try_coalesce_locked(self, snapshot: dict[str, Any], size: int) -> bool:
        if not self._events:
            return False
        previous, previous_size = self._events[-1]
        current_delta = self._delta(snapshot)
        previous_delta = self._delta(previous)
        if (snapshot.get("type") not in _COALESCIBLE or previous.get("type") != snapshot.get("type")
                or current_delta is None or previous_delta is None
                or current_delta[0] != previous_delta[0]
                or not self._identities_compatible(previous, snapshot)):
            return False
        merged = dict(snapshot)
        self._set_delta(merged, current_delta[1], previous_delta[2] + current_delta[2])
        # Coalescing is an ingress optimization, not permission to hand the UI
        # one arbitrarily expensive format/render operation. Leave the next
        # delta queued in order once this bounded display chunk is full.
        if len(self._delta(merged)[2].encode("utf-8")) > MAX_COALESCED_DISPLAY_DELTA_BYTES:  # type: ignore[index]
            return False
        if previous.get("deltaTruncated"):
            merged["deltaTruncated"] = True
        merged = self._fit_delta_snapshot(merged)
        merged_size = len(json.dumps(merged, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if self._event_bytes - previous_size + merged_size > self._event_queue_max_bytes:
            return False
        self._events[-1] = (merged, merged_size)
        self._event_bytes += merged_size - previous_size
        return True

    def _on_worker_event(self, event: dict[str, Any]) -> None:
        snapshot = _event_projection(event)
        with self._space:
            if not self._accepting_events:
                return
            self._sequence += 1
            epoch = event.get("epoch")
            snapshot.update({"epoch": epoch if isinstance(epoch, int) else self._epoch, "sequence": self._sequence})
            snapshot = self._fit_delta_snapshot(snapshot)
            size = len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if self._try_coalesce_locked(snapshot, size):
                self._schedule_drain_locked()
                return
            while (len(self._events) >= self._capacity or self._event_bytes + size > self._event_queue_max_bytes) and self._accepting_events:
                self._space.wait()
            if not self._accepting_events:
                return
            self._events.append((snapshot, size))
            self._event_bytes += size
            self._schedule_drain_locked()

    def _schedule_drain_locked(self) -> None:
        loop = self._ui_loop
        if self._events and loop is not None and not self._drain_scheduled:
            self._drain_scheduled = True
            loop.call_soon_threadsafe(self._drain_on_ui_loop)

    def _drain_on_ui_loop(self) -> None:
        """Deliver one bounded worker batch and yield to the UI between batches.

        Keeping the source queue occupied until this slice is actually removed is
        intentional: it applies backpressure to the runtime instead of moving an
        unbounded burst to a second UI-loop queue.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._event_drain_max_seconds
        delivered = 0
        while delivered < self._event_drain_max_items and loop.time() < deadline:
            with self._space:
                if not self._events:
                    self._drain_scheduled = False
                    return
                event, size = self._events[0]
                listeners = tuple(self._listeners)
            # Never remove an event until every bounded consumer can receive it.
            # This check runs on the UI loop but is deliberately nonblocking.
            if any(predicate is not None and not predicate(event) for _, predicate in listeners):
                with self._space:
                    self._drain_scheduled = False
                return
            with self._space:
                # A subscription change cannot make delivery unsafe; if a new
                # bounded consumer arrived, recheck it on the next UI slice.
                if not self._events or self._events[0][0] is not event:
                    continue
                event, size = self._events.popleft()
                self._event_bytes -= size
                self._space.notify_all()
            for listener, _predicate in listeners:
                try:
                    listener(event)
                except Exception:
                    _LOG.exception("agent runtime event listener failed")
            delivered += 1
        with self._space:
            # A producer can enqueue after the first lock release while this drain
            # is delivering callbacks; ensure it is never left without a wakeup.
            self._drain_scheduled = False
            if self._events and self._ui_loop is not None and not self._drain_scheduled:
                # Do not monopolize asyncio's ready queue during a producer flood:
                # let Textual poll keyboard/input work before the next slice.
                self._drain_scheduled = True
                self._ui_loop.call_later(0.001, self._drain_on_ui_loop)


__all__ = ["AgentRuntimeFacade", "RuntimeBootstrapSpec", "RuntimeClosedError"]
