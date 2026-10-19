# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import textwrap
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from textual.theme import Theme

from rich.cells import cell_len, set_cell_size
from rich.markup import escape as rich_escape
from rich.text import Text

from one.config import VERSION
from one.core.agent_runtime_facade import _fit_event_snapshot
from one.core.agent_session import approval_callback_origin
from one.core.oauth import OAuthError
from one.core.provider_login import entry_id as fetched_entry_id
from one.core.provider_login import run_oauth_login, validate_and_fetch
from one.core.session_manager import SessionInfo, SessionManager, normalize_session_name
from one.core.subagent_monitor import SubagentMonitor
from one.core.types import ModelInfo
from one.tools.common import sanitize_display_text

_UI_EVENT_CAPACITY = 256
_UI_EVENT_MAX_BYTES = 64 * 1024
_UI_EVENT_QUEUE_MAX_BYTES = 512 * 1024
# Direct ``AgentSession`` subscriptions predate the facade readiness protocol.
# They are retained only for compatibility/tests, so give them an independently
# bounded holding lane and fail the emitting caller explicitly on overflow.
# This must not become an unbounded escape hatch around facade backpressure.
_UI_LEGACY_DEFERRED_CAPACITY = 256
_UI_LEGACY_DEFERRED_MAX_BYTES = 512 * 1024
# Keep a reduction turn comfortably below the 8 ms responsiveness gate.  A
# completed event is never split (lifecycle barriers must remain atomic), so
# handlers also defer their transcript paint while a drain is active.
_UI_DRAIN_MAX_ITEMS = 8
_UI_DRAIN_MAX_SECONDS = 0.004
_UI_FRAME_SECONDS = 1 / 30
_SELECTION_COPY_TIMEOUT_SECONDS = 5.0
_UI_MAX_COALESCED_DELTA_BYTES = 512
# A Static update can take more than one refresh turn to settle its wrapped
# height. Tail follow is therefore rechecked after two subsequent layout turns;
# this is viewport-only work and does not relax the transcript paint cap.
_STREAM_FOLLOW_LAYOUT_RECONCILES = 5


class TuiSessionView:
    """Detached, UI-loop-only projection of a runtime facade.

    This is deliberately not a compatibility wrapper around ``AgentSession``:
    its state is copied from bounded snapshots and its operations are facade
    commands.  A few attribute-shaped projections remain for the mature TUI
    renderer, but none contains a runtime-owned object.
    """

    def __init__(self, facade: Any) -> None:
        self._facade = facade
        self._snapshot: dict[str, Any] = {"epoch": 0, "sidebar": {}, "messages": []}
        self._models: list[dict[str, Any]] = []
        self._models_epoch = -1
        self._listeners: list[Any] = []
        self._off: Any = None

    async def refresh(self) -> dict[str, Any]:
        reply = await self._facade.command("snapshot")
        result = reply.get("result", reply)
        self._replace_snapshot(result)
        return self.snapshot

    async def refresh_status(self) -> dict[str, Any]:
        """Refresh only the status paint DTO, never the transcript snapshot."""
        reply = await self._facade.command("status_snapshot")
        result = reply.get("result", reply)
        return result if isinstance(result, dict) else {}

    async def refresh_monitor(self) -> dict[str, Any]:
        """Refresh only the bounded child monitor DTO."""
        reply = await self._facade.command("monitor_snapshot")
        result = reply.get("result", reply)
        return result if isinstance(result, dict) else {}

    def apply_status_snapshot(self, result: dict[str, Any]) -> None:
        if isinstance(result.get("sidebar"), dict):
            self._snapshot["sidebar"] = dict(result["sidebar"])
        if isinstance(result.get("mcp"), list):
            self._snapshot["mcp"] = [dict(item) for item in result["mcp"] if isinstance(item, dict)]
        if isinstance(result.get("epoch"), int):
            self._snapshot["epoch"] = result["epoch"]

    def apply_plan_update(self, event: dict[str, Any]) -> None:
        """Apply an event's plan state without waiting for a full resync."""
        if event.get("cleared") is True:
            self._snapshot["sidebar"].update({"plan": [], "planText": "", "planPresent": False})
        elif event.get("planPresent"):
            if isinstance(event.get("planItems"), list):
                self._snapshot["sidebar"]["plan"] = [dict(item) for item in event["planItems"] if isinstance(item, dict)]
            if isinstance(event.get("plan"), str):
                self._snapshot["sidebar"]["planText"] = event["plan"]
            self._snapshot["sidebar"]["planPresent"] = True

    def apply_monitor_snapshot(self, result: dict[str, Any]) -> None:
        if isinstance(result.get("monitor"), dict):
            self._snapshot["monitor"] = dict(result["monitor"])
        if isinstance(result.get("epoch"), int):
            self._snapshot["epoch"] = result["epoch"]

    @property
    def snapshot(self) -> dict[str, Any]:
        # JSON round-trip prevents a caller retaining our mutable projection.
        return json.loads(json.dumps(self._snapshot, ensure_ascii=False))

    @property
    def sidebar(self) -> dict[str, Any]:
        """Small detached paint DTO; avoid copying the retained transcript."""
        return dict(self._snapshot["sidebar"])

    def _replace_snapshot(self, snapshot: Any) -> None:
        if not isinstance(snapshot, dict):
            return
        sidebar = snapshot.get("sidebar") if isinstance(snapshot.get("sidebar"), dict) else {}
        approval = snapshot.get("approval")
        messages = snapshot.get("messages") if isinstance(snapshot.get("messages"), list) else []
        copied_messages = [dict(m) for m in messages if isinstance(m, dict)]
        self._snapshot = {
            "epoch": int(snapshot.get("epoch", 0)),
            "sidebar": dict(sidebar),
            "messages": copied_messages[-500:],
            "models": dict(snapshot.get("models")) if isinstance(snapshot.get("models"), dict) else {},
            "monitor": dict(snapshot.get("monitor", {}))
            if isinstance(snapshot.get("monitor"), dict)
            else {"revision": 0, "children": []},
            "mcp": [dict(item) for item in snapshot.get("mcp", []) if isinstance(item, dict)],
            "questions": [dict(item) for item in snapshot.get("questions", []) if isinstance(item, dict)],
            "approval": dict(approval) if isinstance(approval, dict) else None,
        }

    async def models(self) -> list[SimpleNamespace]:
        """Fetch the complete catalog through bounded worker-owned pages."""
        if self._models_epoch != self.epoch:
            models: list[dict[str, Any]] = []
            offset = 0
            while True:
                page = await self.command("models_page", offset=offset, limit=100)
                if page.get("epoch") != self.epoch:
                    return []
                entries = page.get("models")
                if not isinstance(entries, list):
                    break
                models.extend(dict(item) for item in entries if isinstance(item, dict))
                next_offset = page.get("nextOffset")
                if not isinstance(next_offset, int) or next_offset <= offset:
                    break
                offset = next_offset
            self._models = models
            self._models_epoch = self.epoch
        return [SimpleNamespace(provider=str(item.get("provider", "")), id=str(item.get("id", ""))) for item in self._models]

    def subscribe(self, listener: Any) -> Any:
        self._listeners.append(listener)
        if self._off is None:
            # Older test/detached facades support only the callback argument.
            # The runtime facade additionally asks before it removes an event.
            try:
                self._off = self._facade.subscribe(self._on_event, can_accept_event=self.can_accept_event)
            except TypeError:
                self._off = self._facade.subscribe(self._on_event)

        def off() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return off

    def _on_event(self, event: dict[str, Any]) -> None:
        for listener in tuple(self._listeners):
            listener(event)

    def can_accept_event(self, event: dict[str, Any]) -> bool:
        """Return whether every UI subscriber can accept this event now."""
        for listener in tuple(self._listeners):
            predicate = getattr(listener, "can_accept_event", None)
            if callable(predicate) and not predicate(event):
                return False
        return True

    def resume_events(self) -> None:
        """Tell the facade that a UI drain made ingress space available."""
        resume = getattr(self._facade, "resume_events", None)
        if callable(resume):
            resume()

    async def command(self, name: str, **payload: Any) -> dict[str, Any]:
        reply = await self._facade.command(name, payload)
        result = reply.get("result", reply)
        if isinstance(result, dict) and "sidebar" in result:
            self._replace_snapshot(result)
        return result if isinstance(result, dict) else {}

    async def assistant_page(self, ref: str, offset: int, limit: int, width: int) -> dict[str, Any]:
        return await self.command(
            "assistant_page", ref=ref, offset=offset, limit=limit, width=width, epoch=self.epoch
        )

    # Detached renderer projections.  SimpleNamespace instances are newly
    # allocated for every access and contain scalar values only.
    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.snapshot["messages"]

    @property
    def epoch(self) -> int:
        """Current runtime session epoch for detached UI rebinding guards."""
        return int(self._snapshot["epoch"])

    @property
    def monitor_snapshot(self) -> dict[str, Any]:
        """Copied monitor projection; the live monitor remains runtime-owned."""
        monitor = self._snapshot.get("monitor", {"revision": 0, "children": []})
        children = monitor.get("children") if isinstance(monitor, dict) else []
        return {"revision": monitor.get("revision", 0), "children": [dict(child) for child in children if isinstance(child, dict)]}

    @property
    def session_id(self) -> str:
        return str(self._snapshot["sidebar"].get("sessionId", "-"))

    @property
    def is_streaming(self) -> bool:
        return bool(self._snapshot["sidebar"].get("streaming", False))

    @property
    def thinking_level(self) -> str:
        return str(self._snapshot["sidebar"].get("thinking", "medium"))

    @property
    def temperature(self) -> float:
        return float(self._snapshot["sidebar"].get("temperature", 0.5))

    @property
    def model(self) -> Any:
        value = str(self._snapshot["sidebar"].get("model", "none"))
        provider, _, model = value.partition("/")
        return None if value == "none" else SimpleNamespace(provider=provider, id=model)

    @property
    def temperature_mode(self) -> str:
        return str(self._snapshot["sidebar"].get("temperatureMode", "balanced"))

    @property
    def pending_message_count(self) -> int:
        return int(self._snapshot["sidebar"].get("pendingMessageCount", 0))

    @property
    def active_tools(self) -> list[str]:
        return list(self._snapshot["sidebar"].get("activeTools", []))

    @property
    def session_file(self) -> str:
        return str(self._snapshot["sidebar"].get("sessionFile", ""))

    @property
    def _storage_dir(self) -> str:
        return str(self._snapshot["sidebar"].get("storageDir", ""))

    @property
    def steering_mode(self) -> str:
        return str(self._snapshot["sidebar"].get("steeringMode", "queue"))

    @property
    def follow_up_mode(self) -> str:
        return str(self._snapshot["sidebar"].get("followUpMode", "queue"))

    @property
    def subagents_hard_disabled(self) -> bool:
        return not bool(self._snapshot["sidebar"].get("subagents", True))

    def get_context_usage(self) -> dict[str, Any]:
        return {"percent": self._snapshot["sidebar"].get("contextPercent", 0.0)}

    def get_pending_queues(self) -> dict[str, list[Any]]:
        s = self._snapshot["sidebar"]
        return {"steering": [None] * int(s.get("queueSteer", 0)), "followUp": [None] * int(s.get("queueFollow", 0))}

    def get_session_stats(self) -> dict[str, Any]:
        s = self._snapshot["sidebar"]
        return {
            "tokens": {
                "input": s.get("tokenInput", 0),
                "output": s.get("tokenOutput", 0),
                "total": s.get("tokenTotal", 0),
            },
            "cost": s.get("cost", 0.0),
        }

    # These methods are intentionally asynchronous command entry points; they
    # are used by the migrated input/abort path and are safe while the worker is
    # blocked in provider I/O.
    async def prompt(self, text: str, images: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if images:
            return await self.command("prompt", text=text, images=images)
        return await self.command("prompt", text=text)

    async def abort(self) -> dict[str, Any]:
        return await self.command("abort")

    async def steer(self, text: str) -> dict[str, Any]:
        return await self.command("ui", operation="steer", payload={"text": text})

    async def follow_up(self, text: str) -> dict[str, Any]:
        return await self.command("ui", operation="follow", payload={"text": text})

    async def compact(self, instructions: str | None = None) -> dict[str, Any]:
        return await self.command("ui", operation="compact", payload={"instructions": instructions})

    async def reload(self) -> dict[str, Any]:
        return await self.command("ui", operation="reload", payload={})

    async def invoke_skill(self, name: str, args: str) -> dict[str, Any]:
        return await self.command("ui", operation="invoke_skill", payload={"name": name, "args": args})

    async def ui(self, operation: str, **payload: Any) -> dict[str, Any]:
        return await self.command("ui", operation=operation, payload=payload)

    async def execute_tui_command(self, command: str) -> dict[str, Any]:
        return await self.ui("execute_bash", command=command)

    async def extension_response(
        self, request_id: str, payload: dict[str, Any] | None = None, cancelled: bool = False
    ) -> dict[str, Any]:
        return await self.ui("answer_extension", id=request_id, payload=payload, cancelled=cancelled)

    async def answer_question(self, question_id: str, answer: str, epoch: int) -> dict[str, Any]:
        return await self.command(
            "answer_question", question_id=question_id, answer=answer, epoch=epoch
        )


def _format_elapsed_ms(value: Any) -> str | None:
    """Format only trustworthy lifecycle durations for literal stream headings."""
    try:
        valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        return None
    if value < 1000:
        return f"{int(value)} ms"
    return f"{value / 1000:.2f} s"


try:
    from textual import events
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.content import Content
    from textual.message import Message
    from textual.style import Style as TextualStyle
    from textual.widgets import Button, Input, Static, TextArea
    from textual.widgets.text_area import Edit

    from one.modes.tui_transcript import TranscriptWidget

    TEXTUAL_AVAILABLE = True
except Exception:  # pragma: no cover
    TEXTUAL_AVAILABLE = False


def build_sidebar_snapshot(
    session: Any,
    retry_state: str = "idle",
) -> dict[str, Any]:
    def normalize_mcp_servers(statuses: Any) -> list[dict[str, Any]]:
        """Project MCP status records into the sidebar's small display DTO."""
        if not isinstance(statuses, list):
            return []
        servers: list[dict[str, Any]] = []
        for status in statuses:
            if not isinstance(status, dict) or not status.get("enabled"):
                continue
            runtime_state = str(status.get("runtimeState") or "").lower()
            servers.append(
                {
                    "name": str(status.get("name") or ""),
                    "transport": str(status.get("transport") or ""),
                    "toolCount": int(status.get("toolCount", len(status.get("tools") or [])) or 0),
                    "running": bool(status.get("running")),
                    "error": status.get("error"),
                    "available": (
                        bool(status.get("running"))
                        and not status.get("error")
                        and runtime_state not in {"failed", "retrying", "disabled"}
                    ),
                }
            )
        return servers

    if isinstance(session, TuiSessionView):
        # The detached snapshot is authoritative.  Do not reconstruct it from
        # compatibility-shaped attributes, which would silently consult a live
        # host if one were ever attached to the view.
        sidebar = session.sidebar
        defaults = {
            "model": "none", "thinking": "medium", "temperature": 0.5,
            "temperatureMode": "balanced", "cwd": ".", "sessionId": "-",
            "streaming": False, "compacting": False, "retry": "on", "coop": "off",
            "contextPercent": 0.0, "contextWindow": None, "contextKnown": False,
            "contextTotalPercent": None,
            "deliveryMode": "idle",
            "queueSteer": 0, "queueFollow": 0, "queueTotal": 0, "tokenInput": 0,
            "tokenOutput": 0, "tokenCacheRead": 0, "tokenCacheWrite": 0,
            "tokenTotal": 0, "cost": 0.0, "subagents": True, "bashOutput": True,
            "mcpEnabled": False, "mcpServers": [],
        }
        defaults.update(sidebar)
        # Snapshot.mcp is retained as a compatibility alias; the sidebar DTO is
        # authoritative and already contains no schemas or raw status records.
        defaults["mcpServers"] = normalize_mcp_servers(defaults.get("mcpServers") or session._snapshot.get("mcp"))
        return defaults
    usage = session.get_context_usage() or {}
    queues = session.get_pending_queues() or {"steering": [], "followUp": []}
    stats = session.get_session_stats() or {}
    tokens = stats.get("tokens") or {}

    steer_count = len(queues.get("steering", []))
    follow_count = len(queues.get("followUp", []))

    # Determine the streaming delivery mode: when is_streaming, pick the mode
    # that normal input will use; otherwise "idle".
    if getattr(session, "is_streaming", False):
        fm = getattr(session, "follow_up_mode", "queue")
        delivery_mode = "followUp" if fm == "follow_up" else "steer"
    else:
        delivery_mode = "idle"

    mcp_manager = getattr(session, "_mcp_manager", None)
    mcp_servers: list[dict[str, Any]] = []
    if mcp_manager is not None:
        try:
            statuses = mcp_manager.server_status()
        except Exception:
            statuses = []
        mcp_servers = normalize_mcp_servers(statuses)

    # Retry: show the actual retry mode string (off/on/unlimited) read from
    # settings when available, falling back to the current on/off logic.
    try:
        retry_display = str(
            getattr(
                getattr(session, "settings_manager", None),
                "get_retry_mode",
                lambda: "on",
            )(),
        )
    except Exception:
        retry_display = None
    if retry_display not in ("off", "on", "unlimited"):
        auto_retry = getattr(session, "auto_retry_enabled", True)
        if not callable(auto_retry):
            auto_retry = bool(auto_retry)
        retry_display = "on" if auto_retry else "off"

    return {
        "model": f"{session.model.provider}/{session.model.id}" if session.model else "none",
        "thinking": getattr(session, "thinking_level", "medium"),
        "temperature": float(getattr(session, "temperature", 0.5)),
        "temperatureMode": getattr(session, "temperature_mode", "balanced"),
        "cwd": getattr(getattr(session, "session_manager", None), "cwd", "."),
        "sessionId": getattr(session, "session_id", "-"),
        "streaming": bool(getattr(session, "is_streaming", False)),
        "compacting": bool(getattr(session, "is_compacting", False)),
        "retry": retry_display,
        "coop": "on" if getattr(session, "approval_callback", None) is not None else "off",
        "contextPercent": float(usage.get("percent") or 0.0),
        "contextTotalPercent": usage.get("contextPercent"),
        "contextWindow": usage.get("contextWindow") if isinstance(usage.get("contextWindow"), int) else None,
        "contextInputLimit": usage.get("inputLimit"),
        "contextOutputLimit": usage.get("outputLimit"),
        "contextMaxWindow": usage.get("maxContextWindow"),
        "effectiveContextBudget": usage.get("effectiveContextWindow"),
        "selectedContextWindow": usage.get("selectedContextWindow"),
        "contextPolicy": usage.get("contextPolicy"),
        "outputReserve": usage.get("outputReserve"),
        "contextBudgetReason": usage.get("budgetReason"),
        "contextSource": usage.get("metadataSource"),
        "contextKnown": isinstance(usage.get("contextWindow"), int),
        "deliveryMode": delivery_mode,
        "queueSteer": steer_count,
        "queueFollow": follow_count,
        "queueTotal": steer_count + follow_count,
        "tokenInput": int(tokens.get("input") or 0),
        "tokenOutput": int(tokens.get("output") or 0),
        "tokenCacheRead": int(tokens.get("cacheRead") or 0),
        "tokenCacheWrite": int(tokens.get("cacheWrite") or 0),
        "tokenTotal": int(tokens.get("total") or 0),
        "cost": float(stats.get("cost") or 0.0),
        "subagents": not bool(getattr(session, "subagents_hard_disabled", False))
        and bool(getattr(getattr(session, "settings_manager", None), "get_subagents_enabled", lambda: True)()),
        "bashOutput": bool(getattr(getattr(session, "settings_manager", None), "get_bash_show_output", lambda: True)()),
        "mcpEnabled": mcp_manager is not None,
        "mcpServers": mcp_servers,
    }


def get_git_branch(cwd: str | os.PathLike[str]) -> str | None:
    """Return the attached Git branch for *cwd*, without surfacing failures."""
    try:
        if not Path(cwd).is_dir():
            return None
        result = subprocess.run(
            ["git", "-C", os.fspath(cwd), "symbolic-ref", "--quiet", "--short", "HEAD"],
            stdout=subprocess.PIPE,
            text=True,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=0.2,
        )
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None
    branch = result.stdout.strip()
    return branch if result.returncode == 0 and branch else None


# Braille spinner frames shown beside the chat stream while the model is working.
_THINKING_FRAMES = "░▒▓█▓▒"
# Legacy sentinel retained only for loading old presentation fixtures. New
# waiting frames live in #waiting_status, outside the transcript cache.
_THINKING_MARK = "__MK__:"
_THINKING_TEXT_MARK = "__MK_THINK__: "
_GIT_BRANCH_CACHE_TTL_SECONDS = 5.0


def _read_logo_lines(path: Traversable) -> tuple[str, ...]:
    """Read logo lines, preserving blank separators; return empty only if entirely blank."""
    lines = tuple(path.read_text(encoding="utf-8").splitlines())
    return lines if any(line.strip() for line in lines) else ()


def _load_tui_logo_lines() -> tuple[str, ...]:
    """Read the packaged startup artwork."""
    try:
        logo_lines = _read_logo_lines(files("one").joinpath("assets").joinpath("logo.txt"))
    except (FileNotFoundError, ModuleNotFoundError, OSError, UnicodeError):
        return ()
    return logo_lines


# Startup artwork is a dedicated visual widget, kept outside both the durable
# conversation and the transcript presentation cache.
_TUI_LOGO_LINES = _load_tui_logo_lines()

# The transcript is a presentation cache.  AgentSession and session JSONL keep
# the complete conversation; this list is deliberately bounded so rebuilding
# the Static widget remains predictable during long-running sessions.
MAX_RENDERED_LINES = 500
# These are presentation limits, not history limits.  Keep them below the
# point where Rich/Textual has to allocate a huge single widget (a single
# newline-free tool result used to bypass MAX_RENDERED_LINES completely).
MAX_RENDERED_BYTES = 256 * 1024
MAX_RENDERED_ROWS = 1_000
MAX_SOURCE_DISPLAY_BYTES = 64 * 1024
# A normal multi-kilobyte streamed answer must remain readable.  Eight KiB
# retains the ordinary 4 KiB response case while preventing one active block
# from making Rich/Textual format tens of thousands of bytes on the UI loop.
# The complete response remains in session history/evidence; this is only the
# live transcript presentation cap.
_MAX_LIVE_ASSISTANT_RENDER_BYTES = 8 * 1024
_DISPLAY_TRUNCATED_MARKER = "[display truncated; full text remains in session history]"
# Keep following when the user is effectively at the tail, without treating a
# reader who has deliberately moved up as an auto-follow target.
_STREAM_FOLLOW_TOLERANCE = 1
# A post-layout callback runs on Textual's UI thread. Keep retained-history
# anchor lookup bounded even when one logical line wraps to many physical rows.
_STREAM_ANCHOR_SEARCH_ROWS = 256
_TRUNCATED_ASSISTANT_MARKER = "[earlier assistant output truncated in viewport]"


def _truncate_display_bytes(value: object, limit: int = MAX_SOURCE_DISPLAY_BYTES) -> str:
    """Return safe text within a UTF-8 byte budget, with an explicit marker.

    Do this before splitting/wrapping, rather than after Rich has processed a
    giant logical line.  Slicing encoded bytes avoids splitting a Unicode code
    point and works for every untrusted transcript source.
    """
    text = sanitize_display_text(str(value))
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    marker = "\n" + _DISPLAY_TRUNCATED_MARKER
    room = max(0, limit - len(marker.encode("utf-8")))
    return encoded[:room].decode("utf-8", "ignore") + marker

# The sidebar shows a focused plan window rather than a character slice of the
# entire plan. This is display-only; session and provider plan state is intact.
_PLAN_SIDEBAR_ITEMS = 3
# Sidebar width is 42 cells including its border and horizontal padding.
_PLAN_SIDEBAR_ITEM_MAX = 38


def _sidebar_plan_window(
    plan: list[dict[str, str]] | str | None, *, max_item_chars: int = _PLAN_SIDEBAR_ITEM_MAX
) -> str:
    """Render at most three focused plan rows for the narrow TUI sidebar."""
    if not plan:
        return ""

    markers = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]", "blocked": "[!]"}

    def window_start(statuses: list[str | None]) -> int:
        count = len(statuses)
        if count <= _PLAN_SIDEBAR_ITEMS:
            return 0
        try:
            focus = statuses.index("in_progress")
        except ValueError:
            try:
                focus = next(index for index, status in enumerate(statuses) if status != "completed")
            except StopIteration:
                focus = count - 1
        return max(0, min(focus - 1, count - _PLAN_SIDEBAR_ITEMS))

    def compact(marker: str, label: str) -> str:
        # Keep one row per item and preserve its status marker on small panes.
        normalized = " ".join(label.split())
        available = max(1, max_item_chars - len(marker) - 1)
        if cell_len(normalized) > available:
            normalized = set_cell_size(normalized, max(0, available - 1)).rstrip() + "…"
        return f"{marker} {normalized}"

    if isinstance(plan, list):
        rows: list[tuple[str, str, str]] = []
        for item in plan:
            if not isinstance(item, dict):
                continue
            status = item.get("status")
            step = item.get("step")
            if status in markers and isinstance(step, str):
                rows.append((markers[status], step, status))
        if not rows:
            return ""
        start = window_start([status for _marker, _step, status in rows])
        return "\n".join(compact(marker, step) for marker, step, _status in rows[start : start + _PLAN_SIDEBAR_ITEMS])

    # Legacy strings remain strings: marker inspection only chooses a visual
    # window and never turns arbitrary persisted text into canonical state.
    lines = plan.splitlines() or [plan]
    statuses: list[str | None] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("[>]"):
            statuses.append("in_progress")
        elif stripped.startswith("[x]"):
            statuses.append("completed")
        elif stripped.startswith("[!]"):
            statuses.append("blocked")
        elif stripped.startswith("[ ]"):
            statuses.append("pending")
        else:
            statuses.append(None)
    start = window_start(statuses)
    return "\n".join(" ".join(line.split()) for line in lines[start : start + _PLAN_SIDEBAR_ITEMS])


# One source of truth for user-visible keyboard shortcuts. Textual itself
# provides many widget/editor bindings, but these are the application-level
# actions users can rely on in the TUI.
TUI_SHORTCUTS: tuple[tuple[str, str], ...] = (
    ("Ctrl+K", "Select model"),
    ("Ctrl+P", "Commands"),
    ("Ctrl+C", "Abort"),
    ("Ctrl+L", "Clear output"),
    ("Ctrl+Q", "Quit"),
    ("Ctrl+Z", "Toggle cooperation"),
    ("Ctrl+S", "Toggle subagents"),
    ("Ctrl+O", "Toggle details"),
    ("Ctrl+V", "Paste clipboard"),
    ("Ctrl+Shift+V", "Paste terminal"),
    ("Ctrl+Alt+V", "Paste image"),
    ("Ctrl+R", "Retry mode"),
    ("Ctrl+← / →", "Adjust temperature"),
    ("Ctrl+↑ / ↓", "Input history"),
    ("Ctrl+F1", "Command help"),
    ("Esc", "Close"),
)


def format_tui_shortcuts() -> str:
    """Render the current application shortcuts for the sidebar and overlay."""
    return "\n".join(f"{key} {description}" for key, description in TUI_SHORTCUTS)


def evaluate_waiting(
    turn_active: bool,
    last_delta_ts: float,
    now: float,
    idle_threshold: float = 0.9,
) -> bool:
    """True whenever the current turn has non-idle execution.

    ``last_delta_ts`` and the legacy threshold remain accepted for compatibility
    with callers/tests, but prose output must not hide the independent footer:
    a model can stream arguments before a long tool call, and tools themselves
    generally emit no deltas.
    """
    del last_delta_ts, now, idle_threshold
    return turn_active


def advance_thinking_frame(frame: int) -> int:
    """Advance the spinner frame index, wrapping around."""
    return (frame + 1) % len(_THINKING_FRAMES)


@dataclass(frozen=True)
class TuiPalette:
    name: str
    screen_bg: str
    screen_fg: str
    panel_bg: str
    panel_border: str
    input_bg: str
    input_border: str
    input_fg: str
    sidebar_bg: str
    info: str
    warn: str
    error: str
    user: str
    assistant: str
    tool: str
    user_bg: str
    assistant_bg: str
    tool_bg: str


BUILTIN_TUI_THEMES: dict[str, TuiPalette] = {
    "default": TuiPalette(
        "default",
        "#0b1220",
        "#dbe7ff",
        "#0f1a2e",
        "#2f466e",
        "#152441",
        "#456ca8",
        "#e7f0ff",
        "#101a30",
        "#8fd3ff",
        "#ffd479",
        "#ff8a8a",
        "#9ff0c4",
        "#8fd3ff",
        "#ffd479",
        "#133a2b",
        "#15324a",
        "#3a3016",
    ),
    "light": TuiPalette(
        "light",
        "#f5f7fb",
        "#1b2433",
        "#ffffff",
        "#b8c6df",
        "#f1f5ff",
        "#8ea4d5",
        "#1d2a44",
        "#f8fbff",
        "#2f5ea8",
        "#a96a00",
        "#b22b2b",
        "#146c43",
        "#2f5ea8",
        "#946200",
        "#d7f4e7",
        "#dfe9ff",
        "#fff0d9",
    ),
    "hacker": TuiPalette(
        "hacker",
        "#020902",
        "#7cff7c",
        "#031003",
        "#1f6f1f",
        "#041804",
        "#2da62d",
        "#a5ff9e",
        "#031203",
        "#7cff7c",
        "#f0ff8c",
        "#ff7070",
        "#9bffb3",
        "#7cff7c",
        "#f0ff8c",
        "#0a2a0a",
        "#092209",
        "#2b2b08",
    ),
    "solarized": TuiPalette(
        "solarized",
        "#002b36",
        "#93a1a1",
        "#073642",
        "#2aa198",
        "#0a3b47",
        "#268bd2",
        "#eee8d5",
        "#0a3742",
        "#268bd2",
        "#b58900",
        "#dc322f",
        "#2aa198",
        "#268bd2",
        "#b58900",
        "#08453f",
        "#0b4050",
        "#4a4309",
    ),
    "fallout": TuiPalette(
        "fallout",
        "#12100b",
        "#ffd77a",
        "#1a170f",
        "#8f7b3f",
        "#211c12",
        "#b89a4d",
        "#ffe8a6",
        "#18140d",
        "#f4c96c",
        "#ffb347",
        "#ff7a62",
        "#ffd77a",
        "#f4c96c",
        "#ffb347",
        "#3b2f14",
        "#332811",
        "#4a3716",
    ),
}


def resolve_tui_theme(name: str | None) -> TuiPalette:
    if not name:
        return BUILTIN_TUI_THEMES["default"]
    return BUILTIN_TUI_THEMES.get(name.strip().lower(), BUILTIN_TUI_THEMES["default"])


def _tui_palette_to_textual_theme(palette: TuiPalette) -> Theme:
    """Map a one ``TuiPalette`` into a Textual ``Theme`` so built-in widgets
    (CommandPalette, Input, OptionList, etc.) pick up the same colours via CSS
    variables such as ``$foreground``, ``$background``, ``$surface``,
    ``$panel``, ``$border``, and ``$block-cursor-*``.

    The mapping is deliberately one-to-one and lossless – every palette slot
    maps to a Textual token that the built-in CSS uses.
    """
    from textual.theme import Theme

    return Theme(
        name=f"one-{palette.name}",
        primary=palette.screen_fg,
        secondary=palette.panel_border,
        warning=palette.warn,
        error=palette.error,
        success=palette.user,
        accent=palette.assistant,
        foreground=palette.screen_fg,
        background=palette.screen_bg,
        surface=palette.panel_bg,
        panel=palette.sidebar_bg,
        boost=palette.tool,
        dark=palette.name != "light",
        variables={
            "border": palette.panel_border,
            "border-blurred": palette.panel_border,
            "block-cursor-background": palette.info,
            "block-cursor-foreground": palette.screen_fg,
            "block-cursor-text-style": "none",
            "block-cursor-blurred-background": palette.info + "55",
            "block-cursor-blurred-foreground": palette.screen_fg,
            "block-cursor-blurred-text-style": "none",
            "button-color-foreground": palette.screen_fg,
            "input-selection-background": palette.info + "35",
            "footer-key-foreground": palette.screen_fg,
        },
    )


# Pre-computed Textual Theme objects keyed by one theme name.
_TEXTUAL_THEME_MAP: dict[str, Theme] = {
    name: _tui_palette_to_textual_theme(palette) for name, palette in BUILTIN_TUI_THEMES.items()
}


