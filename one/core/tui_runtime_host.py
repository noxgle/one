"""TUI-specific, owner-loop command adapter.

This module intentionally keeps the Textual loop away from AgentSession and its
loop-bound collaborators.  The public surface consists solely of JSON DTO
commands; it never returns an AgentSession, manager, registry, or settings
object.
"""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import inspect
import json
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from one.core.agent_session import approval_callback_origin
from one.core.agent_session_runtime import AgentSessionRuntimeHost, create_agent_session_runtime
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.oauth import OAuthError
from one.core.persistence import ensure_private_dir
from one.core.provider_login import entry_id as fetched_entry_id
from one.core.provider_login import run_oauth_login, validate_and_fetch
from one.core.session_manager import SessionManager, get_default_session_dir
from one.core.settings_manager import SettingsManager
from one.core.subagent_monitor import SubagentMonitor
from one.core.temperature import temperature_for_mode
from one.core.types import ModelInfo
from one.mcp import McpManager
from one.resources.resource_loader import DefaultResourceLoader
from one.tools.index import DEFAULT_TOOL_NAMES, all_tools

# Command replies pass through the facade's 64 KiB DTO limit.  Leave room for
# the command envelope and sidebar rather than allowing 500 individually valid
# 32 KiB messages to make a reload impossible.
# This is deliberately below the facade's 64 KiB command envelope.  The value
# is enforced against the *whole JSON document*, not a convenient sub-list.
_SNAPSHOT_MAX_BYTES = 60 * 1024
_SNAPSHOT_ENTRY_MAX_BYTES = 3 * 1024
_SNAPSHOT_MESSAGES_MAX_BYTES = 36 * 1024
_MODELS_PAGE_SIZE = 100
_EVIDENCE_PAGE_CHARS = 12 * 1024
_EVIDENCE_PAGE_BYTES = 12 * 1024


def _bounded_snapshot_text(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    encoded = text.encode("utf-8")
    if len(encoded) <= _SNAPSHOT_ENTRY_MAX_BYTES:
        return text
    return encoded[:_SNAPSHOT_ENTRY_MAX_BYTES].decode("utf-8", "ignore") + "\n<truncated>"


def _snapshot_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))


def _evidence_display(value: Any, key: str = "") -> Any:
    """Allow only sanitized result values; evidence metadata never enters the UI."""
    blocked = {"args", "status", "token", "secret", "password", "authorization", "apikey", "api_key"}
    if key.lower().replace("-", "_") in blocked:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(name): _evidence_display(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_evidence_display(item) for item in value]
    return value if value is None or isinstance(value, (str, int, float, bool)) else str(value)


def _utf8_page(text: str, offset: int) -> tuple[str, int | None, int]:
    """Page sanitized text by UTF-8 bytes, never splitting a codepoint."""
    raw = text.encode("utf-8")
    if offset > len(raw):
        raise ValueError("invalid evidence offset")
    try:
        # An offset supplied by an external caller must also be a codepoint
        # boundary. Otherwise the old retry loop returned an empty page forever.
        raw[:offset].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("invalid evidence offset") from error
    end = min(offset + _EVIDENCE_PAGE_BYTES, len(raw))
    while end > offset:
        try:
            chunk = raw[offset:end].decode("utf-8")
            return chunk, (end if end < len(raw) else None), len(raw)
        except UnicodeDecodeError:
            end -= 1
    return "", (offset if offset < len(raw) else None), len(raw)


def _safe_startup_projection(value: Any) -> dict[str, Any]:
    """Bound startup diagnostics without carrying loader/MCP implementation data."""
    if not isinstance(value, dict):
        return {"mcpErrors": [], "resourceDiagnostics": {}}
    errors = value.get("mcpErrors")
    diagnostics = value.get("resourceDiagnostics")
    return {
        "mcpErrors": [_bounded_snapshot_text(item)[:512] for item in errors[:8]] if isinstance(errors, list) else [],
        "resourceDiagnostics": {
            str(name)[:128]: [_bounded_snapshot_text(item)[:512] for item in entries[:8]]
            for name, entries in diagnostics.items() if isinstance(entries, list)
        } if isinstance(diagnostics, dict) else {},
    }


def _fit_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Hard-fit a complete snapshot while retaining state/identity and newest UI."""
    if _snapshot_size(snapshot) <= _SNAPSHOT_MAX_BYTES:
        return snapshot
    result = dict(snapshot)
    # Drop optional display projections before transcript entries.  A resync is
    # useful only if it still contains the newest conversation and controls.
    for field in ("startup", "mcp", "monitor"):
        if _snapshot_size(result) <= _SNAPSHOT_MAX_BYTES:
            break
        result[field] = [] if isinstance(result.get(field), list) else {"displayOmitted": True}
    if _snapshot_size(result) > _SNAPSHOT_MAX_BYTES and isinstance(result.get("sidebar"), dict):
        sidebar = result["sidebar"]
        result["sidebar"] = {key: sidebar[key] for key in ("sessionId", "model", "streaming", "compacting") if key in sidebar}
    messages = list(result.get("messages", []))
    # Drop oldest content first: a resync remains useful when it preserves the
    # current/latest transcript rather than an arbitrary front prefix.
    omitted = 0
    while messages and _snapshot_size({**result, "messages": messages}) > _SNAPSHOT_MAX_BYTES:
        messages.pop(0)
        omitted += 1
    if omitted:
        marker = {"role": "custom", "content": f"<{omitted} earlier transcript entries omitted by snapshot budget>"}
        while messages and _snapshot_size({**result, "messages": [marker, *messages]}) > _SNAPSHOT_MAX_BYTES:
            messages.pop(0)
            omitted += 1
            marker["content"] = f"<{omitted} earlier transcript entries omitted by snapshot budget>"
        messages.insert(0, marker)
    result["messages"] = messages
    # Bounded projections above should make this unnecessary, but an explicit
    # degradation path protects future additions and pathological escaped text.
    for field in ("startup", "mcp", "monitor", "sidebar"):
        if _snapshot_size(result) <= _SNAPSHOT_MAX_BYTES:
            break
        value = result.get(field)
        if field == "sidebar" and isinstance(value, dict):
            result[field] = {key: value[key] for key in ("sessionId", "model", "streaming", "compacting") if key in value}
        else:
            result[field] = {"displayOmitted": True} if isinstance(value, dict) else []
    # The envelope itself is always valid and stateful; never throw from a
    # presentation request.  The fallback is only reachable for hostile scalar
    # state introduced by a future field.
    if _snapshot_size(result) > _SNAPSHOT_MAX_BYTES:
        result = {
            "epoch": int(snapshot.get("epoch", 0)), "sidebar": {"displayOmitted": True},
            "messages": [{"role": "custom", "content": "<snapshot display content omitted by budget>"}],
            "models": {"count": 0, "paged": True}, "questions": snapshot.get("questions", [])[:16],
            "approval": snapshot.get("approval"), "snapshotOmitted": True,
        }
    return result


@dataclass(frozen=True, slots=True)
class TuiBootstrap:
    cwd: str
    agent_dir: str
    session_path: str | None
    session_dir: str | None
    provider: str | None
    model: str | None
    api_key: str | None
    thinking: str | None
    temperature: float | None
    temperature_mode: str | None
    tools: tuple[str, ...]
    # An empty ``tools`` tuple normally means the CLI did not constrain tool
    # selection.  Keep --no-tools distinct: it deliberately creates a session
    # with no built-in tools.
    no_tools: bool = False
    no_session: bool = False
    no_mcp: bool = False
    no_subagents: bool = False
    no_bash_output: bool = False
    cooperation: bool = False
    continue_session: bool = False
    resume: bool = False
    fork: str | None = None
    scoped_models: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    prompt_templates: tuple[str, ...] = ()
    themes: tuple[str, ...] = ()
    no_extensions: bool = False
    no_skills: bool = False
    no_prompt_templates: bool = False
    no_themes: bool = False
    system_prompt: str | None = None
    append_system_prompt: str | None = None
    # CLI image paths are intentionally imported by the owner loop when the
    # first TUI prompt is sent.  They must not be imported on Textual's loop.
    initial_image_paths: tuple[str, ...] = ()


class TuiRuntimeHost:
    """Explicit command implementation living entirely on the runtime loop."""

    # The facade must not attach a second direct monitor listener: this host
    # forwards identity-only invalidations via session events and owns rebinding.
    owns_monitor_events = True

    def __init__(
        self,
        host: AgentSessionRuntimeHost,
        mcp: McpManager | None,
        startup: dict[str, Any] | None = None,
    ) -> None:
        self._host = host
        self._mcp = mcp
        self._startup = startup or {"mcpErrors": [], "resourceDiagnostics": {}}
        self._epoch = 0
        self._approvals: deque[dict[str, Any]] = deque()
        self._approval_active: dict[str, Any] | None = None
        self._approval_counter = 0
        self._initial_image_paths: tuple[str, ...] = ()
        self._disposed = False
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._off_session: Any = None
        self._off_monitor: Any = None
        self._bind_session_events()
        self._bind_monitor()

    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """Subscribe to detached current-session events without exposing it."""
        self._listeners.append(listener)

        def off() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return off

    def _bind_session_events(self) -> None:
        """Bridge only the current session, rejecting callbacks in flight at a rebind."""
        if callable(self._off_session):
            self._off_session()
        self._off_session = None
        if self._disposed:
            return
        session = self._session()
        if session is None:
            return
        epoch = self._epoch
        subscribe = getattr(session, "subscribe", None)
        if not callable(subscribe):
            return
        source_session_id = str(getattr(session, "session_id", "") or "")

        def forwarded(event: dict[str, Any]) -> None:
            if self._epoch != epoch or self._session() is not session:
                return
            stamped = dict(event)
            # Keep event sessionId (for child/monitor identities) intact while
            # attaching the authoritative owner-session identity separately.
            stamped["epoch"] = epoch
            stamped["sourceSessionId"] = source_session_id
            for listener in tuple(self._listeners):
                listener(stamped)

        self._off_session = subscribe(forwarded)

    def _bind_monitor(self) -> None:
        """Forward monitor invalidations from the current owner session."""
        if callable(self._off_monitor):
            self._off_monitor()
        self._off_monitor = None
        if self._disposed:
            return
        session = self._session()
        if session is None:
            return
        epoch = self._epoch
        monitor = getattr(session, "subagent_monitor", None)
        subscribe = getattr(monitor, "subscribe", None)
        if not callable(subscribe):
            return

        def invalidated(notice: dict[str, Any]) -> None:
            # An old monitor callback may already be in flight during a switch.
            # Never stamp it with the new epoch when the facade publishes it.
            if self._epoch != epoch or self._session() is not session:
                return
            session._emit({
                "type": "subagent_monitor_invalidated",
                "sessionId": notice.get("sessionId"),
                "revision": notice.get("revision"),
                "changeType": notice.get("type"),
            })

        self._off_monitor = subscribe(invalidated)

    @staticmethod
    def _json(value: Any, *, maximum: int = 48 * 1024) -> Any:
        """Return a detached, bounded JSON DTO for command replies."""
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > maximum:
            raise RuntimeError("TUI command reply exceeded its transport budget")
        return json.loads(encoded)

    def _session(self) -> Any:
        return self._host.session

    async def dispatch(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        """The complete TUI command grammar; no reflective dispatch exists here."""
        if command == "snapshot" and not payload:
            return await self.snapshot()
        if command == "prompt" and set(payload) == {"text"}:
            return await self.prompt(payload["text"])
        if command == "abort" and not payload:
            return await self.abort()
        if command == "models_page" and set(payload) == {"offset", "limit"}:
            return self.models_page(payload["offset"], payload["limit"])
        if command == "evidence_page" and set(payload) == {"evidence_id", "offset", "epoch"}:
            return self.evidence_page(payload["evidence_id"], payload["offset"], payload["epoch"])
        if command == "answer_question" and set(payload) == {"question_id", "answer", "epoch"}:
            return await self.answer_question(**payload)
        if command == "approval_resolve" and set(payload) == {"approval_id", "approved", "reason", "epoch"}:
            return await self.approval_resolve(**payload)
        if command == "switch_session" and set(payload) == {"path", "id"}:
            return await self.switch_session(payload["path"], payload["id"])
        if command == "new_session" and not payload:
            return await self.new_session()
        if command == "session_list" and not payload:
            return await self.session_list()
        if command == "ui" and set(payload) == {"operation", "payload"} and isinstance(payload["operation"], str):
            return await self.ui_operation(payload["operation"], payload["payload"])
        raise ValueError(f"invalid TUI runtime command payload: {command}")

    def _mcp_snapshot(self) -> list[dict[str, Any]]:
        """Return the sole compact, schema-free MCP display DTO."""
        mcp = getattr(self, "_mcp", None)
        mcp_servers = []
        for item in (mcp.server_status() if mcp is not None else []):
            if not isinstance(item, dict):
                continue
            runtime_state = str(item.get("runtimeState") or "configured").lower()
            enabled = bool(item.get("enabled"))
            running = bool(item.get("running"))
            error = item.get("error")
            mcp_servers.append({
                "name": _bounded_snapshot_text(item.get("name"))[:256],
                "enabled": enabled,
                "running": running,
                "transport": _bounded_snapshot_text(item.get("transport"))[:64],
                "runtimeState": runtime_state,
                "available": enabled and running and not error and runtime_state not in {"failed", "retrying", "disabled"},
                "toolCount": len(item.get("tools") or []),
                "error": _bounded_snapshot_text(error)[:512] if isinstance(error, str) else "",
            })
        return mcp_servers

    def _sidebar(self) -> dict[str, Any]:
        session = self._session()
        model = session.model
        usage = session.get_context_usage() or {}
        queues = session.get_pending_queues() or {}
        stats = session.get_session_stats() or {}
        tokens = stats.get("tokens") or {}
        return {
            "sessionId": str(session.session_id),
            "cwd": str(session.session_manager.cwd),
            "model": f"{model.provider}/{model.id}" if model else "none",
            "thinking": str(session.thinking_level),
            "temperature": float(session.temperature),
            "streaming": bool(session.is_streaming),
            "compacting": bool(getattr(session, "is_compacting", False)),
            "bashOutput": bool(session.settings_manager.get_bash_show_output()),
            "theme": str(session.settings_manager.get_theme()),
            "infoPanel": str(getattr(session.settings_manager, "get_tui_info_panel", lambda: "top")()),
            "cooperation": bool(session.approval_callback is not None),
            "coop": "on" if session.approval_callback is not None else "off",
            "temperatureMode": str(getattr(session, "temperature_mode", "balanced")),
            "retry": str(getattr(session.settings_manager, "get_retry_mode", lambda: "on")()),
            "subagents": not bool(getattr(session, "subagents_hard_disabled", False))
            and bool(session.settings_manager.get_subagents_enabled()),
            "contextPercent": float(usage.get("percent") or 0),
            "queueSteer": len(queues.get("steering") or []),
            "queueFollow": len(queues.get("followUp") or []),
            "tokenInput": int(tokens.get("input") or 0),
            "tokenOutput": int(tokens.get("output") or 0),
            "tokenTotal": int(tokens.get("total") or 0),
            "cost": float(stats.get("cost") or 0),
            "sessionDir": str(session.session_manager.session_dir or ""),
            "sessionFile": str(getattr(session, "session_file", "") or ""),
            "storageDir": str(getattr(session, "_storage_dir", "") or ""),
            "steeringMode": str(getattr(session, "steering_mode", "queue")),
            "followUpMode": str(getattr(session, "follow_up_mode", "queue")),
            "activeTools": list(getattr(session, "active_tools", []) or []),
            "pendingMessageCount": int(getattr(session, "pending_message_count", 0)),
            "contextKnown": isinstance(usage.get("percent"), (int, float)),
            "deliveryMode": (
                "followUp" if getattr(session, "follow_up_mode", "queue") == "follow_up" else "steer"
            ) if session.is_streaming else "idle",
            "mcpEnabled": self._mcp is not None,
            "toolResultPreviewLines": session.settings_manager.get_tui_tool_result_preview_lines(),
        }

    def _snapshot(self) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        omitted = 0
        # Keep the existing 500 logical-entry viewport while enforcing a total
        # transport budget.  This is an initial/resync projection only; normal
        # streaming must arrive as incremental events.
        source = self._session().messages[-500:]
        used = 0
        # Prefer the latest history when the bounded resync cannot include all
        # of the 500-entry viewport.
        for message in reversed(source):
            role = message.get("role")
            content = message.get("content", "")
            if role not in {"user", "assistant", "toolResult", "custom", "bashExecution"}:
                continue
            if isinstance(content, list):
                content = "".join(
                    str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text"
                )
            original_content = content
            # Extract durable identity before display clipping, otherwise a
            # large JSON tool result loses its [view] action after reload.
            tool_result: dict[str, Any] = {}
            if role == "toolResult":
                try:
                    tool_result = json.loads(str(original_content))
                except (TypeError, ValueError):
                    pass
            encoded = str(content).encode("utf-8")
            if len(encoded) > _SNAPSHOT_ENTRY_MAX_BYTES:
                content = encoded[:_SNAPSHOT_ENTRY_MAX_BYTES].decode("utf-8", "ignore") + "\n<truncated>"
            row: dict[str, Any] = {"role": str(role), "content": str(content)}
            # Durable tool results carry a safe evidence identity.  Do not copy
            # raw result objects, args, schemas, or sidecar contents into a
            # snapshot just to make a transcript link.
            if role == "toolResult":
                if isinstance(tool_result, dict):
                    evidence_id = tool_result.get("evidenceId")
                    if isinstance(evidence_id, str) and evidence_id:
                        row["evidenceId"] = evidence_id[:256]
                    if tool_result.get("evidenceUnavailable"):
                        row["evidenceUnavailable"] = True
            # These fields are display-only and let a restored transcript render
            # local bash history without reading output files or running code.
            if role == "bashExecution":
                row.update({
                    "command": _bounded_snapshot_text(message.get("command")),
                    "output": _bounded_snapshot_text(message.get("output")),
                    "exitCode": message.get("exitCode") if isinstance(message.get("exitCode"), int) else None,
                    "timedOut": bool(message.get("timedOut")),
                    "cancelled": bool(message.get("cancelled")),
                    "truncated": bool(message.get("truncated")),
                })
            elif role == "assistant" and message.get("stopReason") == "abort":
                row["stopReason"] = "abort"
            row_size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if used + row_size > _SNAPSHOT_MESSAGES_MAX_BYTES:
                omitted += 1
                continue
            messages.append(row)
            used += row_size
        messages.reverse()
        if omitted:
            messages.append(
                {"role": "custom", "content": f"<{omitted} transcript entries omitted; live updates continue>"}
            )
        # The model catalogue can be enormous.  It has its own strict,
        # paginated command; a snapshot only says that a catalogue exists.
        registry = getattr(self._session(), "model_registry", None)
        model_count = len(registry.get_available()) if registry is not None else 0
        monitor = getattr(self._session(), "subagent_monitor", None)
        raw_monitor = monitor.snapshot() if monitor is not None else {"revision": 0, "children": []}
        monitor_snapshot = {
            "revision": int(raw_monitor.get("revision", 0)) if isinstance(raw_monitor, dict) else 0,
            "children": [
                {
                    **{key: _bounded_snapshot_text(child.get(key))[:512] if isinstance(child.get(key), str) else child.get(key)
                       for key in ("sessionId", "task", "model", "status", "activity", "currentTool") if key in child},
                    "outputTail": _bounded_snapshot_text(child.get("outputTail"))[:2048],
                    "timeline": [
                        {key: _bounded_snapshot_text(item.get(key))[:1024] if isinstance(item.get(key), str) else item.get(key)
                         for key in ("kind", "text", "tool", "status", "args", "error", "elapsedMs") if key in item}
                        for item in child.get("timeline", [])[-16:] if isinstance(item, dict)
                    ],
                }
                for child in (raw_monitor.get("children", [])[:20] if isinstance(raw_monitor, dict) and isinstance(raw_monitor.get("children"), list) else [])
                if isinstance(child, dict)
            ],
        }
        questions = [
            {"id": str(item.get("id", ""))[:256], "question": _bounded_snapshot_text(item.get("question"))[:1024], "epoch": self._epoch}
            for item in getattr(self._session(), "get_pending_questions", lambda: [])()[:16]
            if isinstance(item, dict) and item.get("id")
        ]
        approval = {
            key: (_bounded_snapshot_text(value)[:_SNAPSHOT_ENTRY_MAX_BYTES] if isinstance(value, str) else value)
            for key, value in (getattr(self, "_approval_active", None) or {}).items()
            if key in {"id", "tool", "origin", "displayArgs"}
        } if getattr(self, "_approval_active", None) else None
        if approval is not None:
            approval["epoch"] = self._epoch
        sidebar = self._sidebar()
        mcp_status = self._mcp_snapshot()
        snapshot = {
            "epoch": self._epoch,
            "sidebar": sidebar,
            "messages": messages,
            "models": {"count": model_count, "paged": True},
            "monitor": monitor_snapshot,
            "mcp": mcp_status,
            "questions": questions,
            "approval": approval,
            "startup": _safe_startup_projection(getattr(self, "_startup", {})),
        }
        return _fit_snapshot(snapshot)

    async def snapshot(self) -> dict[str, Any]:
        return self._snapshot()

    async def prompt(self, text: str) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("prompt text is required")
        images = None
        if self._initial_image_paths:
            from one.core.attachments import AttachmentInput, count_attachments, import_image

            paths = self._initial_image_paths
            # Consume only after successful import, so an invalid attachment is
            # reported rather than silently disappearing from the next prompt.
            count_attachments([AttachmentInput(path=path) for path in paths])
            storage_dir = str(getattr(self._session(), "_storage_dir", "") or "")
            images = [import_image(storage_dir, path).__dict__ for path in paths]
            self._initial_image_paths = ()
        await self._session().prompt(text, images=images)
        return self._snapshot()

    async def abort(self) -> dict[str, Any]:
        await self._session().abort()
        # Abort acknowledgement is a control-plane response.  It must never
        # depend on transcript presentation succeeding.
        return {"epoch": self._epoch, "accepted": True, "aborted": True}

    def models_page(self, offset: Any, limit: Any) -> dict[str, Any]:
        if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= _MODELS_PAGE_SIZE:
            raise ValueError("invalid model page")
        registry = getattr(self._session(), "model_registry", None)
        available = registry.get_available() if registry is not None else []
        page = available[offset : offset + limit]
        return self._json({"epoch": self._epoch, "offset": offset, "total": len(available), "models": [
            {"provider": str(item.provider), "id": str(item.id), "reasoning": bool(item.reasoning), "contextWindow": int(item.context_window or 0)}
            for item in page
        ], "nextOffset": offset + len(page) if offset + len(page) < len(available) else None})

    def evidence_page(self, evidence_id: Any, offset: Any, epoch: Any) -> dict[str, Any]:
        if epoch != self._epoch or not isinstance(evidence_id, str) or not evidence_id or not isinstance(offset, int) or offset < 0:
            raise ValueError("stale or invalid evidence request")
        evidence = self._session().session_manager.read_evidence(evidence_id)
        if not isinstance(evidence, dict):
            return {"epoch": self._epoch, "available": False, "error": "Full result unavailable."}
        raw = _evidence_display(evidence.get("rawResult"))
        text = SubagentMonitor._safe_text(json.dumps(raw, ensure_ascii=False, separators=(",", ":")))
        chunk, next_offset, total = _utf8_page(text, offset)
        return self._json({"epoch": self._epoch, "available": True, "text": chunk, "offset": offset,
                           "nextOffset": next_offset, "totalChars": total})

    async def answer_question(self, question_id: str, answer: str, epoch: int) -> dict[str, Any]:
        if epoch != self._epoch or not question_id or not isinstance(answer, str):
            raise ValueError("stale or invalid question response")
        self._session().answer_question(question_id, answer)
        snapshot = self._snapshot()
        snapshot["accepted"] = True
        return snapshot

    async def approval_resolve(self, approval_id: str, approved: bool, reason: str, epoch: int) -> dict[str, Any]:
        if epoch != self._epoch or self._approval_active is None or approval_id != self._approval_active["id"]:
            raise ValueError("stale or invalid approval response")
        pending = self._approval_active
        self._approval_active = None
        pending["future"].set_result((bool(approved), "" if approved else (reason.strip() or "Rejected by user")))
        self._publish_next_approval()
        snapshot = self._snapshot()
        snapshot["accepted"] = True
        return snapshot

    async def switch_session(self, path: str, session_id: str) -> dict[str, Any]:
        if not isinstance(path, str) or not path or not isinstance(session_id, str) or not session_id:
            raise ValueError("session path is required")
        manager = self._session().session_manager
        if not any(
            item.path == path and item.id == session_id
            for item in SessionManager.list(manager.cwd, manager.session_dir)
        ):
            raise ValueError("Session from the last /sessions list is no longer available. Run /sessions again.")
        self._close_waits("Session changed")
        await self._host.switch_session(path)
        self._epoch += 1
        self._bind_session_events()
        self._bind_monitor()
        return self._snapshot()

    async def new_session(self) -> dict[str, Any]:
        self._close_waits("Session changed")
        await self._host.new_session({})
        self._epoch += 1
        self._bind_session_events()
        self._bind_monitor()
        return self._snapshot()

    async def session_list(self) -> dict[str, Any]:
        manager = self._session().session_manager
        return {
            "sessions": [
                {
                    "id": x.id,
                    "name": x.name,
                    "path": x.path,
                    "cwd": x.cwd,
                    "modified": x.modified.isoformat(),
                    "messageCount": x.message_count,
                }
                for x in SessionManager.list(manager.cwd, manager.session_dir)
            ]
        }

    async def ui_operation(self, operation: str, payload: Any) -> dict[str, Any]:
        """Explicit, UI-facing mutations.  Keep this list auditable; no reflection."""
        if not isinstance(payload, dict):
            raise ValueError("invalid TUI operation payload")
        schemas: dict[str, set[str]] = {
            "set_theme": {"theme"},
            "set_thinking": {"level"},
            "cycle_thinking": set(),
            "set_temperature": {"value"},
            "adjust_temperature": {"delta"},
            "set_retry": {"mode"},
            "set_subagents": {"enabled"},
            "set_bash_output": {"enabled"},
            "set_model": {"provider", "model"},
            "set_cooperation": {"enabled"},
            "cycle_model": set(),
            "steer": {"text"},
            "follow": {"text"},
            "compact": {"instructions"},
            "reload": set(),
            "invoke_skill": {"name", "args"},
            "clear_queues": {"target"},
            "answer_extension": {"id", "payload", "cancelled"},
            "tree": set(),
            "navigate": {"entryId", "summarize", "customInstructions"},
            "fork": {"entryId"},
            "delete_session": {"path"},
            "rename_session": {"path", "name"},
            "status": set(),
            "stats": set(),
            "state": set(),
            "tools": set(),
            "queues": set(),
            "inspect_timeout": set(),
            "skills": set(),
            "execute_bash": {"command"},
            "extension_state": set(),
            "extension_clear": set(),
            "extension_request": {"extension", "uiType", "payload"},
            "auth_status": {"provider"},
            "login": {"provider", "apiKey", "model"},
            "login_refresh": {"provider"},
            "oauth_login": {"provider"},
            "logout": {"provider"},
            "mcp_list": set(),
            "mcp_enable": {"name"},
            "mcp_disable": {"name"},
            "config_get": {"key"},
            "config_set": {"key", "value"},
            "cooperation_status": set(),
        }
        if operation not in schemas or set(payload) != schemas[operation]:
            raise ValueError(f"invalid TUI operation payload: {operation}")
        string_fields = {
            "set_theme": {"theme"},
            "set_thinking": {"level"},
            "set_retry": {"mode"},
            "steer": {"text"},
            "follow": {"text"},
            "invoke_skill": {"name", "args"},
            "fork": {"entryId"},
            "delete_session": {"path"},
            "rename_session": {"path", "name"},
            "navigate": {"entryId"},
        }
        if operation in string_fields:
            if not all(isinstance(payload[field], str) for field in string_fields[operation]):
                raise ValueError(f"invalid TUI operation payload: {operation}")
        if operation in {"set_subagents", "set_bash_output", "set_cooperation", "navigate"} and any(
            not isinstance(payload[key], bool)
            for key in (
                {
                    "set_subagents": {"enabled"},
                    "set_bash_output": {"enabled"},
                    "set_cooperation": {"enabled"},
                    "navigate": {"summarize"},
                }.get(operation, set())
            )
        ):
            raise ValueError(f"invalid TUI operation payload: {operation}")
        session = self._session()
        if operation == "status":
            return self._json(self._snapshot()["sidebar"])
        if operation == "stats":
            return self._json(session.get_session_stats())
        if operation == "state":
            sidebar = self._sidebar()
            return self._json(
                {
                    key: sidebar[key]
                    for key in (
                        "model",
                        "thinking",
                        "streaming",
                        "pendingMessageCount",
                        "queueSteer",
                        "queueFollow",
                        "activeTools",
                        "sessionId",
                        "sessionFile",
                    )
                }
            )
        if operation == "tools":
            return {"tools": list(getattr(session, "active_tools", []) or [])}
        if operation == "queues":
            return self._json(session.get_pending_queues() or {})
        if operation == "inspect_timeout":
            return self._json(getattr(session, "inspect_subagent_timeout", lambda: None)() or {})
        if operation == "skills":
            return self._json(session.resource_loader.get_skills())
        if operation == "execute_bash":
            return self._json(await session.execute_tui_command(str(payload["command"])))
        if operation == "extension_state":
            return self._json(session.get_extension_ui_state())
        if operation == "extension_clear":
            session.clear_extension_ui_history()
            return {"cleared": True}
        if operation == "extension_request":
            return self._json(
                session.request_extension_ui(
                    extension=str(payload["extension"]),
                    ui_type=str(payload["uiType"]),
                    payload=payload["payload"],
                )
            )
        if operation == "auth_status":
            provider = str(payload["provider"])
            registry = session.model_registry
            if provider:
                return self._json(registry.get_provider_auth_status(provider))
            return self._json({"providers": [registry.get_provider_auth_status(item) for item in registry.providers()]})
        if operation == "login_refresh":
            return await self._login_refresh(str(payload["provider"]))
        if operation == "login":
            return await self._login(str(payload["provider"]), str(payload["apiKey"]), str(payload["model"]))
        if operation == "oauth_login":
            return await self._oauth_login(str(payload["provider"]))
        if operation == "logout":
            session.model_registry.remove_provider_credentials(str(payload["provider"]))
            return {"removed": True}
        if operation == "mcp_list":
            return self._json(
                {"available": self._mcp is not None, "servers": self._mcp.server_status() if self._mcp else []}
            )
        if operation == "mcp_enable":
            return await self._mcp_enable(str(payload["name"]))
        if operation == "mcp_disable":
            return await self._mcp_disable(str(payload["name"]))
        if operation == "config_get":
            key = str(payload["key"])
            current: Any = session.settings_manager.merged()
            for part in key.split("."):
                current = current.get(part) if isinstance(current, dict) else None
            return self._json({"key": key, "value": current})
        if operation == "config_set":
            session.settings_manager.set_config_value(str(payload["key"]), payload["value"])
            # Return the authoritative detached sidebar so TUI-only settings
            # take effect immediately without holding a UI-side settings copy.
            result = self._snapshot()
            result["updated"] = str(payload["key"])
            return result
        if operation == "cooperation_status":
            return {
                "enabled": session.approval_callback is not None,
                "tools": sorted(getattr(session, "_approval_tools", [])),
            }
        if operation == "set_theme":
            session.settings_manager.set_theme(str(payload["theme"]))
        elif operation == "set_thinking":
            session.set_thinking_level(str(payload["level"]))
        elif operation == "cycle_thinking":
            session.cycle_thinking_level()
        elif operation == "set_temperature":
            session.set_temperature(float(payload["value"]))
        elif operation == "adjust_temperature":
            session.adjust_temperature(float(payload["delta"]))
        elif operation == "set_retry":
            mode = str(payload["mode"])
            session.settings_manager.set_retry_mode(mode)
            session.set_auto_retry_enabled(mode in ("on", "unlimited"))
        elif operation == "set_subagents":
            session.settings_manager.set_subagents_enabled(bool(payload["enabled"]))
        elif operation == "set_cooperation":
            enabled = bool(payload["enabled"])
            session.settings_manager.set_tool_approval(enabled)
            session.approval_callback = self._approval_callback if enabled else None
            if not enabled:
                self._close_waits("Cooperation disabled")
        elif operation == "set_bash_output":
            session.settings_manager.set_bash_show_output(bool(payload["enabled"]))
        elif operation == "set_model":
            model = session.model_registry.resolve(str(payload["provider"]), str(payload["model"]), allow_dynamic=True)
            if model is None:
                raise ValueError("model not found")
            await session.set_model(model)
            session.settings_manager.set_default_provider(model.provider)
            session.settings_manager.set_default_model(model.id)
        elif operation == "cycle_model":
            model = await session.cycle_model()
            if model is None:
                return {"cycled": False, "snapshot": self._snapshot()}
            session.settings_manager.set_default_provider(model.model.provider)
            session.settings_manager.set_default_model(model.model.id)
        elif operation == "steer":
            await session.steer(str(payload["text"]))
        elif operation == "follow":
            await session.follow_up(str(payload["text"]))
        elif operation == "compact":
            return await session.compact(payload.get("instructions"))
        elif operation == "reload":
            return await session.reload()
        elif operation == "invoke_skill":
            return await session.invoke_skill(str(payload["name"]), str(payload.get("args", "")))
        elif operation == "clear_queues":
            return session.clear_pending_queues(str(payload.get("target", "all")))
        elif operation == "answer_extension":
            session.respond_extension_ui(
                request_id=str(payload["id"]), payload=payload.get("payload"), cancelled=bool(payload.get("cancelled"))
            )
        elif operation == "tree":
            return {"tree": session.session_manager.get_tree(), "leafId": session.session_manager.get_leaf_id()}
        elif operation == "navigate":
            result = await session.navigate_tree(
                str(payload["entryId"]),
                {"summarize": bool(payload["summarize"]), "customInstructions": payload["customInstructions"]},
            )
            if self._session() is not session:
                self._epoch += 1
                self._bind_session_events()
                self._bind_monitor()
            return result
        elif operation == "fork":
            previous_session = session
            result = await self._host.fork(str(payload["entryId"]))
            if self._session() is not previous_session:
                self._epoch += 1
                self._bind_session_events()
                self._bind_monitor()
            return self._snapshot() if result is not None else self._snapshot()
        elif operation == "delete_session":
            SessionManager.delete(str(payload["path"]), session.session_manager.session_dir)
        elif operation == "rename_session":
            target = str(payload["path"])
            name = str(payload["name"])
            (
                session.session_manager
                if target == session.session_manager.path
                else SessionManager.open(target, session.session_manager.session_dir)
            ).set_session_name(name)
        else:
            raise ValueError(f"unsupported TUI operation: {operation}")
        return await self.snapshot()

    async def _login_refresh(self, provider: str) -> dict[str, Any]:
        session = self._session()
        adapter = getattr(session, "providers", {}).get(provider)
        if adapter is None:
            raise ValueError(f"Provider adapter not found for {provider}.")
        key_info = session.model_registry.get_api_key_and_headers(ModelInfo(provider=provider, id=""))
        if not key_info.get("ok"):
            raise ValueError(str(key_info.get("error") or f"No API key for {provider}."))
        ok, error, fetched = await validate_and_fetch(
            adapter, key_info.get("apiKey", ""), provider, headers=key_info.get("headers") or None
        )
        fetched = fetched or []
        if not ok:
            raise ValueError(f"Authorization failed for {provider}: {error}")
        added = session.model_registry.register_models(provider, fetched) if fetched else 0
        if fetched:
            session.model_registry.persist_models(provider, fetched)
        return self._json(
            {
                "ok": True,
                "warning": error or "",
                "models": [fetched_entry_id(item) for item in fetched[:20]],
                "count": len(fetched),
                "added": added,
            }
        )

    async def _login(self, provider: str, api_key: str, model: str) -> dict[str, Any]:
        if not provider:
            raise ValueError("provider is required")
        session = self._session()
        registry = session.model_registry
        adapter = getattr(session, "providers", {}).get(provider)
        fetched: list[Any] = []
        warning = ""
        if adapter is not None and api_key:
            ok, warning, fetched_result = await validate_and_fetch(adapter, api_key, provider, model or None)
            fetched = list(fetched_result or [])
            if not ok:
                raise ValueError(f"Authorization failed for {provider}: {warning}")
        if api_key:
            registry.set_stored_api_key(provider, api_key)
        if fetched:
            registry.register_models(provider, fetched)
            registry.persist_models(provider, fetched)
        session.settings_manager.set_default_provider(provider)
        if model:
            selected = registry.resolve(provider, model, allow_dynamic=True)
            if selected is None:
                raise ValueError(f"Model not found for {provider}: {model}")
            session.settings_manager.set_default_model(selected.id)
            await session.set_model(selected)
        return self._json(
            {
                "ok": True,
                "warning": warning,
                "count": len(fetched),
                "models": [fetched_entry_id(item) for item in fetched[:20]],
            }
        )

    async def _oauth_login(self, provider: str) -> dict[str, Any]:
        session = self._session()
        adapter = getattr(session, "providers", {}).get(provider)
        if adapter is None:
            raise ValueError(f"Provider adapter not found for {provider}.")
        try:
            ok, error, fetched = await run_oauth_login(provider, adapter, session.model_registry)
        except OAuthError as exc:
            raise ValueError(f"Subscription login failed: {exc}") from exc
        if not ok:
            raise ValueError(f"Subscription login failed: {error}")
        fetched = fetched or []
        session.settings_manager.set_default_provider(provider)
        return self._json(
            {
                "ok": True,
                "warning": error or "",
                "count": len(fetched),
                "models": [fetched_entry_id(item) for item in fetched[:20]],
            }
        )

    async def _mcp_enable(self, name: str) -> dict[str, Any]:
        if self._mcp is None:
            raise ValueError("MCP not available (started with --no-mcp).")
        session = self._session()
        config = session.settings_manager.get_mcp_servers().get(name)
        if not config or (not config.get("command") and not config.get("url")):
            raise ValueError(f"No MCP config for '{name}'.")
        added = await self._mcp.enable_server(
            name,
            str(config.get("command", "")),
            config.get("args") or [],
            config.get("env") or {},
            url=config.get("url"),
        )
        session.settings_manager.set_mcp_server_enabled(name, True)
        session.sync_mcp_tools()
        return self._json({"enabled": True, "name": name, "tools": list(added or [])})

    async def _mcp_disable(self, name: str) -> dict[str, Any]:
        if self._mcp is None:
            raise ValueError("MCP not available (started with --no-mcp).")
        session = self._session()
        known = any(item.get("name") == name for item in self._mcp.server_status())
        removed = await self._mcp.disable_server(name)
        if known:
            session.settings_manager.set_mcp_server_enabled(name, False)
            session.sync_mcp_tools()
        return self._json({"disabled": True, "known": known, "name": name, "tools": list(removed or [])})

    async def _approval_callback(self, tool_name: str, args: dict[str, Any]) -> tuple[bool, str]:
        self._approval_counter += 1
        future: asyncio.Future[tuple[bool, str]] = asyncio.get_running_loop().create_future()
        origin = approval_callback_origin.get()
        origin_id = str(getattr(origin, "session_id", "") or "main")
        pending = {
            "id": str(self._approval_counter),
            "future": future,
            "tool": tool_name,
            "origin": origin_id,
            "displayArgs": SubagentMonitor.display_tool_args(args),
        }
        self._approvals.append(pending)
        if self._approval_active is None:
            self._publish_next_approval()
        return await future

    def _publish_next_approval(self) -> None:
        if self._approval_active is not None or not self._approvals:
            return
        self._approval_active = self._approvals.popleft()
        pending = self._approval_active
        self._session()._emit(
            {
                "type": "tui_approval_request",
                "approvalId": pending["id"],
                "tool": pending.get("tool", "tool"),
                "origin": pending.get("origin", "main"),
                "displayArgs": pending.get("displayArgs", ""),
            }
        )

    def _close_waits(self, reason: str) -> None:
        pending = list(self._approvals)
        if self._approval_active:
            pending.append(self._approval_active)
        self._approvals.clear()
        self._approval_active = None
        for item in pending:
            if not item["future"].done():
                item["future"].set_result((False, reason))

    async def dispose(self) -> None:
        self._disposed = True
        self._close_waits("TUI closed")
        if callable(self._off_session):
            self._off_session()
            self._off_session = None
        self._listeners.clear()
        if callable(self._off_monitor):
            self._off_monitor()
            self._off_monitor = None
        failure: BaseException | None = None
        try:
            await self._host.dispose()
        except BaseException as exc:
            failure = exc
        if self._mcp is not None:
            try:
                await self._mcp.close()
            except BaseException:
                if failure is None:
                    raise
        if failure is not None:
            raise failure


def _session_dir_with_fallback(bootstrap: TuiBootstrap, settings: SettingsManager) -> str:
    """Mirror the CLI's writable configured-session-directory check."""
    configured = bootstrap.session_dir or settings.get_session_dir()
    if not configured:
        return get_default_session_dir(bootstrap.cwd, bootstrap.agent_dir)
    try:
        path = Path(configured)
        ensure_private_dir(path)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return str(path)
    except OSError:
        return get_default_session_dir(bootstrap.cwd, bootstrap.agent_dir)