def _paste_from_system_clipboard() -> str | None:
    """Read text from the system clipboard (mirror of the copy tool chain).

    Returns the clipboard text, or `None` if no backend is available.
    """
    system_commands: list[list[str]] = [
        ["wl-paste", "--no-newline"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
        ["pbpaste"],
        ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
    ]
    for cmd in system_commands:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            out = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            if out.returncode == 0 and out.stdout:
                return out.stdout
        except Exception:
            continue
    try:
        import pyperclip  # type: ignore

        return pyperclip.paste() or None
    except Exception:
        pass
    return None


_SLASH_COMMANDS: tuple[str, ...] = (
    "/exit",
    "/quit",
    "/help",
    "/stats",
    "/state",
    "/status",
    "/queue",
    "/tools",
    "/clear",
    "/abort",
    "/model",
    "/model-cycle",
    "/providers",
    "/thinking",
    "/thinking-cycle",
    "/temperature",
    "/theme",
    "/steer",
    "/follow",
    "/compact",
    "/tree",
    "/navigate",
    "/fork",
    "/new",
    "/sessions",
    "/login",
    "/logout",
    "/reload",
    "/retry",
    "/retry-cycle",
    "/skill",
    "/skill list",
    "/config",
    "/extui",
    "/cooperation",
    "/subagents",
    "/agents",
    "/details-show",
    "/mcp",
    "/history",
    "/bash",
    "/inspect-timeout",
    "/paste-image",
    "/layout",
    "/sidebar",
)


def _extract_image_paths(text: str) -> tuple[list[Path], str]:
    """Pull image file paths out of *text* and return ``(paths, clean_text)``.

    Recognised patterns (conservative — no false positives on normal prose):

    1. Quoted paths: ``"path/to/img.png"``, ``'path/to/img.png'``
    2. Angle-bracketed: ``<path/to/img.png>``
    3. Backtick: ```path/to/img.png``
    4. Unquoted whitespace-separated paths that look like files with
       recognised extensions (must start with ``/`` or ``./`` or ``~/``
       or ``~user/``).

    The returned *clean_text* has all matched paths removed.

    Matching strategy: collect all (start, end) match positions from every
    pattern, keep only those pointing to existing files, deduplicate, then
    rebuild the text in a single pass removing all matched character positions.
    """
    # Collect all regex matches as (start, end, group1_text).
    raw_matches: list[tuple[int, int, str]] = []

    # Pattern 1-3: quoted / bracketed paths.
    for pattern in [
        r"""(?<![\\])"([^"]*\.(?:png|jpg|jpeg|webp))"(?![^<]*>)""",
        r"(?<![\\])'([^']*\.(?:png|jpg|jpeg|webp))'(?![^<]*>)",
        r"(?<![\\])`([^`]*\.(?:png|jpg|jpeg|webp))`(?!`)",
        r"<([^>]*\.(?:png|jpg|jpeg|webp))>",
    ]:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            raw_matches.append((m.start(), m.end(), m.group(1)))

    # Pattern 4: unquoted paths.
    unquoted_pat = re.compile(
        r"""(?<![a-zA-Z0-9_./\\])"""  # not preceded by path chars
        r"""(/(?:[^ \t\n\r\f\v<>\"'`]*\.(?:png|jpg|jpeg|webp))"""  # /...ext
        r"""|"""
        r"""\./(?:[^ \t\n\r\f\v<>\"'`]*\.(?:png|jpg|jpeg|webp))"""  # ./...ext
        r"""|"""
        r"""~(?:[^ \t\n\r\f\v<>\"'`]*/)?[^ \t\n\r\f\v<>\"'`]*\.(?:png|jpg|jpeg|webp))"""  # ~/... or ~user/...ext
        r"""(?![a-zA-Z0-9_.])"""  # not followed by path chars
    )
    for m in unquoted_pat.finditer(text):
        raw_matches.append((m.start(), m.end(), m.group(1)))

    # Resolve each candidate and keep only existing files, with path expansion.
    paths: list[Path] = []
    for _start, _end, group_text in raw_matches:
        candidate = Path(os.path.expanduser(group_text))
        if candidate.exists() and candidate.is_file():
            paths.append(candidate)

    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[Path] = []
    for p in paths:
        k = str(p.resolve())
        if k not in seen:
            seen.add(k)
            unique.append(p)

    # If no image paths found, return original text stripped.
    if not unique:
        return [], text.strip()

    # Build a set of character positions to remove (handles dedup/overlap).
    path_set = {str(p.resolve()) for p in unique}
    to_remove: set[int] = set()
    for _start, _end, group_text in raw_matches:
        candidate = Path(os.path.expanduser(group_text))
        if str(candidate.resolve()) in path_set:
            for i in range(_start, _end):
                to_remove.add(i)

    # Rebuild clean text by skipping removed positions.
    clean = "".join(ch for i, ch in enumerate(text) if i not in to_remove)
    clean_text = " ".join(clean.split())

    return unique, clean_text


if TEXTUAL_AVAILABLE:

    class SessionEvent(Message):
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload
            super().__init__()

    class InputSubmitted(Message):
        """Posted by the command input when the user submits.

        Module-level so Textual derives the handler name ``on_input_submitted``
        (a nested class would derive ``on__command_text_area_submitted``).
        """

        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    class _CommandTextArea(TextArea):
        """Multi-line command input whose ctrl+v pastes from the system clipboard.

        Textual's default `action_paste` reads `app.clipboard`, an in-memory
        value that only ever holds text copied inside the app — system
        clipboard content (e.g. copied from a browser) never made it in.

        Enter submits (like the previous single-line Input); Shift+Enter
        inserts a newline; arrow keys move the cursor across lines. Pasting
        very large text is capped to avoid UI/layout blowups.
        """

        _PASTE_MAX_CHARS = 10240

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            # Keep the insertion cursor visible without the distracting blink.
            self.cursor_blink = False
            self._completion_prefix = ""
            self._completion_matches: list[str] = []
            self._completion_index = -1
            self._completion_locked = False  # locked after first completion
            self._history_index: int | None = None

        BINDINGS = [
            ("ctrl+j", "submit", "Submit"),
            ("shift+enter", "newline", "New line"),
        ]

        async def _on_key(self, event: events.Key) -> None:
            # TextArea._on_key swallows Enter (inserts "\n") before widget
            # BINDINGS are ever consulted, so Enter-to-submit must be handled
            # here, ahead of the superclass. Shift+Enter stays a newline.
            if event.key in {"enter", "ctrl+enter"}:
                event.stop()
                event.prevent_default()
                self.post_message(InputSubmitted(self.text))
                return
            if event.key in {"ctrl+up", "ctrl+down"}:
                self._navigate_history(older=event.key == "ctrl+up")
                event.stop()
                event.prevent_default()
                return
            if event.key == "tab":
                # Tab is reserved for slash completion in this input.  Consume
                # it even if nothing can be completed so Textual does not use
                # its default focus traversal to move out of the command box.
                self._complete_slash_command()
                event.stop()
                event.prevent_default()
                return
            # A fresh edit (or a normal cursor/navigation key) begins a new
            # history-navigation cycle. Ctrl+Up/Ctrl+Down above are the only
            # keys that retain the selected history position.
            self.reset_history_navigation()
            # Any edit/navigation invalidates the current completion cycle.
            self._completion_prefix = ""
            self._completion_matches = []
            self._completion_index = -1
            self._completion_locked = False
            # shift+enter and ctrl+v are handled here so they don't reach
            # App._on_key through _OneTextualApp (would cause double fire).
            # ctrl+v is intercepted in _on_key — it is not a
            # _CommandTextArea binding — to prevent the app-level key handler
            # from checking bindings a second time.  Let the binding fire
            # through the normal widget chain, then stop.
            if event.key == "shift+enter":
                # Let the binding action (action_newline) fire, but stop
                # to prevent _OneTextualApp._on_key → App._on_key from
                # processing the same binding a second time.
                self.action_newline()
                event.stop()
                return
            if event.key == "ctrl+v":
                # Ctrl+V is intercepted directly, not via a Binding, to prevent
                # duplicate app-level handling.
                self.action_paste()
                event.stop()
                event.prevent_default()
                return
            if event.key in {"ctrl+left", "ctrl+right"}:
                # TextArea handles word navigation before bubbled App bindings.
                # Dispatch the priority app shortcut here and consume the key so
                # the editor cannot move the cursor as well.
                action = "action_decrease_temperature" if event.key == "ctrl+left" else "action_increase_temperature"
                getattr(self.app, action)()
                event.stop()
                event.prevent_default()
                return
            # TextArea moves its cursor in its own key handler before the event
            # reaches the app. Route agents-panel navigation first so opening
            # the panel does not alter the command being composed.
            if self.app._handle_agents_panel_key(event):
                return
            await super()._on_key(event)

        async def action_submit(self) -> None:
            self.post_message(InputSubmitted(self.text))

        def on_focus(self) -> None:
            if self.placeholder == "Type a command or /help":
                self.placeholder = "Ctrl+Enter send · Ctrl+P commands"

        def on_blur(self) -> None:
            if self.placeholder == "Ctrl+Enter send · Ctrl+P commands":
                self.placeholder = "Type a command or /help"

        def action_newline(self) -> None:
            self.reset_history_navigation()
            self.insert("\n")

        def reset_history_navigation(self) -> None:
            self._history_index = None

        def _navigate_history(self, *, older: bool) -> None:
            history = getattr(self.app, "_command_history", [])
            if older:
                if not history:
                    return
                if self._history_index is None:
                    self._history_index = len(history) - 1
                else:
                    self._history_index = max(0, self._history_index - 1)
                self.text = history[self._history_index]
                self.move_cursor(self.document.end)
                return
            if self._history_index is None:
                return
            if self._history_index >= len(history) - 1:
                self._history_index = None
                self.text = ""
                return
            self._history_index += 1
            self.text = history[self._history_index]
            self.move_cursor(self.document.end)

        @property
        def text(self) -> str:
            return super().text

        @text.setter
        def text(self, value: str) -> None:
            # Reset completion state whenever the text changes so that a
            # newly-typed prefix gets a fresh match set (fixes stale Tab
            # completion after completing a different command).
            super(_CommandTextArea, self.__class__).text.fset(self, value)  # type: ignore[attr-defined]
            self._completion_prefix = ""
            self._completion_matches = []
            self._completion_index = -1
            self._completion_locked = False

        def _complete_slash_command(self) -> bool:
            """Tab-complete the slash command under the cursor.

            Cycles through matches on repeated Tab presses; writes a hint
            line with the available matches when more than one exists.
            Returns True when a completion was applied.
            """
            row, col = self.selection.end
            line = str(self.get_line(row))
            before = line[:col]
            start = max(before.rfind(" "), before.rfind("\t")) + 1
            word = before[start:]
            if not word.startswith("/"):
                return False
            after = line[col:]
            m = re.match(r"\S*", after)
            end = col + (len(m.group(0)) if m else 0)
            prefix = word[1:]
            if not self._completion_locked:
                # First press for this word — find the initial match set.
                self._completion_matches = [c for c in _SLASH_COMMANDS if c.startswith("/" + prefix)]
                self._completion_index = -1
                self._completion_prefix = prefix
                # Lock the match set so that subsequent Tab presses cycle
                # through the same list even when the word under the cursor
                # changes after a completion (e.g. "/mo" → "/model").
                self._completion_locked = True
            matches = self._completion_matches
            if not matches:
                return False
            self._completion_index = (self._completion_index + 1) % len(matches)
            completed = matches[self._completion_index]
            self.edit(Edit(completed, (row, start), (row, end), True))
            self.move_cursor((row, start + len(completed)))
            if len(matches) > 1 and self._completion_index == 0:
                write = getattr(self.app, "_write", None)
                if write:
                    write("[completion] " + " ".join(matches), "info")
            return True

        def action_paste(self) -> None:
            text = _paste_from_system_clipboard()
            if not text:
                text = self.app.clipboard
            self._insert_paste_text(text)

        def _insert_paste_text(self, text: str) -> None:
            """Insert terminal or clipboard text with the common paste guards."""
            if self.read_only or not text:
                return
            self.reset_history_navigation()
            if len(text) > self._PASTE_MAX_CHARS:
                text = text[: self._PASTE_MAX_CHARS]
            if result := self._replace_via_keyboard(text, *self.selection):
                self.move_cursor(result.end_location)

        async def _on_paste(self, event: events.Paste) -> None:
            """Handle bracketed-terminal paste events as the single insertion path.

            Textual's TextArea also has a native ``_on_paste`` that would
            double-insert on the same paste.  We override it with the same
            guarded insert (truncation) and consume the event so the
            superclass handler never fires for us.

            Compatible with Textual >= 0.74.0 (where ``_on_paste`` exists)
            and Textual 8.x (installed 8.2.8).
            """
            self._insert_paste_text(event.text)
            # Consume the event so Textual's native _on_paste (and any
            # subsequent handlers) do not insert a second copy.
            event.stop()
            event.prevent_default()

    class _ApprovalReasonInput(Input):
        """A bounded approval-reason field whose Enter key is intentionally inert."""

        async def action_submit(self) -> None:
            return

    class _OneTextualApp(App[None]):
        CSS = """
        Screen {
            layout: vertical;
            background: #0b1220;
            color: #dbe7ff;
        }

        #root {
            layout: horizontal;
            height: 1fr;
        }

        #main {
            width: 1fr;
            layout: vertical;
            padding: 0 1;
        }

        #header {
            height: 3;
            border: round #2f466e;
            background: #101a30;
            color: #dbe7ff;
            padding: 0 1;
            text-wrap: nowrap;
            text-overflow: ellipsis;
        }

        #conversation {
            margin: 1 0 0 0;
            border: round #2f466e;
            background: #0f1a2e;
            layout: vertical;
        }

        #main.layout-wide #conversation {
            margin: 0;
        }

        #stream_container {
            height: 1fr;
            background: transparent;
            overflow-y: auto;
            scrollbar-gutter: stable;
        }

        #logo {
            height: auto;
            padding: 1 2;
            color: #8db7ff;
        }

        #stream {
            height: auto;
            min-height: 1;
            padding: 0 2;
        }

        #waiting_status {
            height: 1;
            padding: 0 2;
        }

        #input {
            margin: 1 0 0 0;
            border: round #456ca8;
            background: #152441;
            color: #e7f0ff;
            height: auto;
            min-height: 3;
            max-height: 40%;
        }

        #sidebar {
            width: 42;
            height: 1fr;
            border: round #2f466e;
            background: #101a30;
            padding: 0 1;
            overflow-y: auto;
        }

        #ext_panel {
            display: none;
            border: round #7a5cff;
            background: #150f33;
            color: #e8e2ff;
            height: auto;
            max-height: 40%;
            margin: 1 0 0 0;
            padding: 0 2;
            overflow-y: auto;
        }

        #ext_overlay {
            display: none;
            position: absolute;
            offset: 5% 15%;
            width: 90%;
            height: 70%;
            border: heavy #7a5cff;
            background: #0d0a20;
            color: #ece6ff;
            padding: 1 2;
            overflow-y: auto;
            layer: above;
        }

        #shortcuts_overlay {
            display: none;
            position: absolute;
            offset: 20% 20%;
            width: 60%;
            height: auto;
            max-height: 60%;
            border: heavy #456ca8;
            background: #0d1424;
            color: #e7f0ff;
            padding: 1 2;
            layer: above;
        }

        #model_overlay {
            display: none;
            position: absolute;
            offset: 15% 15%;
            width: 70%;
            height: auto;
            max-height: 70%;
            border: heavy #456ca8;
            background: #0d1424;
            color: #e7f0ff;
            padding: 1 2;
            layer: above;
        }

        #ext_panel.visible, #ext_overlay.visible, #shortcuts_overlay.visible, #model_overlay.visible {
            display: block;
        }

        #agents_overlay {
            display: none;
            position: absolute;
            offset: 8% 8%;
            width: 84%;
            height: 78%;
            padding: 1 2;
            layer: above;
            layout: vertical;
        }

        #agents_overlay.visible { display: block; }

        #agents_help { height: auto; }

        #agents_timeline {
            height: 1fr;
            min-height: 3;
            overflow-y: auto;
        }

        #agents_content { height: auto; padding: 0 1; }

        #approval_overlay {
            display: none;
            position: absolute;
            offset: 18% 25%;
            width: 50%;
            height: auto;
            max-height: 55%;
            padding: 1 2;
            layer: above;
        }

        #approval_overlay.visible { display: block; }
        #approval_summary { height: auto; }
        #approval-reason { height: 3; margin: 1 0 0 0; }
        #approval_actions { height: auto; margin: 1 0 0 0; }
        #approval_actions Button { margin: 0 1 0 0; }

        #evidence_overlay {
            display: none;
            position: absolute;
            offset: 8% 8%;
            width: 84%;
            height: 78%;
            padding: 1 2;
            layer: above;
            layout: vertical;
        }
        #evidence_overlay.visible { display: block; }
        #evidence_header { height: auto; }
        #evidence_body { height: 1fr; min-height: 3; overflow-y: auto; }
        #evidence_content { height: auto; padding: 0 1; }
        #evidence_actions { height: auto; margin: 1 0 0 0; }
        #evidence_actions Button { margin: 0 1 0 0; }

        """

        BINDINGS = [
            ("ctrl+c", "abort", "Abort"),
            ("ctrl+l", "clear_stream", "Clear"),
            ("ctrl+q", "quit", "Quit"),
            Binding("ctrl+z", "toggle_cooperation", "Toggle approval", priority=True),
            Binding("alt+a", "approve_tool", "Approve pending tool", priority=True),
            Binding("alt+r", "reject_tool", "Reject pending tool", priority=True),
            ("ctrl+s", "toggle_subagents", "Toggle subagents"),
            Binding("ctrl+o", "toggle_bash_show", "Toggle details output", priority=True),
            Binding("ctrl+r", "cycle_retry_mode", "Cycle retry mode", priority=True),
            Binding("ctrl+k", "show_model_picker", "Model picker", priority=True),
            Binding("ctrl+left", "decrease_temperature", "Decrease temperature", priority=True),
            Binding("ctrl+right", "increase_temperature", "Increase temperature", priority=True),
            # Ctrl+Shift+V is intentionally unbound: terminals turn it into a
            # bracketed events.Paste event, which _CommandTextArea consumes.
            Binding("ctrl+alt+v", "paste_image", "Paste image from clipboard", priority=True),
            ("ctrl+f1", "help", "Help"),
            # Keep this non-priority: the CommandPalette must retain Esc to
            # close itself while this action closes one's shortcuts overlay.
            ("escape", "close_shortcuts", "Close shortcuts"),
        ]

        def __init__(
            self, session: Any, options: dict[str, Any] | None = None, runtime_host: Any | None = None
        ) -> None:
            super().__init__()
            self.session = session
            self.options = options or {}
            self.runtime_host = runtime_host
            self._theme = resolve_tui_theme(
                self.options.get("theme") or (
                    str(session.snapshot["sidebar"].get("theme", "default"))
                    if isinstance(session, TuiSessionView) else getattr(session.settings_manager, "get_theme", lambda: "default")()
                )
            )
            self._off_listener = None
            self._assistant_stream = ""
            self._stream_lines: list[str] = []
            # Logical IDs survive identical/repeated text and make the render
            # cache independent of list position.  _trim_stream repairs this
            # parallel projection for a few legacy test/direct append paths.
            self._stream_line_ids: list[int] = []
            self._stream_line_values: list[str] = []
            self._next_stream_line_id = 0
            self._stream_content_cache: dict[tuple[int, str, int, str, tuple[tuple[int, str], ...]], Content] = {}
            self._stream_cache_width = -1
            self._stream_cache_theme = self._theme.name
            self._stream_cache_hits = 0
            self._stream_cache_misses = 0
            self._rendered_stream_lines: tuple[str, ...] = ()
            # Stream updates may arrive faster than Textual completes a layout
            # pass. Keep one deferred viewport reconciliation, rather than one
            # scroll_end callback per update.
            self._stream_render_generation = 0
            self._deferred_stream_viewport: dict[str, Any] | None = None
            self._stream_viewport_callback_pending = False
            self._stream_viewport_callback_token = 0
            # Number of logical front entries removed since the last rendered
            # stream. This is written only by _trim_stream; never infer it by
            # comparing transcript text (which may legitimately repeat).
            self._stream_front_trim_pending = 0
            self._stream_user_scroll_generation = 0
            self._retry_state = "idle"
            self._turn_timing_id: str | None = None
            self._turn_provider_ids: set[str] = set()
            self._turn_provider_elapsed_ms = 0.0
            self._turn_provider_measurements = 0
            self._turn_timing_reported = False
            self._last_auto_copied = ""
            self._assistant_has_live_delta = False
            self._assistant_live_start_idx = -1
            self._assistant_live_buffer = ""
            self._assistant_live_buffer_bytes = 0
            self._assistant_live_line_count = 0
            self._assistant_live_lines_dirty = False
            self._assistant_live_finalize_pending = False
            # Completed assistant prose is a separately paged virtual block.
            # It is deliberately outside the 500-entry live/history buffer.
            self._completed_virtuals: dict[str, dict[str, Any]] = {}
            self._completed_page_inflight = 0
            self._selection_copy_generation = 0
            # Providers can produce both text and thinking deltas much faster
            # than Textual can paint. Keep *all* session events in one ordered
            # accumulator: delta runs are painted at a bounded cadence and a
            # lifecycle event is a barrier that flushes everything before it.
            # The listener may be called from a provider thread, so it must not
            # touch Textual widgets or post one message per token.
            self._pending_ui_events: list[dict[str, Any]] = []
            # Legacy direct sessions cannot participate in the facade readiness
            # protocol. Keep their overflow off the emitting callback and move
            # it into the bounded accumulator from a later UI drain. This is a
            # compatibility/test-only lane: it is hard-capped and raises rather
            # than silently losing lifecycle, approval, or error events.
            self._legacy_deferred_ui_events: deque[dict[str, Any]] = deque()
            self._legacy_deferred_ui_event_bytes = 0
            # This lock protects the final bounded ingress after the facade; it
            # is never waited on from a Textual callback.
            self._pending_ui_events_lock = threading.Condition()
            self._pending_ui_event_bytes = 0
            # One coalesced flush wakeup may be queued at a time; the 30 Hz
            # interval remains a safety net if no wakeup can be posted.
            self._ui_flush_wakeup_pending = False
            # Event reduction owns model updates; rendering is deferred to the
            # frame scheduler so one lifecycle event cannot rebuild a large
            # transcript inside a bounded reduction slice.
            self._ui_drain_active = False
            self._session_listener_generation = 0
            self._bound_session: Any | None = None
            # Git is strictly a presentation adornment.  Its subprocess probe
            # must never run on Textual's loop, and its result is useful only
            # for the cwd/session generation that requested it.
            self._git_branch_cache: dict[str, tuple[float, str | None]] = {}
            self._git_branch_inflight: set[str] = set()
            self._sidebar_generation = 0
            self._sidebar_cwd = ""
            # Lifecycle reductions can arrive in a tight ordered batch. The
            # sidebar builds several Rich Text blocks and triggers layout, so
            # keep that noncritical work out of the bounded reduction slice.
            self._sidebar_refresh_dirty = False
            self._sidebar_refresh_timer: Any = None
            self._sidebar_next_paint = 0.0
            self._is_unmounting = False
            # Compatibility observability for the existing direct TUI tests.
            # The ordered event list above remains the sole rendering source.
            self._pending_assistant_deltas = ""
            self._pending_assistant_deltas_lock = self._pending_ui_events_lock
            self._assistant_stream_open = False
            self._active_tool_block: tuple[str, int, int, str] | None = None
            # The current parent tool call is the sole association point for
            # subagent_start.  Tokens never contain child IDs or task text.
            self._active_tool_call_id: int | None = None
            self._next_tool_call_id = 0
            self._active_subagent_link_tokens: list[int] = []
            self._turn_active = False
            self._last_delta_ts = 0.0
            self._stream_render_dirty = False
            self._stream_render_timer: Any = None
            # The token is set before registering the callback. Textual may run
            # a very-short timer before ``set_timer`` returns; without it, the
            # returned, already-fired Timer can overwrite the callback's
            # ``None`` and permanently suppress subsequent dirty frames.
            self._stream_render_timer_token: object | None = None
            self._stream_next_paint = 0.0
            # Distinguish a newly requested frame from an identical transcript.
            # In particular, a spinner can return to the same glyph while a
            # timer is pending; tuple equality alone must not make that frame
            # consume the dirty state without updating the widget.
            self._stream_render_revision = 0
            self._rendered_stream_revision = -1
            self._thinking_frame = 0
            self._thinking_active = False
            self._thinking_label_shown = False
            self._thinking_buffer: str = ""
            self._thinking_line_idx: int | None = None
            self._thinking_label_idx: int | None = None
            self._command_history: list[str] = []
            self._approval_pending: dict[str, Any] | None = None
            self._approval_waiters: deque[dict[str, Any]] = deque()
            self._cooperation_disabled = False
            self._approval_rebinding = False
            self._extension_ui_pending_request: dict[str, Any] | None = None
            self._login_pending: dict[str, Any] | None = None
            self._ask_user_pending: dict[str, Any] | None = None
            self._pending_clipboard_image_bytes: bytes | None = None
            self._session_delete_pending: SessionInfo | None = None
            self._info_layout = (
                str(session.snapshot["sidebar"].get("infoPanel", "top"))
                if isinstance(session, TuiSessionView) else getattr(session.settings_manager, "get_tui_info_panel", lambda: "top")()
            )
            # The layout mode owns information-panel visibility. The persisted
            # setting only selects its initial mode.
            self._layout_mode = "wide" if self._info_layout == "sidebar" else "compact"
            self._model_picker_models: list[ModelInfo] = []
            # Numbered /sessions targets must keep referring to the list the
            # user saw, rather than to a newly mtime-sorted filesystem scan.
            self._session_list_snapshot: list[SessionInfo] | None = None
            # Monitor notifications are deliberately only invalidations.  The UI
            # reads one copied snapshot at most ten times a second rather than
            # accepting one queued message per child token.
            self._off_monitor_listener: Any = None
            self._monitor_generation = 0
            self._monitor_revision = -1
            self._monitor_snapshot: dict[str, Any] = {"revision": 0, "children": []}
            self._monitor_dirty = False
            self._monitor_dirty_lock = threading.Lock()
            self._monitor_refresh_inflight = False
            self._status_refresh_inflight = False
            self._status_refresh_dirty = False
            self._selected_agent_id: str | None = None
            self._agents_panel_open = False
            # A link token is generated only for an application-created stream
            # label. It is never inferred from transcript text.
            self._agent_link_tokens: dict[int, tuple[int, str, int, int]] = {}
            self._agent_link_labels: dict[int, str] = {}
            self._agent_links_by_line: dict[int, tuple[int, ...]] = {}
            self._next_agent_link_token = 0
            self._evidence_link_tokens: dict[int, tuple[int, str, int, int]] = {}
            self._evidence_links_by_line: dict[int, tuple[int, ...]] = {}
            # Evidence actions are attached to immutable retained line IDs, not
            # mutable list offsets.  Tool completion and front trimming can
            # splice the display list after a link was created.
            self._evidence_link_line_ids: dict[int, int] = {}
            # Render-time evidence lookup must be by immutable line identity;
            # scanning every token for every retained line made long tool runs
            # quadratic in the number of links.
            self._evidence_tokens_by_line_id: dict[int, tuple[int, ...]] = {}
            self._next_evidence_link_token = 0
            self._evidence_panel: dict[str, Any] | None = None
            # Increment before scheduling so stale coroutines cannot reopen a
            # closed panel or paint a response from a prior session epoch.
            self._evidence_request_generation = 0

        def compose(self) -> ComposeResult:
            with Horizontal(id="root"):
                with Vertical(id="main"):
                    yield Static(id="header")
                    with Vertical(id="conversation"):
                        with VerticalScroll(id="stream_container"):
                            yield Static("", id="logo")
                            yield TranscriptWidget("", id="stream")
                        yield Static("", id="waiting_status")
                    yield Static("", id="ext_panel")
                    yield Static("", id="ext_overlay")
                    yield Static("", id="shortcuts_overlay")
                    yield Static("", id="model_overlay")
                    with Vertical(id="evidence_overlay"):
                        yield Static("", id="evidence_header")
                        with VerticalScroll(id="evidence_body"):
                            yield Static("", id="evidence_content")
                        with Horizontal(id="evidence_actions"):
                            yield Button("Previous page", id="evidence-prev")
                            yield Button("Next page", id="evidence-next")
                            yield Button("Close", id="evidence-close")
                    with Vertical(id="agents_overlay"):
                        yield Static("", id="agents_help")
                        with VerticalScroll(id="agents_timeline"):
                            yield Static("", id="agents_content")
                    with Vertical(id="approval_overlay"):
                        yield Static("", id="approval_summary")
                        yield _ApprovalReasonInput(
                            placeholder="Optional rejection reason", max_length=1024, id="approval-reason"
                        )
                        with Horizontal(id="approval_actions"):
                            yield Button("Approve (Alt+A)", id="approval-approve", variant="success")
                            yield Button("Reject (Alt+R)", id="approval-reject", variant="error")
                    yield _CommandTextArea(placeholder="Type a command or /help", id="input", soft_wrap=True)
                yield Static(id="sidebar")

        def get_system_commands(self, screen: Any) -> list[tuple[str, str, Any, bool]]:  # noqa: ARG002
            """Expose only current one commands in Textual's Ctrl+P palette.

            Textual's default Theme and Keys commands use its own theme registry
            and merged widget bindings, both of which differ from one.
            """
            commands: list[tuple[str, str, Any, bool]] = [
                ("Show one keyboard shortcuts", "Show the current one TUI shortcuts", self.action_show_shortcuts, True),
                ("Show one slash-command help", "Show the current slash-command reference", self.action_help, True),
                ("Clear conversation stream", "Clear the visible conversation stream", self.action_clear_stream, True),
                ("Toggle cooperation", "Require approval for mutating tools", self.action_toggle_cooperation, True),
                ("Toggle subagents", "Enable or disable subagent tools", self.action_toggle_subagents, True),
                ("Show current-session agents", "Inspect direct child agents", self.action_show_agents, True),
                ("Toggle details output", "Show or hide detailed tool output", self.action_toggle_bash_show, True),
                ("Quit one", "Quit the application", self.exit, True),
            ]
            for theme_name in sorted(BUILTIN_TUI_THEMES):
                commands.append(
                    (
                        f"Theme: {theme_name}",
                        f"Set the one theme to {theme_name}",
                        lambda name=theme_name: self._set_theme(name),
                        True,
                    )
                )
            return commands

        def on_mount(self) -> None:
            self.query_one("#input", TextArea).focus()
            # Register every one TUI palette as a Textual Theme so the built-in
            # CommandPalette (Ctrl+P) and other widgets pick up the same colours
            # via CSS variables ($foreground, $background, $surface, $panel,
            # $block-cursor-*, $border, …).
            for textual_theme in _TEXTUAL_THEME_MAP.values():
                self.register_theme(textual_theme)
            self.theme = f"one-{self._theme.name}"
            self._apply_theme(self._theme.name)
            # Drive the "waiting for the model" spinner while a turn is in
            # flight but no assistant text is currently streaming.
            self.set_interval(0.15, self._tick_waiting)
            self.set_interval(1 / 30, self._flush_pending_ui_events)
            self.set_interval(0.1, self._flush_monitor_refresh)
            self._bind_session()
            self._refresh_sidebar()
            self._apply_information_layout()
            self._refresh_logo()

        def _bind_session(self) -> None:
            """(Re)subscribe to session events.

            Used on mount and after /fork, which swaps the underlying session
            for a fresh one — the old listener must be detached first.
            """
            # Initial and same-session binds preserve any active approval. A
            # changed session must fail closed before listener detachment.
            self._invalidate_deferred_stream_viewport()
            self._close_evidence_panel(focus_input=False)
            self._stream_front_trim_pending = 0
            if self._bound_session is not None and self._bound_session is not self.session:
                self._reject_all_approvals("Session changed")
            # Invalidate before detaching and discard the old transcript's
            # accepted events. A callback already in flight from the old
            # session must not append after this boundary.
            with self._pending_ui_events_lock:
                self._session_listener_generation += 1
                listener_generation = self._session_listener_generation
                self._pending_ui_events = []
                self._legacy_deferred_ui_events.clear()
                self._legacy_deferred_ui_event_bytes = 0
                self._pending_ui_event_bytes = 0
                self._pending_ui_events_lock.notify_all()
                self._pending_assistant_deltas = ""
                self._ui_flush_wakeup_pending = False
                self._last_delta_ts = 0.0
            self._sidebar_generation += 1
            self._sidebar_cwd = ""
            if callable(self._off_listener):
                try:
                    self._off_listener()
                except Exception:
                    pass
                self._off_listener = None
            self._bind_subagent_monitor()

            def _listener(event: dict[str, Any]) -> None:
                # Preserve the session's event order, including the ordering
                # between thinking and ordinary output. Lifecycle events are
                # processed as barriers by _flush_pending_ui_events.
                wake_ui = False
                with self._pending_ui_events_lock:
                    if listener_generation != self._session_listener_generation:
                        return
                    event_size = self._ui_event_size(event)
                    # Runtime facade DTOs are already capped.  A legacy direct
                    # session is still required to use this bound; an oversized
                    # event is held upstream rather than silently truncated.
                    if event_size > _UI_EVENT_MAX_BYTES:
                        event = self._reduce_oversized_ui_event(event)
                        event_size = self._ui_event_size(event)
                    if self._legacy_deferred_ui_events:
                        # Do not let a newer direct event pass deferred output.
                        self._defer_legacy_ui_event_locked(event, event_size)
                    elif self._try_coalesce_ui_delta_locked(event, event_size):
                        pass
                    else:
                        # The detached facade checks can_accept_event before
                        # dispatching.  Direct legacy delivery must also never
                        # wait on Textual's loop; retain an event for a later
                        # UI drain rather than blocking or dropping it. The
                        # compatibility lane has a hard cap and explicit
                        # BufferError signal on pressure.
                        if not self._can_accept_ui_event_locked(event, event_size):
                            self._defer_legacy_ui_event_locked(event, event_size)
                        else:
                            self._pending_ui_events.append(event)
                            self._pending_ui_event_bytes += event_size
                    if (
                        event.get("type") == "message_update"
                        and event.get("assistantMessageEvent", {}).get("type") == "text_delta"
                    ):
                        self._pending_assistant_deltas += str(event.get("assistantMessageEvent", {}).get("delta", ""))
                    # Reasoning is useful transcript content, but it must not
                    # suppress the independent waiting/abort footer.  Only
                    # visible assistant prose counts as output activity.
                    if (
                        event.get("type") == "message_update"
                        and event.get("assistantMessageEvent", {}).get("type") == "text_delta"
                        and event.get("assistantMessageEvent", {}).get("delta")
                    ):
                        self._last_delta_ts = time.monotonic()
                    if self.is_running and not self._ui_flush_wakeup_pending:
                        self._ui_flush_wakeup_pending = True
                        wake_ui = True
                if wake_ui:
                    # A coalesced private UI wakeup; session payloads stay in
                    # the ordered accumulator and no token adds a message.
                    try:
                        self.post_message(SessionEvent({"type": "_flush_pending_ui_events"}))
                    except Exception:
                        try:
                            self.call_from_thread(
                                self.post_message,
                                SessionEvent({"type": "_flush_pending_ui_events"}),
                            )
                        except Exception:
                            # The timer remains a safe fallback; permit a
                            # later lifecycle event to attempt a wakeup again.
                            with self._pending_ui_events_lock:
                                self._ui_flush_wakeup_pending = False

            _listener.can_accept_event = self._can_accept_ui_event  # type: ignore[attr-defined]

            self._off_listener = self.session.subscribe(_listener)
            self._bound_session = self.session
            self._approval_rebinding = False
            # Approval callbacks and their Futures belong to the runtime
            # worker.  A detached view receives an ID-bound request event and
            # returns a command; never install a legacy callback on it.
            if not isinstance(self.session, TuiSessionView) and (
                self.session.approval_callback is None
                and getattr(self.session.settings_manager, "get_tool_approval", lambda: False)()
            ):
                self._install_approval_callback(self.session)

        def _prepare_session_change(self) -> None:
            """Fail closed while a runtime host replaces the bound session."""
            self._approval_rebinding = True
            self._reject_all_approvals("Session changed")

        def _bind_subagent_monitor(self) -> None:
            """Bind the current parent's monitor before taking its snapshot."""
            self._monitor_generation += 1
            generation = self._monitor_generation
            if callable(self._off_monitor_listener):
                try:
                    self._off_monitor_listener()
                except Exception:
                    pass
            self._off_monitor_listener = None
            self._monitor_revision = -1
            self._monitor_snapshot = {"revision": 0, "children": []}
            self._selected_agent_id = None
            self._agents_panel_open = False
            self._clear_agent_link_metadata()
            self._evidence_link_tokens.clear()
            self._evidence_links_by_line.clear()
            self._evidence_link_line_ids.clear()
            self._evidence_tokens_by_line_id.clear()
            with self._monitor_dirty_lock:
                self._monitor_dirty = False
            if isinstance(self.session, TuiSessionView):
                self._accept_monitor_snapshot(self.session.monitor_snapshot, generation)
                return
            monitor = getattr(self.session, "subagent_monitor", None)
            if monitor is None:
                return

            def invalidated(_notice: dict[str, Any]) -> None:
                # This may run in a provider thread. Do not touch widgets or
                # carry preview text across this boundary.
                if generation != self._monitor_generation:
                    return
                with self._monitor_dirty_lock:
                    self._monitor_dirty = True

            try:
                # Subscribe first: a change between this call and snapshot()
                # leaves the dirty flag set and is recovered on the next tick.
                self._off_monitor_listener = monitor.subscribe(invalidated)
                self._accept_monitor_snapshot(monitor.snapshot(), generation)
            except Exception:
                self._off_monitor_listener = None

        def _accept_monitor_snapshot(self, snapshot: dict[str, Any], generation: int) -> bool:
            if generation != self._monitor_generation:
                return False
            revision = snapshot.get("revision")
            if not isinstance(revision, int) or revision < self._monitor_revision:
                return False
            children = snapshot.get("children")
            if not isinstance(children, list):
                return False
            self._monitor_revision = revision
            self._monitor_snapshot = {"revision": revision, "children": children}
            return True

        def _flush_monitor_refresh(self) -> None:
            with self._monitor_dirty_lock:
                if not self._monitor_dirty:
                    return
                if isinstance(self.session, TuiSessionView) and self._monitor_refresh_inflight:
                    return
                # Keep dirty until the response is accepted. An invalidation
                # during the await leaves it true and schedules one retry.
                self._monitor_dirty = False
            if not isinstance(self.session, TuiSessionView):
                monitor = getattr(self.session, "subagent_monitor", None)
                if monitor is None:
                    return
                try:
                    changed = self._accept_monitor_snapshot(monitor.snapshot(), self._monitor_generation)
                except Exception:
                    return
                if changed:
                    self._schedule_sidebar_refresh()
                    self._render_agents_panel()
                return
            with self._monitor_dirty_lock:
                self._monitor_refresh_inflight = True
            if isinstance(self.session, TuiSessionView):
                session = self.session
                epoch = session.epoch
                generation = self._monitor_generation

                async def refresh() -> None:
                    try:
                        result = await session.refresh_monitor()
                        # A delayed detached snapshot must not repaint a newly
                        # bound session/epoch after /new, /sessions, or /fork.
                        if (
                            self.session is session
                            and generation == self._monitor_generation
                            and epoch == session.epoch
                            and result.get("epoch") == epoch
                        ):
                            session.apply_monitor_snapshot(result)
                            if self._accept_monitor_snapshot(session.monitor_snapshot, generation):
                                self._schedule_sidebar_refresh()
                                self._render_agents_panel()
                    except Exception as exc:
                        self._write(f"[Monitor] {exc}", "error")
                    finally:
                        with self._monitor_dirty_lock:
                            self._monitor_refresh_inflight = False
                asyncio.create_task(refresh())
                return

        def _refresh_monitor_snapshot(self) -> bool:
            """Synchronously obtain the current bound monitor snapshot."""
            if isinstance(self.session, TuiSessionView):
                self._accept_monitor_snapshot(self.session.monitor_snapshot, self._monitor_generation)
                return True
            monitor = getattr(self.session, "subagent_monitor", None)
            if monitor is None:
                return False
            try:
                return self._accept_monitor_snapshot(monitor.snapshot(), self._monitor_generation)
            except Exception:
                return False

        def _rebuild_session_transcript(self) -> None:
            """Replace the viewport cache with the loaded durable transcript."""
            self._stream_lines = []
            self._stream_line_ids = []
            self._stream_line_values = []
            self._stream_content_cache.clear()
            self._stream_front_trim_pending = 0
            self._clear_agent_link_metadata()
            self._reset_turn_timing()
            self._rendered_stream_lines = ()
            self._assistant_stream = ""
            self._assistant_stream_open = False
            self._assistant_has_live_delta = False
            self._assistant_live_start_idx = -1
            self._assistant_live_buffer = ""
            self._assistant_live_line_count = 0
            self._completed_virtuals = {}
            self._active_tool_block = None
            self._remove_thinking_line()
            for message in self.session.messages:
                role = message.get("role")
                content = message.get("content", "")
                if isinstance(content, list):
                    content = "".join(str(part.get("text", "")) for part in content if part.get("type") == "text")
                historical_abort = (
                    role == "assistant" and message.get("stopReason") == "abort" and content == "Request aborted."
                )
                # An abort is persisted so the provider history remains
                # complete.  On reload it is not a live abort, though: render
                # it as historical status rather than replaying the live
                # assistant wording that prompted the session switch.
                if historical_abort:
                    self._write("Previous request was aborted.", "info")
                if role in {"user", "assistant"}:
                    if not historical_abort:
                        # Native tool-call carrier messages are durable provider
                        # context, not user-visible assistant prose.  They often
                        # have no text and used to render a transient [empty]
                        # panel before the terminal finish summary on restore.
                        if (
                            role == "assistant"
                            and isinstance(message.get("_nativeToolCalls"), list)
                            and not str(content).strip()
                        ):
                            continue
                        completed_ref = message.get("completedRef")
                        if role == "assistant" and isinstance(completed_ref, str) and isinstance(self.session, TuiSessionView):
                            self._open_completed_virtual(completed_ref)
                            continue
                        self._write_chat_block(role, str(content))
                elif role == "toolResult":
                    # Durable tool-result content is a JSON envelope. Render
                    # its visible projection, not the envelope's raw fields.
                    display_content = str(content)
                    try:
                        payload = json.loads(display_content)
                    except (TypeError, ValueError):
                        payload = None
                    if isinstance(payload, dict):
                        value = payload.get("outputText", payload.get("error", payload.get("result", display_content)))
                        if isinstance(value, str):
                            display_content = value
                    self._write_result_block(display_content)
                    evidence_id = message.get("evidenceId")
                    if isinstance(evidence_id, str):
                        self._write_evidence_link(evidence_id, unavailable=bool(message.get("evidenceUnavailable")))
                elif role == "custom":
                    self._write_chat_block("custom", str(content))
                elif role == "bashExecution":
                    # Local-only history: rebuilding never executes a command
                    # or reads a referenced raw-output file.
                    self._write_manual_command_block(str(message.get("command", "")))
                    output = str(message.get("output", ""))
                    if isinstance(self.session, TuiSessionView):
                        show_bash = bool(self.session.snapshot["sidebar"].get("bashOutput", True))
                    else:
                        show_bash = bool(getattr(self.session.settings_manager, "get_bash_show_output", lambda: True)())
                    if not show_bash:
                        output = ""
                    self._write_manual_result(
                        output,
                        exit_code=message.get("exitCode"),
                        timed_out=bool(message.get("timedOut", False)),
                        cancelled=bool(message.get("cancelled", False)),
                        truncated=bool(message.get("truncated", False)),
                        full_output_path=message.get("fullOutputPath"),
                        stored=True,
                    )
            self._schedule_stream_render()
            # Rebuild is a new transcript boundary, not an incremental trim
            # relative to the previously mounted session.
            self._stream_front_trim_pending = 0

        def _session_infos(self) -> list[SessionInfo]:
            if not isinstance(self.session, TuiSessionView):
                manager = self.session.session_manager
                return SessionManager.list(manager.cwd, manager.session_dir)
            # The worker is authoritative.  A numbered target intentionally
            # resolves only against the most recent detached list DTO.
            return list(self._session_list_snapshot or [])

        def _invalidate_session_list_snapshot(self) -> None:
            self._session_list_snapshot = None

        def _resolve_session_target(self, target: str) -> tuple[SessionInfo | None, str | None]:
            infos = self._session_infos()
            target = target.strip()
            matches = [info for info in infos if info.name == target]
            if matches:
                if len(matches) != 1:
                    return None, "Session name is ambiguous; use its number from /sessions."
                return matches[0], None
            if target.isdigit():
                index = int(target)
                numbered_infos = self._session_list_snapshot if self._session_list_snapshot is not None else infos
                if not 1 <= index <= len(numbered_infos):
                    return None, f"Session number out of range (1..{len(numbered_infos)})"
                selected = numbered_infos[index - 1]
                # Do not open a replacement that happens to have acquired the
                # cached path. A stale numbered target must be explicit.
                live = next((info for info in infos if info.path == selected.path and info.id == selected.id), None)
                if live is None:
                    return None, "Session from the last /sessions list is no longer available. Run /sessions again."
                return live, None
            return None, "No session has that exact name. Use /sessions to list sessions."

        def _parse_session_rename_target(self, rename_rest: str) -> tuple[str | None, str | None, str | None]:
            """Return a target and replacement, rejecting overlapping-name ambiguity."""
            target, separator, new_name = rename_rest.partition(" ")
            if not separator or not new_name.strip():
                return None, None, "Usage: /sessions rename <number|full exact name> <new name>"
            if target.isdigit():
                return target, new_name, None

            names = {
                info.name for info in self._session_infos() if info.name and rename_rest.startswith(info.name + " ")
            }
            if not names:
                return target, new_name, None
            if len(names) != 1:
                return None, None, "Session rename target is ambiguous; use its number from /sessions."
            name = names.pop()
            return name, rename_rest[len(name) + 1 :], None

        async def _list_sessions(self) -> None:
            if isinstance(self.session, TuiSessionView):
                result = await self.session.command("session_list")
                infos = []
                for item in result.get("sessions", []):
                    if not isinstance(item, dict):
                        continue
                    try:
                        infos.append(
                            SessionInfo(
                                path=str(item["path"]),
                                id=str(item["id"]),
                                cwd=str(item.get("cwd", "")),
                                name=item.get("name") if isinstance(item.get("name"), str) else None,
                                modified=datetime.fromisoformat(str(item["modified"])),
                                created=datetime.fromisoformat(str(item["modified"])),
                                message_count=int(item.get("messageCount", 0)),
                            )
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
            else:
                infos = self._session_infos()
            self._session_list_snapshot = list(infos)
            if not infos:
                self._write("No persisted sessions in this project.", "info")
                return
            now = time.time()
            lines = ["Sessions (newest first):"]
            for index, info in enumerate(infos, 1):
                seconds = max(0, int(now - info.modified.timestamp()))
                age = (
                    f"{seconds // 86400}d ago"
                    if seconds >= 86400
                    else f"{seconds // 3600}h ago"
                    if seconds >= 3600
                    else f"{seconds // 60}m ago"
                )
                name = info.name or "(unnamed)"
                active = " *" if info.id == self.session.session_id else ""
                lines.append(f"{index}. {name} — {age}, {info.message_count} messages{active}")
            lines.append("Load: /sessions <number|full exact name>")
            self._write("\n".join(lines), "info")

        async def _switch_to_session(self, info: SessionInfo) -> None:
            if self.session.is_streaming:
                self._write("Stop or wait for the active turn before loading a session.", "error")
                return
            if self.runtime_host is None:
                self._write("Runtime host unavailable: session loading is not supported in this context.", "error")
                return
            self._prepare_session_change()
            if isinstance(self.session, TuiSessionView):
                await self.session.command("switch_session", path=info.path, id=info.id)
            else:
                await self.runtime_host.switch_session(info.path)
                self.session = self.runtime_host.session
            self._bind_session()
            self._invalidate_session_list_snapshot()
            self._clear_extension_ui_state()
            self._rebuild_session_transcript()
            self._write(f"Loaded session: {info.name or '(unnamed)'}.", "info")
            self._refresh_sidebar()

        def on_unmount(self) -> None:
            self._approval_rebinding = True
            self._is_unmounting = True
            self._reject_all_approvals("TUI closed")
            # Release a producer currently backpressured at the final ingress
            # boundary. The detached listener observes the generation change and
            # returns without retaining post-close UI state.
            with self._pending_ui_events_lock:
                self._session_listener_generation += 1
                self._pending_ui_events = []
                self._legacy_deferred_ui_events.clear()
                self._legacy_deferred_ui_event_bytes = 0
                self._pending_ui_event_bytes = 0
                self._pending_ui_events_lock.notify_all()
            if callable(self._off_listener):
                try:
                    self._off_listener()
                except Exception:
                    pass
                self._off_listener = None
            if callable(self._off_monitor_listener):
                try:
                    self._off_monitor_listener()
                except Exception:
                    pass
                self._off_monitor_listener = None

        def _schedule_stream_render(self) -> None:
            """Paint transcript mutations no more than once per mounted frame.

            The synchronous path is intentionally limited to pre-mount/test
            initialization, where no Textual paint loop exists yet.
            """
            self._stream_render_revision += 1
            if not self.is_running:
                self._render_stream()
                return
            self._stream_render_dirty = True
            now = time.monotonic()
            if self._stream_render_timer is not None:
                return
            # Textual timers cannot use a zero interval (it divides by the
            # interval internally); one millisecond still schedules the next
            # UI turn while preserving the frame cap.
            delay = max(0.001, self._stream_next_paint - now)
            timer_token = object()
            self._stream_render_timer_token = timer_token
            timer = self.set_timer(delay, lambda: self._paint_scheduled_stream(timer_token))
            # A sub-millisecond/same-turn timer may have completed before
            # set_timer returned. Do not resurrect its stale handle.
            if self._stream_render_timer_token is timer_token:
                self._stream_render_timer = timer

        def _schedule_sidebar_refresh(self) -> None:
            """Coalesce sidebar/layout work to one frame after lifecycle reduction."""
            if self._is_unmounting:
                return
            if isinstance(self.session, TuiSessionView):
                self._request_status_refresh()
            if not self.is_running:
                self._refresh_sidebar()
                return
            self._sidebar_refresh_dirty = True
            if self._sidebar_refresh_timer is not None:
                return
            delay = max(0.001, self._sidebar_next_paint - time.monotonic())
            self._sidebar_refresh_timer = self.set_timer(delay, self._paint_scheduled_sidebar)

        def _request_status_refresh(self) -> None:
            """Coalesce cheap status commands without delaying input controls."""
            self._status_refresh_dirty = True
            if self._status_refresh_inflight:
                return
            session = self.session
            generation = self._session_listener_generation
            epoch = session.epoch
            self._status_refresh_inflight = True
            self._status_refresh_dirty = False

            async def refresh() -> None:
                try:
                    result = await session.refresh_status()
                    if (
                        self.session is session
                        and generation == self._session_listener_generation
                        and epoch == session.epoch
                        and result.get("epoch") == epoch
                    ):
                        session.apply_status_snapshot(result)
                        # Paint the accepted projection now. Do not schedule
                        # through _schedule_sidebar_refresh here: that would
                        # request another status snapshot forever.
                        self._refresh_sidebar()
                except Exception:
                    pass
                finally:
                    self._status_refresh_inflight = False
                    if self._status_refresh_dirty and not self._is_unmounting:
                        self._request_status_refresh()

            asyncio.create_task(refresh())

        def _paint_scheduled_sidebar(self) -> None:
            self._sidebar_refresh_timer = None
            if self._is_unmounting:
                self._sidebar_refresh_dirty = False
                return
            if not self._sidebar_refresh_dirty:
                return
            self._sidebar_refresh_dirty = False
            self._sidebar_next_paint = time.monotonic() + _UI_FRAME_SECONDS
            self._refresh_sidebar()

        def _paint_scheduled_stream(self, timer_token: object | None = None) -> None:
            if timer_token is not None and timer_token is not self._stream_render_timer_token:
                return
            self._stream_render_timer = None
            self._stream_render_timer_token = None
            if not self._stream_render_dirty:
                return
            self._stream_render_dirty = False
            self._stream_next_paint = time.monotonic() + _UI_FRAME_SECONDS
            self._render_stream()

        def _render_stream(self) -> None:
            if self._ui_drain_active:
                # ``_schedule_stream_render`` renders synchronously before the
                # app has mounted. Mark it dirty instead of recursively calling
                # back into this guard; the next mounted frame will paint it.
                self._stream_render_dirty = True
                if self.is_running and self._stream_render_timer is None:
                    self._schedule_stream_render()
                return
            self._materialize_assistant_live_lines()
            # A stream update can be requested by several lifecycle paths in a
            # frame. Rebuilding identical Rich Text is pure wasted work and can
            # invalidate a mouse selection while output is still streaming.
            try:
                stream_widget = self.query_one("#stream", TranscriptWidget)
            except Exception:
                return
            # ``region.width`` can oscillate as VerticalScroll adds/removes its
            # scrollbar while a completed virtual block changes from its
            # provisional one-row height. Use TranscriptWidget's committed wrap
            # width so the page geometry and transcript offsets share one
            # stable width through that layout transition.
            width = stream_widget._width
            cache_context_changed = width != self._stream_cache_width or self._stream_cache_theme != self._theme.name
            if (
                tuple(self._stream_lines) == self._rendered_stream_lines
                and self._rendered_stream_revision == self._stream_render_revision
                and not cache_context_changed
                and not stream_widget.needs_reflow
            ):
                return
            try:
                stream_container = self.query_one("#stream_container", VerticalScroll)
            except Exception:
                return
            if cache_context_changed:
                self._stream_content_cache.clear()
                self._stream_cache_width = width
                self._stream_cache_theme = self._theme.name
                stream_widget.invalidate_wrap()
            self._sync_stream_line_ids()
            # Sample this before replacing the content.  Updating a Static can
            # change its wrapped height (and therefore max_scroll_y), so the
            # post-update position alone cannot tell whether a reader had
            # intentionally scrolled away from the tail.  A zero maximum is
            # the initial-layout/single-viewport case and should follow once
            # there is enough content to scroll.
            previous_scroll_y = stream_container.scroll_y
            previous_max_scroll_y = stream_container.max_scroll_y
            previous_scroll_target_y = stream_container.scroll_target_y
            sampled_follow_stream = (
                not self._rendered_stream_lines
                or previous_max_scroll_y <= 0
                or previous_scroll_y >= previous_max_scroll_y - _STREAM_FOLLOW_TOLERANCE
            )
            # A tail update already awaiting layout remains tail intent through
            # additional output. Its old max_scroll_y is no longer the tail
            # once content grows, so sampling only the current position would
            # accidentally lose follow before the one deferred callback runs.
            follow_stream = sampled_follow_stream or bool(
                self._deferred_stream_viewport and self._deferred_stream_viewport.get("follow")
            )
            # Numeric scroll position remains correct for ordinary appends. A
            # visual anchor is needed only when retained history was dropped
            # from the front (where that numeric row now names different text).
            front_trim_count = self._stream_front_trim_pending
            front_trimmed = front_trim_count > 0
            anchor = (
                self._capture_stream_anchor(stream_widget, stream_container)
                if not follow_stream and front_trimmed
                else None
            )
            # Textual requests only visible physical rows from TranscriptWidget.
            # In particular, do not append 500 Content fragments or Static.update
            # an aggregate visual on every token/lifecycle event.
            def content_for_line(line_index: int) -> Content:
                line = self._stream_lines[line_index]
                if line.startswith(_THINKING_MARK):
                    return Content.from_markup(line[len(_THINKING_MARK) :] + "\n")
                if line.startswith(_THINKING_TEXT_MARK):
                    return Content.from_markup(line[len(_THINKING_TEXT_MARK) :] + "\n")
                if line in {"Thinking:", "Thought:"}:
                    return Content.from_markup("[i]$line[/]\n", line=line)
                return self._cached_stream_content_line(line_index, line, width)

            line_revisions = [
                (
                    self._agent_links_by_line.get(index, ()),
                    self._evidence_tokens_by_line_id.get(line_id, ()),
                    self._theme.name,
                )
                for index, line_id in enumerate(self._stream_line_ids)
            ]
            virtual_blocks: dict[int, tuple[int, dict[int, str], Any]] = {}
            for index, line in enumerate(self._stream_lines):
                if not line.startswith("\0completed:"):
                    continue
                ref = line.removeprefix("\0completed:")
                virtual = self._completed_virtuals.get(ref)
                if virtual is not None:
                    if virtual["width"] != width:
                        virtual["request_generation"] += 1
                        virtual.update({"total": 1, "rows": {}, "pages": [], "pending": set(), "queued": set(), "error": None, "geometry_loaded": False})
                        virtual["width"] = width
                        # A completed placeholder can be outside the current
                        # viewport, so paint may never ask it for row zero.
                        # Prime the geometry/page at the actual widget width
                        # instead of relying on a demand paint that may not
                        # occur until the reader scrolls there.
                        self._need_completed_virtual_row(ref, 0)
                    virtual_blocks[index] = (virtual["total"], virtual["rows"], lambda row, ref=ref: self._need_completed_virtual_row(ref, row))
            stream_widget.set_transcript(
                self._stream_line_ids, self._stream_lines, line_revisions, content_for_line, virtual_blocks=virtual_blocks
            )
            self._rendered_stream_lines = tuple(self._stream_lines)
            self._rendered_stream_revision = self._stream_render_revision
            try:
                self._queue_stream_viewport_reconcile(
                    stream_container,
                    follow=follow_stream,
                    previous_scroll_y=previous_scroll_y,
                    previous_max_scroll_y=previous_max_scroll_y,
                    previous_scroll_target_y=previous_scroll_target_y,
                    anchor=anchor,
                    front_trim_count=front_trim_count,
                )
                self._stream_front_trim_pending = 0
            except Exception:
                pass

        def _check_resize(self) -> None:
            """Reflow after Textual has delivered its resize to the active screen."""
            super()._check_resize()
            # Physical selection coordinates name wrapped rows. A pending copy
            # must not reinterpret them after a terminal resize.
            self._selection_copy_generation += 1
            if self._stream_lines:
                try:
                    self.query_one("#stream", TranscriptWidget).invalidate_wrap()
                except Exception:
                    pass
                self.call_after_refresh(self._render_stream)

        def _need_completed_virtual_row(self, ref: str, row: int) -> None:
            """Schedule, never perform, the page request demanded by paint."""
            virtual = self._completed_virtuals.get(ref)
            if virtual is None or row in virtual["rows"]:
                return
            virtual["queued"].add(row)
            self._start_completed_page_requests(ref)

        def _start_completed_page_requests(self, ref: str) -> None:
            """Bound page work while retaining every demanded row for later fetch."""
            virtual = self._completed_virtuals.get(ref)
            if virtual is None or virtual.get("error") is not None:
                return
            while virtual["queued"] and len(virtual["pending"]) < 2 and self._completed_page_inflight < 4:
                # A single request consumes nearby demands through nextOffset;
                # rows stay queued until the returned range proves they arrived.
                available = {
                    row for row in virtual["queued"]
                    if not any(offset <= row < offset + 96 for offset in virtual["pending"])
                }
                if not available:
                    break
                offset = min(available)
                virtual["pending"].add(offset)
                self._completed_page_inflight += 1
                asyncio.create_task(self._fetch_completed_virtual_page(ref, offset, virtual["request_generation"]))

        async def _fetch_completed_virtual_page(self, ref: str, offset: int, request_generation: int | None = None) -> None:
            virtual = self._completed_virtuals.get(ref)
            if virtual is None or not isinstance(self.session, TuiSessionView):
                return
            generation = self._session_listener_generation
            ref = virtual["ref"]
            # ``-1`` was the old unknown-width sentinel. It is truthy, so the
            # former ``or`` fallback sent width -1 through the facade and the
            # host correctly rejected it. Keep every transport request valid.
            width = max(1, min(512, int(virtual.get("width", self._main_width()))))

            try:
                page = await self.session.assistant_page(ref, offset, 96, width)
                current = self._completed_virtuals.get(ref)
                if (
                    current is None or current["ref"] != ref or generation != self._session_listener_generation
                    or request_generation != current["request_generation"] or width != current["width"]
                    or page.get("epoch") != self.session.epoch or page.get("width") != width
                ):
                    return
                rows = page.get("rows")
                if not isinstance(rows, list) or not all(isinstance(row, str) for row in rows):
                    current["error"] = "unavailable"
                    return
                current["total"] = int(page.get("totalRows", 0))
                current["geometry_loaded"] = True
                current["rows"].update({offset + index: row for index, row in enumerate(rows)})
                next_offset = page.get("nextOffset")
                loaded_end = int(next_offset) if isinstance(next_offset, int) else offset + len(rows)
                current["queued"] = {row for row in current["queued"] if not offset <= row < loaded_end}
                current["pages"].append(offset)
                while len(current["pages"]) > 12:
                    old = current["pages"].pop(0)
                    for row in range(old, old + 96):
                        current["rows"].pop(row, None)
                # An explicit long copy owns a stable source snapshot. Do not
                # repaint each fetched page (which would demand viewport rows
                # again and churn the bounded display cache beneath the copy).
                if current.get("selection_copy") is None:
                    self._stream_render_revision += 1
                    self._schedule_stream_render()
            except Exception:
                current = self._completed_virtuals.get(ref)
                if current is not None and current.get("ref") == ref and request_generation == current["request_generation"]:
                    current["error"] = "unavailable"
            finally:
                current = self._completed_virtuals.get(ref)
                if current is not None:
                    current["pending"].discard(offset)
                self._completed_page_inflight = max(0, self._completed_page_inflight - 1)
                if current is not None:
                    self._start_completed_page_requests(ref)

        def _open_completed_virtual(self, ref: str) -> None:
            """Replace a bounded live preview with lazy canonical assistant rows."""
            if not isinstance(self.session, TuiSessionView):
                return
            self._discard_live_assistant_block()
            width = max(1, min(512, int(self._main_width())))
            self._completed_virtuals[ref] = {
                "ref": ref, "total": 1, "width": width, "rows": {}, "pages": [], "pending": set(), "queued": set(), "request_generation": 0, "error": None, "geometry_loaded": False,
            }
            self._append_stream_lines(["\0completed:" + ref])
            # Do this before the next frame as the completed block may be
            # below the visible rows and therefore never trigger render_line.
            self._need_completed_virtual_row(ref, 0)

        def _sync_stream_line_ids(self) -> None:
            """Repair direct test list setup without diffing on a paint path.

            Normal mutations retain the parallel IDs explicitly.  A few old
            tests assign ``_stream_lines`` directly; that unsupported shortcut
            gets fresh IDs at changed positions and loses actions there rather
            than matching repeated text to a potentially wrong result.
            """
            if not self._stream_lines:
                self._stream_line_ids = []
                self._stream_line_values = []
                return
            if self._stream_line_values != self._stream_lines:
                old_values = self._stream_line_values
                old_ids = self._stream_line_ids
                rebuilt: list[int] = []
                changed = False
                for index, value in enumerate(self._stream_lines):
                    if index < len(old_values) and index < len(old_ids) and old_values[index] == value:
                        rebuilt.append(old_ids[index])
                    else:
                        self._next_stream_line_id += 1
                        rebuilt.append(self._next_stream_line_id)
                        changed = True
                self._stream_line_ids = rebuilt
                if changed and len(old_values) == len(self._stream_lines) and not (
                    self._agent_links_by_line or self._evidence_links_by_line
                ):
                    # A same-size direct replacement is the legacy test setup
                    # bridge. Drop actions rather than attaching an identical
                    # replacement value to an old trusted target. Appends and
                    # front trims retain their explicit bookkeeping below.
                    self._agent_links_by_line.clear()
                    self._evidence_links_by_line.clear()
                    self._evidence_link_line_ids.clear()
                    self._evidence_tokens_by_line_id.clear()
            while len(self._stream_line_ids) < len(self._stream_lines):
                self._next_stream_line_id += 1
                self._stream_line_ids.append(self._next_stream_line_id)
            self._stream_line_values = list(self._stream_lines)
            # A live tail replacement gets a new ID. Drop its old rendered
            # fragment immediately instead of retaining every streamed version.
            retained_ids = set(self._stream_line_ids)
            if self._stream_content_cache:
                self._stream_content_cache = {
                    key: content for key, content in self._stream_content_cache.items() if key[0] in retained_ids
                }

        def _replace_stream_slice(self, start: int, end: int, values: list[str]) -> None:
            """Apply a normal presentation splice while preserving retained IDs.

            This is the only mutation primitive for replace/delete paths that
            can sit before an evidence line.  It prevents a live assistant or
            tool-body resize from rebinding a later, repeated result heading.
            """
            self._sync_stream_line_ids()
            start = max(0, min(start, len(self._stream_lines)))
            end = max(start, min(end, len(self._stream_lines)))
            removed_ids = set(self._stream_line_ids[start:end])
            replacement_ids: list[int] = []
            for _value in values:
                self._next_stream_line_id += 1
                replacement_ids.append(self._next_stream_line_id)
            delta = len(values) - (end - start)
            self._stream_lines[start:end] = values
            self._stream_line_ids[start:end] = replacement_ids
            self._stream_line_values[start:end] = values
            if removed_ids:
                self._stream_content_cache = {
                    key: content for key, content in self._stream_content_cache.items() if key[0] not in removed_ids
                }
                for line_id in removed_ids:
                    for token in self._evidence_tokens_by_line_id.pop(line_id, ()):
                        self._evidence_link_tokens.pop(token, None)
                        self._evidence_link_line_ids.pop(token, None)

            def rebase(index: int) -> int | None:
                if start <= index < end:
                    return None
                return index + delta if index >= end else index

            self._agent_links_by_line = {
                rebased: tokens for line, tokens in self._agent_links_by_line.items()
                if (rebased := rebase(line)) is not None
            }
            self._evidence_links_by_line = {
                rebased: tokens for line, tokens in self._evidence_links_by_line.items()
                if (rebased := rebase(line)) is not None
            }
            self._agent_link_tokens = {
                token: (generation, session_id, rebased, call_id)
                for token, (generation, session_id, line, call_id) in self._agent_link_tokens.items()
                if (rebased := rebase(line)) is not None
            }

        def _append_stream_lines(self, values: list[str]) -> None:
            self._replace_stream_slice(len(self._stream_lines), len(self._stream_lines), values)

        def _set_stream_line(self, index: int, value: str) -> None:
            """Replace text in an existing logical block without changing ID."""
            self._sync_stream_line_ids()
            if not 0 <= index < len(self._stream_lines):
                return
            line_id = self._stream_line_ids[index]
            self._stream_lines[index] = value
            self._stream_line_values[index] = value
            self._stream_content_cache = {
                key: content for key, content in self._stream_content_cache.items() if key[0] != line_id
            }

        def _cached_stream_content_line(self, line_index: int, line: str, width: int) -> Content:
            line_id = self._stream_line_ids[line_index]
            tokens = self._agent_links_by_line.get(line_index, ())
            links = tuple((token, self._agent_link_labels[token]) for token in tokens if token in self._agent_link_labels)
            evidence_links = self._evidence_tokens_by_line_id.get(line_id, ())
            key = (line_id, line, width, self._theme.name, links, evidence_links)
            cached = self._stream_content_cache.get(key)
            if cached is not None:
                self._stream_cache_hits += 1
                return cached
            self._stream_cache_misses += 1
            rendered = self._stream_content_line(line_index, line, links)
            self._stream_content_cache[key] = rendered
            return rendered

        def _invalidate_deferred_stream_viewport(self) -> None:
            """Make an already queued layout callback harmless."""
            self._stream_render_generation += 1
            self._deferred_stream_viewport = None
            # Textual does not expose cancellation for call_after_refresh.
            # A token makes a callback queued for the old session/stream inert.
            self._stream_viewport_callback_token += 1
            self._stream_viewport_callback_pending = False

        def _capture_stream_anchor(self, stream_widget: Static, container: VerticalScroll) -> tuple[str, int] | None:
            """Capture a retained visual row and its offset in the viewport.

            The widget's screen region plus the container's scroll offset gives
            its stable content origin, including the logo before the transcript.
            Using rendered rows (rather than logical list indexes) also accounts
            for wrapping.
            """
            try:
                content_origin = stream_widget.region.y - container.region.y + container.scroll_y
                local_top = max(0, int(container.scroll_y - content_origin))
                visible_rows = max(1, int(container.scrollable_content_region.height))
                for offset in range(visible_rows):
                    row = stream_widget.render_line(local_top + offset).text.rstrip()
                    if row.strip():
                        return row, local_top + offset
            except Exception:
                pass
            return None

        def _queue_stream_viewport_reconcile(
            self,
            container: VerticalScroll,
            *,
            follow: bool,
            previous_scroll_y: float,
            previous_max_scroll_y: float,
            previous_scroll_target_y: float,
            anchor: tuple[str, int] | None,
            front_trim_count: int,
        ) -> None:
            """Coalesce post-layout tail follow and retained-history anchoring."""
            self._stream_render_generation += 1
            generation = self._stream_render_generation
            existing = self._deferred_stream_viewport
            # A deliberate scroll between renders overrides an older queued
            # follow. Compare target as well as current y: an animated user
            # scrollbar drag may still be visually near the old tail.
            if existing and existing.get("follow") and not follow:
                existing = None
            if existing:
                previous_scroll_y = existing["scroll_y"]
                previous_max_scroll_y = existing["max_scroll_y"]
                previous_scroll_target_y = existing["target_y"]
                # Keep the original retained row while a burst of front trims
                # waits for one layout pass; intermediate updates can otherwise
                # move Textual's numeric scroll before we reconcile it.
                if not existing.get("follow"):
                    anchor = existing.get("anchor")
                    front_trim_count += existing.get("front_trim_count", 0)
            self._deferred_stream_viewport = {
                "session_generation": self._session_listener_generation,
                "render_generation": generation,
                "container": container,
                "follow": follow,
                "scroll_y": previous_scroll_y,
                "max_scroll_y": previous_max_scroll_y,
                "target_y": previous_scroll_target_y,
                "anchor": anchor,
                "front_trim_count": front_trim_count,
                "user_scroll_generation": self._stream_user_scroll_generation,
                # A fresh render gets one current-layout reconciliation plus
                # these settling checks. A newer render replaces this pending
                # intent and restarts the checks for its own layout.
                "follow_layout_reconciles": _STREAM_FOLLOW_LAYOUT_RECONCILES,
            }
            if not self._stream_viewport_callback_pending:
                self._stream_viewport_callback_pending = True
                self._stream_viewport_callback_token += 1
                callback_token = self._stream_viewport_callback_token
                self.call_after_refresh(lambda: self._reconcile_stream_viewport(callback_token))

        def _reconcile_stream_viewport(self, callback_token: int) -> None:
            """Apply the latest viewport intent only after its layout is current."""
            if callback_token != self._stream_viewport_callback_token:
                return
            self._stream_viewport_callback_pending = False
            pending = self._deferred_stream_viewport
            if pending is None:
                return
            # A rejected/stale intent is consumed. Successful tail follow may
            # explicitly retain it below for its bounded layout-settling pass.
            self._deferred_stream_viewport = None
            if (
                pending["session_generation"] != self._session_listener_generation
                or pending["render_generation"] != self._stream_render_generation
            ):
                return
            container = pending["container"]
            try:
                # Both tail follow and retained-history anchoring are merely
                # stale snapshots of viewer intent. A later wheel event must
                # always win. The position/target guard also covers scrollbar
                # drags and keyboard scrolling, which don't produce a wheel
                # event on the app.
                if pending["user_scroll_generation"] != self._stream_user_scroll_generation:
                    return
                if (
                    abs(container.scroll_y - pending["scroll_y"]) > _STREAM_FOLLOW_TOLERANCE
                    or abs(container.scroll_target_y - pending["target_y"]) > _STREAM_FOLLOW_TOLERANCE
                ):
                    return
                if pending["follow"]:
                    # Do not snap back when wheel/scrollbar input moved upward
                    # after scheduling. target_y catches an in-flight animated
                    # user scroll before scroll_y has reached that target.
                    if (
                        container.scroll_y < pending["scroll_y"] - _STREAM_FOLLOW_TOLERANCE
                        or container.scroll_target_y < pending["target_y"] - _STREAM_FOLLOW_TOLERANCE
                    ):
                        return
                    container.scroll_end(animate=False, immediate=True)
                    # Textual may finish wrapping/layout after this refresh
                    # callback. Retain only the latest tail intent for bounded
                    # post-layout checks, so a later user scroll still wins
                    # while the final end is not sampled too early.
                    remaining = pending.get("follow_layout_reconciles", 0)
                    if isinstance(remaining, int) and remaining > 0:
                        pending["follow_layout_reconciles"] = remaining - 1
                        self._deferred_stream_viewport = pending
                        self._stream_viewport_callback_pending = True
                        self._stream_viewport_callback_token += 1
                        next_token = self._stream_viewport_callback_token
                        self.call_after_refresh(lambda: self._reconcile_stream_viewport(next_token))
                    return
                anchor = pending["anchor"]
                if anchor is not None:
                    stream_widget = self.query_one("#stream", Static)
                    anchor_text, previous_row = anchor
                    row_index = self._find_stream_anchor_row(
                        stream_widget,
                        anchor_text,
                        max(0, previous_row - pending["front_trim_count"]),
                    )
                    if row_index is not None:
                        container.scroll_to(
                            y=max(0, pending["scroll_y"] + row_index - previous_row),
                            animate=False,
                            immediate=True,
                        )
                        return
                # The anchor was trimmed or is no longer rendered. Preserve
                # the prior numeric position and let Textual safely clamp it.
                container.scroll_to(y=pending["scroll_y"], animate=False, immediate=True)
            except Exception:
                return

        @staticmethod
        def _find_stream_anchor_row(stream_widget: Static, anchor_text: str, expected_row: int) -> int | None:
            """Find one unambiguous retained row near its expected position.

            Do not search a whole wrapped transcript here: this runs in
            ``call_after_refresh`` on the UI thread. Duplicate text is not a
            stable anchor, so fall back to Textual's safely clamped numeric
            position rather than selecting an arbitrary matching row.
            """
            height = int(stream_widget.virtual_size.height)
            start = max(0, expected_row - _STREAM_ANCHOR_SEARCH_ROWS)
            stop = min(height, expected_row + _STREAM_ANCHOR_SEARCH_ROWS + 1)
            matches = [
                row_index
                for row_index in range(start, stop)
                if stream_widget.render_line(row_index).text.rstrip() == anchor_text
            ]
            return matches[0] if len(matches) == 1 else None

        def on_mouse_scroll_up(self, event: Any) -> None:
            """Invalidate a queued viewport restore after reader wheel input."""
            if getattr(event, "control", None) is self.query_one("#stream_container", VerticalScroll):
                self._stream_user_scroll_generation += 1

        def on_mouse_scroll_down(self, event: Any) -> None:
            """Invalidate a queued viewport restore after reader wheel input."""
            if getattr(event, "control", None) is self.query_one("#stream_container", VerticalScroll):
                self._stream_user_scroll_generation += 1

        def _refresh_logo(self) -> None:
            """Render startup artwork separately from the selectable transcript."""
            full_logo = "\n".join(_TUI_LOGO_LINES)
            logo = full_logo if full_logo.strip() else "one"
            try:
                self.query_one("#logo", Static).update(logo)
            except Exception:
                pass

        def _main_width(self) -> int:
            """Return the rendered conversation-column width when available."""
            try:
                width = self.query_one("#main").region.width
                if width > 0:
                    return width
            except Exception:
                pass
            return self.size.width

        @staticmethod
        def _append_stream_line(text: Text, line: str, *, in_tool_block: bool = False) -> None:
            """Append a literal transcript line, styling recognized tool status."""
            is_tool_line = line.startswith(("tool: ", "tool (", "tool start ", "tool ok: ", "tool err: "))
            status_start = len(line)
            if is_tool_line or in_tool_block:
                for suffix in (" [ok]", " [err]", "[ok]", "[err]"):
                    if line.endswith(suffix):
                        status_start = len(line) - len(suffix)
                        break

            if not is_tool_line:
                text.append(line[:status_start])
            else:
                args_start = line.find("{")
                if args_start < 0 or args_start > status_start:
                    text.append(line[:status_start], style="bold")
                else:
                    text.append(line[:args_start], style="bold")
                    # Arguments are literal text, not Rich markup, and intentionally
                    # have no inherited bold style.
                    text.append(line[args_start:status_start])
            if status_start < len(line):
                text.append(line[status_start:], style="bold")
            text.append("\n")

        def _stream_content_line(
            self, line_index: int, line: str, indexed_links: tuple[tuple[int, str], ...] | None = None
        ) -> Content:
            """Return one literal line plus any trusted generated view actions."""
            links = list(indexed_links) if indexed_links is not None else [
                (token, self._agent_link_labels[token])
                for token in self._agent_links_by_line.get(line_index, ())
                if token in self._agent_link_labels
                and self._agent_link_tokens.get(token, (None,))[0] == self._monitor_generation
            ]
            line_id = self._stream_line_ids[line_index]
            evidence_links = [
                (token, "[view]")
                for token, linked_line_id in self._evidence_link_line_ids.items()
                if linked_line_id == line_id
                if token in self._evidence_link_tokens
                and self._evidence_link_tokens[token][0] == self._monitor_generation
                and (
                    not isinstance(self.session, TuiSessionView)
                    or self._evidence_link_tokens[token][3] == self.session.epoch
                )
            ]
            if not links and not evidence_links:
                return self._literal_stream_content(line)
            links.sort()
            # The link location and label come solely from trusted metadata.
            # In particular, never parse the wrapped task/argument body to
            # rediscover a suffix: all child links belong on the short Tool:
            # status row recorded by _write_subagent_entry.
            content = self._literal_stream_content(line, newline=False)
            for token, label in links:
                content = content.append(" ")
                content = content.append(
                    Content.from_markup(f"[@click=app.open_agent_link({token})]$label[/]", label=label)
                )
            for token, label in evidence_links:
                content = content.append(" ")
                content = content.append(
                    Content.from_markup(f"[@click=app.open_evidence_link({token})]$label[/]", label=label)
                )
            return content.append("\n")

        @staticmethod
        def _literal_stream_content(line: str, *, newline: bool = True) -> Content:
            """Literal line retaining the old tool/status emphasis without markup."""
            content = Content.from_markup("$line" + ("\n" if newline else ""), line=line)
            is_tool_line = line.startswith(("tool: ", "tool (", "tool start ", "tool ok: ", "tool err: "))
            if not is_tool_line:
                return content
            status_start = len(line)
            for suffix in (" [ok]", " [err]", "[ok]", "[err]"):
                if line.endswith(suffix):
                    status_start = len(line) - len(suffix)
                    break
            args_start = line.find("{")
            if args_start < 0 or args_start > status_start:
                return content.stylize(TextualStyle(bold=True), 0, status_start).stylize(
                    TextualStyle(bold=True), status_start, len(line)
                )
            return content.stylize(TextualStyle(bold=True), 0, args_start).stylize(
                TextualStyle(bold=True), status_start, len(line)
            )

        @staticmethod
        def _ui_event_size(event: dict[str, Any]) -> int:
            """Count detached event bytes conservatively for the ingress budget."""
            try:
                return len(json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8"))
            except (TypeError, ValueError):
                return _UI_EVENT_MAX_BYTES + 1

        @staticmethod
        def _reduce_oversized_ui_event(event: dict[str, Any]) -> dict[str, Any]:
            """Safely reduce malformed direct-session display events."""
            reduced = dict(event)
            if reduced.get("type") == "tool_call_end" and isinstance(reduced.get("result"), dict):
                key = "outputText" if reduced.get("ok") else "error"
                value = reduced["result"].get(key)
                if isinstance(value, str):
                    reduced.setdefault(key, value)
                # Match facade DTOs: retain one visible projection, never both.
                reduced.pop("result", None)
            return _fit_event_snapshot(reduced, _UI_EVENT_MAX_BYTES)

        @staticmethod
        def _ui_delta(event: dict[str, Any]) -> tuple[str, str] | None:
            if event.get("type") == "message_update":
                nested = event.get("assistantMessageEvent")
                if isinstance(nested, dict) and nested.get("type") == "text_delta" and isinstance(nested.get("delta"), str):
                    return "message_update", nested["delta"]
            if event.get("type") == "thinking_delta" and isinstance(event.get("delta"), str):
                return "thinking_delta", event["delta"]
            return None

        def _try_coalesce_ui_delta_locked(self, event: dict[str, Any], event_size: int) -> bool:
            """Coalesce only adjacent compatible deltas; all other events are barriers."""
            if not self._pending_ui_events:
                return False
            previous = self._pending_ui_events[-1]
            current_delta = self._ui_delta(event)
            previous_delta = self._ui_delta(previous)
            if current_delta is None or previous_delta is None or current_delta[0] != previous_delta[0]:
                return False
            # Do not merge explicitly different streams.
            for key in ("sessionId", "messageId", "turnId", "id"):
                if previous.get(key) is not None and event.get(key) is not None and previous[key] != event[key]:
                    return False
            merged = dict(event)
            if current_delta[0] == "message_update":
                nested = dict(event["assistantMessageEvent"])
                nested["delta"] = previous_delta[1] + current_delta[1]
                merged["assistantMessageEvent"] = nested
            else:
                merged["delta"] = previous_delta[1] + current_delta[1]
            merged_size = self._ui_event_size(merged)
            if len((self._ui_delta(merged) or ("", "", ""))[1].encode("utf-8")) > _UI_MAX_COALESCED_DELTA_BYTES:
                return False
            if merged_size > _UI_EVENT_MAX_BYTES or (
                self._pending_ui_event_bytes - self._ui_event_size(previous) + merged_size > _UI_EVENT_QUEUE_MAX_BYTES
            ):
                return False
            self._pending_ui_events[-1] = merged
            self._pending_ui_event_bytes += merged_size - self._ui_event_size(previous)
            return True

        def _defer_legacy_ui_event_locked(self, event: dict[str, Any], event_size: int) -> None:
            """Append a direct-session event or explicitly report bounded overflow.

            The direct listener cannot wait for the UI loop.  It also cannot
            discard a lifecycle barrier: callers receive ``BufferError`` and
            retain ownership of the source event rather than observing a silent
            loss. Facade-backed production delivery uses readiness backpressure
            before reaching this compatibility-only path.
            """
            if (
                len(self._legacy_deferred_ui_events) >= _UI_LEGACY_DEFERRED_CAPACITY
                or self._legacy_deferred_ui_event_bytes + event_size > _UI_LEGACY_DEFERRED_MAX_BYTES
            ):
                raise BufferError("legacy TUI deferred ingress is full; event was not accepted")
            self._legacy_deferred_ui_events.append(event)
            self._legacy_deferred_ui_event_bytes += event_size

        def _can_accept_ui_event_locked(self, event: dict[str, Any], event_size: int | None = None) -> bool:
            """Check bounded ingress capacity without mutating it or waiting."""
            size = self._ui_event_size(event) if event_size is None else event_size
            if size > _UI_EVENT_MAX_BYTES:
                return False
            if len(self._pending_ui_events) < _UI_EVENT_CAPACITY and self._pending_ui_event_bytes + size <= _UI_EVENT_QUEUE_MAX_BYTES:
                return True
            # A compatible tail delta can still be accepted by coalescing.
            if not self._pending_ui_events or self._ui_delta(event) is None:
                return False
            previous = self._pending_ui_events[-1]
            previous_delta = self._ui_delta(previous)
            current_delta = self._ui_delta(event)
            if previous_delta is None or current_delta is None or previous_delta[0] != current_delta[0]:
                return False
            for key in ("sessionId", "messageId", "turnId", "id"):
                if previous.get(key) is not None and event.get(key) is not None and previous[key] != event[key]:
                    return False
            merged = dict(event)
            if current_delta[0] == "message_update":
                nested = dict(event["assistantMessageEvent"])
                nested["delta"] = previous_delta[1] + current_delta[1]
                merged["assistantMessageEvent"] = nested
            else:
                merged["delta"] = previous_delta[1] + current_delta[1]
            merged_size = self._ui_event_size(merged)
            return merged_size <= _UI_EVENT_MAX_BYTES and (
                self._pending_ui_event_bytes - self._ui_event_size(previous) + merged_size <= _UI_EVENT_QUEUE_MAX_BYTES
            )

        def _can_accept_ui_event(self, event: dict[str, Any]) -> bool:
            with self._pending_ui_events_lock:
                return self._can_accept_ui_event_locked(event)

        def _flush_pending_ui_events(self) -> None:
            """Drain ordered session events without creating a token-rate UI backlog."""
            with self._pending_ui_events_lock:
                # External test helpers may seed the compatibility list directly;
                # recalculate before removing a bounded slice.
                self._pending_ui_event_bytes = sum(self._ui_event_size(event) for event in self._pending_ui_events)
                self._legacy_deferred_ui_event_bytes = sum(
                    self._ui_event_size(event) for event in self._legacy_deferred_ui_events
                )
                pending = self._pending_ui_events[:_UI_DRAIN_MAX_ITEMS]
                del self._pending_ui_events[:len(pending)]
                self._pending_ui_event_bytes -= sum(self._ui_event_size(event) for event in pending)
                while self._legacy_deferred_ui_events:
                    event = self._legacy_deferred_ui_events[0]
                    size = self._ui_event_size(event)
                    if self._try_coalesce_ui_delta_locked(event, size):
                        self._legacy_deferred_ui_events.popleft()
                        self._legacy_deferred_ui_event_bytes -= size
                        continue
                    if not self._can_accept_ui_event_locked(event, size):
                        break
                    self._legacy_deferred_ui_events.popleft()
                    self._legacy_deferred_ui_event_bytes -= size
                    self._pending_ui_events.append(event)
                    self._pending_ui_event_bytes += size
                self._pending_assistant_deltas = ""
                if not self._legacy_deferred_ui_events:
                    self._legacy_deferred_ui_event_bytes = 0
                # The queued wakeup is consumed when its drain begins. A
                # concurrent lifecycle append can now schedule its successor.
                self._ui_flush_wakeup_pending = False
                self._pending_ui_events_lock.notify_all()
            if isinstance(self.session, TuiSessionView):
                self.session.resume_events()
            if not pending:
                return

            delta_events: list[dict[str, Any]] = []

            def flush_deltas() -> None:
                if not delta_events:
                    return
                changed = False
                # Process in event order, but collapse contiguous deltas of the
                # same kind so wrapping/rebuilding happens once per run.
                index = 0
                while index < len(delta_events):
                    event = delta_events[index]
                    event_type = event.get("type")
                    if event_type == "message_update":
                        assistant_event = event.get("assistantMessageEvent", {})
                        kind = assistant_event.get("type")
                    else:
                        kind = "thinking"
                    chunks: list[str] = []
                    while index < len(delta_events):
                        candidate = delta_events[index]
                        if candidate.get("type") == "message_update":
                            candidate_kind = candidate.get("assistantMessageEvent", {}).get("type")
                        else:
                            candidate_kind = "thinking"
                        if candidate_kind != kind:
                            break
                        if candidate_kind == "text_delta":
                            chunks.append(str(candidate.get("assistantMessageEvent", {}).get("delta", "")))
                        else:
                            chunks.append(str(candidate.get("delta", "")))
                        index += 1
                    delta = "".join(chunks)
                    if kind == "text_delta":
                        changed = self._apply_assistant_deltas(delta) or changed
                    else:
                        changed = self._apply_thinking_delta(delta) or changed
                delta_events.clear()
                if changed:
                    self._schedule_stream_render()

            deadline = time.monotonic() + _UI_DRAIN_MAX_SECONDS
            processed = 0
            self._ui_drain_active = True
            try:
                for event in pending:
                    # Do not begin another atomic event after this slice has
                    # expired. One individual handler may finish just beyond
                    # the deadline, which is why expensive transcript painting
                    # is explicitly deferred above.
                    event_type = event.get("type")
                    is_text_delta = (
                        event_type == "message_update"
                        and event.get("assistantMessageEvent", {}).get("type") == "text_delta"
                    )
                    # A bounded display delta is safe to finish after the
                    # immediately preceding lifecycle barrier. Keep the
                    # barrier+delta transition atomic while yielding before
                    # any later lifecycle/formatting work.
                    if processed and time.monotonic() >= deadline and not (
                        processed == 1 and (is_text_delta or event_type == "thinking_delta")
                    ):
                        break
                    processed += 1
                    if is_text_delta or event_type == "thinking_delta":
                        # Keep formatting bounded at the event chunk limit.
                        # Applying an entire delta run at its lifecycle barrier
                        # can turn a nominally time-budgeted drain into one
                        # large trim/wrap operation.
                        if is_text_delta:
                            changed = self._apply_assistant_deltas(
                                str(event.get("assistantMessageEvent", {}).get("delta", ""))
                            )
                        else:
                            changed = self._apply_thinking_delta(str(event.get("delta", "")))
                        if changed:
                            self._schedule_stream_render()
                        if processed >= _UI_DRAIN_MAX_ITEMS:
                            break
                        continue
                    flush_deltas()
                    self._handle_session_event(event)
                    if processed >= _UI_DRAIN_MAX_ITEMS:
                        break
                flush_deltas()
            finally:
                self._ui_drain_active = False
            # Do not clear a wakeup scheduled by an append racing this drain.
            # With no successor batch, the coalescing flag is reset at both
            # drain boundaries.
            with self._pending_ui_events_lock:
                # A time-budget expiration returns untouched events to the front,
                # preserving their sequence before concurrent producer events.
                if processed < len(pending):
                    remainder = pending[processed:]
                    self._pending_ui_events[0:0] = remainder
                    self._pending_ui_event_bytes += sum(self._ui_event_size(event) for event in remainder)
                if not self._pending_ui_events:
                    self._ui_flush_wakeup_pending = False
                else:
                    # A tiny delayed continuation yields the Textual input queue;
                    # it is not the paint cadence/fallback interval.
                    self.set_timer(0.001, self._flush_pending_ui_events)

        def _flush_pending_assistant_deltas(self) -> None:
            """Backward-compatible test helper for the ordered UI drain."""
            self._flush_pending_ui_events()

        def _apply_assistant_deltas(self, delta: str) -> bool:
            if not delta or not self._assistant_stream_open:
                return False
            # This is only a fallback for providers that omit a final message;
            # durable history owns the complete response. Do not retain a
            # second unbounded cumulative copy in the UI.
            self._assistant_stream = _truncate_display_bytes(self._assistant_stream + delta)
            self._append_assistant_delta(delta)
            return True

        def _apply_thinking_delta(self, delta: str) -> bool:
            """Append one coalesced thinking run without rendering it yet."""
            if not delta or (not delta.strip() and not self._thinking_label_shown):
                return False
            started_segment = not self._thinking_label_shown
            if started_segment:
                self._stream_lines.append("Thinking:")
                self._thinking_label_idx = len(self._stream_lines) - 1
                self._thinking_label_shown = True
                self._thinking_buffer = ""
                self._thinking_line_idx = None
            self._thinking_buffer = _truncate_display_bytes(self._thinking_buffer + sanitize_display_text(delta))
            escaped = rich_escape(self._thinking_buffer)
            line_text = f"{_THINKING_TEXT_MARK}[i {self._theme.info}]{escaped}[/]"
            if (
                self._thinking_line_idx is not None
                and 0 <= self._thinking_line_idx < len(self._stream_lines)
                and self._stream_lines[self._thinking_line_idx].startswith(_THINKING_TEXT_MARK)
            ):
                self._stream_lines[self._thinking_line_idx] = line_text
            else:
                # Spinner insertion/removal can shift a live index without a
                # 500-line trim. Recover the current segment's marked line
                # rather than duplicating the accumulated thinking transcript.
                if not started_segment:
                    for index in range(len(self._stream_lines) - 1, -1, -1):
                        if self._stream_lines[index].startswith(_THINKING_TEXT_MARK):
                            self._thinking_line_idx = index
                            self._stream_lines[index] = line_text
                            break
                    else:
                        self._stream_lines.append("")
                        self._stream_lines.append(line_text)
                        self._thinking_line_idx = len(self._stream_lines) - 1
                        self._trim_stream(render=False)
                else:
                    self._stream_lines.append("")
                    self._stream_lines.append(line_text)
                    self._thinking_line_idx = len(self._stream_lines) - 1
                    self._trim_stream(render=False)
            return True

        def _remove_thinking_line(self) -> None:
            """Clear the independent waiting status without touching transcript."""
            try:
                self.query_one("#waiting_status", Static).update("")
            except Exception:
                pass
            self._thinking_active = False

        def _finalize_thinking_block(self) -> None:
            """Close/reset the thinking-text block so the next turn starts clean.

            Resets only the thinking-block flags; previous thinking segments
            remain visible as part of the turn transcript. The animated
            spinner is handled by _remove_thinking_line / _tick_waiting.
            """
            if self._thinking_label_shown:
                # The waiting spinner is inserted before a live reasoning
                # block and removed asynchronously.  Its removal can shift
                # the label before this lifecycle barrier runs, so the cached
                # absolute index is only an optimization, never the source of
                # truth.  Finalize the latest active label in the transcript.
                label_idx = self._thinking_label_idx
                if not (
                    label_idx is not None
                    and 0 <= label_idx < len(self._stream_lines)
                    and self._stream_lines[label_idx] == "Thinking:"
                ):
                    label_idx = next(
                        (
                            index
                            for index in range(len(self._stream_lines) - 1, -1, -1)
                            if self._stream_lines[index] == "Thinking:"
                        ),
                        None,
                    )
                if label_idx is not None:
                    self._stream_lines[label_idx] = "Thinking:"
            self._thinking_label_shown = False
            self._thinking_buffer = ""
            self._thinking_line_idx = None
            self._thinking_label_idx = None

        def _tick_waiting(self) -> None:
            """Animate a tiny status widget; never rebuild the transcript."""
            now = time.monotonic()
            if not evaluate_waiting(self._turn_active, self._last_delta_ts, now):
                if self._thinking_active:
                    self._remove_thinking_line()
                return
            # Check for user-gate pending state: show a paused indicator only
            # for ask_user (approval gate is no longer shown as a paused label
            # — the approval prompt widget handles that instead).
            pending_label = ""
            if self._ask_user_pending is not None:
                pending_label = "waiting for your response"
            if pending_label:
                line = f"[{self._theme.warn}]⏸ {pending_label}[/]"
            else:
                self._thinking_frame = advance_thinking_frame(self._thinking_frame)
                frame = _THINKING_FRAMES[self._thinking_frame]
                line = f"[{self._theme.info}]{frame} Ctrl+C \u2014 abort[/]"
            try:
                self.query_one("#waiting_status", Static).update(Content.from_markup(line))
            except Exception:
                pass
            self._thinking_active = True

        def _write(self, text: str, kind: str = "normal", *, render: bool = True) -> None:
            # Store one logical display line per entry. In particular, a large
            # tool/error payload with embedded newlines must not bypass the
            # viewport cap merely because it arrived through _write.
            self._stream_lines.extend(_truncate_display_bytes(text).splitlines() or [""])
            self._trim_stream(render=render)

        def _write_evidence_link(self, evidence_id: str, *, unavailable: bool = False) -> None:
            """Create an app-owned evidence link; transcript text is never parsed."""
            if unavailable or not evidence_id:
                self._write("Full result unavailable.", "warn")
                return
            self._write("Full sanitized result:", "info", render=False)
            line = len(self._stream_lines) - 1
            self._next_evidence_link_token += 1
            token = self._next_evidence_link_token
            self._evidence_link_tokens[token] = (
                self._monitor_generation, evidence_id, 0,
                self.session.epoch if isinstance(self.session, TuiSessionView) else 0,
            )
            self._evidence_links_by_line[line] = (*self._evidence_links_by_line.get(line, ()), token)
            self._sync_stream_line_ids()
            self._evidence_link_line_ids[token] = self._stream_line_ids[line]
            line_id = self._stream_line_ids[line]
            self._evidence_tokens_by_line_id[line_id] = (
                *self._evidence_tokens_by_line_id.get(line_id, ()), token
            )
            self._rendered_stream_lines = ()
            self._schedule_stream_render()

        def _write_subagent_entry(self, session_id: str, task: Any) -> None:  # noqa: ARG002
            """Attach a child to its active spawn status heading, never task text."""
            active = self._active_tool_block
            call_id = self._active_tool_call_id
            if not session_id or active is None or active[0] != "spawn_subagent" or call_id is None:
                return
            _name, start, end, _text = active
            if not (0 <= start <= end <= len(self._stream_lines)):
                return
            # _write_bounded_panel returns the body start. The dedicated Tool:
            # heading directly precedes it and remains short even when args wrap.
            heading_line = start - 1
            if heading_line < 0 or not self._stream_lines[heading_line].startswith("Tool:"):
                return
            self._next_agent_link_token += 1
            token = self._next_agent_link_token
            self._active_subagent_link_tokens.append(token)
            # A second child turns the first fixed label into its ordinal form.
            for ordinal, existing in enumerate(self._active_subagent_link_tokens, 1):
                self._agent_link_labels[existing] = (
                    "[view]" if len(self._active_subagent_link_tokens) == 1 else f"[view {ordinal}]"
                )
            self._agent_link_tokens[token] = (self._monitor_generation, session_id, heading_line, call_id)
            self._agent_links_by_line[heading_line] = tuple(
                [*self._agent_links_by_line.get(heading_line, ()), token]
            )
            # Inline actions are generated from metadata rather than stored in
            # _stream_lines, so invalidate the otherwise pure line cache.
            self._rendered_stream_lines = ()
            self._schedule_stream_render()

        def _write_compaction_result(self, result: dict[str, Any]) -> None:
            """Render a compact human-readable result for the TUI."""
            if result.get("aborted"):
                self._write("Compaction aborted.", "warn")
                return

            if result.get("skipped"):
                reason = (
                    "the session is busy"
                    if result.get("busy")
                    else "the history is already within the configured limit"
                )
                self._write(f"Compaction skipped: {reason}.", "info")
                return

            summary = str(result.get("summary") or "").strip()
            lines = ["Compaction complete."]
            if summary:
                lines.extend(["Summary:", summary])
            if "tokensBefore" in result:
                lines.append(f"Tokens before: {result['tokensBefore']}")
            if "kept" in result:
                lines.append(f"Messages kept: {result['kept']}")
            self._write("\n".join(lines), "info")

        def _write_lifecycle_separator(self) -> None:
            """Insert a display-only horizontal rule through the normal stream path."""
            width = 84
            try:
                stream_widget = self.query_one("#stream_container")
                current_width = int(getattr(getattr(stream_widget, "size", None), "width", 0) or 0)
                if current_width > 0:
                    width = max(1, current_width - 6)
            except Exception:
                pass
            # A separator is always a separate visual block. Do not add a
            # second blank line when the preceding block already supplied one.
            if self._stream_lines and self._stream_lines[-1] != "":
                self._write("", render=False)
            self._write("─" * width)

        def _trim_stream(self, *, render: bool = True) -> int:
            """Trim *_stream_lines* to ``MAX_RENDERED_LINES`` entries. Returns the number of
            lines dropped from the front (0 if no trim happened) so callers
            can rebase any absolute indexes they hold."""
            self._sync_stream_line_ids()
            before = len(self._stream_lines)
            # Enforce byte and estimated wrapped-row budgets before Content is
            # constructed.  The estimate deliberately over-retains no giant
            # logical line: it uses terminal cell width, not newline count.
            width = max(10, self._main_width() - 2)
            per_line_limit = min(MAX_RENDERED_BYTES, width * MAX_RENDERED_ROWS)
            for index, line in enumerate(self._stream_lines):
                if len(line.encode("utf-8")) > per_line_limit or cell_len(line) > width * MAX_RENDERED_ROWS:
                    self._stream_lines[index] = _truncate_display_bytes(line, per_line_limit)
            used_bytes = used_rows = 0
            start = len(self._stream_lines)
            for index in range(len(self._stream_lines) - 1, -1, -1):
                line = self._stream_lines[index]
                byte_count = len(line.encode("utf-8")) + 1
                rows = max(1, math.ceil(max(1, cell_len(line)) / width))
                if start < len(self._stream_lines) and (
                    used_bytes + byte_count > MAX_RENDERED_BYTES or used_rows + rows > MAX_RENDERED_ROWS
                ):
                    break
                used_bytes += byte_count
                used_rows += rows
                start = index
            desired_start = max(len(self._stream_lines) - MAX_RENDERED_LINES, start)
            if desired_start:
                dropped = desired_start
                dropped_rows = sum(
                    max(1, math.ceil(max(1, cell_len(line)) / width))
                    for line in self._stream_lines[:desired_start]
                )
                self._stream_lines = self._stream_lines[desired_start:]
                self._stream_line_ids = self._stream_line_ids[desired_start:]
                self._stream_line_values = self._stream_line_values[desired_start:]
                retained_ids = set(self._stream_line_ids)
                self._stream_content_cache = {
                    key: content for key, content in self._stream_content_cache.items() if key[0] in retained_ids
                }
                # Reconciliation works in rendered rows, not logical entries:
                # one removed long line can otherwise move a retained anchor
                # beyond the bounded lookup window.
                self._stream_front_trim_pending += dropped_rows
            else:
                dropped = 0

            if dropped:
                self._agent_link_tokens = {
                    token: (generation, session_id, line_index - dropped, call_id)
                    for token, (generation, session_id, line_index, call_id) in self._agent_link_tokens.items()
                    if line_index >= dropped
                }
                self._agent_links_by_line = {
                    line_index - dropped: tuple(token for token in tokens if token in self._agent_link_tokens)
                    for line_index, tokens in self._agent_links_by_line.items()
                    if line_index >= dropped
                }
                self._prune_agent_link_metadata()
                self._evidence_link_tokens = {
                    token: (generation, evidence_id, offset, epoch)
                    for token, (generation, evidence_id, offset, epoch) in self._evidence_link_tokens.items()
                    if any(token in tokens for line, tokens in self._evidence_links_by_line.items() if line >= dropped)
                }
                self._evidence_links_by_line = {
                    line - dropped: tuple(token for token in tokens if token in self._evidence_link_tokens)
                    for line, tokens in self._evidence_links_by_line.items() if line >= dropped
                }
                retained_line_ids = set(self._stream_line_ids)
                self._evidence_link_line_ids = {
                    token: line_id for token, line_id in self._evidence_link_line_ids.items()
                    if token in self._evidence_link_tokens and line_id in retained_line_ids
                }
                self._evidence_tokens_by_line_id = {
                    line_id: tuple(token for token in tokens if token in self._evidence_link_tokens)
                    for line_id, tokens in self._evidence_tokens_by_line_id.items()
                    if line_id in retained_line_ids
                }

            # --- rebase assistant live bookkeeping ------------------------
            if dropped and self._assistant_live_start_idx >= 0:
                new_idx = self._assistant_live_start_idx - dropped
                if new_idx < 0:
                    # The live block was partially or fully trimmed away.
                    self._assistant_live_start_idx = -1
                    self._assistant_live_buffer = ""
                    self._assistant_live_line_count = 0
                    self._assistant_has_live_delta = False
                elif new_idx + self._assistant_live_line_count > len(self._stream_lines):
                    # The block extends beyond the trimmed list — invalidate.
                    self._assistant_live_start_idx = -1
                    self._assistant_live_buffer = ""
                    self._assistant_live_line_count = 0
                    self._assistant_has_live_delta = False
                else:
                    self._assistant_live_start_idx = new_idx

            # --- rebase active tool block --------------------------------
            if dropped:
                if self._thinking_line_idx is not None:
                    new_idx = self._thinking_line_idx - dropped
                    self._thinking_line_idx = new_idx if new_idx >= 0 else None
                if self._thinking_label_idx is not None:
                    new_idx = self._thinking_label_idx - dropped
                    self._thinking_label_idx = new_idx if new_idx >= 0 else None
                active = self._active_tool_block
                if active is not None:
                    _name, start, end, _text = active
                    new_start = start - dropped
                    new_end = end - dropped
                    if new_start < 0:
                        self._active_tool_block = None
                    elif new_end > len(self._stream_lines):
                        self._active_tool_block = None
                    else:
                        self._active_tool_block = (_name, new_start, new_end, _text)
            else:
                active = self._active_tool_block
                if active is not None:
                    _name, start, end, _text = active
                    if start >= len(self._stream_lines) or end > len(self._stream_lines):
                        self._active_tool_block = None

            if render:
                self._schedule_stream_render()
            return dropped

        def _chat_panel_width(self) -> int:
            try:
                stream_widget = self.query_one("#stream_container")
                stream_width = int(getattr(getattr(stream_widget, "size", None), "width", 0) or 0)
                # Keep small side paddings (indent + inner spaces) and avoid tiny values.
                if stream_width > 12:
                    return max(20, stream_width - 6)
            except Exception:
                pass
            return 84

        def _format_chat_panel(
            self,
            role: str,
            text: str,
            width: int | None = None,
            pad_y: int = 0,
            pad_x: int = 2,
        ) -> list[str]:
            content = _truncate_display_bytes(text).strip() or "[empty]"
            raw_lines = content.splitlines() or [content]
            wrapped: list[str] = []
            target_width = width or self._chat_panel_width()
            content_width = max(10, target_width)
            for raw in raw_lines:
                if not raw.strip():
                    wrapped.append("")
                    continue
                pieces = textwrap.wrap(
                    raw,
                    width=content_width,
                    replace_whitespace=False,
                    drop_whitespace=False,
                    break_long_words=True,
                    break_on_hyphens=False,
                )
                wrapped.extend(pieces if pieces else [""])
            out: list[str] = []
            for _ in range(max(0, pad_y)):
                out.append("")
            out.extend(wrapped)
            for _ in range(max(0, pad_y)):
                out.append("")
            if len(out) > MAX_RENDERED_ROWS:
                return [_DISPLAY_TRUNCATED_MARKER, *out[-(MAX_RENDERED_ROWS - 1) :]]
            return out

        def _append_assistant_delta(self, delta: str) -> None:
            if not delta:
                return
            self._remove_thinking_line()
            if not self._assistant_has_live_delta:
                self._stream_lines.append("")
                self._stream_lines.append("Assistant:")
                self._assistant_live_start_idx = len(self._stream_lines)
                self._assistant_live_buffer = ""
                self._assistant_live_line_count = 0  # will be set below
                self._assistant_has_live_delta = True
            self._append_live_assistant_source(delta)
            self._assistant_live_lines_dirty = True
            # Wrapping/replacing the live panel is presentation work. During a
            # bounded event reduction, leave it for the already frame-capped
            # stream paint instead of doing it once for every input DTO.
            if self._ui_drain_active:
                return
            self._materialize_assistant_live_lines()

        def _append_live_assistant_source(self, delta: str) -> None:
            """Append a bounded live source without re-encoding its whole prefix."""
            if not self._assistant_live_buffer:
                self._assistant_live_buffer_bytes = 0
            if self._assistant_live_buffer.endswith(_DISPLAY_TRUNCATED_MARKER):
                return
            clean_delta = sanitize_display_text(delta)
            encoded = clean_delta.encode("utf-8")
            if self._assistant_live_buffer_bytes + len(encoded) <= MAX_SOURCE_DISPLAY_BYTES:
                self._assistant_live_buffer += clean_delta
                self._assistant_live_buffer_bytes += len(encoded)
                return
            marker = "\n" + _DISPLAY_TRUNCATED_MARKER
            available = max(0, MAX_SOURCE_DISPLAY_BYTES - len(marker.encode("utf-8")) - self._assistant_live_buffer_bytes)
            self._assistant_live_buffer += encoded[:available].decode("utf-8", "ignore") + marker
            self._assistant_live_buffer_bytes = len(self._assistant_live_buffer.encode("utf-8"))

        def _materialize_assistant_live_lines(self) -> None:
            """Build the current live panel only from a scheduled paint path."""
            if not self._assistant_live_lines_dirty:
                return
            # Keep the complete bounded live source for later lifecycle state,
            # but format it once per scheduled frame. Reformatting an
            # ever-growing live message for every token defeats the drain
            # budget even when paint is frame-capped.
            # Apply the live presentation byte budget before `_format_chat_panel`
            # splits or wraps text.  Reserving marker space keeps even the
            # formatted input within the named cap (rather than appending a
            # marker after an 8 KiB prefix).
            live_render_text = self._assistant_live_buffer
            # Tool/native carrier streams may open a lifecycle block without
            # any user-visible prose.  They are context/control traffic, not
            # an empty assistant response.
            if not live_render_text.strip():
                self._assistant_live_lines_dirty = False
                return
            encoded_live = live_render_text.encode("utf-8")
            if len(encoded_live) > _MAX_LIVE_ASSISTANT_RENDER_BYTES:
                marker = "\n" + _TRUNCATED_ASSISTANT_MARKER
                room = max(0, _MAX_LIVE_ASSISTANT_RENDER_BYTES - len(marker.encode("utf-8")))
                live_render_text = encoded_live[:room].decode("utf-8", "ignore") + marker
            panel_lines = self._format_chat_panel("assistant", live_render_text)
            # A single message can be larger than the viewport. Keep its tail
            # live (so subsequent deltas continue to replace it) and make the
            # omitted prefix explicit rather than silently presenting a partial
            # message as complete.
            if len(panel_lines) > MAX_RENDERED_LINES:
                panel_lines = [_TRUNCATED_ASSISTANT_MARKER, *panel_lines[-(MAX_RENDERED_LINES - 1) :]]
            if self._assistant_live_start_idx >= 0:
                tail_start = self._assistant_live_start_idx + self._assistant_live_line_count
                tail = self._stream_lines[tail_start:]
                self._replace_stream_slice(self._assistant_live_start_idx, tail_start, panel_lines)
            else:
                self._append_stream_lines(panel_lines)
            self._assistant_live_line_count = len(panel_lines)
            self._trim_stream(render=False)
            self._assistant_live_lines_dirty = False
            if self._assistant_live_finalize_pending:
                end = self._assistant_live_start_idx + self._assistant_live_line_count
                self._replace_stream_slice(max(0, end), max(0, end), [""])
                self._trim_stream(render=False)
                self._assistant_stream = ""
                self._assistant_has_live_delta = False
                self._assistant_live_start_idx = -1
                self._assistant_live_buffer = ""
                self._assistant_live_buffer_bytes = 0
                self._assistant_live_line_count = 0
                self._assistant_live_finalize_pending = False

        def _discard_live_assistant_block(self) -> None:
            """Remove the currently-tracked live assistant block from
            *_stream_lines*.  Uses the tracked start index and line count so
            that content appended *after* the block (retry status, tool output,
            error messages, etc.) survives intact — this is the key difference
            from the brute-force ``del self._stream_lines[idx:]`` used in
            *tool_call_start*.

            Call this BEFORE resetting live bookkeeping so the buffer is still
            available to calculate the exact extent of the block.
            """
            idx = self._assistant_live_start_idx
            count = self._assistant_live_line_count
            if idx >= 0 and count > 0 and idx < len(self._stream_lines):
                self._replace_stream_slice(idx, idx + count, [])
            # Always invalidate bookkeeping — whether we deleted anything or
            # not, the caller will reset / rebuild state.
            self._assistant_live_start_idx = -1
            self._assistant_live_buffer = ""
            self._assistant_live_line_count = 0
            self._assistant_has_live_delta = False

        def _write_chat_block(self, role: str, text: str) -> None:
            self._remove_thinking_line()
            self._stream_lines.append("")
            labels = {"user": "You", "assistant": "Assistant", "custom": "Assistant"}
            self._stream_lines.append(f"{labels.get(role, 'Assistant')}:")
            rendered_text = text
            self._stream_lines.extend(self._format_chat_panel(role, rendered_text))
            self._stream_lines.append("")
            self._trim_stream()

        def _write_bounded_panel(self, heading: str, text: str) -> tuple[int, int]:
            self._remove_thinking_line()
            self._stream_lines.append("")
            self._stream_lines.append(heading)
            start = len(self._stream_lines)
            panel_lines = self._bounded_tool_panel(text, result=heading == "Result:")
            self._stream_lines.extend(panel_lines)
            end = len(self._stream_lines)
            self._stream_lines.append("")
            dropped = self._trim_stream()
            # Return post-trim indices so callers get positions that are
            # valid against the current (trimmed) _stream_lines list.
            return start - dropped, end - dropped

        def _write_tool_block(self, text: str, *, tool_name: str | None = None) -> tuple[int, int]:
            heading = "Tool: ○" + (f" {tool_name}" if tool_name else "")
            return self._write_bounded_panel(heading, text)

        def _write_result_block(self, text: str) -> tuple[int, int]:
            return self._write_bounded_panel("Result:", text)

        def _write_manual_command_block(self, command: str) -> tuple[int, int]:
            display_lines = self._format_chat_panel("tool", f"/bash {command}", pad_y=0)
            if len(display_lines) > 40:
                display_lines = ["Command display truncated", *display_lines[:40]]
            self._remove_thinking_line()
            self._stream_lines.extend(("", "Command:"))
            start = len(self._stream_lines)
            self._stream_lines.extend(display_lines)
            end = len(self._stream_lines)
            self._stream_lines.append("")
            dropped = self._trim_stream()
            return start - dropped, end - dropped

        def _bounded_manual_panel(
            self, text: str, *, stored: bool, truncated: bool, full_output_path: object
        ) -> list[str]:
            panel_lines = self._format_chat_panel("tool", text, pad_y=0)
            if len(panel_lines) <= 40:
                return panel_lines
            notice = "Output display truncated"
            if stored:
                notice += "; full output remains in current session history"
            if truncated and isinstance(full_output_path, str) and full_output_path:
                safe_path = " ".join(sanitize_display_text(full_output_path).split())
                if len(safe_path) > 200:
                    safe_path = safe_path[:199] + "…"
                if safe_path:
                    notice += f"; recorded raw output log: {safe_path}"
            return [f"[{notice}]", *panel_lines[:40]]

        def _write_manual_result(
            self,
            output: str,
            *,
            exit_code: object,
            timed_out: bool,
            cancelled: bool,
            truncated: bool,
            full_output_path: object,
            stored: bool,
        ) -> None:
            status = "cancelled" if cancelled else "timed out" if timed_out else f"exitCode={exit_code}"
            self._remove_thinking_line()
            self._stream_lines.extend(("", "Result:"))
            if output.rstrip():
                self._stream_lines.extend(
                    self._bounded_manual_panel(
                        output.rstrip(), stored=stored, truncated=truncated, full_output_path=full_output_path
                    )
                )
            self._stream_lines.extend(self._format_chat_panel("tool", f"[{status}]", pad_y=0))
            self._stream_lines.append("")
            self._trim_stream()

        def _write_plan_block(self, text: str | None) -> None:
            """Render a standalone plan update without claiming a tool block."""
            if text:
                self._write_bounded_panel("Plan:", text)
                return
            self._remove_thinking_line()
            self._stream_lines.extend(("", "Plan: cleared", ""))
            self._trim_stream()

        def _tool_result_preview_lines(self) -> int:
            if isinstance(self.session, TuiSessionView):
                value = self.session.snapshot["sidebar"].get("toolResultPreviewLines", 100)
            else:
                value = getattr(self.session.settings_manager, "get_tui_tool_result_preview_lines", lambda: 100)()
            return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 1000 else 100

        def _bounded_tool_panel(self, text: str, *, result: bool = False) -> list[str]:
            """Bound result previews by source lines before terminal wrapping.

            Tool headings and plans use their normal presentation bounds; only
            result payloads use the configurable logical-line preview setting.
            """
            if not result:
                return self._format_chat_panel("tool", text, pad_y=0)
            logical_lines = text.splitlines() or [text]
            limit = self._tool_result_preview_lines()
            truncated = len(logical_lines) > limit
            visible = "\n".join(logical_lines[:limit])
            # The transport/display byte cap remains authoritative. State that
            # fact rather than implying that a short rendered preview is full.
            byte_cutoff = len(visible.encode("utf-8")) > MAX_SOURCE_DISPLAY_BYTES
            panel_lines = self._format_chat_panel("tool", visible, pad_y=0)
            if truncated:
                panel_lines.append(f"[preview limited to {limit} logical lines]")
            elif byte_cutoff:
                panel_lines.append("[preview stopped at display byte limit]")
            return panel_lines

        def _finish_tool_block(self, tool_name: str, status: str, elapsed_ms: Any = None) -> bool:
            """Append a completed status to the matching active tool block."""
            active = self._active_tool_block
            if active is None:
                return False
            active_name, start, end, text = active
            if active_name != tool_name or not (0 <= start <= end <= len(self._stream_lines)):
                return False
            self._replace_stream_slice(start, end, self._bounded_tool_panel(f"{text} [{status}]"))
            badge = "✓" if status in {"ok", "success"} else "△" if status in {"warning", "disabled"} else "×"
            if start > 0 and self._stream_lines[start - 1].startswith("Tool:"):
                elapsed = _format_elapsed_ms(elapsed_ms)
                self._set_stream_line(start - 1, f"Tool: {badge} {tool_name}" + (f" · {elapsed}" if elapsed else ""))
            self._active_tool_block = None
            self._active_tool_call_id = None
            self._active_subagent_link_tokens = []
            self._trim_stream(render=False)
            self._schedule_stream_render()
            return True

        def _reset_turn_timing(self) -> None:
            self._turn_timing_id = None
            self._turn_provider_ids.clear()
            self._turn_provider_elapsed_ms = 0.0
            self._turn_provider_measurements = 0
            self._turn_timing_reported = False

        def _write_turn_timing_summary(self, event: dict[str, Any]) -> None:
            turn_id = event.get("turnId")
            if self._turn_timing_reported or not turn_id or turn_id != self._turn_timing_id:
                return
            self._turn_timing_reported = True
            elapsed = _format_elapsed_ms(event.get("elapsedMs"))
            if elapsed is None:
                self._turn_timing_id = None
                self._turn_provider_ids.clear()
                self._turn_provider_elapsed_ms = 0.0
                self._turn_provider_measurements = 0
                return
            summary = f"Time: turn {elapsed}"
            if self._turn_provider_measurements:
                provider = _format_elapsed_ms(self._turn_provider_elapsed_ms)
                if provider:
                    request_word = "request" if self._turn_provider_measurements == 1 else "requests"
                    summary += f" · provider {provider} / {self._turn_provider_measurements} {request_word}"
            self._write(summary, "info")
            self._turn_timing_id = None
            self._turn_provider_ids.clear()
            self._turn_provider_elapsed_ms = 0.0
            self._turn_provider_measurements = 0

        def _apply_theme(self, theme_name: str) -> bool:
            theme = BUILTIN_TUI_THEMES.get((theme_name or "").strip().lower())
            if theme is None:
                return False
            self._theme = theme
            # Theme styles are part of cached Content. Width is keyed at render
            # time; clear eagerly for a theme transition.
            self._stream_content_cache.clear()
            try:
                self.screen.styles.background = theme.screen_bg
                self.screen.styles.color = theme.screen_fg
            except Exception:
                pass
            try:
                stream_widget = self.query_one("#stream_container")
                stream_widget.styles.background = theme.panel_bg
                stream_widget.styles.border = None
                stream_widget.styles.color = theme.screen_fg
            except Exception:
                pass
            try:
                input_widget = self.query_one("#input", TextArea)
                input_widget.styles.background = theme.input_bg
                input_widget.styles.border = ("round", theme.input_border)
                input_widget.styles.color = theme.input_fg
            except Exception:
                pass
            try:
                sidebar_widget = self.query_one("#sidebar", Static)
                sidebar_widget.styles.background = theme.sidebar_bg
                sidebar_widget.styles.border = ("round", theme.panel_border)
                sidebar_widget.styles.color = theme.screen_fg
            except Exception:
                pass
            try:
                header_widget = self.query_one("#header", Static)
                header_widget.styles.background = theme.sidebar_bg
                header_widget.styles.border = ("round", theme.panel_border)
                header_widget.styles.color = theme.screen_fg
            except Exception:
                pass
            try:
                logo_widget = self.query_one("#logo", Static)
                logo_widget.styles.color = theme.screen_fg
            except Exception:
                pass
            try:
                stream_text = self.query_one("#stream", Static)
                stream_text.styles.color = theme.screen_fg
            except Exception:
                pass
            try:
                conversation = self.query_one("#conversation")
                conversation.styles.background = theme.panel_bg
                conversation.styles.border = ("round", theme.panel_border)
            except Exception:
                pass
            try:
                waiting_status = self.query_one("#waiting_status", Static)
                waiting_status.styles.background = theme.panel_bg
                waiting_status.styles.color = theme.info
            except Exception:
                pass
            try:
                ext_panel = self.query_one("#ext_panel", Static)
                ext_panel.styles.color = theme.screen_fg
            except Exception:
                pass
            try:
                ext_overlay = self.query_one("#ext_overlay", Static)
                ext_overlay.styles.color = theme.screen_fg
            except Exception:
                pass
            try:
                shortcuts_overlay = self.query_one("#shortcuts_overlay", Static)
                shortcuts_overlay.styles.background = theme.panel_bg
                shortcuts_overlay.styles.border = ("heavy", theme.input_border)
                shortcuts_overlay.styles.color = theme.screen_fg
            except Exception:
                pass
            try:
                model_overlay = self.query_one("#model_overlay", Static)
                model_overlay.styles.background = theme.panel_bg
                model_overlay.styles.border = ("heavy", theme.panel_border)
                model_overlay.styles.color = theme.screen_fg
            except Exception:
                pass
            try:
                agents_overlay = self.query_one("#agents_overlay", Vertical)
                agents_overlay.styles.background = theme.panel_bg
                agents_overlay.styles.border = ("heavy", theme.panel_border)
                agents_overlay.styles.color = theme.screen_fg
                self.query_one("#agents_help", Static).styles.color = theme.screen_fg
                self.query_one("#agents_content", Static).styles.color = theme.screen_fg
            except Exception:
                pass
            try:
                approval_overlay = self.query_one("#approval_overlay", Vertical)
                approval_overlay.styles.background = theme.panel_bg
                approval_overlay.styles.border = ("heavy", theme.panel_border)
                approval_overlay.styles.color = theme.screen_fg
                approval_summary = self.query_one("#approval_summary", Static)
                approval_summary.styles.background = theme.panel_bg
                approval_summary.styles.color = theme.screen_fg
                reason = self.query_one("#approval-reason", Input)
                reason.styles.background = theme.panel_bg
                reason.styles.color = theme.screen_fg
                reason.styles.border = ("round", theme.input_border)
                approval_actions = self.query_one("#approval_actions", Horizontal)
                approval_actions.styles.background = theme.panel_bg
                approval_actions.styles.color = theme.screen_fg
                approve = self.query_one("#approval-approve", Button)
                approve.styles.background = theme.user_bg
                approve.styles.color = theme.user
                approve.styles.border = ("round", theme.user)
                reject = self.query_one("#approval-reject", Button)
                reject.styles.background = theme.panel_bg
                reject.styles.color = theme.error
                reject.styles.border = ("round", theme.error)
                if self._approval_pending is not None:
                    approval_summary.update(self._approval_summary(self._approval_pending))
            except Exception:
                pass
            try:
                evidence_overlay = self.query_one("#evidence_overlay", Vertical)
                evidence_overlay.styles.background = theme.panel_bg
                evidence_overlay.styles.border = ("heavy", theme.panel_border)
                evidence_overlay.styles.color = theme.screen_fg
                self.query_one("#evidence_header", Static).styles.color = theme.screen_fg
                self.query_one("#evidence_body", VerticalScroll).styles.background = theme.panel_bg
                self.query_one("#evidence_body", VerticalScroll).styles.color = theme.screen_fg
                self.query_one("#evidence_content", Static).styles.color = theme.screen_fg
                evidence_actions = self.query_one("#evidence_actions", Horizontal)
                evidence_actions.styles.background = theme.panel_bg
                evidence_actions.styles.color = theme.screen_fg
                evidence_prev = self.query_one("#evidence-prev", Button)
                evidence_prev.styles.background = theme.user_bg
                evidence_prev.styles.color = theme.user
                evidence_prev.styles.border = ("round", theme.user)
                evidence_next = self.query_one("#evidence-next", Button)
                evidence_next.styles.background = theme.user_bg
                evidence_next.styles.color = theme.user
                evidence_next.styles.border = ("round", theme.user)
                evidence_close = self.query_one("#evidence-close", Button)
                evidence_close.styles.background = theme.panel_bg
                evidence_close.styles.color = theme.error
                evidence_close.styles.border = ("round", theme.error)
            except Exception:
                pass
            # A facade-originated theme change need not add transcript output,
            # so force a repaint even when the logical lines are unchanged.
            self._rendered_stream_lines = ()
            self._schedule_stream_render()
            return True

        def _set_theme(self, theme_name: str) -> bool:
            """Apply and persist a theme for both /theme and Ctrl+P callers."""
            if isinstance(self.session, TuiSessionView):
                asyncio.create_task(self._run_view_command("set_theme", {"theme": theme_name}))
                return True
            if not self._apply_theme(theme_name):
                return False
            if not isinstance(self.session, TuiSessionView):
                self.session.settings_manager.set_theme(theme_name)
            self._refresh_sidebar()
            self._write(f"Theme set to {theme_name}", "info")
            return True

        async def _run_view_command(self, operation: str, payload: dict[str, Any]) -> bool:
            """Run an explicit facade mutation and make every failure visible."""
            try:
                await self.session.command("ui", operation=operation, payload=payload)
                if operation == "set_theme":
                    self._apply_theme(str(self.session.snapshot["sidebar"].get("theme", "default")))
                self._refresh_sidebar()
                return True
            except Exception as exc:
                self._handle_session_event(
                    {"type": "tui_command_error", "operation": operation, "error": str(exc)}
                )
                return False

        def _toast(self, message: str, severity: str = "information", timeout: float = 1.8) -> None:
            notify_fn = getattr(self, "notify", None)
            if callable(notify_fn):
                try:
                    notify_fn(message, severity=severity, timeout=timeout)
                    return
                except TypeError:
                    try:
                        notify_fn(message)
                        return
                    except Exception:
                        return
                except Exception:
                    return

        def _copy_to_clipboard(self, text: str) -> tuple[str, str]:
            payload = text or ""
            if not payload:
                return ("failed", "Empty selection")
            # Prefer native clipboard tools: these give real copy semantics.
            system_commands: list[list[str]] = [
                ["wl-copy"],
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
                ["pbcopy"],
                ["clip.exe"],
                ["clip"],
            ]
            for cmd in system_commands:
                if shutil.which(cmd[0]) is None:
                    continue
                try:
                    subprocess.run(
                        cmd, input=payload, text=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    return ("copied", cmd[0])
                except Exception:
                    continue
            try:
                import pyperclip  # type: ignore

                pyperclip.copy(payload)
                return ("copied", "pyperclip")
            except Exception:
                pass
            try:
                # Textual clipboard request may be ignored by some terminals.
                copy_fn = getattr(self, "copy_to_clipboard", None)
                if callable(copy_fn):
                    copy_fn(payload)
                    return ("attempted", "terminal")
            except Exception:
                pass
            return ("failed", "no clipboard backend")

        def _runtime_status(self, snapshot: dict[str, Any]) -> str:
            """Return the concise runtime state shared by the header and sidebar."""
            if self._approval_pending is not None:
                return "awaiting approval"
            if self._ask_user_pending is not None:
                return "waiting for input"
            if snapshot["compacting"]:
                return "compacting"
            if self._retry_state != "idle":
                return "retrying"
            if snapshot["streaming"] or self._turn_active:
                return "working"
            return "ready"

        def _refresh_header(self, snapshot: dict[str, Any]) -> None:
            """Render the compact status panel without rebuilding details."""
            model = sanitize_display_text(str(snapshot["model"]))
            coop = "ON" if snapshot["coop"] == "on" or self._approval_pending is not None else "OFF"
            if self._approval_pending is not None:
                coop += " (PENDING)"
            thinking = sanitize_display_text(str(snapshot["thinking"]))
            runtime_status = self._runtime_status(snapshot)
            temperature_mode = sanitize_display_text(str(snapshot["temperatureMode"]))
            suffix = (
                f" | Mode: {temperature_mode} ({snapshot['temperature']:.1f})"
                f" | think: {thinking} | coop: {coop.lower()} | status: {runtime_status}"
            )
            prefix = f"v{VERSION} | "
            # Account for main padding, header borders, and header padding.
            # Keep the variable provider/model segment within the available
            # width first; CSS still clips safely at exceptionally narrow sizes.
            available_width = max(1, self._main_width() - 6)
            max_model_length = max(1, available_width - len(prefix) - len(suffix))
            if len(model) > max_model_length:
                model = model[: max_model_length - 1] + "…" if max_model_length > 1 else "…"
            header = Text(f"{prefix}{model}{suffix}")
            try:
                header_widget = self.query_one("#header", Static)
                header_widget.styles.height = 3
                header_widget.update(header)
            except Exception:
                pass
            self._refresh_logo()

        def _apply_information_layout(self) -> None:
            try:
                main_widget = self.query_one("#main")
                main_widget.set_class(self._layout_mode == "wide", "layout-wide")
            except Exception:
                pass
            try:
                self.query_one("#sidebar", Static).styles.display = "block" if self._layout_mode == "wide" else "none"
            except Exception:
                pass
            try:
                self.query_one("#header", Static).styles.display = "block" if self._layout_mode == "compact" else "none"
            except Exception:
                pass
            # The layout pass updates #main's region. Refresh the independent
            # logo afterward so its breakpoint uses that rendered width.
            try:
                self.call_after_refresh(self._refresh_logo)
            except Exception:
                pass

        @staticmethod
        def _context_meter(percent: float) -> str:
            if not 0 <= percent <= 100:
                return "unknown"
            filled = min(10, max(0, round(percent / 10)))
            return f"{'#' * filled}{'-' * (10 - filled)} {percent:.1f}%"

        def _refresh_sidebar(self) -> None:
            if self._is_unmounting:
                return
            s = build_sidebar_snapshot(self.session)
            cwd_key = os.fspath(s["cwd"])
            self._sidebar_cwd = cwd_key
            self._refresh_header(s)
            if s["compacting"]:
                status = "compacting…"
            elif self._retry_state != "idle":
                status = "retrying…"
            elif s["streaming"]:
                status = "working…"
            else:
                status = "idle"
            if not s["mcpEnabled"]:
                mcp_lines = ["off"]
            elif not s["mcpServers"]:
                mcp_lines = ["none"]
            else:
                mcp_lines = []
            plan = (
                self.session.sidebar.get("plan") or self.session.sidebar.get("planText", "")
                if isinstance(self.session, TuiSessionView)
                else getattr(self.session, "_plan", None)
            )

            info_block = Text()
            info_block.append_text(Text.from_markup(f"[b {self._theme.info}]Info[/]"))
            info_block.append("\n")
            info_block.append(f"Version: {VERSION}\n")
            info_block.append(f"Model: {sanitize_display_text(s['model'])}\n")
            info_block.append(f"Theme: {sanitize_display_text(self._theme.name)}\n")
            info_block.append(f"Thinking: {sanitize_display_text(s['thinking'])}\n")
            info_block.append(f"Mode: {sanitize_display_text(s['temperatureMode'])} ({s['temperature']:.1f})\n")
            context = self._context_meter(s["contextPercent"]) if s["contextKnown"] else "unknown"
            info_block.append(f"Input budget: {context}\n")
            info_block.append(f"Retry: {sanitize_display_text(s['retry'])}\n")
            info_block.append(f"Status: {sanitize_display_text(status)}\n")
            info_block.append(f"Delivery: {sanitize_display_text(s['deliveryMode'])}\n")
            info_block.append(f"coop: {'on' if s['coop'] == 'on' or self._approval_pending is not None else 'off'}\n")
            running_agents = sum(
                1 for child in self._monitor_snapshot.get("children", []) if child.get("status") == "running"
            )
            info_block.append(f"Subagents: {'on' if s['subagents'] else 'off'} ({running_agents} running)\n")
            info_block.append(f"Details: {'on' if s['bashOutput'] else 'off'}\n")
            cwd = sanitize_display_text(cwd_key)
            branch = self._cached_git_branch(cwd_key)
            cwd_display = f"{cwd} ({sanitize_display_text(branch)})" if branch else cwd
            info_block.append(f"cwd: {cwd_display}\n")
            info_block.append(f"Session: {sanitize_display_text(s['sessionId'])}\n")

            plan_block = Text()
            if plan:
                display = _sidebar_plan_window(plan)
                display = sanitize_display_text(display)
                plan_block.append_text(Text.from_markup(f"[b {self._theme.info}]Plan[/]"))
                plan_block.append("\n")
                plan_block.append(display + "\n")

            mcp_block = Text()
            mcp_block.append_text(Text.from_markup(f"[b {self._theme.info}]MCP[/]"))
            mcp_block.append("\n")
            if s["mcpEnabled"] and s["mcpServers"]:
                for server in s["mcpServers"]:
                    if not isinstance(server, dict):
                        continue
                    available = bool(server.get("available", False))
                    marker = "[ok]" if available else "[!]"
                    marker_style = self._theme.user if available else self._theme.error
                    mcp_block.append(marker, style=marker_style)
                    mcp_block.append(f" {sanitize_display_text(str(server.get('name') or ''))}\n")
            else:
                mcp_block.append("\n".join(mcp_lines) + "\n")

            keys_block = Text()
            keys_block.append_text(Text.from_markup(f"[b {self._theme.info}]Keys[/]"))
            keys_block.append("\n")
            keys_block.append(format_tui_shortcuts() + "\n")

            sidebar = Text()
            sidebar.append_text(info_block)
            sidebar.append_text(plan_block)
            sidebar.append("\n")
            sidebar.append_text(mcp_block)
            sidebar.append("\n")
            sidebar.append_text(keys_block)

            try:
                self.query_one("#sidebar", Static).update(sidebar)
            except Exception:
                pass
            self._apply_information_layout()

        def _cached_git_branch(self, cwd: str) -> str | None:
            """Return a fresh cached branch and request a non-blocking refresh.

            ``get_git_branch`` intentionally remains synchronous for its
            command-line/helper callers, but is invoked only by ``to_thread``
            from this paint path.  Failed probes are cached as ``None`` too,
            avoiding repeated failed subprocess launches on every repaint.
            """
            now = time.monotonic()
            cached = self._git_branch_cache.get(cwd)
            if cached is not None and cached[0] > now:
                return cached[1]
            if cwd not in self._git_branch_inflight:
                self._start_git_branch_probe(cwd)
            return cached[1] if cached is not None else None

        def _start_git_branch_probe(self, cwd: str) -> None:
            """Start at most one worker-thread Git probe for a cwd."""
            self._git_branch_inflight.add(cwd)
            session = self.session
            generation = self._sidebar_generation

            async def probe() -> None:
                try:
                    branch = await asyncio.to_thread(get_git_branch, cwd)
                except Exception:
                    # ``get_git_branch`` already contains expected subprocess
                    # failures, but cache an unexpected probe failure as well.
                    branch = None
                finally:
                    self._git_branch_inflight.discard(cwd)
                self._git_branch_cache[cwd] = (time.monotonic() + _GIT_BRANCH_CACHE_TTL_SECONDS, branch)
                # A probe may outlive a session switch or a cwd change.  It can
                # populate its own cache entry, but never repaint that new view.
                if (
                    self.session is not session
                    or self._sidebar_generation != generation
                    or self._sidebar_cwd != cwd
                    or not self.is_running
                ):
                    return
                try:
                    self._schedule_sidebar_refresh()
                except Exception:
                    # The app can be unmounting while a worker finishes.
                    pass

            asyncio.create_task(probe())

        def _monitor_child(self, session_id: str) -> dict[str, Any] | None:
            return next(
                (child for child in self._monitor_snapshot.get("children", []) if child.get("sessionId") == session_id),
                None,
            )

        def _clear_agent_link_metadata(self) -> None:
            """Discard trusted inline links with their transcript references."""
            self._agent_link_tokens.clear()
            self._agent_link_labels.clear()
            self._agent_links_by_line.clear()
            self._active_subagent_link_tokens = []
            self._evidence_link_tokens.clear()
            self._evidence_links_by_line.clear()
            self._evidence_link_line_ids.clear()
            self._evidence_tokens_by_line_id.clear()

        def _prune_agent_link_metadata(self) -> None:
            """Keep inline actions exactly in sync with retained transcript lines."""
            valid_tokens = {
                token
                for token, (generation, _session_id, line_index, _call_id) in self._agent_link_tokens.items()
                if generation == self._monitor_generation and 0 <= line_index < len(self._stream_lines)
            }
            self._agent_link_tokens = {
                token: target for token, target in self._agent_link_tokens.items() if token in valid_tokens
            }
            self._agent_link_labels = {
                token: label for token, label in self._agent_link_labels.items() if token in valid_tokens
            }
            indexed: dict[int, list[int]] = {}
            for token, (_generation, _session_id, line_index, _call_id) in self._agent_link_tokens.items():
                indexed.setdefault(line_index, []).append(token)
            self._agent_links_by_line = {line: tuple(tokens) for line, tokens in indexed.items()}

        def _open_agents_panel(self, session_id: str | None = None) -> None:
            # A lifecycle event can reach the main stream before the coalesced
            # monitor refresh tick. Exact child links therefore check a fresh
            # authoritative snapshot rather than the cached one.
            if session_id is not None:
                self._refresh_monitor_snapshot()
            if session_id is not None and self._monitor_child(session_id) is None:
                self._write("Agent preview is no longer available.", "info")
                return
            children = self._monitor_snapshot.get("children", [])
            if session_id is not None:
                self._selected_agent_id = session_id
            elif self._selected_agent_id is None and children:
                self._selected_agent_id = str(children[0].get("sessionId") or "")
            self._agents_panel_open = True
            self._render_agents_panel()
            try:
                self.query_one("#agents_overlay", Vertical).add_class("visible")
            except Exception:
                pass

        def _close_agents_panel(self) -> None:
            self._agents_panel_open = False
            try:
                self.query_one("#agents_overlay", Vertical).remove_class("visible")
                self.query_one("#input", TextArea).focus()
            except Exception:
                pass

        def _handle_agents_panel_key(self, event: events.Key) -> bool:
            """Handle panel navigation from either the input or the app."""
            if not self._agents_panel_open or event.key not in {"left", "right", "up", "down", "escape"}:
                return False
            if event.key == "escape":
                self._close_agents_panel()
            elif event.key in {"left", "right"}:
                self._select_agent_offset(-1 if event.key == "left" else 1)
            else:
                try:
                    timeline = self.query_one("#agents_timeline", VerticalScroll)
                    timeline.scroll_to(y=timeline.scroll_y + (-3 if event.key == "up" else 3), animate=False)
                except Exception:
                    pass
            event.stop()
            event.prevent_default()
            return True

        def _select_agent_offset(self, offset: int) -> None:
            children = self._monitor_snapshot.get("children", [])
            if not children:
                return
            ids = [str(child.get("sessionId") or "") for child in children]
            try:
                index = ids.index(self._selected_agent_id or "")
            except ValueError:
                index = 0
            self._selected_agent_id = ids[(index + offset) % len(ids)]
            self._render_agents_panel()

        def _render_agents_panel(self) -> None:
            if not self._agents_panel_open:
                return
            children = self._monitor_snapshot.get("children", [])
            if self._selected_agent_id and self._monitor_child(self._selected_agent_id) is None:
                self._selected_agent_id = None
            if self._selected_agent_id is None and children:
                self._selected_agent_id = str(children[0].get("sessionId") or "")
            help_text = Text("Agents — current session only (read-only)\n", style="bold")
            help_text.append("Left/Right select · Up/Down scroll timeline · Esc close")
            if not children:
                content = Text("No direct child agents in this session.\n")
            else:
                selected = self._monitor_child(self._selected_agent_id or "")
                content = Text()
                if selected is not None:
                    position = next(
                        (
                            index
                            for index, child in enumerate(children, 1)
                            if child.get("sessionId") == self._selected_agent_id
                        ),
                        1,
                    )
                    task = sanitize_display_text(str(selected.get("task") or ""))[:120]
                    model = sanitize_display_text(str(selected.get("model") or ""))[:80]
                    elapsed = max(
                        0.0,
                        (selected.get("completedAt") or time.monotonic())
                        - float(selected.get("startedMonotonic") or time.monotonic()),
                    )
                    content.append(f"Agent {position}/{len(children)} · {task}\n", style="bold")
                    content.append(f"Model: {model} · {elapsed:.1f}s\n")
                    tool = selected.get("currentTool")
                    activity = sanitize_display_text(str(selected.get("activity") or ""))
                    content.append(f"Status: {sanitize_display_text(str(selected.get('status') or 'unknown'))}")
                    if tool:
                        content.append(f" · Tool: {sanitize_display_text(str(tool))}")
                    if activity:
                        content.append(f" · {activity}")
                    content.append("\n\nTimeline\n", style="bold")
                    if selected.get("timelineTruncated") or selected.get("previewTruncated"):
                        content.append("[earlier preview text truncated]\n")
                    timeline = [item for item in selected.get("timeline", []) if isinstance(item, dict)]
                    has_response = any(item.get("kind") == "response" and item.get("text") for item in timeline)
                    if not has_response:
                        # Tool blocks below still make current activity clear;
                        # avoid implying the child has done nothing.
                        content.append("[no visible response text]\n")
                    for item in timeline:
                        if item.get("kind") == "response":
                            text = sanitize_display_text(str(item.get("text") or ""))
                            if text:
                                content.append(text)
                                if not text.endswith("\n"):
                                    content.append("\n")
                            continue
                        if item.get("kind") != "tool":
                            continue
                        name = sanitize_display_text(str(item.get("tool") or "tool"))
                        running = item.get("state") == "running"
                        badge = "○" if running else ("✓" if item.get("ok") else "×")
                        elapsed = _format_elapsed_ms(item.get("elapsedMs"))
                        timeout = item.get("effectiveTimeout")
                        timeout_text = (
                            f" (timeout {int(timeout)}s)" if isinstance(timeout, (int, float)) and timeout > 0 else ""
                        )
                        content.append(
                            f"Tool: {badge} {name}"
                            + (timeout_text if running else "")
                            + (f" · {elapsed}" if elapsed else "")
                            + "\n"
                        )
                        args = sanitize_display_text(str(item.get("args") or ""))
                        if args:
                            content.append(f"Arguments: {args}\n")
                        result = sanitize_display_text(str(item.get("output") or ""))
                        if result:
                            content.append("Result:\n" if item.get("ok") else "Error:\n")
                            content.append(result)
                            content.append("\n")
            try:
                self.query_one("#agents_help", Static).update(help_text)
                self.query_one("#agents_content", Static).update(content)
            except Exception:
                pass

        def _copy_selected_stream_text(self, text: str) -> bool:
            """Copy an already-snapshotted selection exactly once."""
            if not text.strip() or text == self._last_auto_copied:
                return False
            status, _backend = self._copy_to_clipboard(text)
            if status == "copied":
                self._last_auto_copied = text
                self._toast("selection copied", severity="information")
            elif status == "attempted":
                self._toast("clipboard request sent to terminal (OSC52). terminal may ignore it.", severity="warning")
            else:
                self._toast("no clipboard backend. Install wl-clipboard/xclip/xsel or pyperclip.", severity="warning")
            return True

        def _copy_transcript_selection(self, widget: Any, selection: Any) -> None:
            """Copy a stable drag only after every virtual source row is present."""
            if selection is None:
                return
            if not isinstance(self.session, TuiSessionView):
                text, _separator = widget.get_selection(selection)
                self._copy_selected_stream_text(text)
                return
            self._selection_copy_generation += 1
            token, generation, epoch = self._selection_copy_generation, self._session_listener_generation, self.session.epoch

            async def copy() -> None:
                from bisect import bisect_right

                from textual.geometry import Offset
                from textual.selection import Selection

                start = selection.start.y if selection.start is not None else 0
                end = selection.end.y if selection.end is not None else widget._offsets[-1] - 1
                if start > end:
                    start, end = end, start
                deadline = time.monotonic() + _SELECTION_COPY_TIMEOUT_SECONDS

                # Do not clamp a selection to provisional virtual geometry.
                # The first page establishes a completed block's real physical
                # height asynchronously; wait for that height to reach the
                # widget, then either snapshot every selected row or fail closed.
                while end >= widget._offsets[-1]:
                    for index, line in enumerate(self._stream_lines):
                        if line.startswith("\0completed:"):
                            self._need_completed_virtual_row(line.removeprefix("\0completed:"), 0)
                    if time.monotonic() >= deadline:
                        self._toast("Copy unavailable: selection geometry did not settle.", severity="warning")
                        return
                    await asyncio.sleep(0.01)
                    geometry_settled = True
                    for index, line in enumerate(self._stream_lines):
                        if not line.startswith("\0completed:"):
                            continue
                        virtual = self._completed_virtuals.get(line.removeprefix("\0completed:"))
                        if virtual is None or not virtual.get("geometry_loaded"):
                            geometry_settled = False
                            continue
                        if widget._offsets[index + 1] - widget._offsets[index] != virtual["total"]:
                            geometry_settled = False
                    if geometry_settled:
                        self._toast("Copy unavailable: selection geometry changed.", severity="warning")
                        return
                geometry_offsets = tuple(widget._offsets)
                wanted: list[tuple[int, str | None, int]] = []
                for y in range(max(0, start), end + 1):
                    index = bisect_right(widget._offsets, y) - 1
                    ref = None
                    row = y - widget._offsets[index]
                    if 0 <= index < len(self._stream_lines) and self._stream_lines[index].startswith("\0completed:"):
                        ref = self._stream_lines[index].removeprefix("\0completed:")
                    wanted.append((y, ref, row))
                saved: dict[tuple[str, int], str] = {}
                refs = {ref for _y, ref, _row in wanted if ref is not None}
                for ref in refs:
                    virtual = self._completed_virtuals.get(ref)
                    if virtual is not None:
                        virtual["selection_copy"] = token

                def finish_copy() -> None:
                    for ref in refs:
                        virtual = self._completed_virtuals.get(ref)
                        if virtual is not None and virtual.get("selection_copy") == token:
                            virtual.pop("selection_copy", None)
                    self._stream_render_revision += 1
                    self._schedule_stream_render()

                def is_current() -> bool:
                    if (
                        token != self._selection_copy_generation
                        or generation != self._session_listener_generation
                        or epoch != self.session.epoch
                        or tuple(widget._offsets) != geometry_offsets
                    ):
                        return False
                    # Textual removes this mapping when a selection is cleared;
                    # a different drag replaces it. Do not copy an old snapshot.
                    try:
                        if widget.text_selection != selection:
                            return False
                    except Exception:
                        return False
                    return all(ref is None or "\0completed:" + ref in self._stream_lines for _y, ref, _row in wanted)

                while True:
                    if not is_current():
                        finish_copy()
                        return
                    missing: tuple[str, int] | None = None
                    for _y, ref, row in wanted:
                        if ref is None:
                            continue
                        virtual = self._completed_virtuals.get(ref)
                        if virtual is None or virtual.get("error") is not None:
                            self._toast("Copy unavailable: completed response page could not be loaded.", severity="warning")
                            finish_copy()
                            return
                        key = (ref, row)
                        # Saved rows deliberately outlive the 12-page display
                        # cache for this one explicit copy action only.
                        if key in saved:
                            continue
                        value = virtual["rows"].get(row)
                        if value is None:
                            missing = key
                            break
                        else:
                            saved[key] = value
                    if missing is None:
                        break
                    if time.monotonic() >= deadline:
                        self._toast("Copy unavailable: timed out loading the completed response.", severity="warning")
                        finish_copy()
                        return
                    # One demand at a time keeps selection fetching sequential
                    # and lets the normal per-block/global pager limits govern
                    # work already queued by the viewport.
                    self._need_completed_virtual_row(*missing)
                    self._toast("Copying selection…", timeout=0.8)
                    await asyncio.sleep(0.01)
                if not is_current():
                    finish_copy()
                    return
                rows = [saved[(ref, row)] if ref is not None else widget.render_line(y).text for y, ref, row in wanted]
                start_x = selection.start.x if selection.start else 0
                end_x = selection.end.x if selection.end else len(rows[-1])
                if selection.start and selection.end and (selection.start.y, start_x) > (selection.end.y, end_x):
                    start_x, end_x = end_x, start_x
                text = Selection(Offset(start_x, 0), Offset(end_x, len(rows) - 1)).extract("\n".join(rows))
                if is_current():
                    self._copy_selected_stream_text(text)
                finish_copy()

            asyncio.create_task(copy())

        def _try_auto_copy_selected_stream_text(self) -> None:
            # Textual 8 keeps arbitrary text selections in `screen.selections`
            # (not on the widget); `Static` has no `selected_text` attribute.
            try:
                stream_widget = self.query_one("#stream", Static)
                if stream_widget not in self.screen.selections:
                    return
                text = str(self.screen.get_selected_text() or "")
            except Exception:
                return
            self._copy_selected_stream_text(text)

        def on_mouse_up(self, event: events.MouseUp) -> None:
            # After a drag the screen retains the selection; after a plain
            # click it is cleared, so this only fires for real selections. Take
            # the snapshot before an already-queued stream refresh replaces the
            # Static content. Some Textual versions expose the selection only
            # after refresh, for which the old deferred path remains a fallback.
            try:
                stream_widget = self.query_one("#stream", TranscriptWidget)
                # TranscriptWidget owns its deferred selection snapshot. Avoid
                # the app-level fallback copying a partially loaded virtual drag.
                if stream_widget in self.screen.selections:
                    return
                selected = str(self.screen.get_selected_text() or "")
            except Exception:
                selected = ""
            if selected.strip():
                self._copy_selected_stream_text(selected)
            else:
                self.call_after_refresh(self._try_auto_copy_selected_stream_text)

        async def _handle_command(self, cmd: str) -> None:
            session = self.session
            original = cmd.strip()
            cmd = {
                "/q": "/exit",
                "/h": "/help",
                "/s": "/status",
                "/st": "/status",
                "/m": "/model",
                "/t": "/thinking",
                "/c": "/clear",
                "/qq": "/queue clear",
                "/mc": "/model-cycle",
                "/tc": "/thinking-cycle",
                "/ns": "/new",
            }.get(cmd.strip(), cmd)
            if cmd in {"/exit", "/quit"}:
                self.exit()
                return
            # Handle /history BEFORE recording (don't record the query itself).
            if cmd == "/history":
                if not self._command_history:
                    self._write("No history yet.", "info")
                else:
                    recent = self._command_history[-50:]
                    for i, entry in enumerate(recent, 1):
                        self._write(f"{i}. {entry}", "info")
                return
            if cmd.startswith("/history "):
                arg = cmd[len("/history ") :].strip()
                try:
                    idx = int(arg)
                except ValueError:
                    self._write(f"Invalid number: {arg}", "error")
                    return
                recent = self._command_history[-50:]
                if idx < 1 or idx > len(recent):
                    self._write(f"Index out of range (1..{len(recent)})", "error")
                    return
                history_cmd = recent[idx - 1]
                input_widget = self.query_one("#input", TextArea)
                input_widget.text = history_cmd
                input_widget.focus()
                return
            # Record slash commands, retaining their typed alias. /history is
            # handled above so inspecting history never records itself.
            if original.startswith("/"):
                self._record_input_history(original)
            if cmd == "/help":
                self._write("/exit /quit | /help | /stats | /state /status | /queue | /tools | /clear | /abort", "info")
                self._write(
                    "/model [provider/model] | /model-cycle | /providers [number|name [model-number|id]] | /thinking [level] | /thinking-cycle | /theme [name]",
                    "info",
                )
                self._write(
                    "/steer <text> | /follow <text> | /compact [instructions] | /tree | /navigate <id> [--summary <text>] | /fork <id> | /new | /sessions [number|full exact name] | /login [status|refresh <provider>|provider [apiKey] [model] [subscription]] | /logout <provider>",
                    "info",
                )
                self._write(
                    "/sessions rename <number|full exact name> <new name> | /sessions delete <number|full exact name>",
                    "info",
                )
                self._write(
                    "/retry <on|off|unlimited> | /retry-cycle | /config [key] [value] | /extui <list|request|respond|cancel|clear>",
                    "info",
                )
                self._write(
                    "/skill [list] | /skill:<name> [args] | /cooperation [on|off] | /subagents [on|off] | /details-show [on|off] | /history [n] (last 50 inputs; Ctrl+Up/Down) | /mcp [list|enable|disable] | /bash <command> | /paste-image",
                    "info",
                )
                self._write("/inspect-timeout", "info")
                self._write("/layout <compact|wide|focus>", "info")
                self._write("  compact: header visible; sidebar hidden", "info")
                self._write("  wide: header hidden; sidebar visible", "info")
                self._write("  focus: header and sidebar hidden", "info")
                self._write("/sidebar <hide|show>", "info")
                self._write("  hide: hide the sidebar; show: switch to wide", "info")
                self._write("/agents (current-session direct children; read-only)", "info")
                return
            if cmd == "/clear":
                self.action_clear_stream()
                return
            if cmd == "/agents":
                self._open_agents_panel()
                return
            if cmd == "/paste-image":
                self.action_paste_image()
                return
            self._assistant_has_live_delta = False
            self._assistant_live_start_idx = -1
            self._assistant_live_buffer = ""
            self._assistant_live_line_count = 0
            # The production TUI is always a detached view.  Route the legacy
            # administrative surface before its historical in-process handlers
            # below so none of these operations can reach a live session object.
            if isinstance(session, TuiSessionView) and await self._handle_detached_command(cmd):
                return
            if cmd == "/abort":
                await session.abort()
                self._turn_active = False
                self._remove_thinking_line()
                self._schedule_stream_render()
                self._write("[abort requested]", "warn")
                return
            if cmd == "/status":
                s = build_sidebar_snapshot(session)
                self._write(json.dumps(s, ensure_ascii=False), "info")
                return
            if cmd.startswith("/layout"):
                mode = cmd[len("/layout") :].strip().lower()
                if mode not in {"compact", "wide", "focus"}:
                    self._write("Usage: /layout <compact|wide|focus>", "error")
                    return
                # This is deliberately runtime-only: compact retains the
                # logo/status panel, wide adds the detailed sidebar, and focus
                # removes both information panels.
                self._layout_mode = mode
                self._refresh_sidebar()
                self._write(f"Layout set to {mode}.", "info")
                return
            if cmd.startswith("/sidebar"):
                mode = cmd[len("/sidebar") :].strip().lower()
                if mode not in {"hide", "show"}:
                    self._write("Usage: /sidebar <hide|show>", "error")
                    return
                if mode == "show":
                    self._layout_mode = "wide"
                elif self._layout_mode == "wide":
                    self._layout_mode = "compact"
                self._refresh_sidebar()
                self._write("Sidebar shown." if mode == "show" else "Sidebar hidden.", "info")
                return
            if cmd == "/stats":
                self._write(json.dumps(session.get_session_stats(), ensure_ascii=False), "info")
                return
            if cmd == "/state":
                self._write(
                    json.dumps(
                        {
                            "model": {"provider": session.model.provider, "id": session.model.id}
                            if session.model
                            else None,
                            "thinkingLevel": session.thinking_level,
                            "isStreaming": session.is_streaming,
                            "pendingMessageCount": session.pending_message_count,
                            "pendingQueues": session.get_pending_queues(),
                            "activeTools": session.active_tools,
                            "sessionId": session.session_id,
                            "sessionFile": session.session_file,
                        },
                        ensure_ascii=False,
                    ),
                    "info",
                )
                return
            if cmd == "/tools":
                self._write(json.dumps({"tools": session.active_tools}, ensure_ascii=False), "info")
                return
            if cmd == "/sessions":
                await self._list_sessions()
                return
            if cmd.startswith("/sessions "):
                rest = cmd[len("/sessions ") :].strip()
                if rest.startswith("delete "):
                    target, error = self._resolve_session_target(rest[len("delete ") :])
                    if error:
                        self._write(error, "error")
                    elif session.is_streaming:
                        self._write("Stop or wait for the active turn before deleting a session.", "error")
                    else:
                        self._session_delete_pending = target
                        self._write(
                            f"Delete session '{target.name or '(unnamed)'}'? Type yes to confirm, anything else cancels.",
                            "warn",
                        )
                    return
                if rest.startswith("rename "):
                    rename_rest = rest[len("rename ") :].strip()
                    target_text, new_name, error = self._parse_session_rename_target(rename_rest)
                    if error:
                        self._write(error, "error")
                        return
                    target, error = self._resolve_session_target(target_text)
                    if error:
                        self._write(error, "error")
                        return
                    if session.is_streaming:
                        self._write("Stop or wait for the active turn before renaming a session.", "error")
                        return
                    try:
                        normalized = normalize_session_name(new_name)
                        duplicate = next(
                            (
                                info
                                for info in self._session_infos()
                                if info.path != target.path and info.name == normalized
                            ),
                            None,
                        )
                        if duplicate is not None:
                            raise ValueError("A session with that name already exists")
                        if not isinstance(session, TuiSessionView):
                            if target.id == session.session_id:
                                session.set_session_name(normalized)
                                manager = session.session_manager
                                renamed = manager.get_session_name()
                            else:
                                manager = SessionManager.open(target.path, session.session_manager.session_dir)
                                manager.set_session_name(normalized)
                                renamed = manager.get_session_name()
                        else:
                            await session.command(
                                "ui", operation="rename_session", payload={"path": target.path, "name": normalized}
                            )
                            renamed = normalized
                    except ValueError as exc:
                        self._write(str(exc), "error")
                        return
                    self._write(f"Renamed session to: {renamed}.", "info")
                    self._invalidate_session_list_snapshot()
                    self._refresh_sidebar()
                    return
                target, error = self._resolve_session_target(rest)
                if error:
                    self._write(error, "error")
                    return
                await self._switch_to_session(target)
                return
            if cmd == "/inspect-timeout":
                diag = session.inspect_subagent_timeout()
                if not diag:
                    self._write("No subagent-timeout diagnostics available.", "info")
                else:
                    lines = [
                        f"operation: {diag.get('operation', 'unknown')}",
                        f"errorType: {diag.get('errorType', 'unknown')}",
                        f"externalState: {diag.get('externalState', 'unknown')}",
                        f"sessionId: {diag.get('sessionId', 'unknown')}",
                        f"lastTool: {diag.get('lastTool', 'unknown')}",
                        f"lastEvent: {diag.get('lastEvent', 'unknown')}",
                        f"elapsedSec: {diag.get('elapsedSec', 'N/A')}",
                        f"error: {diag.get('error', '(none)')}",
                        f"summary: {diag.get('summary', '(none)')}",
                        f"actionable: {diag.get('actionableHint', '')}",
                    ]
                    self._write("\n".join(lines), "info")
                return
            if cmd == "/reload":
                info = await session.reload()
                skills = info.get("skills", [])
                diagnostics = info.get("diagnostics", [])
                self._write("Resources reloaded.", "info")
                if skills:
                    names = ", ".join(s.get("name", "") for s in skills)
                    self._write(f"Skills: {names}", "info")
                if diagnostics:
                    for d in diagnostics:
                        self._write(f"  ⚠ {d}", "warn")
                self._refresh_sidebar()
                return
            # /skill and /skill list list available skills; /skill: remains an alias.
            if cmd in {"/skill", "/skill list"}:
                skills_info = session.resource_loader.get_skills()
                skills = skills_info.get("skills", [])
                if not skills:
                    self._write(
                        "No skills discovered. Place SKILL.md files in project/global skill directories.", "info"
                    )
                else:
                    self._write("Available skills:", "info")
                    for s in skills:
                        self._write(f"  {s['name']}: {s['description'][:80]}", "info")
                return
            # /skill:<name> [args] — explicit skill invocation.
            if cmd.startswith("/skill:"):
                rest = cmd[len("/skill:") :].strip()
                if not rest:
                    skills_info = session.resource_loader.get_skills()
                    skills = skills_info.get("skills", [])
                    if not skills:
                        self._write(
                            "No skills discovered. Place SKILL.md files in project/global skill directories.", "info"
                        )
                    else:
                        self._write("Available skills:", "info")
                        for s in skills:
                            self._write(f"  {s['name']}: {s['description'][:80]}", "info")
                    return
                # Parse name (up to first space) and arguments.
                parts = rest.split(None, 1)
                skill_name = parts[0]
                skill_args = parts[1] if len(parts) > 1 else ""
                result = await session.invoke_skill(skill_name, skill_args)
                if result.get("ok"):
                    self._write(f"Skill '{skill_name}' loaded ({result.get('bodyLength', 0)} chars).", "info")
                    # Trust warning for unreviewed skills.
                    self._write(
                        "⚠ Trust warning: skill instructions are loaded as user input. "
                        "Review the skill before granting cooperation approval for file operations.",
                        "warn",
                    )
                else:
                    err_type = result.get("errorType", "SkillError")
                    err_msg = result.get("error", "unknown")
                    if err_type == "BusySessionError":
                        self._write(f"BusySessionError: {err_msg}", "error")
                    else:
                        self._write(f"Skill error: {err_msg}", "error")
                return
            if cmd == "/model":
                current = f"{session.model.provider}/{session.model.id}" if session.model else "none"
                detached_models = await session.models() if isinstance(session, TuiSessionView) else None
                providers = (
                    sorted({item.provider for item in detached_models})
                    if detached_models is not None
                    else session.model_registry.providers()
                )
                self._write(
                    json.dumps(
                        {"current": current, "providers": providers, "usage": "/model <provider>/<model-id>"},
                        ensure_ascii=False,
                    ),
                    "info",
                )
                return
            if cmd == "/model-cycle":
                if isinstance(session, TuiSessionView):
                    result = await session.command("ui", operation="cycle_model", payload={})
                    if not result.get("cycled", True):
                        self._write("No available models to cycle.", "error")
                    else:
                        self._write(f"Model cycled to {session.model.provider}/{session.model.id}", "info")
                    self._refresh_sidebar()
                    return
                result = await session.cycle_model()
                if not result:
                    self._write("No available models to cycle.", "error")
                    return
                session.settings_manager.set_default_provider(result.model.provider)
                session.settings_manager.set_default_model(result.model.id)
                self._write(f"Model cycled to {result.model.provider}/{result.model.id}", "info")
                self._refresh_sidebar()
                return
            if cmd == "/providers" or cmd.startswith("/providers "):
                rest = cmd[len("/providers") :].strip()
                detached_models = (
                    await session.models()
                    if isinstance(session, TuiSessionView)
                    else None
                )
                logged_in = (
                    sorted({m.provider for m in detached_models})
                    if detached_models is not None
                    else sorted({m.provider for m in session.model_registry.get_available()})
                )
                if not rest:
                    if not logged_in:
                        self._write("No logged-in providers. Use /login <provider> [apiKey] first.", "info")
                        return
                    current_provider = session.model.provider if session.model else None
                    lines = ["Logged-in providers:"]
                    for i, p in enumerate(logged_in, 1):
                        count = (
                            len([m for m in detached_models if m.provider == p])
                            if detached_models is not None
                            else len(session.model_registry.models_for_provider(p))
                        )
                        marker = "* " if p == current_provider else "  "
                        cur = f"   (current: {session.model.id})" if p == current_provider and session.model else ""
                        lines.append(f"{marker}{i}. {p}   {count} models{cur}")
                    lines.append(
                        "Usage: /providers <number|name> lists its models; /providers <number|name> <model-number|id> switches"
                    )
                    self._write("\n".join(lines), "info")
                    return
                parts = rest.split(None, 1)
                prov_arg = parts[0]
                provider = None
                if prov_arg.isdigit():
                    idx = int(prov_arg)
                    if 1 <= idx <= len(logged_in):
                        provider = logged_in[idx - 1]
                elif prov_arg in logged_in:
                    provider = prov_arg
                if not provider:
                    self._write(f"Provider not logged in or unknown: {prov_arg} (see /providers)", "error")
                    return
                models = (
                    [m for m in detached_models if m.provider == provider]
                    if detached_models is not None
                    else session.model_registry.models_for_provider(provider)
                )
                if len(parts) == 1:
                    lines = [f"{provider} models:"]
                    for i, m in enumerate(models, 1):
                        lines.append(f"  {i}. {m.id}")
                    lines.append(f"Usage: /providers {prov_arg} <model-number|id> to switch")
                    self._write("\n".join(lines), "info")
                    return
                sel = parts[1].strip()
                model = None
                if sel.isdigit():
                    midx = int(sel)
                    if 1 <= midx <= len(models):
                        model = models[midx - 1]
                else:
                    for m in models:
                        if m.id == sel:
                            model = m
                            break
                if not model:
                    self._write(f"Model not found for {provider}: {sel} (see /providers {prov_arg})", "error")
                    return
                if isinstance(session, TuiSessionView):
                    await session.command(
                        "ui", operation="set_model", payload={"provider": model.provider, "model": model.id}
                    )
                else:
                    await session.set_model(model)
                    session.settings_manager.set_default_provider(model.provider)
                    session.settings_manager.set_default_model(model.id)
                self._write(f"Model set to {model.provider}/{model.id} (saved as default)", "info")
                self._refresh_sidebar()
                return
            if cmd.startswith("/model "):
                val = cmd[len("/model ") :].strip()
                if "/" not in val:
                    # Provider-only: auto-pick the provider's first registered model.
                    provider = val
                    models = (
                        [item for item in await session.models() if item.provider == provider]
                        if isinstance(session, TuiSessionView)
                        else session.model_registry.models_for_provider(provider)
                    )
                    if not models:
                        self._write(f"Provider not found or has no models: {provider}", "error")
                        return
                    model = models[0]
                    provider, model_id = model.provider, model.id
                else:
                    provider, model_id = val.split("/", 1)
                    model = (
                        SimpleNamespace(provider=provider, id=model_id)
                        if isinstance(session, TuiSessionView)
                        else session.model_registry.resolve(provider, model_id, allow_dynamic=True)
                    )
                if not model:
                    self._write(f"Model not found: {provider}/{model_id}", "error")
                    return
                if isinstance(session, TuiSessionView):
                    await session.command(
                        "ui", operation="set_model", payload={"provider": model.provider, "model": model.id}
                    )
                else:
                    await session.set_model(model)
                    session.settings_manager.set_default_provider(model.provider)
                    session.settings_manager.set_default_model(model.id)
                is_dynamic = (
                    not any(
                        m.provider == provider and m.id == model_id
                        for m in await session.models()
                    )
                    if isinstance(session, TuiSessionView)
                    else session.model_registry.find(provider, model_id) is None
                )
                if is_dynamic:
                    self._write(f"Model set to {provider}/{model_id} (dynamic, saved as default)", "info")
                else:
                    self._write(f"Model set to {provider}/{model_id} (saved as default)", "info")
                self._refresh_sidebar()
                return
            if cmd == "/thinking":
                self._write(f"Current thinking: {session.thinking_level}", "info")
                self._write("Usage: /thinking <off|on|minimal|low|medium|high|xhigh>", "info")
                return
            if cmd.startswith("/thinking "):
                level = cmd[len("/thinking ") :].strip()
                if isinstance(session, TuiSessionView):
                    await session.command("ui", operation="set_thinking", payload={"level": level})
                else:
                    session.set_thinking_level(level)
                self._write(f"Thinking level set to {session.thinking_level}", "info")
                self._refresh_sidebar()
                return
            if cmd == "/thinking-cycle":
                if isinstance(session, TuiSessionView):
                    await session.command("ui", operation="cycle_thinking", payload={})
                    level = session.thinking_level
                else:
                    level = session.cycle_thinking_level()
                self._write(f"Thinking level cycled to {level}", "info")
                self._refresh_sidebar()
                return
            if cmd == "/temperature":
                self._write(f"Temperature: {session.temperature_mode} ({session.temperature:.1f})", "info")
                self._write("Usage: /temperature <0.0..1.2|coder|balanced|creative|experimental>", "info")
                return
            if cmd.startswith("/temperature "):
                value = cmd[len("/temperature ") :].strip()
                try:
                    from one.core.temperature import temperature_for_mode

                    temperature = temperature_for_mode(value) if value.isalpha() else float(value)
                    if isinstance(session, TuiSessionView):
                        await session.command("ui", operation="set_temperature", payload={"value": temperature})
                    else:
                        session.set_temperature(temperature)
                except ValueError as exc:
                    self._write(str(exc), "error")
                    return
                self._write(f"Temperature set to {session.temperature_mode} ({session.temperature:.1f})", "info")
                self._refresh_sidebar()
                return
            if cmd == "/queue":
                self._write(json.dumps(session.get_pending_queues(), ensure_ascii=False), "info")
                return
            if cmd.startswith("/queue "):
                mode = cmd[len("/queue ") :].strip().lower()
                if mode.startswith("clear"):
                    target = "all"
                    parts = mode.split(maxsplit=1)
                    if len(parts) == 2:
                        target = parts[1]
                    try:
                        cleared = session.clear_pending_queues(target)
                    except Exception:
                        self._write("Usage: /queue clear [all|steering|follow]", "error")
                        return
                    self._write(json.dumps(cleared, ensure_ascii=False), "info")
                    self._refresh_sidebar()
                    return
                self._write("Usage: /queue or /queue clear [all|steering|follow]", "error")
                return
            if cmd == "/theme":
                current = (
                    session.snapshot["sidebar"].get("theme", "default")
                    if isinstance(session, TuiSessionView)
                    else session.settings_manager.get_theme()
                )
                self._write(f"Current theme: {current}", "info")
                self._write("Available: " + ", ".join(sorted(BUILTIN_TUI_THEMES.keys())), "info")
                self._write("Usage: /theme <name>", "info")
                return
            if cmd.startswith("/theme "):
                name = cmd[len("/theme ") :].strip()
                if not name:
                    self._write("Usage: /theme <name>", "error")
                    return
                if not self._set_theme(name):
                    self._write("Unknown theme. Available: " + ", ".join(sorted(BUILTIN_TUI_THEMES.keys())), "error")
                    return
                return
            if cmd.startswith("/steer "):
                await session.steer(cmd[len("/steer ") :].strip())
                self._write("Queued steering message.", "info")
                self._refresh_sidebar()
                return
            if cmd.startswith("/follow "):
                await session.follow_up(cmd[len("/follow ") :].strip())
                self._write("Queued follow-up message.", "info")
                self._refresh_sidebar()
                return
            if cmd.startswith("/compact"):
                instructions = cmd[len("/compact") :].strip() or None
                result = await session.compact(instructions)
                self._write_compaction_result(result)
                self._refresh_sidebar()
                return
            if cmd.strip() == "/tree":
                if isinstance(session, TuiSessionView):
                    tree_result = await session.command("ui", operation="tree", payload={})
                    tree, leaf_id = tree_result.get("tree", []), tree_result.get("leafId")
                else:
                    tree = session.session_manager.get_tree()
                    leaf_id = session.session_manager.get_leaf_id()
                rendered: list[str] = []

                def render_tree(nodes: list[dict[str, Any]], depth: int = 0) -> None:
                    for node in nodes:
                        e = node["entry"]
                        label = e.get("type")
                        if e.get("type") == "message":
                            role = str((e.get("message") or {}).get("role", "?"))
                            label = f"message:{role}"
                        marker = "*" if e.get("id") == leaf_id else " "
                        rendered.append(f"{'  ' * depth}{marker} {label} {e.get('id')}")
                        render_tree(node.get("children", []), depth + 1)

                render_tree(tree)
                self._write("\n".join(rendered) if rendered else "(empty session)", "info")
                return
            if cmd.startswith("/navigate"):
                parts = cmd.split()
                if len(parts) < 2:
                    self._write("Usage: /navigate <entryId> [--summary <text>]", "error")
                    return
                entry_id = parts[1]
                summarize = "--summary" in parts
                custom = None
                if summarize:
                    idx = parts.index("--summary")
                    custom = " ".join(parts[idx + 1 :]) or None
                try:
                    if isinstance(session, TuiSessionView):
                        result = await session.command(
                            "ui",
                            operation="navigate",
                            payload={"entryId": entry_id, "summarize": summarize, "customInstructions": custom},
                        )
                    else:
                        result = await session.navigate_tree(
                            entry_id, {"summarize": summarize, "customInstructions": custom}
                        )
                    self._write(json.dumps(result, ensure_ascii=False), "info")
                except ValueError as e:
                    self._write(str(e), "error")
                self._refresh_sidebar()
                return
            if cmd.startswith("/fork "):
                entry_id = cmd[len("/fork ") :].strip()
                if not entry_id:
                    self._write("Usage: /fork <entryId>", "error")
                    return
                if self.runtime_host is None:
                    self._write("Runtime host unavailable: /fork is not supported in this context.", "error")
                    return
                try:
                    self._prepare_session_change()
                    if isinstance(self.session, TuiSessionView):
                        result = await self.session.command("ui", operation="fork", payload={"entryId": entry_id})
                    else:
                        result = await self.runtime_host.fork(entry_id)
                        self.session = self.runtime_host.session
                    self._bind_session()
                    self._invalidate_session_list_snapshot()
                    self._clear_extension_ui_state()
                    self._write(json.dumps(result, ensure_ascii=False), "info")
                except ValueError as e:
                    self._write(str(e), "error")
                self._refresh_sidebar()
                return
            if cmd == "/new":
                if self.runtime_host is None:
                    self._write("Runtime host unavailable: /new is not supported in this context.", "error")
                    return
                self._prepare_session_change()
                if isinstance(self.session, TuiSessionView):
                    result = await self.session.command("new_session")
                else:
                    result = await self.runtime_host.new_session({})
                    self.session = self.runtime_host.session
                self._bind_session()
                self._invalidate_session_list_snapshot()
                # Fresh session: drop stale stream content from the old one.
                stream_widget = self.query_one("#stream")
                stream_widget.update("")
                self._stream_lines = []
                self._stream_front_trim_pending = 0
                self._reset_turn_timing()
                self._assistant_has_live_delta = False
                self._assistant_live_start_idx = -1
                self._assistant_live_buffer = ""
                self._assistant_live_line_count = 0
                self._clear_extension_ui_state()
                self._write("New session started.", "info")
                self._refresh_sidebar()
                return
            if cmd.startswith("/login"):
                rest = cmd[len("/login") :].strip()
                parts = rest.split() if rest else []
                if parts and parts[0] == "status":
                    if len(parts) == 2:
                        self._write(
                            json.dumps(session.model_registry.get_provider_auth_status(parts[1]), ensure_ascii=False),
                            "info",
                        )
                    else:
                        providers = session.model_registry.providers()
                        statuses = [session.model_registry.get_provider_auth_status(p) for p in providers]
                        self._write(json.dumps({"providers": statuses}, ensure_ascii=False), "info")
                    return
                if parts and parts[0] == "refresh":
                    # Phase 15: re-fetch the live model list with the stored key.
                    provider = parts[1] if len(parts) >= 2 else ""
                    if not provider:
                        self._write("Provider is required. Usage: /login refresh <provider>", "error")
                        return
                    adapter = getattr(session, "providers", {}).get(provider)
                    if adapter is None:
                        self._write(f"Provider adapter not found for {provider}.", "error")
                        return
                    key_info = session.model_registry.get_api_key_and_headers(ModelInfo(provider=provider, id=""))
                    if not key_info.get("ok"):
                        self._write(key_info.get("error") or f"No API key for {provider}.", "error")
                        return
                    api_key = key_info.get("apiKey", "")
                    ok, error, fetched = await validate_and_fetch(
                        adapter, api_key, provider, headers=key_info.get("headers") or None
                    )
                    if not ok:
                        self._write(f"Authorization failed for {provider}: {error}", "error")
                        return
                    if error:
                        self._write(f"Warning: {error}", "warn")
                    if not fetched:
                        self._write(f"No models fetched for {provider} (no models endpoint).", "info")
                        return
                    added = session.model_registry.register_models(provider, fetched)
                    session.model_registry.persist_models(provider, fetched)
                    lines = [f"Authorized. Fetched {len(fetched)} models ({added} new)."]
                    for i, m in enumerate(fetched[:20], 1):
                        lines.append(f"  {i}. {fetched_entry_id(m)}")
                    if len(fetched) > 20:
                        lines.append(f"  ... and {len(fetched) - 20} more")
                    lines.append(f"Pick with /providers {provider} <model-number|id> or /model {provider}/<id>.")
                    self._write("\n".join(lines), "info")
                    return
                provider = parts[0] if len(parts) >= 1 else ""
                if not provider:
                    self._write("Provider is required. Usage: /login <provider> [apiKey] [model]", "error")
                    return
                # Phase 18: subscription OAuth login (`/login <provider> subscription`).
                if len(parts) >= 2 and parts[1].lower() in {"subscription", "oauth"}:
                    adapter = getattr(session, "providers", {}).get(provider)
                    if adapter is None:
                        self._write(f"Provider adapter not found for {provider}.", "error")
                        return
                    try:
                        ok, error, fetched = await run_oauth_login(provider, adapter, session.model_registry)
                    except OAuthError as e:
                        self._write(f"Subscription login failed: {e}", "error")
                        return
                    if not ok:
                        self._write(f"Subscription login failed: {error}", "error")
                        return
                    session.settings_manager.set_default_provider(provider)
                    lines = [f"Subscription login stored for {provider}."]
                    if fetched:
                        lines.append(f"Fetched {len(fetched)} models:")
                        lines += [f"  {i}. {fetched_entry_id(m)}" for i, m in enumerate(fetched[:20], 1)]
                        if len(fetched) > 20:
                            lines.append(f"  ... and {len(fetched) - 20} more")
                        lines.append(f"Pick with /providers {provider} <model-number|id> or /model {provider}/<id>.")
                    else:
                        lines.append("No models fetched — try /login refresh later.")
                    self._write("\n".join(lines), "info")
                    return
                requires_api_key = session.model_registry.requires_api_key(provider)
                api_key = parts[1] if len(parts) >= 2 else ""
                if requires_api_key and not api_key:
                    env_var = session.model_registry.get_provider_auth_status(provider).get("envVar")
                    hint = f" or set {env_var}" if env_var else ""
                    self._write(f"API key is required for {provider}{hint}. Type the key below.", "warn")
                    self._login_pending = {"provider": provider, "envVar": env_var}
                    try:
                        input_widget = self.query_one("#input", TextArea)
                        input_widget.placeholder = f"API key for {provider}:"
                        input_widget.focus()
                    except Exception:
                        pass
                    return
                model_input = parts[2] if len(parts) >= 3 else ""
                await self._complete_login(provider, api_key, model_input)
                return
            if cmd.startswith("/logout "):
                provider = cmd[len("/logout ") :].strip()
                if not provider:
                    self._write("Usage: /logout <provider>", "error")
                    return
                session.model_registry.remove_provider_credentials(provider)
                self._write(f"Removed credentials for {provider} locally.", "info")
                return
            if cmd.startswith("/retry "):
                mode = cmd[len("/retry ") :].strip().lower()
                if mode not in {"on", "off", "unlimited"}:
                    self._write("Usage: /retry <on|off|unlimited>", "error")
                    return
                if isinstance(session, TuiSessionView):
                    await session.command("ui", operation="set_retry", payload={"mode": mode})
                else:
                    session.settings_manager.set_retry_mode(mode)
                    session.set_auto_retry_enabled(mode in ("on", "unlimited"))
                self._write(f"Auto-retry set to {mode}.", "info")
                self._refresh_sidebar()
                return
            if cmd == "/retry-cycle":
                current = getattr(
                    getattr(session, "settings_manager", None),
                    "get_retry_mode",
                    lambda: "on",
                )()
                if current == "off":
                    new_mode = "on"
                elif current == "on":
                    new_mode = "unlimited"
                else:
                    new_mode = "off"
                session.settings_manager.set_retry_mode(new_mode)
                session.set_auto_retry_enabled(new_mode in ("on", "unlimited"))
                self._write(f"Auto-retry set to {new_mode}.", "info")
                self._refresh_sidebar()
                return
            if cmd == "/subagents":
                if getattr(session, "subagents_hard_disabled", False):
                    self._write("Subagents: unavailable (--no-subagents for this session).", "info")
                else:
                    state = session.settings_manager.get_subagents_enabled()
                    self._write(f"Subagents: {'on' if state else 'off'}", "info")
                self._write("Usage: /subagents <on|off>", "info")
                return
            if cmd.startswith("/subagents "):
                mode = cmd[len("/subagents ") :].strip().lower()
                if mode not in {"on", "off"}:
                    self._write("Usage: /subagents <on|off>", "error")
                    return
                if getattr(session, "subagents_hard_disabled", False):
                    self._write(
                        "Subagents are unavailable for this session because it was started with --no-subagents.", "info"
                    )
                    return
                if isinstance(session, TuiSessionView):
                    await session.command("ui", operation="set_subagents", payload={"enabled": mode == "on"})
                else:
                    session.settings_manager.set_subagents_enabled(mode == "on")
                self._write(f"Subagents set to {mode}.", "info")
                self._refresh_sidebar()
                return
            # Keep /bash-show as an undocumented compatibility alias; settings
            # continue to use the legacy bash.showOutput storage key.
            if cmd in {"/details-show", "/bash-show"}:
                state = getattr(session.settings_manager, "get_bash_show_output", lambda: True)()
                self._write(f"Details output: {'on' if state else 'off'}", "info")
                self._write("Usage: /details-show <on|off>", "info")
                return
            if cmd.startswith("/details-show ") or cmd.startswith("/bash-show "):
                prefix = "/details-show " if cmd.startswith("/details-show ") else "/bash-show "
                mode = cmd[len(prefix) :].strip().lower()
                if mode not in {"on", "off"}:
                    self._write("Usage: /details-show <on|off>", "error")
                    return
                if isinstance(session, TuiSessionView):
                    await session.command("ui", operation="set_bash_output", payload={"enabled": mode == "on"})
                else:
                    session.settings_manager.set_bash_show_output(mode == "on")
                self._write(f"Details output set to {mode}.", "info")
                self._refresh_sidebar()
                return
            if cmd == "/mcp":
                manager = getattr(session, "_mcp_manager", None)
                if manager is None:
                    self._write("MCP not available (started with --no-mcp).", "info")
                    return
                statuses = manager.server_status()
                if not statuses:
                    self._write("No MCP servers configured. Add mcpServers to settings.json (see README).", "info")
                    return
                for s in statuses:
                    if s["error"]:
                        state = f"error: {s['error']}"
                    elif not s.get("enabled", True):
                        state = "disabled"
                    elif s["running"]:
                        state = "running"
                    else:
                        state = "configured (not started)"
                    tools_list = ", ".join(s["tools"]) if s["tools"] else "(none)"
                    self._write(f"- {s['name']}: {state} [{tools_list}]", "info")
                self._write("Usage: /mcp list | /mcp enable <name> | /mcp disable <name>", "info")
                return
            if cmd.startswith("/mcp "):
                rest = cmd[len("/mcp ") :].strip()
                parts = rest.split(maxsplit=1)
                sub = parts[0].lower() if parts else ""
                if sub == "list":
                    manager = getattr(session, "_mcp_manager", None)
                    if manager is None:
                        self._write("MCP not available (started with --no-mcp).", "info")
                        return
                    statuses = manager.server_status()
                    if not statuses:
                        self._write("No MCP servers configured. Add mcpServers to settings.json (see README).", "info")
                        return
                    for s in statuses:
                        if s["error"]:
                            state = f"error: {s['error']}"
                        elif not s.get("enabled", True):
                            state = "disabled"
                        elif s["running"]:
                            state = "running"
                        else:
                            state = "configured (not started)"
                        tools_list = ", ".join(s["tools"]) if s["tools"] else "(none)"
                        self._write(f"- {s['name']}: {state} ({s.get('transport', 'stdio')}) [{tools_list}]", "info")
                    self._write("Usage: /mcp list | /mcp enable <name> | /mcp disable <name>", "info")
                    return
                if sub == "enable":
                    if not parts or len(parts) < 2:
                        self._write("Usage: /mcp list | /mcp enable <name> | /mcp disable <name>", "error")
                        return
                    server_name = parts[1]
                    cfg = session.settings_manager.get_mcp_servers().get(server_name)
                    if not cfg or (not cfg.get("command") and not cfg.get("url")):
                        self._write(
                            f"No MCP config for '{server_name}'. Add mcpServers.<name> to settings.json (see README).",
                            "error",
                        )
                        return
                    manager = getattr(session, "_mcp_manager", None)
                    if manager is None:
                        self._write("MCP not available (started with --no-mcp).", "error")
                        return
                    try:
                        added = await manager.enable_server(
                            server_name,
                            str(cfg.get("command", "")),
                            cfg.get("args") or [],
                            cfg.get("env") or {},
                            url=cfg.get("url"),
                        )
                        session.settings_manager.set_mcp_server_enabled(server_name, True)
                        session.sync_mcp_tools()
                        if added:
                            self._write(f"Server '{server_name}' enabled. Tools: {', '.join(added)}", "info")
                        else:
                            self._write(f"Server '{server_name}' already running.", "info")
                    except RuntimeError as e:
                        self._write(str(e), "error")
                elif sub == "disable":
                    if len(parts) < 2:
                        self._write("Usage: /mcp list | /mcp enable <name> | /mcp disable <name>", "error")
                        return
                    server_name = parts[1]
                    manager = getattr(session, "_mcp_manager", None)
                    if manager is None:
                        self._write("MCP not available (started with --no-mcp).", "error")
                        return
                    try:
                        known = any(status["name"] == server_name for status in manager.server_status())
                        removed = await manager.disable_server(server_name)
                        if known:
                            session.settings_manager.set_mcp_server_enabled(server_name, False)
                            session.sync_mcp_tools()
                        if not known:
                            self._write(f"No running server named '{server_name}'.", "warn")
                        elif removed:
                            self._write(f"Server '{server_name}' disabled. Removed tools: {', '.join(removed)}", "info")
                        else:
                            self._write(f"Server '{server_name}' disabled.", "info")
                    except RuntimeError as e:
                        self._write(str(e), "error")
                else:
                    self._write("Usage: /mcp list | /mcp enable <name> | /mcp disable <name>", "error")
                return
            if cmd.startswith("/config"):
                rest = cmd[len("/config") :].strip()
                if not rest:
                    self._write(json.dumps(session.settings_manager.get_global_settings(), ensure_ascii=False), "info")
                    return
                parts = rest.split(maxsplit=1)
                if len(parts) == 1:
                    key = parts[0]
                    if key == "tui.manualBashMode":
                        cur = getattr(session.settings_manager, "get_tui_manual_bash_mode", lambda: "strict")()
                    else:
                        cur = session.settings_manager.merged()
                        for p in key.split("."):
                            if isinstance(cur, dict):
                                cur = cur.get(p)
                            else:
                                cur = None
                    self._write(json.dumps({"key": key, "value": cur}, ensure_ascii=False), "info")
                    return
                key, raw = parts
                val: Any = raw
                try:
                    val = json.loads(raw)
                except Exception:
                    pass
                session.settings_manager.set_config_value(key, val)
                self._write(f"Updated {key}.", "info")
                return
            if cmd.strip() in {"/extui", "/ext-ui"} or cmd.startswith("/extui ") or cmd.startswith("/ext-ui "):
                rest = cmd.split(" ", 1)[1].strip() if " " in cmd else ""
                parts = rest.split(maxsplit=3) if rest else []
                sub = parts[0].lower() if parts else ""
                if sub == "list":
                    self._write(json.dumps(session.get_extension_ui_state(), ensure_ascii=False), "info")
                    return
                if sub == "clear":
                    session.clear_extension_ui_history()
                    self._write("Extension UI history cleared.", "info")
                    return
                if sub == "request":
                    if len(parts) < 3:
                        self._write("Usage: /extui request <extension> <widget|overlay> [jsonPayload]", "error")
                        return
                    extension = parts[1]
                    ui_type = parts[2]
                    payload: dict[str, Any] = {}
                    if len(parts) >= 4 and parts[3].strip():
                        try:
                            parsed = json.loads(parts[3])
                            payload = parsed if isinstance(parsed, dict) else {"value": parsed}
                        except Exception:
                            self._write("Invalid JSON payload for /extui request", "error")
                            return
                    try:
                        req = session.request_extension_ui(extension=extension, ui_type=ui_type, payload=payload)
                    except ValueError as e:
                        self._write(str(e), "error")
                        return
                    self._write(json.dumps(req, ensure_ascii=False), "info")
                    return
                if sub in {"respond", "cancel"}:
                    if len(parts) < 2:
                        self._write(f"Usage: /extui {sub} <requestId> [jsonPayload]", "error")
                        return
                    request_id = parts[1]
                    payload: dict[str, Any] = {}
                    if sub == "respond" and len(parts) >= 3 and parts[2].strip():
                        raw_payload = rest.split(maxsplit=2)[2]
                        try:
                            parsed = json.loads(raw_payload)
                            payload = parsed if isinstance(parsed, dict) else {"value": parsed}
                        except Exception:
                            self._write("Invalid JSON payload for /extui respond", "error")
                            return
                    try:
                        resp = session.respond_extension_ui(
                            request_id=request_id, payload=payload, cancelled=sub == "cancel"
                        )
                    except ValueError as e:
                        self._write(str(e), "error")
                        return
                    self._write(json.dumps(resp, ensure_ascii=False), "info")
                    return
                self._write("Usage: /extui <list|request|respond|cancel|clear>", "error")
                return
            if cmd.strip() == "/cooperation":
                enabled = session.approval_callback is not None
                self._write(
                    json.dumps({"enabled": enabled, "tools": sorted(session._approval_tools)}, ensure_ascii=False),
                    "info",
                )
                return
            if cmd.startswith("/cooperation "):
                mode = cmd[len("/cooperation ") :].strip().lower()
                if mode in {"on", "enable", "yes", "1", "true"}:
                    enabled = True
                elif mode in {"off", "disable", "no", "0", "false"}:
                    enabled = False
                else:
                    self._write("Usage: /cooperation [on|off]", "error")
                    return
                try:
                    session.settings_manager.set_tool_approval(enabled)
                except Exception as e:
                    self._write(f"Unable to persist cooperation setting: {e}", "error")
                    return
                if enabled:
                    self._install_approval_callback(session)
                    self._cooperation_disabled = False
                    self._write("[Cooperation] enabled: mutating tools (bash/write/edit) ask first", "info")
                else:
                    session.approval_callback = None
                    self._cooperation_disabled = True
                    self._reject_all_approvals("Cooperation disabled")
                    self._write("[Cooperation] disabled: all tools run freely", "info")
                self._refresh_sidebar()
                return
            if cmd.startswith("/bash "):
                command = cmd[len("/bash ") :].strip()
                if not command:
                    self._write("Usage: /bash <command>", "error")
                    return
                # Unlike other slash commands, /bash is an explicit local
                # action and is visible before validation or execution.
                self._write_manual_command_block(command)
                command_session = session
                command_generation = self._session_listener_generation
                try:
                    result = await session.execute_tui_command(command)
                    # Results that finish after /sessions or /fork remain in
                    # the old local history; never attach them to this view.
                    if command_session is not self.session or command_generation != self._session_listener_generation:
                        return
                    output = str(result.get("output") or "")
                    if not getattr(session.settings_manager, "get_bash_show_output", lambda: True)():
                        output = ""
                    self._write_manual_result(
                        output,
                        exit_code=result.get("exitCode"),
                        timed_out=bool(result.get("timedOut", False)),
                        cancelled=bool(result.get("cancelled", False)),
                        truncated=bool(result.get("truncated", False)),
                        full_output_path=result.get("fullOutputPath"),
                        stored=True,
                    )
                except asyncio.CancelledError:
                    if command_session is self.session and command_generation == self._session_listener_generation:
                        self._write("[bash] przerwano", "warn")
                except Exception as e:
                    if command_session is self.session and command_generation == self._session_listener_generation:
                        self._write(str(e), "error")
                return

            self._write(f"Unknown command: {cmd}. Use /help.", "error")

        async def _handle_detached_command(self, cmd: str) -> bool:
            """Handle commands whose old implementation touched worker-owned state."""
            session = self.session
            assert isinstance(session, TuiSessionView)

            async def call(operation: str, **payload: Any) -> dict[str, Any] | None:
                try:
                    return await session.ui(operation, **payload)
                except Exception as exc:
                    self._write(str(exc), "error")
                    return None

            # Session selection is parsed in the UI, but all persisted-session
            # mutations remain commands owned by TuiRuntimeHost.  Keep this
            # ahead of the legacy handlers below: those handlers can open a
            # SessionManager or mutate a live AgentSession.
            if cmd == "/sessions":
                await self._list_sessions()
                return True
            if cmd.startswith("/sessions "):
                rest = cmd[len("/sessions ") :].strip()
                if rest.startswith("delete "):
                    target, error = self._resolve_session_target(rest[len("delete ") :])
                    if error:
                        self._write(error, "error")
                    elif session.is_streaming:
                        self._write("Stop or wait for the active turn before deleting a session.", "error")
                    else:
                        self._session_delete_pending = target
                        self._write(
                            f"Delete session '{target.name or '(unnamed)'}'? Type yes to confirm, anything else cancels.",
                            "warn",
                        )
                    return True
                if rest.startswith("rename "):
                    target_text, new_name, error = self._parse_session_rename_target(rest[len("rename ") :].strip())
                    if error:
                        self._write(error, "error")
                        return True
                    target, error = self._resolve_session_target(target_text or "")
                    if error:
                        self._write(error, "error")
                        return True
                    if session.is_streaming:
                        self._write("Stop or wait for the active turn before renaming a session.", "error")
                        return True
                    try:
                        normalized = normalize_session_name(new_name or "")
                        if any(info.path != target.path and info.name == normalized for info in self._session_infos()):
                            raise ValueError("A session with that name already exists")
                    except ValueError as exc:
                        self._write(str(exc), "error")
                        return True
                    result = await call("rename_session", path=target.path, name=normalized)
                    if result is not None:
                        self._write(f"Renamed session to: {normalized}.", "info")
                        self._invalidate_session_list_snapshot()
                        self._refresh_sidebar()
                    return True
                target, error = self._resolve_session_target(rest)
                if error:
                    self._write(error, "error")
                else:
                    await self._switch_to_session(target)
                return True

            if cmd == "/status":
                result = await call("status")
                if result is not None:
                    self._write(json.dumps(result, ensure_ascii=False), "info")
                return True
            if cmd == "/stats":
                result = await call("stats")
                if result is not None:
                    self._write(json.dumps(result, ensure_ascii=False), "info")
                return True
            if cmd == "/state":
                result = await call("state")
                if result is not None:
                    self._write(json.dumps(result, ensure_ascii=False), "info")
                return True
            if cmd == "/tools":
                result = await call("tools")
                if result is not None:
                    self._write(json.dumps(result, ensure_ascii=False), "info")
                return True
            if cmd == "/queue":
                result = await call("queues")
                if result is not None:
                    self._write(json.dumps(result, ensure_ascii=False), "info")
                return True
            if cmd.startswith("/queue clear"):
                target = cmd[len("/queue clear") :].strip() or "all"
                result = await call("clear_queues", target=target)
                if result is not None:
                    self._write(json.dumps(result, ensure_ascii=False), "info")
                return True
            if cmd == "/inspect-timeout":
                result = await call("inspect_timeout")
                self._write(
                    json.dumps(result or {"message": "No subagent-timeout diagnostics available."}, ensure_ascii=False),
                    "info",
                )
                return True
            if cmd == "/reload":
                result = await call("reload")
                if result is not None:
                    self._write(json.dumps(result, ensure_ascii=False), "info")
                return True
            if cmd in {"/skill", "/skill list"} or cmd == "/skill:":
                result = await call("skills")
                if result is not None:
                    self._write(json.dumps(result.get("skills", result), ensure_ascii=False), "info")
                return True
            if cmd.startswith("/skill:"):
                parts = cmd[len("/skill:") :].strip().split(None, 1)
                if not parts:
                    result = await call("skills")
                else:
                    result = await call("invoke_skill", name=parts[0], args=parts[1] if len(parts) > 1 else "")
                if result is not None:
                    self._write(json.dumps(result, ensure_ascii=False), "info")
                return True
            if cmd.startswith("/bash "):
                command = cmd[len("/bash ") :].strip()
                if not command:
                    self._write("Usage: /bash <command>", "error")
                    return True
                self._write_manual_command_block(command)
                result = await call("execute_bash", command=command)
                if result is not None:
                    output = (
                        str(result.get("output") or "") if session.snapshot["sidebar"].get("bashOutput", True) else ""
                    )
                    self._write_manual_result(
                        output,
                        exit_code=result.get("exitCode"),
                        timed_out=bool(result.get("timedOut")),
                        cancelled=bool(result.get("cancelled")),
                        truncated=bool(result.get("truncated")),
                        full_output_path=result.get("fullOutputPath"),
                        stored=True,
                    )
                return True
            if cmd.startswith("/login"):
                parts = cmd[len("/login") :].strip().split()
                if parts and parts[0] == "status":
                    result = await call("auth_status", provider=parts[1] if len(parts) > 1 else "")
                elif len(parts) >= 2 and parts[0] == "refresh":
                    result = await call("login_refresh", provider=parts[1])
                elif len(parts) >= 2 and parts[1].lower() in {"subscription", "oauth"}:
                    result = await call("oauth_login", provider=parts[0])
                elif parts:
                    provider, api_key, model = (
                        parts[0],
                        parts[1] if len(parts) > 1 else "",
                        parts[2] if len(parts) > 2 else "",
                    )
                    if not api_key:
                        self._login_pending = {"provider": provider, "envVar": None}
                        self.query_one("#input", TextArea).placeholder = f"API key for {provider}:"
                        return True
                    result = await call("login", provider=provider, apiKey=api_key, model=model)
                else:
                    self._write("Usage: /login <provider> [apiKey] [model]", "error")
                    return True
                if result is not None:
                    self._write(json.dumps(result, ensure_ascii=False), "info")
                return True
            if cmd.startswith("/logout "):
                result = await call("logout", provider=cmd[len("/logout ") :].strip())
                if result is not None:
                    self._write("Removed credentials locally.", "info")
                return True
            if cmd == "/mcp" or cmd == "/mcp list":
                result = await call("mcp_list")
                if result is not None:
                    self._write(json.dumps(result, ensure_ascii=False), "info")
                return True
            if cmd.startswith("/mcp enable ") or cmd.startswith("/mcp disable "):
                enabled = cmd.startswith("/mcp enable ")
                name = cmd.split(maxsplit=2)[2]
                result = await call("mcp_enable" if enabled else "mcp_disable", name=name)
                if result is not None:
                    self._write(json.dumps(result, ensure_ascii=False), "info")
                return True
            if cmd.startswith("/config"):
                rest = cmd[len("/config") :].strip()
                if not rest:
                    self._write("Use /config <key> [jsonValue] in detached TUI.", "info")
                    return True
                key, *raw = rest.split(maxsplit=1)
                if not raw:
                    result = await call("config_get", key=key)
                else:
                    try:
                        value = json.loads(raw[0])
                    except json.JSONDecodeError:
                        value = raw[0]
                    result = await call("config_set", key=key, value=value)
                if result is not None:
                    self._write(json.dumps(result, ensure_ascii=False), "info")
                return True
            if cmd.startswith("/extui") or cmd.startswith("/ext-ui"):
                rest = cmd.split(" ", 1)[1].strip() if " " in cmd else ""
                parts = rest.split(maxsplit=3)
                sub = parts[0] if parts else ""
                if sub == "list":
                    result = await call("extension_state")
                elif sub == "clear":
                    result = await call("extension_clear")
                elif sub == "request" and len(parts) >= 3:
                    try:
                        payload = json.loads(parts[3]) if len(parts) == 4 else {}
                    except json.JSONDecodeError:
                        self._write("Invalid JSON payload for /extui request", "error")
                        return True
                    result = await call(
                        "extension_request",
                        extension=parts[1],
                        uiType=parts[2],
                        payload=payload if isinstance(payload, dict) else {"value": payload},
                    )
                elif sub in {"respond", "cancel"} and len(parts) >= 2:
                    raw = parts[2] if len(parts) > 2 else "{}"
                    try:
                        payload = json.loads(raw) if sub == "respond" else {}
                    except json.JSONDecodeError:
                        self._write("Invalid JSON payload for /extui respond", "error")
                        return True
                    result = await call(
                        "answer_extension",
                        id=parts[1],
                        payload=payload if isinstance(payload, dict) else {"value": payload},
                        cancelled=sub == "cancel",
                    )
                else:
                    self._write("Usage: /extui <list|request|respond|cancel|clear>", "error")
                    return True
                if result is not None:
                    self._write(json.dumps(result, ensure_ascii=False), "info")
                return True
            if cmd == "/cooperation":
                result = await call("cooperation_status")
                if result is not None:
                    self._write(json.dumps(result, ensure_ascii=False), "info")
                return True
            if cmd.startswith("/cooperation "):
                mode = cmd[len("/cooperation ") :].strip().lower()
                if mode not in {"on", "off"}:
                    self._write("Usage: /cooperation [on|off]", "error")
                    return True
                result = await call("set_cooperation", enabled=mode == "on")
                if result is not None:
                    self._write(f"[Cooperation] {'enabled' if mode == 'on' else 'disabled'}", "info")
                return True
            return False

        def _record_input_history(self, value: str) -> None:
            """Store a non-empty submitted input in the local 50-entry history."""
            if not value.strip():
                return
            self._command_history.append(value)
            if len(self._command_history) > 50:
                del self._command_history[:-50]

        async def on_input_submitted(self, event: InputSubmitted) -> None:
            text = event.value.strip()
            input_widget = self.query_one("#input", _CommandTextArea)
            input_widget.text = ""
            input_widget.reset_history_navigation()
            if self._model_picker_models:
                if text.startswith("/"):
                    self._dismiss_model_picker()
                else:
                    await self._select_picker_model(text)
                    return
            if self._session_delete_pending is not None:
                target = self._session_delete_pending
                self._session_delete_pending = None
                if text.lower() != "yes":
                    self._write("Session deletion cancelled.", "info")
                    return
                if self.session.is_streaming:
                    self._write("Stop or wait for the active turn before deleting a session.", "error")
                    return
                try:
                    active = target.id == self.session.session_id
                    if active:
                        if self.runtime_host is None:
                            self._write("Runtime host unavailable: cannot replace the active session.", "error")
                            return
                        if not isinstance(self.session, TuiSessionView):
                            await self.runtime_host.new_session({})
                            self.session = self.runtime_host.session
                        else:
                            await self.session.command("new_session")
                        self._bind_session()
                        self._invalidate_session_list_snapshot()
                        self._rebuild_session_transcript()
                        self._clear_extension_ui_state()
                    if not isinstance(self.session, TuiSessionView):
                        SessionManager.delete(target.path, self.session.session_manager.session_dir)
                    else:
                        await self.session.command("ui", operation="delete_session", payload={"path": target.path})
                except (OSError, ValueError) as exc:
                    self._write(f"Session deletion failed: {exc}", "error")
                    return
                self._write(f"Deleted session: {target.name or '(unnamed)'}.", "info")
                self._invalidate_session_list_snapshot()
                if active:
                    self._write("New session started.", "info")
                self._refresh_sidebar()
                return

            image_paths: list[Path] = []
            clean_text: str = ""
            if text:
                image_paths, clean_text = _extract_image_paths(text)

            # Slash-command dispatch (only when the *clean* text starts with /
            # and we don't have a standalone absolute image path).
            if clean_text.startswith("/"):
                if not (image_paths and not clean_text.lstrip("/").strip()):
                    # Not a standalone absolute image path → real slash command.
                    # Drop any queued clipboard image — the slash command takes
                    # priority.
                    self._pending_clipboard_image_bytes = None
                    await self._handle_command(text)
                    return
                # Standalone absolute image path: fall through to image import.

            # Slash commands are recorded by _handle_command so /history can
            # remain excluded. Ordinary prompts retain their original multiline
            # content rather than the stripped dispatch value.
            self._record_input_history(event.value)

            # ------------------------------------------------------------------
            # Pending clipboard image import runs after slash-command check.
            # ------------------------------------------------------------------
            clipboard_image_refs: list[dict[str, Any]] | None = None
            if self._pending_clipboard_image_bytes is not None:
                clipboard_image_refs = self._import_and_attach_clipboard_image()

            if self._extension_ui_pending_request is not None:
                await self._handle_extension_ui_answer(clean_text)
                return
            if self._login_pending is not None:
                await self._handle_login_answer(clean_text)
                return
            if self._ask_user_pending is not None:
                await self._handle_ask_user_answer(clean_text)
                return

            # Combine clipboard refs (if any) with file-path-extracted refs.
            file_image_refs: list[dict[str, Any]] = []
            if clipboard_image_refs is not None:
                file_image_refs.extend(clipboard_image_refs)
            if not clean_text and not image_paths and not file_image_refs:
                return

            # Centralized image-count validation (file + clipboard).
            if image_paths or file_image_refs:
                from one.core.attachments import AttachmentValidationError, validate_image_count

                try:
                    validate_image_count(len(image_paths), clipboard_count=len(file_image_refs))
                except AttachmentValidationError as e:
                    self._write(str(e), "error")
                    return

            # Import file-path-extracted images.
            if image_paths:
                try:
                    from one.core.attachments import (
                        AttachmentInput,
                        AttachmentValidationError,
                        count_attachments,
                        import_image,
                    )
                except ImportError:
                    self._write("Image support is unavailable", "error")
                    return

                storage_dir = self.session._storage_dir or ""
                try:
                    count_attachments([AttachmentInput(path=p) for p in image_paths])
                    for p in image_paths:
                        ref = import_image(storage_dir, str(p))
                        file_image_refs.append(ref.__dict__)
                except AttachmentValidationError:
                    self._write(f"Invalid image: {image_paths[0]}", "error")
                    return
                except Exception as e:  # noqa: BLE001
                    self._write(f"Failed to import image: {e}", "error")
                    return

            image_refs = file_image_refs

            # Write user block (with [IMG] marker if images present).
            if clean_text or image_paths or file_image_refs:
                has_images = bool(image_paths or file_image_refs)
                if image_paths:
                    first_img = str(image_paths[0])
                elif file_image_refs:
                    first_img = file_image_refs[0].get("blob_hash") or file_image_refs[0].get("blobPath", "")
                else:
                    first_img = ""
                if has_images and clean_text:
                    self._write_chat_block("user", f"{clean_text}\n[IMG] {first_img}")
                elif has_images:
                    self._write_chat_block("user", f"[IMG] {first_img}")
                else:
                    self._write_chat_block("user", clean_text)
                self._turn_active = True
                self._last_delta_ts = 0.0

                async def _run_prompt() -> None:
                    queued = False
                    prompt_completed = False
                    try:
                        if self.session.is_streaming:
                            # Resolve the delivery mode the same way as
                            # agent_session.prompt() so feedback matches actual
                            # behavior (steer vs followUp).
                            sm = self.session.steering_mode
                            if sm == "follow_up":
                                resolved = "followUp"
                            elif sm == "queue":
                                resolved = "steer"
                            else:
                                resolved = "followUp" if self.session.follow_up_mode == "follow_up" else "steer"
                            await self.session.prompt(
                                clean_text,
                                images=image_refs if image_refs else None,
                            )
                            self._write(
                                f"Queued ({resolved})." if resolved == "steer" else "Queued follow-up message.",
                                "info",
                            )
                            queued = True
                        else:
                            await self.session.prompt(
                                clean_text,
                                images=image_refs if image_refs else None,
                            )
                        prompt_completed = True
                    except Exception as e:
                        self._write(f"[error] {e}", "error")
                        self._refresh_sidebar()
                    finally:
                        # The detached runtime acknowledges ``prompt`` only
                        # after its owner-loop turn has completed, but its
                        # ordered event bridge may still have message_end and
                        # turn_end queued on Textual.  Let that authoritative
                        # turn_end transition the spinner: clearing it here
                        # lets consumers observe completion before the final
                        # assistant delta has even entered the UI reducer.
                        # Legacy in-loop sessions retain the old fallback.
                        if not queued and (
                            not isinstance(self.session, TuiSessionView) or not prompt_completed
                        ):
                            self._turn_active = False
                            self._remove_thinking_line()
                            self._schedule_stream_render()
                        self._refresh_sidebar()

                asyncio.create_task(_run_prompt())
            return

        def _import_and_attach_clipboard_image(self) -> list[dict[str, Any]] | None:
            """Import pending clipboard image bytes via the attachment pipeline.

            Returns a list of image reference dicts (for ``images=`` in
            ``session.prompt``) on success, ``None`` when there was nothing
            pending, or ``[]`` on error (the error is already toast-ed).
            """
            from pathlib import Path

            try:
                from one.core.attachments import (
                    AttachmentInput,
                    AttachmentValidationError,
                    count_attachments,
                    import_image,
                )
                from one.core.clipboard_image import clipboard_image_to_temp_path
            except ImportError:
                self._toast("Clipboard-image backend unavailable", severity="warning")
                self._pending_clipboard_image_bytes = None
                return None
            if not self._pending_clipboard_image_bytes:
                return None
            raw = self._pending_clipboard_image_bytes
            self._pending_clipboard_image_bytes = None  # consume immediately

            storage_dir = getattr(self.session, "_storage_dir", "") or ""
            tmp_path = clipboard_image_to_temp_path(storage_dir, raw)
            if tmp_path is None:
                self._toast("Failed to write clipboard image temp file", severity="error")
                return None
            try:
                count_attachments([AttachmentInput(path=tmp_path)])
                ref = import_image(storage_dir, tmp_path)
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass
                return [ref.__dict__]
            except AttachmentValidationError as exc:
                self._pending_clipboard_image_bytes = raw  # restore for retry
                self._toast(f"Invalid image data: {exc}", severity="error")
                return None
            except Exception as exc:  # noqa: BLE001
                self._pending_clipboard_image_bytes = raw  # restore for retry
                self._toast(f"Failed to import clipboard image: {exc}", severity="error")
                return None
            finally:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass

        def action_paste_image(self) -> None:
            """Acquire image bytes from the system clipboard and queue for import."""
            try:
                from one.core.clipboard_image import acquire_clipboard_image  # noqa: F401
            except ImportError:
                self._toast("No clipboard-image backend available", severity="warning")
                return
            from one.core.clipboard_image import acquire_clipboard_image

            raw = acquire_clipboard_image()
            if raw is None:
                self._toast("No image data in clipboard", severity="warning")
                return
            self._pending_clipboard_image_bytes = raw
            self._toast("Image queued — submit to attach", severity="info")

        async def _handle_extension_ui_answer(self, text: str) -> None:
            """Route the input widget answer back to the pending extension UI request."""
            req = self._extension_ui_pending_request
            if req is None:
                return
            request_id = str(req.get("id") or "")
            try:
                if not text:
                    if isinstance(self.session, TuiSessionView):
                        await self.session.extension_response(request_id, cancelled=True)
                    else:
                        self.session.respond_extension_ui(request_id=request_id, cancelled=True)
                else:
                    try:
                        parsed = json.loads(text)
                        payload = parsed if isinstance(parsed, dict) else {"answer": text}
                    except Exception:
                        payload = {"answer": text}
                    if isinstance(self.session, TuiSessionView):
                        await self.session.extension_response(request_id, payload=payload)
                    else:
                        self.session.respond_extension_ui(request_id=request_id, payload=payload)
                # Success — clear pending state (panel, placeholder, request ref).
                # The extension_ui_response event also drives _clear_extension_ui_state;
                # that call is safe (idempotent) so both paths may fire.
                self._clear_extension_ui_state()
            except Exception as e:
                self._write(f"[ExtUI] error responding to {request_id}: {e}", "error")

        async def _handle_login_answer(self, text: str) -> None:
            """Route the input widget answer back to the pending /login api-key prompt."""
            pending = self._login_pending
            if pending is None:
                return
            self._login_pending = None
            try:
                input_widget = self.query_one("#input", TextArea)
                input_widget.placeholder = "Type a command or /help"
            except Exception:
                pass
            api_key = text.strip()
            if not api_key:
                env_var = pending.get("envVar")
                hint = f" or set {env_var}" if env_var else ""
                self._write(f"API key is required for {pending.get('provider')}{hint}.", "error")
                return
            await self._complete_login(pending.get("provider"), api_key, "")

        async def _handle_ask_user_answer(self, text: str) -> None:
            """Route the input widget answer back to the pending ask_user question."""
            pending = self._ask_user_pending
            if pending is None:
                return
            self._ask_user_pending = None
            try:
                input_widget = self.query_one("#input", TextArea)
                input_widget.placeholder = "Type a command or /help"
            except Exception:
                pass
            answer = text.strip() or "(no answer)"
            try:
                if isinstance(self.session, TuiSessionView):
                    epoch = self._detached_waiter_epoch(pending)
                    if epoch is None:
                        raise ValueError("stale or invalid question response")
                    await self.session.answer_question(str(pending.get("id") or ""), answer, epoch)
                else:
                    await self.session.answer_question(str(pending.get("id") or ""), answer)
            except ValueError as e:
                self._write(f"[AskUser] {e}", "error")

        async def _complete_login(self, provider: str, api_key: str, model_input: str) -> None:
            """Persist provider apiKey + defaults and optionally switch the model."""
            session = self.session
            if isinstance(session, TuiSessionView):
                try:
                    result = await session.ui("login", provider=provider, apiKey=api_key, model=model_input)
                except Exception as exc:
                    self._write(str(exc), "error")
                    return
                self._write(json.dumps(result, ensure_ascii=False), "info")
                self._refresh_sidebar()
                return
            # Phase 11: validate the key BEFORE storing, then fetch + register
            # the provider's model list.
            adapter = getattr(session, "providers", {}).get(provider)
            if adapter is None:
                self._write(f"Provider adapter not found for {provider}; key stored without validation.", "warn")
            else:
                ok, error, fetched = await validate_and_fetch(adapter, api_key, provider, model_input or None)
                if not ok:
                    self._write(f"Authorization failed for {provider}: {error}", "error")
                    return
                if error:
                    self._write(f"Warning: {error}", "warn")
                if fetched:
                    added = session.model_registry.register_models(provider, fetched)
                    session.model_registry.persist_models(provider, fetched)
                    preview = ", ".join(fetched_entry_id(m) for m in fetched[:20]) + (
                        "..." if len(fetched) > 20 else ""
                    )
                    self._write(f"Authorized. Fetched {len(fetched)} models ({added} new): {preview}", "info")
            selected_model = None
            if model_input:
                selected_model = session.model_registry.resolve(provider, model_input, allow_dynamic=True)
                if not selected_model:
                    self._write(f"Model not found for {provider}: {model_input}", "error")
                    return
            if api_key:
                session.model_registry.set_stored_api_key(provider, api_key)
            session.settings_manager.set_default_provider(provider)
            if selected_model:
                session.settings_manager.set_default_model(selected_model.id)
                await session.set_model(
                    ModelInfo(
                        provider=selected_model.provider,
                        id=selected_model.id,
                        reasoning=selected_model.reasoning,
                        context_window=selected_model.context_window,
                    )
                )
            self._write(
                (f"Stored key for {provider}." if api_key else f"Configured provider {provider}.")
                + (f" Default model set to {selected_model.id}." if selected_model else ""),
                "info",
            )
            self._schedule_sidebar_refresh()

        def _approval_summary(self, pending: dict[str, Any]) -> Text:
            """Build the pending-approval summary using the active palette."""
            args = str(pending.get("displayArgs") or SubagentMonitor.display_tool_args(pending.get("args", {})))[:600]
            queued = len(self._approval_waiters) - 1
            summary = Text("PENDING TOOL APPROVAL\n", style=f"bold {self._theme.warn}")
            body_style = self._theme.screen_fg
            summary.append(f"Agent: {self._approval_origin_label(pending.get('origin'))}\n", style=body_style)
            summary.append(f"Tool: {sanitize_display_text(str(pending['tool']))}\n", style=body_style)
            summary.append(f"Arguments: {args or '{}'}\n", style=body_style)
            if queued:
                summary.append(f"{queued} additional approval request(s) waiting\n", style=body_style)
            summary.append("Approve runs this tool. Reject sends the reason below.", style=body_style)
            return summary

        def _approval_origin_label(self, origin: Any) -> str:
            """Return a safe, bounded identity for the requesting session."""
            if origin is None or origin is self.session:
                return "Main agent"
            session_id = sanitize_display_text(str(getattr(origin, "session_id", "unknown")))[:12]
            task = ""
            for child in self._monitor_snapshot.get("children", []):
                if str(child.get("sessionId")) == str(getattr(origin, "session_id", "")):
                    task = sanitize_display_text(str(child.get("task") or ""))[:80]
                    break
            return f"Child {session_id}" + (f": {task}" if task else "")

        def _install_approval_callback(self, session: Any) -> None:
            """Bind an approval callback to exactly one session lifetime."""

            async def callback(tool_name: str, args: dict[str, Any]) -> tuple[bool, str]:
                if self._approval_rebinding or session is not self.session:
                    return False, "Session changed"
                return await self._approval_prompt(tool_name, args, approval_callback_origin.get())

            session.approval_callback = callback

        def _show_next_approval(self) -> None:
            """Display exactly one FIFO approval request without taking input focus."""
            previous = self._approval_pending
            self._approval_pending = self._approval_waiters[0] if self._approval_waiters else None
            try:
                overlay = self.query_one("#approval_overlay", Vertical)
                if self._approval_pending is None:
                    overlay.remove_class("visible")
                else:
                    if self._approval_pending is not previous:
                        self.query_one("#approval-reason", Input).value = ""
                    self.query_one("#approval_summary", Static).update(self._approval_summary(self._approval_pending))
                    overlay.add_class("visible")
            except Exception:
                pass
            self._schedule_sidebar_refresh()

        @staticmethod
        def _detached_waiter_epoch(pending: dict[str, Any]) -> int | None:
            """Return a usable detached runtime epoch without coercing bad DTOs."""
            epoch = pending.get("epoch")
            return epoch if type(epoch) is int and epoch >= 0 else None

        def _resolve_approval(self, approved: bool, reason: str = "Rejected by user") -> None:
            pending = self._approval_pending
            if pending is None:
                return
            if isinstance(self.session, TuiSessionView):
                if pending.get("resolving"):
                    return
                pending["resolving"] = True
                approval_id = str(pending.get("id") or "")
                epoch = self._detached_waiter_epoch(pending)
                if epoch is None:
                    self._write("[Approval] stale or invalid approval response", "error")
                    try:
                        self._approval_waiters.remove(pending)
                    except ValueError:
                        pass
                    self._show_next_approval()
                    return

                async def resolve() -> None:
                    try:
                        await self.session.command(
                            "approval_resolve",
                            approval_id=approval_id,
                            approved=approved,
                            reason="" if approved else reason,
                            epoch=epoch,
                        )
                    except Exception as exc:
                        self._write(f"[Approval] {exc}", "error")
                        pending["resolving"] = False
                        return
                    if self._approval_pending is pending:
                        self._approval_waiters.popleft()
                        self._show_next_approval()

                asyncio.create_task(resolve())
                return
            future = pending["future"]
            if not future.done():
                future.set_result((approved, "" if approved else reason.strip() or "Rejected by user"))

        def _reject_all_approvals(self, reason: str) -> None:
            for pending in tuple(self._approval_waiters):
                # Detached waiters are UI DTOs (ID/tool/origin/epoch), not
                # local callback futures.  Their runtime-owned futures are
                # rejected by the set_cooperation command or host disposal;
                # only dismiss the local surface here.
                future = pending.get("future")
                if future is not None and not future.done():
                    future.set_result((False, reason))
            self._approval_waiters.clear()
            self._show_next_approval()

        def _approval_reason(self) -> str:
            try:
                return self.query_one("#approval-reason", Input).value.strip()
            except Exception:
                return ""

        async def _approval_prompt(self, tool_name: str, args: dict[str, Any], origin: Any = None) -> tuple[bool, str]:
            """Queue fail-closed approvals behind a dedicated overlay control."""
            # Children inherit this callback at spawn time. Once cooperation is
            # disabled on the parent, inherited callbacks must reject rather
            # than opening a fresh approval surface.
            if self._cooperation_disabled:
                return False, "Cooperation disabled"
            future: asyncio.Future[tuple[bool, str]] = asyncio.get_running_loop().create_future()
            pending = {"tool": tool_name, "args": args, "origin": origin, "future": future}
            self._approval_waiters.append(pending)
            if self._approval_pending is None:
                self._show_next_approval()
            self._toast(f"Approve: {tool_name}", severity="warning", timeout=8.0)
            try:
                return await future
            except asyncio.CancelledError:
                # A lifecycle transition may resolve the fail-closed decision
                # while Textual is also cancelling its callback task. Keep the
                # explicit lifecycle reason rather than losing it to that race.
                if future.done() and not future.cancelled():
                    return future.result()
                return False, "Approval cancelled"
            finally:
                try:
                    self._approval_waiters.remove(pending)
                except ValueError:
                    pass
                if self._approval_pending is pending:
                    self._show_next_approval()

        async def _on_key(self, event: events.Key) -> None:
            """Keep overlay navigation ahead of normal app key dispatch."""
            if event.key == "ctrl+c" and self._approval_pending is not None:
                self._resolve_approval(False, "aborted by user")
                event.stop()
                event.prevent_default()
                return
            if self._handle_evidence_panel_key(event):
                return
            if self._handle_agents_panel_key(event):
                return
            await super()._on_key(event)
            # Prevent MRO double-dispatch: MessagePump invokes App._on_key
            # again after this handler (nothing set prevent_default yet),
            # which would fire bubbled bindings (backspace/delete/arrows)
            # a second time. Harmless no-op on older Textual single dispatch.
            event.prevent_default()

        async def _on_paste(self, event: events.Paste) -> None:
            """Recover terminal paste when a terminal dialog cleared focus.

            Textual routes a real paste to the active screen when no widget is
            focused.  Only recover that no-focus case: a different focused
            widget owns its paste event and must not be redirected to input.
            """
            if self.focused is not None:
                return
            if not event.text:
                event.stop()
                event.prevent_default()
                return
            input_widget = self.query_one("#input", _CommandTextArea)
            input_widget.focus()
            input_widget._insert_paste_text(event.text)
            event.stop()
            event.prevent_default()

        async def action_abort(self) -> None:
            await self.session.abort()
            self._turn_active = False
            self._remove_thinking_line()
            self._schedule_stream_render()
            self._write("[abort requested]", "warn")
            self._refresh_sidebar()

        def action_show_agents(self) -> None:
            self._open_agents_panel()

        def action_open_agent_link(self, token: int) -> None:
            """Open a trusted generated inline action target."""
            target = self._agent_link_tokens.get(token)
            if target is None:
                return
            generation, session_id, line_index, call_id = target
            if (
                generation != self._monitor_generation
                or token not in self._agent_links_by_line.get(line_index, ())
                or token not in self._agent_link_labels
                or not (0 <= line_index < len(self._stream_lines))
                or not isinstance(call_id, int)
            ):
                return
            self._refresh_monitor_snapshot()
            if not any(child.get("sessionId") == session_id for child in self._monitor_snapshot.get("children", [])):
                return
            self._open_agents_panel(session_id)

        def action_open_evidence_link(self, token: int) -> None:
            """Fetch one bounded, session/epoch-bound safe evidence page."""
            target = self._evidence_link_tokens.get(token)
            if target is None:
                self._write("Full result unavailable: this link is no longer in the active transcript.", "warn")
                return
            generation, evidence_id, offset, epoch = target
            if (
                generation != self._monitor_generation
                or (isinstance(self.session, TuiSessionView) and epoch != self.session.epoch)
                or self._evidence_link_line_ids.get(token) not in set(self._stream_line_ids)
            ):
                self._write("Full result unavailable: this link belongs to an earlier session view.", "warn")
                return
            self._schedule_evidence_page(generation, evidence_id, offset, ())

        def _schedule_evidence_page(
            self, generation: int, evidence_id: str, offset: int, previous_offsets: tuple[int, ...]
        ) -> None:
            """Start one page request and invalidate every older response."""
            self._evidence_request_generation += 1
            request_generation = self._evidence_request_generation
            asyncio.create_task(
                self._open_evidence_page(generation, evidence_id, offset, previous_offsets, request_generation)
            )

        async def _open_evidence_page(
            self, generation: int, evidence_id: str, offset: int,
            previous_offsets: tuple[int, ...], request_generation: int,
        ) -> None:
            request_session = self.session
            if not isinstance(request_session, TuiSessionView) or generation != self._monitor_generation:
                return
            request_epoch = request_session.epoch
            if request_generation != self._evidence_request_generation:
                return
            try:
                result = await request_session.command(
                    "evidence_page", evidence_id=evidence_id, offset=offset, epoch=request_epoch
                )
            except Exception:
                if request_generation == self._evidence_request_generation:
                    # Do not expose runtime exception text: it can contain paths,
                    # transport details, or provider data.
                    self._write("Unable to open full result: the runtime rejected the request.", "warn")
                return
            if (
                request_generation != self._evidence_request_generation
                or request_session is not self.session
                or generation != self._monitor_generation
                or request_epoch != request_session.epoch
            ):
                return
            if result.get("epoch") != request_epoch:
                self._write("Full result unavailable: the session view changed.", "warn")
                return
            if not result.get("available"):
                self._write("Full result is no longer available.", "warn")
                return
            self._evidence_panel = {
                "generation": generation, "evidenceId": evidence_id, "offset": offset,
                "text": str(result.get("text", "")), "nextOffset": result.get("nextOffset"),
                # Byte offsets may be shortened at a UTF-8 boundary. Keep the
                # actual returned starts rather than guessing with a character
                # subtraction when navigating backwards.
                "previousOffsets": previous_offsets,
            }
            self._render_evidence_panel()

        def _render_evidence_panel(self) -> None:
            panel = self._evidence_panel
            if panel is None:
                return
            previous = "PageUp previous" if panel.get("previousOffsets") else "PageUp unavailable"
            following = "PageDown next" if isinstance(panel.get("nextOffset"), int) else "PageDown unavailable"
            # Rich Text is literal data, unlike a string passed to Static. This
            # prevents evidence from supplying markup or Textual @click actions.
            self.query_one("#evidence_header", Static).update(
                Text(f"Full sanitized result ({panel['offset']}) — {previous} · {following} · Esc close")
            )
            self.query_one("#evidence_content", Static).update(
                Text(panel.get("text") or "Full result unavailable.")
            )
            overlay = self.query_one("#evidence_overlay", Vertical)
            overlay.add_class("visible")

        def _close_evidence_panel(self, *, focus_input: bool = True) -> None:
            self._evidence_request_generation += 1
            self._evidence_panel = None
            try:
                self.query_one("#evidence_overlay", Vertical).remove_class("visible")
                if focus_input:
                    self.query_one("#input", TextArea).focus()
            except Exception:
                pass

        def _navigate_evidence_panel(self, direction: str) -> None:
            panel = self._evidence_panel
            if panel is None:
                return
            history = tuple(item for item in panel.get("previousOffsets", ()) if isinstance(item, int))
            if direction == "next" and isinstance(panel.get("nextOffset"), int):
                self._schedule_evidence_page(
                    panel["generation"], panel["evidenceId"], panel["nextOffset"], history + (panel["offset"],)
                )
            elif direction == "prev" and history:
                self._schedule_evidence_page(panel["generation"], panel["evidenceId"], history[-1], history[:-1])

        def _handle_evidence_panel_key(self, event: events.Key) -> bool:
            panel = self._evidence_panel
            if panel is None or event.key not in {"escape", "pageup", "page_up", "pagedown", "page_down"}:
                return False
            if event.key == "escape":
                self._close_evidence_panel()
            elif event.key in {"pagedown", "page_down"}:
                self._navigate_evidence_panel("next")
            elif event.key in {"pageup", "page_up"}:
                self._navigate_evidence_panel("prev")
            event.stop()
            event.prevent_default()
            return True

        def on_button_pressed(self, event: Button.Pressed) -> None:
            """Route fixed app-created controls without interpreting user text."""
            button_id = str(event.button.id or "")
            if button_id == "approval-approve":
                self._resolve_approval(True)
                return
            if button_id == "approval-reject":
                self._resolve_approval(False, self._approval_reason())
                return
            if button_id == "evidence-prev":
                self._navigate_evidence_panel("prev")
                return
            if button_id == "evidence-next":
                self._navigate_evidence_panel("next")
                return
            if button_id == "evidence-close":
                self._close_evidence_panel()
                return

        def action_toggle_cooperation(self) -> None:
            if not isinstance(self.session, TuiSessionView):
                enabled = self.session.approval_callback is None
                try:
                    self.session.settings_manager.set_tool_approval(enabled)
                except Exception as e:
                    self._write(f"Unable to persist cooperation setting: {e}", "error")
                    return
                if enabled:
                    self._install_approval_callback(self.session)
                    self._cooperation_disabled = False
                    self._write(
                        "[Cooperation] enabled: mutating tools (bash/write/edit/plan/apply_patch) ask first", "info"
                    )
                else:
                    self.session.approval_callback = None
                    self._cooperation_disabled = True
                    self._reject_all_approvals("Cooperation disabled")
                    self._write("[Cooperation] disabled: all tools run freely", "info")
                self._schedule_sidebar_refresh()
                return
            asyncio.create_task(self._toggle_cooperation())

        async def _toggle_cooperation(self) -> None:
            """Ctrl+Z: toggle ask-before-running (cooperation) mode.

            Disabling cooperation releases pending approvals and model
            questions.
            """
            enabled = not bool(self.session.snapshot["sidebar"].get("cooperation", False))
            if isinstance(self.session, TuiSessionView):
                if not await self._run_view_command("set_cooperation", {"enabled": enabled}):
                    return
                self._cooperation_disabled = not enabled
                if not enabled:
                    self._reject_all_approvals("Cooperation disabled")
                self._write(f"[Cooperation] {'enabled' if enabled else 'disabled'}", "info")
                return

        def action_approve_tool(self) -> None:
            self._resolve_approval(True)

        def action_reject_tool(self) -> None:
            self._resolve_approval(False, self._approval_reason())

        def action_show_model_picker(self) -> None:
            """Open a small, authenticated-model-only picker using existing selection semantics."""
            if isinstance(self.session, TuiSessionView):
                asyncio.create_task(self._open_detached_model_picker())
                return
            self._open_model_picker(list(self.session.model_registry.get_available()))

        async def _open_detached_model_picker(self) -> None:
            self._open_model_picker(await self.session.models())

        def _open_model_picker(self, models: list[Any]) -> None:
            overlay = self.query_one("#model_overlay", Static)
            if self._model_picker_models:
                self._dismiss_model_picker()
                return
            self._model_picker_models = list(models)
            if not self._model_picker_models:
                self._write("No available authenticated models. Use /login first.", "warn")
                return
            current_model = self.session.model
            current = f"{rich_escape(current_model.provider)}/{rich_escape(current_model.id)}" if current_model else "none"
            lines = [f"Model picker — current: {current}", "Type a number and press Enter (Ctrl+K cancels):"]
            lines.extend(f"{i}. {rich_escape(model.provider)}/{rich_escape(model.id)}" for i, model in enumerate(self._model_picker_models, 1))
            overlay.update("\n".join(lines))
            overlay.add_class("visible")
            self.query_one("#input", TextArea).focus()

        async def _select_picker_model(self, text: str) -> None:
            models = self._model_picker_models
            self._dismiss_model_picker()
            if not text.isdigit() or not 1 <= int(text) <= len(models):
                self._write("Model picker cancelled: enter a listed number.", "warn")
                return
            model = models[int(text) - 1]
            if not isinstance(self.session, TuiSessionView):
                await self.session.set_model(model)
                self.session.settings_manager.set_default_provider(model.provider)
                self.session.settings_manager.set_default_model(model.id)
            else:
                await self.session.command(
                    "ui", operation="set_model", payload={"provider": model.provider, "model": model.id}
                )
            self._write(f"Model set to {rich_escape(model.provider)}/{rich_escape(model.id)} (saved as default)", "info")
            self._refresh_sidebar()

        def _dismiss_model_picker(self) -> None:
            """Close the picker without changing the current model."""
            self._model_picker_models = []
            try:
                self.query_one("#model_overlay", Static).remove_class("visible")
            except Exception:
                pass

        def action_toggle_subagents(self) -> None:
            asyncio.create_task(self._toggle_subagents())

        async def _toggle_subagents(self) -> None:
            if getattr(self.session, "subagents_hard_disabled", False):
                self._write(
                    "Subagents are unavailable for this session because it was started with --no-subagents.", "info"
                )
                self._schedule_sidebar_refresh()
                return
            enabled = True
            if isinstance(self.session, TuiSessionView):
                enabled = bool(self.session.snapshot["sidebar"].get("subagents", True))
            else:
                enabled = getattr(getattr(self.session, "settings_manager", None), "get_subagents_enabled", lambda: True)()
            if isinstance(self.session, TuiSessionView):
                if not await self._run_view_command("set_subagents", {"enabled": not enabled}):
                    return
            if not isinstance(self.session, TuiSessionView):
                self.session.settings_manager.set_subagents_enabled(not enabled)
            self._write(f"Subagents {'enabled' if not enabled else 'disabled'}.", "info")
            self._refresh_sidebar()

        def action_toggle_bash_show(self) -> None:
            asyncio.create_task(self._toggle_bash_show())

        async def _toggle_bash_show(self) -> None:
            state = True
            if isinstance(self.session, TuiSessionView):
                state = bool(self.session.snapshot["sidebar"].get("bashOutput", True))
            else:
                state = getattr(getattr(self.session, "settings_manager", None), "get_bash_show_output", lambda: True)()
            if isinstance(self.session, TuiSessionView):
                if not await self._run_view_command("set_bash_output", {"enabled": not state}):
                    return
            if not isinstance(self.session, TuiSessionView):
                self.session.settings_manager.set_bash_show_output(not state)
            self._write(f"Bash output: {'on' if not state else 'off'}.", "info")
            self._refresh_sidebar()

        def action_cycle_retry_mode(self) -> None:
            asyncio.create_task(self._cycle_retry_mode())

        async def _cycle_retry_mode(self) -> None:
            """Ctrl+R: cycle retry mode off → on → unlimited → off."""
            current = "on"
            if isinstance(self.session, TuiSessionView):
                current = self.session.snapshot["sidebar"].get("retry", "on")
            else:
                current = getattr(
                    getattr(self.session, "settings_manager", None),
                    "get_retry_mode",
                    lambda: "on",
                )()
            if current == "off":
                new_mode = "on"
            elif current == "on":
                new_mode = "unlimited"
            else:
                new_mode = "off"
            if isinstance(self.session, TuiSessionView):
                if not await self._run_view_command("set_retry", {"mode": new_mode}):
                    return
            if not isinstance(self.session, TuiSessionView):
                self.session.settings_manager.set_retry_mode(new_mode)
                self.session.set_auto_retry_enabled(new_mode in ("on", "unlimited"))
            self._write(f"Auto-retry set to {new_mode}.", "info")
            self._refresh_sidebar()

        def action_decrease_temperature(self) -> None:
            asyncio.create_task(self._decrease_temperature())

        async def _decrease_temperature(self) -> None:
            if isinstance(self.session, TuiSessionView):
                await self._run_view_command("adjust_temperature", {"delta": -0.1})
            if not isinstance(self.session, TuiSessionView):
                self.session.adjust_temperature(-0.1)
            self._refresh_sidebar()

        def action_increase_temperature(self) -> None:
            asyncio.create_task(self._increase_temperature())

        async def _increase_temperature(self) -> None:
            if isinstance(self.session, TuiSessionView):
                await self._run_view_command("adjust_temperature", {"delta": 0.1})
            if not isinstance(self.session, TuiSessionView):
                self.session.adjust_temperature(0.1)
            self._refresh_sidebar()

        def action_clear_stream(self) -> None:
            # Clear is a lifecycle barrier: apply pending state transitions
            # first, then intentionally discard the visible transcript.
            self._flush_pending_ui_events()
            self._invalidate_deferred_stream_viewport()
            stream_widget = self.query_one("#stream")
            stream_widget.update("")
            self._stream_lines = []
            self._stream_front_trim_pending = 0
            self._clear_agent_link_metadata()
            self._rendered_stream_lines = ()
            with self._pending_ui_events_lock:
                self._pending_ui_events = []
                self._pending_ui_event_bytes = 0
                self._pending_assistant_deltas = ""
                self._ui_flush_wakeup_pending = False
                self._pending_ui_events_lock.notify_all()
            self._assistant_stream_open = False
            self._assistant_stream = ""
            self._assistant_has_live_delta = False
            self._assistant_live_start_idx = -1
            self._assistant_live_buffer = ""
            self._assistant_live_line_count = 0
            self._active_tool_block = None
            self._reset_turn_timing()
            self._thinking_active = False
            self._thinking_label_shown = False
            self._thinking_buffer = ""
            self._thinking_line_idx = None
            self._last_auto_copied = ""
            self._dismiss_model_picker()

        def action_show_shortcuts(self) -> None:
            """Show one-owned shortcuts rather than Textual's merged key panel."""
            content = Text()
            content.append_text(Text.from_markup(f"[b {self._theme.info}]one keyboard shortcuts[/]"))
            content.append("\n\n")
            content.append(format_tui_shortcuts())
            content.append("\n\nPress Esc to close.")
            try:
                overlay = self.query_one("#shortcuts_overlay", Static)
                overlay.update(content)
                overlay.add_class("visible")
            except Exception:
                pass

        def action_close_shortcuts(self) -> None:
            if self._agents_panel_open:
                self._close_agents_panel()
                return
            try:
                self.query_one("#shortcuts_overlay", Static).remove_class("visible")
            except Exception:
                pass

        def action_help(self) -> None:
            self._write(
                "/help /stats /state /status /tools /model /model-cycle /providers /thinking /thinking-cycle /temperature [value|mode] /theme /queue /steer /follow /compact /tree /navigate /fork /new /sessions [number|full exact name] /login [status|refresh <provider>|provider [apiKey] [model] [subscription]] /logout /reload /retry /retry-cycle /skill [list] /skill:<name> [args] /config /extui /cooperation /subagents /agents /details-show /history /mcp /bash /paste-image /abort /clear /exit",
                "info",
            )

        def _show_extension_panel(self, req: dict[str, Any]) -> None:
            """Render an extension widget/overlay payload as a TUI component."""
            ui_type = str(req.get("uiType") or "widget")
            title = str(req.get("title") or "")
            payload = req.get("payload") or {}
            content = Text()
            content.append_text(Text.from_markup(f"[b]{title or req.get('extension', 'extension')}[/b]"))
            content.append("\n")
            content.append(json.dumps(payload, ensure_ascii=False, indent=2))
            try:
                if ui_type == "overlay":
                    overlay = self.query_one("#ext_overlay", Static)
                    overlay.update(content)
                    overlay.add_class("visible")
                else:
                    panel = self.query_one("#ext_panel", Static)
                    panel.update(content)
                    panel.add_class("visible")
            except Exception:
                pass

        def _hide_extension_panels(self) -> None:
            for widget_id in ("#ext_panel", "#ext_overlay"):
                try:
                    self.query_one(widget_id, Static).remove_class("visible")
                except Exception:
                    pass

        def _clear_extension_ui_state(self) -> None:
            self._extension_ui_pending_request = None
            self._hide_extension_panels()
            try:
                self.query_one("#input", TextArea).placeholder = "Type a command or /help"
            except Exception:
                pass

        async def on_session_event(self, message: SessionEvent) -> None:
            """Handle manually-posted session events on Textual's UI loop.

            Normal session delivery goes through the ordered accumulator above;
            retaining this handler keeps direct UI-event tests/extensions working.
            """
            if message.payload.get("type") == "_flush_pending_ui_events":
                self._flush_pending_ui_events()
            else:
                self._handle_session_event(message.payload)

        def _handle_session_event(self, event: dict[str, Any]) -> None:
            et = event.get("type")
            if et == "subagent_monitor_invalidated":
                with self._monitor_dirty_lock:
                    self._monitor_dirty = True
            elif et == "status_invalidated":
                # The owner emits this only after its coalesced context refresh.
                # Queued notifications from a prior session must not request or
                # apply a snapshot against the current session view.
                if isinstance(self.session, TuiSessionView) and event.get("epoch") == self.session.epoch:
                    self._request_status_refresh()
            elif et == "tui_command_error":
                self._write(f"[{event.get('operation', 'command')}] {event.get('error', 'command failed')}", "error")
            elif et == "mcp_availability":
                name = sanitize_display_text(str(event.get("name") or "server"))
                reason = sanitize_display_text(str(event.get("reasonCode") or "updated"))
                self._write(f"MCP {name}: {reason}", "warn")
            elif et == "subagent_start":
                self._write_subagent_entry(str(event.get("sessionId") or ""), event.get("task"))
            elif et == "message_start":
                msg = event.get("message", {})
                if msg.get("role", event.get("role")) == "assistant":
                    # ``message_end`` only *schedules* the final live-tail
                    # materialization so token bursts do not format on every
                    # event.  A following prompt can, however, reach this
                    # lifecycle barrier before that frame runs.  Do not let
                    # its normal stale-live cleanup discard the completed
                    # predecessor in that window.
                    if self._assistant_live_finalize_pending:
                        self._materialize_assistant_live_lines()
                    # Discard any partial block left from a failed stream
                    # before resetting bookkeeping so the buffer is still
                    # available to calculate the block extent.
                    self._discard_live_assistant_block()
                    self._assistant_stream = ""
                    self._assistant_has_live_delta = False
                    self._assistant_live_start_idx = -1
                    self._assistant_live_buffer = ""
                    self._assistant_live_line_count = 0
                    self._assistant_live_lines_dirty = False
                    self._assistant_live_finalize_pending = False
                    self._assistant_stream_open = True
            elif et == "message_update":
                ae = event.get("assistantMessageEvent", {})
                if ae.get("type") == "text_delta":
                    delta = str(ae.get("delta", ""))
                    # Deltas are coalesced in the session listener.  Retain
                    # this path for manually-posted test/UI events.
                    self._last_delta_ts = time.monotonic()
                    if self._apply_assistant_deltas(delta):
                        self._schedule_stream_render()
            elif et == "message_end":
                msg = event.get("message", {})
                if msg.get("role", event.get("role")) == "assistant":
                    completed_ref = event.get("completedRef")
                    if (
                        event.get("completed") is True
                        and isinstance(completed_ref, str)
                        and isinstance(self.session, TuiSessionView)
                        and event.get("completedSource") == self.session.session_id
                    ):
                        self._open_completed_virtual(completed_ref)
                        self._assistant_stream = ""
                        self._assistant_has_live_delta = False
                        self._assistant_live_start_idx = -1
                        self._assistant_live_buffer = ""
                        self._assistant_live_line_count = 0
                        self._assistant_stream_open = False
                        self._schedule_stream_render()
                        return
                    # When the provider suppressed streamed content (tool JSON
                    # detected early), the message_end carries suppressed=True.
                    # Skip creating an assistant block — the tool call events
                    # below will render the response correctly.
                    suppressed = event.get("suppressed", False)
                    if not suppressed:
                        # If live deltas were already streamed, the content is
                        # visible in the stream — don't re-render it.
                        if self._assistant_has_live_delta:
                            if self._assistant_live_lines_dirty:
                                # Preserve the message-end ordering without
                                # pulling frame-only wrapping back into this
                                # lifecycle reduction.
                                self._assistant_live_finalize_pending = True
                                self._schedule_stream_render()
                            else:
                                end = self._assistant_live_start_idx + self._assistant_live_line_count
                                self._stream_lines.insert(max(0, end), "")
                                self._trim_stream()
                        else:
                            text = self._assistant_stream.strip()
                            if not text:
                                content = msg.get("content", "")
                                if isinstance(content, list):
                                    text = "".join(
                                        x.get("text", "") for x in content if x.get("type") == "text"
                                    ).strip()
                                else:
                                    text = str(content).strip()
                            # Empty native/tool carrier messages have no
                            # user-visible content.  Do not manufacture a
                            # misleading error-like panel; genuine empty final
                            # responses are normalized by AgentSession.
                            if text:
                                self._write_chat_block("assistant", text)
                    # Always reset live state regardless of suppression.
                    if not self._assistant_live_finalize_pending:
                        self._assistant_stream = ""
                        self._assistant_has_live_delta = False
                        self._assistant_live_start_idx = -1
                        self._assistant_live_buffer = ""
                        self._assistant_live_line_count = 0
                    self._assistant_stream_open = False
            elif et == "thinking_delta":
                if self._apply_thinking_delta(str(event.get("delta", ""))):
                    self._schedule_stream_render()
            elif et == "turn_start":
                # A turn is in flight — arm the waiting spinner. This also
                # covers turns the session starts by itself (queued follow-ups
                # drained after the previous turn), which never pass through
                # on_input_submitted.
                self._turn_active = True
                # Retries preserve the agent/turn id and provider totals.
                if event.get("turnId") and event.get("turnId") != self._turn_timing_id:
                    self._reset_turn_timing()
                    self._turn_timing_id = str(event["turnId"])
                self._finalize_thinking_block()
            elif et == "agent_start":
                self._reset_turn_timing()
                if event.get("turnId"):
                    self._turn_timing_id = str(event["turnId"])
            elif et == "provider_request_end":
                turn_id = event.get("turnId")
                request_id = event.get("providerRequestId") or event.get("requestId")
                elapsed = event.get("elapsedMs")
                if (
                    self._turn_timing_id
                    and not self._turn_timing_reported
                    and turn_id == self._turn_timing_id
                    and request_id
                    and str(request_id) not in self._turn_provider_ids
                    and _format_elapsed_ms(elapsed) is not None
                ):
                    self._turn_provider_ids.add(str(request_id))
                    self._turn_provider_elapsed_ms += float(elapsed)
                    self._turn_provider_measurements += 1
            elif et == "compaction_start":
                self._write_lifecycle_separator()
            elif et == "tool_call_start":
                tool_name = str(event.get("tool") or "tool")
                # The detached facade never sends raw argument dictionaries.
                # It supplies a redacted literal operation summary instead.
                args_text = str(event.get("displayArgs") or json.dumps(event.get("args", {}), ensure_ascii=False))
                # A tool block must follow visible assistant prose. This is a
                # genuine ordering barrier, unlike ordinary token updates.
                self._materialize_assistant_live_lines()
                self._finalize_thinking_block()
                self._assistant_stream_open = False
                # Remove any partially-rendered assistant delta so that
                # subsequent nudge/recovery attempts don't stack duplicate
                # blocks (the JSON that was streamed as assistant text is
                # actually a tool-call payload).
                self._discard_live_assistant_block()
                # Render the per-call effective timeout when it's a positive
                # number; omit the suffix for non-time-limited tools.
                et_val = event.get("effectiveTimeout")
                if isinstance(et_val, (int, float)) and et_val > 0:
                    text = f"tool (timeout {int(et_val)}s): {tool_name} {args_text}"
                else:
                    text = f"tool: {tool_name} {args_text}"
                if tool_name == "finish":
                    self._write_lifecycle_separator()
                start, end = self._write_tool_block(text, tool_name=tool_name)
                self._active_tool_block = (tool_name, start, end, text)
                self._next_tool_call_id += 1
                self._active_tool_call_id = self._next_tool_call_id
                self._active_subagent_link_tokens = []
            elif et == "tool_call_parse_failed":
                # Do not show model-supplied arguments or raw fragments here:
                # they can be malformed/untrusted.  The session will make its
                # single bounded repair attempt when applicable.
                if event.get("source") is None and event.get("category") is None:
                    reason = {
                        "malformed_native_tool_call": "malformed native tool call",
                        "conflicting_native_tool_call_id": "conflicting native tool-call ID",
                        "control_tool_in_native_batch": "control tool in native batch",
                    }.get(event.get("reason"), "invalid tool-call candidate")
                    self._write(f"[Tool] {reason}; no tool was executed.", "warn")
                else:
                    source = {
                        "response": "Response",
                        "nudge": "Nudge",
                        "format_repair": "Format repair",
                    }.get(event.get("source"), "Tool call")
                    category = {
                        "malformed_json": "malformed JSON",
                        "invalid_tool_call_shape": "invalid tool-call shape",
                    }.get(event.get("category"), "invalid tool-call candidate")
                    self._write(f"[Tool] {source}: {category}; no tool was executed.", "warn")
            elif et == "tui_approval_request":
                # Runtime-owned FIFO approval.  The event has no Future or
                # live child/session object; reply is correlated by ID+epoch.
                # Copy sanitized displayArgs from the host so the approval
                # summary can render the actual tool arguments.
                display_args = event.get("displayArgs")
                if isinstance(display_args, str):
                    display_args = display_args[:512]
                pending = {
                    "id": str(event.get("approvalId") or ""),
                    "tool": str(event.get("tool") or "tool"),
                    "args": {},
                    "origin": event.get("origin"),
                    "epoch": event.get("epoch"),
                    "displayArgs": display_args,
                }
                if isinstance(self.session, TuiSessionView) and self._detached_waiter_epoch(pending) is None:
                    self._write("[Approval] ignored stale or invalid approval request", "error")
                    return
                self._approval_waiters.append(pending)
                if self._approval_pending is None:
                    self._show_next_approval()
                self._toast(f"Approve: {event.get('tool', 'tool')}", severity="warning", timeout=8.0)
            elif et == "tool_approval_rejected":
                self._write(f"[Rejected] {event.get('tool')}: {event.get('reason', '')}", "warn")
            elif et == "ask_user":
                pending = {"id": str(event.get("id") or ""), "epoch": event.get("epoch")}
                if isinstance(self.session, TuiSessionView) and self._detached_waiter_epoch(pending) is None:
                    self._write("[AskUser] ignored stale or invalid question request", "error")
                    return
                self._ask_user_pending = pending
                self._write_tool_block(f"agent asks: {event.get('question')}")
                self._toast("Agent waiting for response", severity="warning")
                try:
                    input_widget = self.query_one("#input", TextArea)
                    input_widget.placeholder = "Answer for agent:"
                    input_widget.focus()
                except Exception:
                    pass
            elif et == "ask_user_released":
                # The session released a model question after cooperation was
                # disabled. Do not leave a stale input prompt/spinner behind.
                self._ask_user_pending = None
                try:
                    self.query_one("#input", TextArea).placeholder = "Type a command or /help"
                except Exception:
                    pass
                self._schedule_stream_render()
                self._schedule_sidebar_refresh()
            elif et == "cooperation_changed" and not event.get("enabled"):
                self._cooperation_disabled = True
                self._reject_all_approvals("Cooperation disabled")
            elif et == "ask_user_answered":
                self._write(f"[AskUser] answer: {event.get('answer')}", "info")
            elif et == "extension_ui_request":
                ext = str(event.get("extension") or "unknown")
                ui_type = str(event.get("uiType") or "widget")
                title = str(event.get("title") or "")
                _req_id = str(event.get("id") or "")
                heading = f"[ExtUI] {ext} ({ui_type})" + (f" - {title}" if title else "")
                self._write_tool_block(heading)
                self._write(json.dumps(event.get("payload") or {}, ensure_ascii=False, indent=2), "info")
                self._extension_ui_pending_request = dict(event)
                try:
                    input_widget = self.query_one("#input", TextArea)
                    input_widget.placeholder = "Answer for extension (JSON or text; empty = cancel)"
                    input_widget.focus()
                except Exception:
                    pass
                self._show_extension_panel(dict(event))
            elif et == "extension_ui_response":
                rid = str(event.get("requestId") or "")
                cancelled = bool(event.get("cancelled"))
                resp_payload = event.get("payload") or {}
                self._write(
                    f"[ExtUI] response {rid} cancelled={cancelled} " + json.dumps(resp_payload, ensure_ascii=False),
                    "info",
                )
                if (
                    self._extension_ui_pending_request is not None
                    and str(self._extension_ui_pending_request.get("id") or "") == rid
                ):
                    self._clear_extension_ui_state()
            elif et == "tool_call_end":
                status = "ok" if event.get("ok") else "err"
                tool_name = str(event.get("tool") or "tool")
                self._finalize_thinking_block()
                elapsed = _format_elapsed_ms(event.get("elapsedMs"))
                if not self._finish_tool_block(tool_name, status, event.get("elapsedMs")):
                    badge = "✓" if status == "ok" else "×"
                    self._write_bounded_panel(
                        f"Tool: {badge} {tool_name}" + (f" · {elapsed}" if elapsed else ""),
                        f"tool {status}: {tool_name}",
                    )
                show_output = True
                if isinstance(self.session, TuiSessionView):
                    show_output = bool(self.session.snapshot["sidebar"].get("bashOutput", True))
                else:
                    show_output = bool(getattr(self.session.settings_manager, "get_bash_show_output", lambda: True)())
                if tool_name != "finish" and show_output:
                    # Facade DTOs keep the bounded visible result at top level
                    # so it is not JSON-counted twice at UI ingress. Direct
                    # legacy sessions still use the nested result shape.
                    key = "outputText" if event.get("ok") else "error"
                    out = event.get(key) if isinstance(event.get(key), str) else SubagentMonitor.visible_tool_result(event)
                    if out:
                        self._write_result_block(out)
                        if out.endswith("<truncated>"):
                            self._write("[preview stopped at transport byte limit]", "warn")
                    evidence_id = event.get("evidenceId")
                    if isinstance(evidence_id, str):
                        self._write_evidence_link(evidence_id, unavailable=bool(event.get("evidenceUnavailable")))
                    elif event.get("evidenceUnavailable"):
                        self._write_evidence_link("", unavailable=True)
            elif et == "tool_call_nudge_start":
                self._finalize_thinking_block()
            elif et == "turn_end":
                self._retry_state = "idle"
                self._turn_active = False
                self._assistant_stream_open = False
                self._finalize_thinking_block()
                self._remove_thinking_line()
                self._schedule_stream_render()
                if event.get("ok") is False:
                    err = str(event.get("error") or "Unknown error").strip()
                    self._write(f"[error] {err}", "error")
            elif et == "agent_end":
                self._write_turn_timing_summary(event)
            elif et == "auto_retry_end":
                self._retry_state = "idle"
                self._turn_active = False
                self._assistant_stream_open = False
                self._remove_thinking_line()
                self._schedule_stream_render()
            elif et == "auto_retry_start":
                self._retry_state = f"retry-{event.get('attempt')}"
                self._assistant_stream_open = False
                # Defensive: discard any partial block that survived the
                # preceding turn_end (e.g. when turn_end fires without a
                # message_end for a killed stream).
                self._discard_live_assistant_block()
                self._write(
                    f"[retry] attempt {event.get('attempt')}/{event.get('maxAttempts')} in {event.get('delayMs')}ms",
                    "warn",
                )
            elif et == "plan_update":
                # A projection can omit plan content under a transport budget;
                # omission is not a clear.  Only an explicit cleared marker
                # changes the visible plan state.
                if isinstance(self.session, TuiSessionView):
                    self.session.apply_plan_update(event)
                if event.get("cleared") is True or event.get("plan") == "":
                    self._write_plan_block(None)
                elif event.get("plan"):
                    from one.tools.plan import render_plan

                    # This is an informational standalone block. Do not make
                    # it the active lifecycle tool block: the surrounding
                    # plan tool's later tool_call_end must still finish its
                    # own Tool: status panel.
                    self._write_plan_block(render_plan(event["plan"]))

            # Delta batches repaint only the stream. Lifecycle changes mark
            # the sidebar dirty, then one post-drain frame performs the
            # expensive widget/layout update.
            if not (
                et == "thinking_delta"
                or (et == "message_update" and event.get("assistantMessageEvent", {}).get("type") == "text_delta")
            ):
                self._schedule_sidebar_refresh()


class TuiMode:
    def __init__(self, runtime_host: Any, options: dict[str, Any] | None = None) -> None:
        self.runtime_host = runtime_host
        self.options = options or {}

    async def run(self) -> None:
        if not TEXTUAL_AVAILABLE:
            raise RuntimeError("TUI mode requires 'textual'. Install dependencies: pip install -e .")
        # Production supplies AgentRuntimeFacade, never a live runtime host.
        # Keep the explicit host route only for existing in-process test apps.
        facade = (
            self.runtime_host
            if hasattr(self.runtime_host, "command") and not hasattr(self.runtime_host, "session")
            else None
        )
        session: Any | None = None
        try:
            if facade is not None:
                await facade.start()
                session = TuiSessionView(facade)
                await session.refresh()
            else:
                session = self.runtime_host.session
            app = _OneTextualApp(session, self.options, self.runtime_host)
            if facade is None and (
                bool(self.options.get("cooperation"))
                or getattr(session.settings_manager, "get_tool_approval", lambda: False)()
            ):
                app._install_approval_callback(session)
            await app.run_async()
        finally:
            # Abort the session on Ctrl+Q (or any exit) so in-flight
            # provider calls, tool tasks, and bash subprocesses are
            # cancelled before the main cleanup path runs.
            if session is not None:
                try:
                    await session.abort()
                except Exception:
                    # Startup failures may leave no command intake path. The
                    # facade's owned shutdown below remains authoritative.
                    pass
            if facade is not None:
                try:
                    await facade.close()
                finally:
                    facade.finalize()