def _safe_startup_data(mcp: McpManager | None, loader: DefaultResourceLoader) -> dict[str, Any]:
    """Copy bounded startup diagnostics for the UI; never print from its worker."""

    def strings(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        return [str(value)[:4096] for value in values[:50]]

    diagnostics: dict[str, list[str]] = {}
    for name, getter_name in (
        ("skills", "get_skills"),
        ("extensions", "get_extensions"),
        ("prompts", "get_prompts"),
        ("themes", "get_themes"),
    ):
        getter = getattr(loader, getter_name, None)
        if callable(getter):
            result = getter()
            if isinstance(result, dict):
                values = strings(result.get("diagnostics"))
                if values:
                    diagnostics[name] = values
    return {
        "mcpErrors": strings(mcp.errors() if mcp is not None else []),
        "resourceDiagnostics": diagnostics,
    }


async def _close_if_possible(value: Any) -> None:
    """Best-effort construction rollback for objects owned by this worker."""
    for name in ("dispose", "close"):
        method = getattr(value, name, None)
        if callable(method):
            result = method()
            if inspect.isawaitable(result):
                await result
            return


async def create_tui_runtime(bootstrap: TuiBootstrap) -> TuiRuntimeHost:
    """Create all TUI resources on the worker loop, never on Textual's loop."""
    settings = SettingsManager.create(bootstrap.cwd, bootstrap.agent_dir)
    if bootstrap.no_bash_output:
        settings.set_bash_show_output(False, persist=False)
    auth = AuthStorage.create(str(Path(bootstrap.agent_dir) / "auth.json"))
    registry = ModelRegistry.create(auth, str(Path(bootstrap.agent_dir) / "models.json"))
    provider = bootstrap.provider or settings.get_default_provider() or "openai"
    if bootstrap.api_key:
        api_key_provider = provider
        if bootstrap.model and "/" in bootstrap.model and not bootstrap.provider:
            api_key_provider = bootstrap.model.split("/", 1)[0]
        auth.set_runtime_api_key(api_key_provider, bootstrap.api_key)
    loader = DefaultResourceLoader(
        cwd=bootstrap.cwd,
        agent_dir=bootstrap.agent_dir,
        settings_manager=settings,
        additional_extension_paths=list(bootstrap.extensions),
        additional_skill_paths=list(bootstrap.skills),
        additional_prompt_template_paths=list(bootstrap.prompt_templates),
        additional_theme_paths=list(bootstrap.themes),
        no_extensions=bootstrap.no_extensions,
        no_skills=bootstrap.no_skills,
        no_prompt_templates=bootstrap.no_prompt_templates,
        no_themes=bootstrap.no_themes,
        system_prompt=bootstrap.system_prompt,
        append_system_prompt=bootstrap.append_system_prompt,
    )
    mcp = None if bootstrap.no_mcp else McpManager.create(settings)
    runtime = None
    try:
        # This is deliberately before MCP/session construction, matching the
        # CLI startup ordering and keeping worker diagnostics available below.
        await loader.reload()
        if mcp is not None:
            await mcp.start()
        session_dir = _session_dir_with_fallback(bootstrap, settings)
        if bootstrap.no_session:
            manager = SessionManager.in_memory(bootstrap.cwd)
        elif bootstrap.session_path:
            manager = SessionManager.open(bootstrap.session_path, session_dir)
        elif bootstrap.continue_session or bootstrap.resume:
            manager = SessionManager.continue_recent(bootstrap.cwd, session_dir)
        elif bootstrap.fork:
            source = bootstrap.fork
            if "/" not in source and "\\" not in source and not source.endswith(".jsonl"):
                matches = [
                    item for item in SessionManager.list(bootstrap.cwd, session_dir) if item.id.startswith(source)
                ]
                if not matches:
                    matches = [item for item in SessionManager.list_all() if item.id.startswith(source)]
                if not matches:
                    raise ValueError(f"No session found matching '{bootstrap.fork}'")
                source = matches[0].path
            manager = SessionManager.fork_from(source, bootstrap.cwd, session_dir)
        else:
            manager = SessionManager.create(bootstrap.cwd, session_dir)
        model = None
        if bootstrap.model:
            selected_provider, selected_model = provider, bootstrap.model
            if "/" in selected_model and not bootstrap.provider:
                selected_provider, selected_model = selected_model.split("/", 1)
            model = registry.resolve(selected_provider, selected_model, allow_dynamic=True)
        if model is None:
            model = registry.select_default(settings.get_default_provider(), settings.get_default_model())
        scoped_models: list[dict[str, Any]] = []
        for raw in bootstrap.scoped_models:
            model_name, separator, thinking = raw.partition(":")
            if "/" in model_name:
                scoped_provider, scoped_id = model_name.split("/", 1)
                scoped = registry.find(scoped_provider, scoped_id)
            else:
                scoped = next((item for item in registry.all() if model_name.lower() in item.id.lower()), None)
            if scoped is not None:
                scoped_models.append({"model": scoped, "thinkingLevel": thinking if separator else None})
        tool_names = list(bootstrap.tools) if (bootstrap.no_tools or bootstrap.tools) else list(DEFAULT_TOOL_NAMES)
        if bootstrap.no_subagents:
            tool_names = [name for name in tool_names if name != "spawn_subagent"]
        bad_tools = [name for name in tool_names if name not in all_tools]
        if bad_tools:
            raise ValueError(f"Unknown tools: {', '.join(bad_tools)}")
        runtime_bootstrap = {
            "agentDir": bootstrap.agent_dir,
            "authStorage": auth,
            "modelRegistry": registry,
            "settingsManager": settings,
            "resourceLoader": loader,
            "resourceLoaderAlreadyLoaded": True,
            "model": model,
            "thinkingLevel": bootstrap.thinking or settings.get_default_thinking_level(),
            "temperature": (
                bootstrap.temperature
                if bootstrap.temperature is not None
                else (
                    temperature_for_mode(bootstrap.temperature_mode)
                    if bootstrap.temperature_mode
                    else settings.get_default_temperature()
                )
            ),
            "tools": [all_tools[name] for name in tool_names],
            "mcpManager": mcp,
            "subagentsHardDisabled": bootstrap.no_subagents,
            "scopedModels": scoped_models,
        }
        runtime_options = {
            "cwd": manager.cwd,
            "sessionManager": manager,
            "resourceLoader": loader,
        }
        runtime = await create_agent_session_runtime(runtime_bootstrap, runtime_options)
        host = TuiRuntimeHost(AgentSessionRuntimeHost({}, runtime), mcp, _safe_startup_data(mcp, loader))
        host._initial_image_paths = tuple(bootstrap.initial_image_paths)
        if bootstrap.cooperation or settings.get_tool_approval():
            runtime.session.approval_callback = host._approval_callback
        return host
    except BaseException:
        # Do not let a failed disposer mask construction failure or prevent later
        # owners from receiving their rollback attempt.
        for value in (runtime, mcp, loader):
            if value is None:
                continue
            try:
                await _close_if_possible(value)
            except BaseException:
                pass
        raise


TUI_COMMANDS = frozenset(
    {
        "snapshot",
        "prompt",
        "abort",
        "models_page",
        "evidence_page",
        "answer_question",
        "approval_resolve",
        "switch_session",
        "new_session",
        "session_list",
        "ui",
    }
)
