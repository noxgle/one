from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import textwrap
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.markup import escape as rich_escape
from rich.text import Text

from one.config import VERSION
from one.core.oauth import OAuthError
from one.core.provider_login import entry_id as fetched_entry_id
from one.core.provider_login import run_oauth_login, validate_and_fetch
from one.core.session_manager import SessionInfo, SessionManager, normalize_session_name
from one.core.types import ModelInfo
from one.tools.common import sanitize_display_text

try:
    from textual import events
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.message import Message
    from textual.widgets import Static, TextArea
    from textual.widgets.text_area import Edit

    TEXTUAL_AVAILABLE = True
except Exception:  # pragma: no cover
    TEXTUAL_AVAILABLE = False


def build_sidebar_snapshot(
    session: Any,
    retry_state: str = "idle",
) -> dict[str, Any]:
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
        for s in statuses:
            if not s.get("enabled"):
                continue
            runtime_state = str(s.get("runtimeState") or "").lower()
            available = (
                bool(s.get("running"))
                and not s.get("error")
                and runtime_state not in {"failed", "retrying", "disabled"}
            )
            mcp_servers.append(
                {
                    "name": str(s.get("name") or ""),
                    "transport": str(s.get("transport") or ""),
                    "toolCount": len(s.get("tools") or []),
                    "running": bool(s.get("running")),
                    "error": s.get("error"),
                    "available": available,
                }
            )

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
        "cwd": getattr(getattr(session, "session_manager", None), "cwd", "."),
        "sessionId": getattr(session, "session_id", "-"),
        "streaming": bool(getattr(session, "is_streaming", False)),
        "compacting": bool(getattr(session, "is_compacting", False)),
        "retry": retry_display,
        "coop": "on" if getattr(session, "approval_callback", None) is not None else "off",
        "contextPercent": float(usage.get("percent") or 0.0),
        "contextKnown": isinstance(usage.get("percent"), (int, float)),
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
        "subagents": bool(getattr(getattr(session, "settings_manager", None), "get_subagents_enabled", lambda: True)()),
        "bashOutput": bool(getattr(getattr(session, "settings_manager", None), "get_bash_show_output", lambda: True)()),
        "mcpEnabled": mcp_manager is not None,
        "mcpServers": mcp_servers,
    }


# Braille spinner frames shown in the chat stream while the model is working.
_THINKING_FRAMES = "░▒▓█▓▒"
# Sentinel prefix marking the animated "waiting" line inside _stream_lines so
# it can be located and removed reliably (even after list trimming). The
# __MK__: prefix is stripped at render time, so it never shows in the UI.
_THINKING_MARK = "__MK__:"
_THINKING_TEXT_MARK = "__MK_THINK__: "

# Startup artwork is a dedicated visual widget, kept outside both the durable
# conversation and the transcript presentation cache.
_TUI_LOGO_LINES: tuple[str, ...] = (
    r" ██████╗ ███╗   ██╗███████╗",
    r"██╔═══██╗████╗  ██║██╔════╝",
    r"██║   ██║██╔██╗ ██║█████╗  ",
    r"██║   ██║██║╚██╗██║██╔══╝  ",
    r"╚██████╔╝██║ ╚████║███████╗",
    r" ╚═════╝ ╚═╝  ╚═══╝╚══════╝",
    "",
    r"███████╗ ██████╗ ██████╗ ",
    r"██╔════╝██╔═══██╗██╔══██╗",
    r"█████╗  ██║   ██║██████╔╝",
    r"██╔══╝  ██║   ██║██╔══██╗",
    r"██║     ╚██████╔╝██║  ██║",
    r"╚═╝      ╚═════╝ ╚═╝  ╚═╝",
    "",
    r"███████╗██╗   ██╗███████╗██████╗ ██╗   ██╗ ██████╗ ███╗   ██╗███████╗",
    r"██╔════╝██║   ██║██╔════╝██╔══██╗╚██╗ ██╔╝██╔═══██╗████╗  ██║██╔════╝",
    r"█████╗  ██║   ██║█████╗  ██████╔╝ ╚████╔╝ ██║   ██║██╔██╗ ██║█████╗  ",
    r"██╔══╝  ╚██╗ ██╔╝██╔══╝  ██╔══██╗  ╚██╔╝  ██║   ██║██║╚██╗██║██╔══╝  ",
    r"███████╗ ╚████╔╝ ███████╗██║  ██║   ██║   ╚██████╔╝██║ ╚████║███████╗",
    r"╚══════╝  ╚═══╝  ╚══════╝╚═╝  ╚═╝   ╚═╝    ╚═════╝ ╚═╝  ╚═══╝╚══════╝",
)

# The transcript is a presentation cache.  AgentSession and session JSONL keep
# the complete conversation; this list is deliberately bounded so rebuilding
# the Static widget remains predictable during long-running sessions.
MAX_RENDERED_LINES = 500
_TRUNCATED_ASSISTANT_MARKER = "[earlier assistant output truncated in viewport]"

# Maximum characters for the plan section in the sidebar (truncated with …).
_PLAN_SIDEBAR_MAX = 200

# One source of truth for user-visible keyboard shortcuts. Textual itself
# provides many widget/editor bindings, but these are the application-level
# actions users can rely on in the TUI.
TUI_SHORTCUTS: tuple[tuple[str, str], ...] = (
    ("Ctrl+K", "provider/model picker"),
    ("Ctrl+P", "command palette"),
    ("Ctrl+C", "abort"),
    ("Ctrl+L", "clear stream"),
    ("Ctrl+Q", "quit"),
    ("Ctrl+Z", "toggle cooperation"),
    ("Ctrl+S", "toggle subagents"),
    ("Ctrl+O", "toggle details output"),
    ("Ctrl+V", "paste text from the host/system clipboard"),
    ("Ctrl+Shift+V", "paste terminal text (SSH-safe)"),
    ("Ctrl+Alt+V", "paste image from the system clipboard"),
    ("Ctrl+R", "cycle retry mode"),
    ("Ctrl+Up / Ctrl+Down", "navigate input history (50 entries)"),
    ("Ctrl+F1", "show slash-command help"),
    ("Esc", "close the shortcuts panel"),
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
    """True when a turn is in flight but no assistant text has streamed recently.

    Covers: waiting for the first token, thinking between tool calls, tool
    execution and auto-retry delays. While text deltas arrive the indicator
    stays hidden (the text itself is the feedback).
    """
    return turn_active and (now - last_delta_ts) >= idle_threshold


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
        r'''(?<![\\])"([^"]*\.(?:png|jpg|jpeg|webp))"(?![^<]*>)''',
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

        #stream_container {
            height: 1fr;
            margin: 1 0 0 0;
            border: round #2f466e;
            background: #0f1a2e;
            overflow-y: auto;
        }

        #main.layout-wide #stream_container {
            margin: 0;
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
        """

        BINDINGS = [
            ("ctrl+c", "abort", "Abort"),
            ("ctrl+l", "clear_stream", "Clear"),
            ("ctrl+q", "quit", "Quit"),
            Binding("ctrl+z", "toggle_cooperation", "Toggle approval", priority=True),
            ("ctrl+s", "toggle_subagents", "Toggle subagents"),
            Binding("ctrl+o", "toggle_bash_show", "Toggle details output", priority=True),
            Binding("ctrl+r", "cycle_retry_mode", "Cycle retry mode", priority=True),
            Binding("ctrl+k", "show_model_picker", "Model picker", priority=True),
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
                self.options.get("theme") or getattr(session.settings_manager, "get_theme", lambda: "default")()
            )
            self._off_listener = None
            self._assistant_stream = ""
            self._stream_lines: list[str] = []
            self._rendered_stream_lines: tuple[str, ...] = ()
            self._retry_state = "idle"
            self._last_auto_copied = ""
            self._assistant_has_live_delta = False
            self._assistant_live_start_idx = -1
            self._assistant_live_buffer = ""
            self._assistant_live_line_count = 0
            # Providers can produce both text and thinking deltas much faster
            # than Textual can paint. Keep *all* session events in one ordered
            # accumulator: delta runs are painted at a bounded cadence and a
            # lifecycle event is a barrier that flushes everything before it.
            # The listener may be called from a provider thread, so it must not
            # touch Textual widgets or post one message per token.
            self._pending_ui_events: list[dict[str, Any]] = []
            self._pending_ui_events_lock = threading.Lock()
            # One coalesced flush wakeup may be queued at a time; the 30 Hz
            # interval remains a safety net if no wakeup can be posted.
            self._ui_flush_wakeup_pending = False
            self._session_listener_generation = 0
            # Compatibility observability for the existing direct TUI tests.
            # The ordered event list above remains the sole rendering source.
            self._pending_assistant_deltas = ""
            self._pending_assistant_deltas_lock = self._pending_ui_events_lock
            self._assistant_stream_open = False
            self._active_tool_block: tuple[str, int, int, str] | None = None
            self._turn_active = False
            self._last_delta_ts = 0.0
            self._thinking_frame = 0
            self._thinking_active = False
            self._thinking_label_shown = False
            self._thinking_buffer: str = ""
            self._thinking_line_idx: int | None = None
            self._thinking_label_idx: int | None = None
            self._command_history: list[str] = []
            self._approval_queue: asyncio.Queue | None = None
            self._approval_pending: dict[str, Any] | None = None
            self._extension_ui_pending_request: dict[str, Any] | None = None
            self._login_pending: dict[str, Any] | None = None
            self._ask_user_pending: dict[str, Any] | None = None
            self._pending_clipboard_image_bytes: bytes | None = None
            self._session_delete_pending: SessionInfo | None = None
            self._info_layout = getattr(session.settings_manager, "get_tui_info_panel", lambda: "top")()
            # The layout mode owns information-panel visibility. The persisted
            # setting only selects its initial mode.
            self._layout_mode = "wide" if self._info_layout == "sidebar" else "compact"
            self._model_picker_models: list[ModelInfo] = []
            # Numbered /sessions targets must keep referring to the list the
            # user saw, rather than to a newly mtime-sorted filesystem scan.
            self._session_list_snapshot: list[SessionInfo] | None = None

        def compose(self) -> ComposeResult:
            with Horizontal(id="root"):
                with Vertical(id="main"):
                    yield Static(id="header")
                    with VerticalScroll(id="stream_container"):
                        yield Static("", id="logo")
                        yield Static("", id="stream")
                    yield Static("", id="ext_panel")
                    yield Static("", id="ext_overlay")
                    yield Static("", id="shortcuts_overlay")
                    yield Static("", id="model_overlay")
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
            self._apply_theme(self._theme.name)
            # Drive the "waiting for the model" spinner while a turn is in
            # flight but no assistant text is currently streaming.
            self.set_interval(0.15, self._tick_waiting)
            self.set_interval(1 / 30, self._flush_pending_ui_events)
            self._bind_session()
            self._refresh_sidebar()
            self._apply_information_layout()
            self._refresh_logo()

        def _bind_session(self) -> None:
            """(Re)subscribe to session events.

            Used on mount and after /fork, which swaps the underlying session
            for a fresh one — the old listener must be detached first.
            """
            # Invalidate before detaching and discard the old transcript's
            # accepted events. A callback already in flight from the old
            # session must not append after this boundary.
            with self._pending_ui_events_lock:
                self._session_listener_generation += 1
                listener_generation = self._session_listener_generation
                self._pending_ui_events = []
                self._pending_assistant_deltas = ""
                self._ui_flush_wakeup_pending = False
                self._last_delta_ts = 0.0
            if callable(self._off_listener):
                try:
                    self._off_listener()
                except Exception:
                    pass
                self._off_listener = None
            def _listener(event: dict[str, Any]) -> None:
                # Preserve the session's event order, including the ordering
                # between thinking and ordinary output. Lifecycle events are
                # processed as barriers by _flush_pending_ui_events.
                wake_ui = False
                with self._pending_ui_events_lock:
                    if listener_generation != self._session_listener_generation:
                        return
                    self._pending_ui_events.append(event)
                    if (
                        event.get("type") == "message_update"
                        and event.get("assistantMessageEvent", {}).get("type") == "text_delta"
                    ):
                        self._pending_assistant_deltas += str(event.get("assistantMessageEvent", {}).get("delta", ""))
                    if event.get("type") in ("message_update", "thinking_delta"):
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

            self._off_listener = self.session.subscribe(_listener)

        def _rebuild_session_transcript(self) -> None:
            """Replace the viewport cache with the loaded durable transcript."""
            self._stream_lines = []
            self._rendered_stream_lines = ()
            self._assistant_stream = ""
            self._assistant_stream_open = False
            self._assistant_has_live_delta = False
            self._assistant_live_start_idx = -1
            self._assistant_live_buffer = ""
            self._assistant_live_line_count = 0
            self._active_tool_block = None
            self._remove_thinking_line()
            for message in self.session.messages:
                role = message.get("role")
                content = message.get("content", "")
                if isinstance(content, list):
                    content = "".join(str(part.get("text", "")) for part in content if part.get("type") == "text")
                historical_abort = (
                    role == "assistant"
                    and message.get("stopReason") == "abort"
                    and content == "Request aborted."
                )
                # An abort is persisted so the provider history remains
                # complete.  On reload it is not a live abort, though: render
                # it as historical status rather than replaying the live
                # assistant wording that prompted the session switch.
                if historical_abort:
                    self._write("Previous request was aborted.", "info")
                if role in {"user", "assistant"}:
                    if not historical_abort:
                        self._write_chat_block(role, str(content))
                elif role == "toolResult":
                    self._write_tool_block(str(content))
                elif role == "custom":
                    self._write_chat_block("custom", str(content))
            self._render_stream()

        def _session_infos(self) -> list[SessionInfo]:
            manager = self.session.session_manager
            return SessionManager.list(manager.cwd, manager.session_dir)

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

            names = {info.name for info in self._session_infos() if info.name and rename_rest.startswith(info.name + " ")}
            if not names:
                return target, new_name, None
            if len(names) != 1:
                return None, None, "Session rename target is ambiguous; use its number from /sessions."
            name = names.pop()
            return name, rename_rest[len(name) + 1 :], None

        def _list_sessions(self) -> None:
            infos = self._session_infos()
            self._session_list_snapshot = list(infos)
            if not infos:
                self._write("No persisted sessions in this project.", "info")
                return
            now = time.time()
            lines = ["Sessions (newest first):"]
            for index, info in enumerate(infos, 1):
                seconds = max(0, int(now - info.modified.timestamp()))
                age = f"{seconds // 86400}d ago" if seconds >= 86400 else f"{seconds // 3600}h ago" if seconds >= 3600 else f"{seconds // 60}m ago"
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
            await self.runtime_host.switch_session(info.path)
            self.session = self.runtime_host.session
            self._bind_session()
            self._invalidate_session_list_snapshot()
            self._clear_extension_ui_state()
            self._rebuild_session_transcript()
            self._write(f"Loaded session: {info.name or '(unnamed)'}.", "info")
            self._refresh_sidebar()

        def on_unmount(self) -> None:
            if callable(self._off_listener):
                try:
                    self._off_listener()
                except Exception:
                    pass
                self._off_listener = None

        def _render_stream(self) -> None:
            rendered_lines = tuple(self._stream_lines)
            # A stream update can be requested by several lifecycle paths in a
            # frame. Rebuilding identical Rich Text is pure wasted work and can
            # invalidate a mouse selection while output is still streaming.
            if rendered_lines == self._rendered_stream_lines:
                return
            try:
                stream_widget = self.query_one("#stream")
            except Exception:
                return
            text = Text()
            in_tool_block = False
            for line in self._stream_lines:
                if line.startswith(_THINKING_MARK):
                    # Intentional markup (colour + spinner frame).
                    text.append_text(Text.from_markup(line[len(_THINKING_MARK) :]))
                elif line.startswith(_THINKING_TEXT_MARK):
                    # Thinking text with intentional markup (theme colour).
                    text.append_text(Text.from_markup(line[len(_THINKING_TEXT_MARK) :]))
                elif line in {"Thinking:", "Thought:"}:
                    text.append(line + "\n", style="italic")
                else:
                    is_tool_line = line.startswith(("tool: ", "tool (", "tool start ", "tool ok: ", "tool err: "))
                    in_tool_block = is_tool_line or (in_tool_block and bool(line))
                    self._append_stream_line(text, line, in_tool_block=in_tool_block)
                    if not line:
                        in_tool_block = False
            stream_widget.update(text)
            self._rendered_stream_lines = rendered_lines
            try:
                self.query_one("#stream_container", VerticalScroll).scroll_end(animate=False)
            except Exception:
                pass

        def _refresh_logo(self) -> None:
            """Render startup artwork separately from the selectable transcript."""
            available_width = self._main_width()
            show_full_logo = self.size.height >= 30 and available_width >= 90
            logo = "\n".join(_TUI_LOGO_LINES) if show_full_logo else "one"
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

        def _flush_pending_ui_events(self) -> None:
            """Drain ordered session events without creating a token-rate UI backlog."""
            with self._pending_ui_events_lock:
                pending, self._pending_ui_events = self._pending_ui_events, []
                self._pending_assistant_deltas = ""
                # The queued wakeup is consumed when its drain begins. A
                # concurrent lifecycle append can now schedule its successor.
                self._ui_flush_wakeup_pending = False
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
                    self._render_stream()

            for event in pending:
                event_type = event.get("type")
                is_text_delta = (
                    event_type == "message_update"
                    and event.get("assistantMessageEvent", {}).get("type") == "text_delta"
                )
                if is_text_delta or event_type == "thinking_delta":
                    delta_events.append(event)
                    continue
                flush_deltas()
                self._handle_session_event(event)
            flush_deltas()
            # Do not clear a wakeup scheduled by an append racing this drain.
            # With no successor batch, the coalescing flag is reset at both
            # drain boundaries.
            with self._pending_ui_events_lock:
                if not self._pending_ui_events:
                    self._ui_flush_wakeup_pending = False

        def _flush_pending_assistant_deltas(self) -> None:
            """Backward-compatible test helper for the ordered UI drain."""
            self._flush_pending_ui_events()

        def _apply_assistant_deltas(self, delta: str) -> bool:
            if not delta or not self._assistant_stream_open:
                return False
            self._assistant_stream += delta
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
            self._thinking_buffer += sanitize_display_text(delta)
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
            """Drop the animated 'waiting' line from the stream, if present.

            Also removes the blank separator lines that wrapped the spinner, so
            removing it does not leave extra vertical gaps between blocks.
            """
            for i in range(len(self._stream_lines) - 1, -1, -1):
                if self._stream_lines[i].startswith(_THINKING_MARK):
                    self._stream_lines.pop(i)
                    if i < len(self._stream_lines) and self._stream_lines[i] == "":
                        self._stream_lines.pop(i)
                    if i > 0 and self._stream_lines[i - 1] == "":
                        self._stream_lines.pop(i - 1)
                    break
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
            """Animate the in-stream spinner while the model is working."""
            now = time.monotonic()
            if not evaluate_waiting(self._turn_active, self._last_delta_ts, now):
                if self._thinking_active:
                    self._remove_thinking_line()
                    self._render_stream()
                return
            # Check for user-gate pending state: show a paused indicator only
            # for ask_user (approval gate is no longer shown as a paused label
            # — the approval prompt widget handles that instead).
            pending_label = ""
            if self._ask_user_pending is not None:
                pending_label = "waiting for your response"
            if pending_label:
                line = f"{_THINKING_MARK}[{self._theme.warn}]⏸ {pending_label}[/]"
            else:
                self._thinking_frame = advance_thinking_frame(self._thinking_frame)
                frame = _THINKING_FRAMES[self._thinking_frame]
                line = f"{_THINKING_MARK}[{self._theme.info}]{frame} Ctrl+C abort[/]"
            if self._thinking_active:
                # Rewrite the existing spinner line in place.
                for i in range(len(self._stream_lines) - 1, -1, -1):
                    if self._stream_lines[i].startswith(_THINKING_MARK):
                        self._stream_lines[i] = line
                        self._render_stream()
                        break
                else:
                    # The line was trimmed away; re-append it.
                    self._stream_lines.append("")
                    self._stream_lines.append(line)
                    self._stream_lines.append("")
                    self._trim_stream()
            else:
                self._stream_lines.append("")
                self._stream_lines.append(line)
                self._stream_lines.append("")
                self._trim_stream()
                self._thinking_active = True

        def _write(self, text: str, kind: str = "normal", *, render: bool = True) -> None:
            # Store one logical display line per entry. In particular, a large
            # tool/error payload with embedded newlines must not bypass the
            # viewport cap merely because it arrived through _write.
            self._stream_lines.extend(sanitize_display_text(text).splitlines() or [""])
            self._trim_stream(render=render)

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
            self._write("─" * width)

        def _trim_stream(self, *, render: bool = True) -> int:
            """Trim *_stream_lines* to ``MAX_RENDERED_LINES`` entries. Returns the number of
            lines dropped from the front (0 if no trim happened) so callers
            can rebase any absolute indexes they hold."""
            before = len(self._stream_lines)
            if before > MAX_RENDERED_LINES:
                dropped = before - MAX_RENDERED_LINES
                self._stream_lines = self._stream_lines[-MAX_RENDERED_LINES:]
            else:
                dropped = 0

            # --- rebase assistant live bookkeeping ------------------------
            if dropped and self._assistant_live_start_idx >= 0:
                new_idx = self._assistant_live_start_idx - dropped
                if new_idx < 0:
                    # The live block was partially or fully trimmed away.
                    self._assistant_live_start_idx = -1
                    self._assistant_live_buffer = ""
                    self._assistant_live_line_count = 0
                    self._assistant_has_live_delta = False
                elif (
                    new_idx + self._assistant_live_line_count
                    > len(self._stream_lines)
                ):
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
                        self._active_tool_block = (
                            _name, new_start, new_end, _text
                        )
            else:
                active = self._active_tool_block
                if active is not None:
                    _name, start, end, _text = active
                    if start >= len(self._stream_lines) or end > len(self._stream_lines):
                        self._active_tool_block = None

            if render:
                self._render_stream()
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
            content = sanitize_display_text(text).strip() or "[empty]"
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
            self._assistant_live_buffer += delta
            panel_lines = self._format_chat_panel("assistant", self._assistant_live_buffer)
            # A single message can be larger than the viewport. Keep its tail
            # live (so subsequent deltas continue to replace it) and make the
            # omitted prefix explicit rather than silently presenting a partial
            # message as complete.
            if len(panel_lines) > MAX_RENDERED_LINES:
                panel_lines = [_TRUNCATED_ASSISTANT_MARKER, *panel_lines[-(MAX_RENDERED_LINES - 1) :]]
            if self._assistant_live_start_idx >= 0:
                tail_start = self._assistant_live_start_idx + self._assistant_live_line_count
                tail = self._stream_lines[tail_start:]
                self._stream_lines = self._stream_lines[: self._assistant_live_start_idx] + panel_lines + tail
            else:
                self._stream_lines.extend(panel_lines)
            self._assistant_live_line_count = len(panel_lines)
            self._trim_stream(render=False)

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
                del self._stream_lines[idx : idx + count]
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

        def _write_tool_block(self, text: str) -> tuple[int, int]:
            self._remove_thinking_line()
            self._stream_lines.append("")
            self._stream_lines.append("Tool: ○")
            start = len(self._stream_lines)
            panel_lines = self._bounded_tool_panel(text)
            self._stream_lines.extend(panel_lines)
            end = len(self._stream_lines)
            self._stream_lines.append("")
            dropped = self._trim_stream()
            # Return post-trim indices so callers get positions that are
            # valid against the current (trimmed) _stream_lines list.
            return start - dropped, end - dropped

        def _bounded_tool_panel(self, text: str) -> list[str]:
            panel_lines = self._format_chat_panel("tool", text, pad_y=0)
            display_limit = 40
            if len(panel_lines) > display_limit:
                return ["[tool output preview; full result remains in evidence]", *panel_lines[:display_limit]]
            return panel_lines

        def _finish_tool_block(self, tool_name: str, status: str) -> bool:
            """Append a completed status to the matching active tool block."""
            active = self._active_tool_block
            if active is None:
                return False
            active_name, start, end, text = active
            if active_name != tool_name or not (0 <= start <= end <= len(self._stream_lines)):
                return False
            self._stream_lines[start:end] = self._bounded_tool_panel(f"{text} [{status}]")
            badge = "✓" if status in {"ok", "success"} else "△" if status in {"warning", "disabled"} else "×"
            if start > 0 and self._stream_lines[start - 1].startswith("Tool:"):
                self._stream_lines[start - 1] = f"Tool: {badge} {tool_name}"
            self._active_tool_block = None
            self._trim_stream(render=False)
            self._render_stream()
            return True

        def _apply_theme(self, theme_name: str) -> bool:
            theme = BUILTIN_TUI_THEMES.get((theme_name or "").strip().lower())
            if theme is None:
                return False
            self._theme = theme
            try:
                self.screen.styles.background = theme.screen_bg
                self.screen.styles.color = theme.screen_fg
            except Exception:
                pass
            try:
                stream_widget = self.query_one("#stream_container")
                stream_widget.styles.background = theme.panel_bg
                stream_widget.styles.border = ("round", theme.panel_border)
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
            return True

        def _set_theme(self, theme_name: str) -> bool:
            """Apply and persist a theme for both /theme and Ctrl+P callers."""
            if not self._apply_theme(theme_name):
                return False
            self.session.settings_manager.set_theme(theme_name)
            self._refresh_sidebar()
            self._write(f"Theme set to {theme_name}", "info")
            return True

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
            suffix = f" | THINK: {thinking} | COOP: {coop} | STATUS: {runtime_status}"
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
            s = build_sidebar_snapshot(self.session)
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
            plan_text = getattr(self.session, "_plan", None)

            info_block = Text()
            info_block.append_text(Text.from_markup(f"[b {self._theme.info}]Info[/]"))
            info_block.append("\n")
            info_block.append(f"Version: {VERSION}\n")
            info_block.append(f"Model: {sanitize_display_text(s['model'])}\n")
            info_block.append(f"Theme: {sanitize_display_text(self._theme.name)}\n")
            info_block.append(f"Thinking: {sanitize_display_text(s['thinking'])}\n")
            context = self._context_meter(s["contextPercent"]) if s["contextKnown"] else "unknown"
            info_block.append(f"Context: {context}\n")
            info_block.append(f"Retry: {sanitize_display_text(s['retry'])}\n")
            info_block.append(f"Status: {sanitize_display_text(status)}\n")
            info_block.append(f"Delivery: {sanitize_display_text(s['deliveryMode'])}\n")
            info_block.append(f"COOP: {'ON' if s['coop'] == 'on' or self._approval_pending is not None else 'OFF'}\n")
            info_block.append(f"Subagents: {'on' if s['subagents'] else 'off'}\n")
            info_block.append(f"Details: {'on' if s['bashOutput'] else 'off'}\n")
            info_block.append(f"CWD: {sanitize_display_text(s['cwd'])}\n")
            info_block.append(f"Session: {sanitize_display_text(s['sessionId'])}\n")

            plan_block = Text()
            if plan_text:
                display = plan_text[:_PLAN_SIDEBAR_MAX] + "…" if len(plan_text) > _PLAN_SIDEBAR_MAX else plan_text
                display = sanitize_display_text(display)
                plan_block.append_text(Text.from_markup(f"[b {self._theme.info}]Plan[/]"))
                plan_block.append("\n")
                plan_block.append(display + "\n")

            mcp_block = Text()
            mcp_block.append_text(Text.from_markup(f"[b {self._theme.info}]MCP[/]"))
            mcp_block.append("\n")
            if s["mcpEnabled"] and s["mcpServers"]:
                for server in s["mcpServers"]:
                    marker = "[ok]" if server["available"] else "[!]"
                    marker_style = self._theme.user if server["available"] else self._theme.error
                    mcp_block.append(marker, style=marker_style)
                    mcp_block.append(f" {sanitize_display_text(server['name'])}\n")
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
                    "/retry <on|off|unlimited> | /retry-cycle | /config [key] [value] | /extui <list|request|respond|cancel|clear>", "info"
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
                return
            if cmd == "/clear":
                self.action_clear_stream()
                return
            if cmd == "/paste-image":
                self.action_paste_image()
                return
            self._assistant_has_live_delta = False
            self._assistant_live_start_idx = -1
            self._assistant_live_buffer = ""
            self._assistant_live_line_count = 0
            if cmd == "/abort":
                await session.abort()
                self._turn_active = False
                self._remove_thinking_line()
                self._render_stream()
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
                self._list_sessions()
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
                        self._write(f"Delete session '{target.name or '(unnamed)'}'? Type yes to confirm, anything else cancels.", "warn")
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
                            (info for info in self._session_infos() if info.path != target.path and info.name == normalized),
                            None,
                        )
                        if duplicate is not None:
                            raise ValueError("A session with that name already exists")
                        if target.id == session.session_id:
                            session.set_session_name(normalized)
                            manager = session.session_manager
                        else:
                            manager = SessionManager.open(target.path, session.session_manager.session_dir)
                            manager.set_session_name(normalized)
                    except ValueError as exc:
                        self._write(str(exc), "error")
                        return
                    self._write(f"Renamed session to: {manager.get_session_name()}.", "info")
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
                    self._write("No skills discovered. Place SKILL.md files in project/global skill directories.", "info")
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
                        self._write("No skills discovered. Place SKILL.md files in project/global skill directories.", "info")
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
                    self._write("⚠ Trust warning: skill instructions are loaded as user input. "
                                "Review the skill before granting cooperation approval for file operations.", "warn")
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
                providers = session.model_registry.providers()
                self._write(
                    json.dumps(
                        {"current": current, "providers": providers, "usage": "/model <provider>/<model-id>"},
                        ensure_ascii=False,
                    ),
                    "info",
                )
                return
            if cmd == "/model-cycle":
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
                logged_in = sorted({m.provider for m in session.model_registry.get_available()})
                if not rest:
                    if not logged_in:
                        self._write("No logged-in providers. Use /login <provider> [apiKey] first.", "info")
                        return
                    current_provider = session.model.provider if session.model else None
                    lines = ["Logged-in providers:"]
                    for i, p in enumerate(logged_in, 1):
                        count = len(session.model_registry.models_for_provider(p))
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
                models = session.model_registry.models_for_provider(provider)
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
                    models = session.model_registry.models_for_provider(provider)
                    if not models:
                        self._write(f"Provider not found or has no models: {provider}", "error")
                        return
                    model = models[0]
                    provider, model_id = model.provider, model.id
                else:
                    provider, model_id = val.split("/", 1)
                    model = session.model_registry.resolve(provider, model_id, allow_dynamic=True)
                if not model:
                    self._write(f"Model not found: {provider}/{model_id}", "error")
                    return
                await session.set_model(model)
                session.settings_manager.set_default_provider(model.provider)
                session.settings_manager.set_default_model(model.id)
                if session.model_registry.find(provider, model_id) is None:
                    self._write(f"Model set to {provider}/{model_id} (dynamic, saved as default)", "info")
                else:
                    self._write(f"Model set to {provider}/{model_id} (saved as default)", "info")
                self._refresh_sidebar()
                return
            if cmd == "/thinking":
                self._write(f"Current thinking: {session.thinking_level}", "info")
                self._write("Usage: /thinking <off|minimal|low|medium|high|xhigh>", "info")
                return
            if cmd.startswith("/thinking "):
                level = cmd[len("/thinking ") :].strip()
                session.set_thinking_level(level)
                self._write(f"Thinking level set to {session.thinking_level}", "info")
                self._refresh_sidebar()
                return
            if cmd == "/thinking-cycle":
                level = session.cycle_thinking_level()
                self._write(f"Thinking level cycled to {level}", "info")
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
                current = session.settings_manager.get_theme()
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
                    result = await session.navigate_tree(
                        entry_id,
                        {"summarize": summarize, "customInstructions": custom},
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
                result = await self.runtime_host.new_session({})
                self.session = self.runtime_host.session
                self._bind_session()
                self._invalidate_session_list_snapshot()
                # Fresh session: drop stale stream content from the old one.
                stream_widget = self.query_one("#stream")
                stream_widget.update("")
                self._stream_lines = []
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
                state = session.settings_manager.get_subagents_enabled()
                self._write(f"Subagents: {'on' if state else 'off'}", "info")
                self._write("Usage: /subagents <on|off>", "info")
                return
            if cmd.startswith("/subagents "):
                mode = cmd[len("/subagents ") :].strip().lower()
                if mode not in {"on", "off"}:
                    self._write("Usage: /subagents <on|off>", "error")
                    return
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
                    session.approval_callback = self._approval_prompt
                    self._write("[Cooperation] enabled: mutating tools (bash/write/edit) ask first", "info")
                else:
                    session.approval_callback = None
                    self._write("[Cooperation] disabled: all tools run freely", "info")
                self._refresh_sidebar()
                return
            if cmd.startswith("/bash "):
                command = cmd[len("/bash ") :].strip()
                if not command:
                    self._write("Usage: /bash <command>", "error")
                    return
                try:
                    result = await session.execute_bash(command)
                    output = (result.get("output") or "").rstrip()
                    if result.get("timedOut") or result.get("cancelled"):
                        # Avoid duplicating "Command timed out" if output already ends with it.
                        error_msg = result.get("error", "(timeout)")
                        if result.get("timedOut") and output.lower().endswith("command timed out"):
                            self._write(f"[bash] {error_msg}", "warn")
                            stripped = output[: -len("Command timed out")].rstrip()
                            if stripped:
                                self._write_tool_block(stripped)
                        else:
                            self._write(f"[bash] {error_msg}", "warn")
                            if output:
                                self._write_tool_block(output)
                    elif getattr(session.settings_manager, "get_bash_show_output", lambda: True)() and output:
                        self._write_tool_block(output)
                    else:
                        self._write(f"[bash] exitCode={result.get('exitCode')}", "info")
                except asyncio.CancelledError:
                    self._write("[bash] przerwano", "warn")
                except Exception as e:
                    self._write(str(e), "error")
                return

            self._write(f"Unknown command: {cmd}. Use /help.", "error")

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
            if self._approval_pending is not None:
                await self._handle_approval_answer(text)
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
                        await self.runtime_host.new_session({})
                        self.session = self.runtime_host.session
                        self._bind_session()
                        self._invalidate_session_list_snapshot()
                        self._rebuild_session_transcript()
                        self._clear_extension_ui_state()
                    SessionManager.delete(target.path, self.session.session_manager.session_dir)
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
                                resolved = (
                                    "followUp"
                                    if self.session.follow_up_mode == "follow_up"
                                    else "steer"
                                )
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
                    except Exception as e:
                        self._write(f"[error] {e}", "error")
                        self._refresh_sidebar()
                    finally:
                        if not queued:
                            self._turn_active = False
                            self._remove_thinking_line()
                            self._render_stream()
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

        async def _handle_approval_answer(self, text: str) -> None:
            """Route the input widget answer back to the pending approval prompt."""
            queue = self._approval_queue
            if queue is None:
                self._approval_pending = None
                return
            pending = self._approval_pending or {}
            low = text.lower()
            if pending.get("stage") == "reason":
                if not text:
                    self._write("[Approve] reason is required — enter a reason or 'y' to approve", "warn")
                    return
                queue.put_nowait(("no", text))
                return
            if not text or low in {"y", "yes"}:
                queue.put_nowait(("yes", ""))
                return
            if low.startswith("n"):
                reason = text[1:].strip()
                if reason:
                    queue.put_nowait(("no", reason))
                    return
                pending["stage"] = "reason"
                try:
                    self.query_one("#input", TextArea).placeholder = "Rejection reason:"
                except Exception:
                    pass
                self._write("[Approve] provide a rejection reason", "warn")
                return
            # Any other non-empty text is treated as the rejection reason.
            queue.put_nowait(("no", text))

        async def _handle_extension_ui_answer(self, text: str) -> None:
            """Route the input widget answer back to the pending extension UI request."""
            req = self._extension_ui_pending_request
            if req is None:
                return
            request_id = str(req.get("id") or "")
            try:
                if not text:
                    self.session.respond_extension_ui(request_id=request_id, cancelled=True)
                else:
                    try:
                        parsed = json.loads(text)
                        payload = parsed if isinstance(parsed, dict) else {"answer": text}
                    except Exception:
                        payload = {"answer": text}
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
                self.session.answer_question(str(pending.get("id") or ""), answer)
            except ValueError as e:
                self._write(f"[AskUser] {e}", "error")

        async def _complete_login(self, provider: str, api_key: str, model_input: str) -> None:
            """Persist provider apiKey + defaults and optionally switch the model."""
            session = self.session
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
            self._refresh_sidebar()

        async def _approval_prompt(self, tool_name: str, args: dict[str, Any]) -> tuple[bool, str]:
            """Cooperation mode callback: ask the user via the input widget."""
            self._approval_pending = {"tool": tool_name, "args": args, "stage": "answer"}
            self._approval_queue = asyncio.Queue()
            self._write(f"[Approve] {tool_name} {json.dumps(args, ensure_ascii=False)}", "warn")
            self._toast(f"Approve: {tool_name}", severity="warning", timeout=8.0)
            try:
                input_widget = self.query_one("#input", TextArea)
                input_widget.placeholder = "Accept (Enter) / n + reason"
                input_widget.focus()
            except Exception:
                pass
            self._refresh_sidebar()
            try:
                verdict, reason = await self._approval_queue.get()
            except asyncio.CancelledError:
                verdict, reason = "no", "aborted by user"
            finally:
                self._approval_pending = None
                self._approval_queue = None
                try:
                    self.query_one("#input", TextArea).placeholder = "Type a command or /help"
                except Exception:
                    pass
                self._refresh_sidebar()
            if verdict == "yes":
                return True, ""
            return False, reason

        async def _on_key(self, event: events.Key) -> None:
            """Catch Ctrl-C when an approval prompt is open so it rejects
            the pending approval instead of aborting the entire session.

            The ctrl-c interception path is synchronous-safe: put_nowait +
            event.stop() + prevent_default + return — no ``await`` needed
            because we never reach ``super()`` on that branch.
            """
            if event.key == "ctrl+c" and self._approval_pending is not None:
                queue = self._approval_queue
                if queue is not None:
                    queue.put_nowait(("no", "aborted by user"))
                event.stop()
                event.prevent_default()
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
            self._render_stream()
            self._write("[abort requested]", "warn")
            self._refresh_sidebar()

        def action_toggle_cooperation(self) -> None:
            """Ctrl+Z: toggle ask-before-running (cooperation) mode.

            Affects future tool calls only; a pending approval prompt keeps
            waiting for its answer.
            """
            enabled = self.session.approval_callback is None
            try:
                self.session.settings_manager.set_tool_approval(enabled)
            except Exception as e:
                self._write(f"Unable to persist cooperation setting: {e}", "error")
                return
            if enabled:
                self.session.approval_callback = self._approval_prompt
                self._write("[Cooperation] enabled: mutating tools (bash/write/edit/plan/apply_patch) ask first", "info")
            else:
                self.session.approval_callback = None
                self._write("[Cooperation] disabled: all tools run freely", "info")
            self._refresh_sidebar()

        def action_show_model_picker(self) -> None:
            """Open a small, authenticated-model-only picker using existing selection semantics."""
            overlay = self.query_one("#model_overlay", Static)
            if self._model_picker_models:
                self._dismiss_model_picker()
                return
            self._model_picker_models = list(self.session.model_registry.get_available())
            if not self._model_picker_models:
                self._write("No available authenticated models. Use /login first.", "warn")
                return
            current = f"{self.session.model.provider}/{self.session.model.id}" if self.session.model else "none"
            lines = [f"Model picker — current: {current}", "Type a number and press Enter (Ctrl+K cancels):"]
            lines.extend(f"{i}. {model.provider}/{model.id}" for i, model in enumerate(self._model_picker_models, 1))
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
            await self.session.set_model(model)
            self.session.settings_manager.set_default_provider(model.provider)
            self.session.settings_manager.set_default_model(model.id)
            self._write(f"Model set to {model.provider}/{model.id} (saved as default)", "info")
            self._refresh_sidebar()

        def _dismiss_model_picker(self) -> None:
            """Close the picker without changing the current model."""
            self._model_picker_models = []
            try:
                self.query_one("#model_overlay", Static).remove_class("visible")
            except Exception:
                pass

        def action_toggle_subagents(self) -> None:
            enabled = getattr(getattr(self.session, "settings_manager", None), "get_subagents_enabled", lambda: True)()
            self.session.settings_manager.set_subagents_enabled(not enabled)
            self._write(f"Subagents {'enabled' if not enabled else 'disabled'}.", "info")
            self._refresh_sidebar()

        def action_toggle_bash_show(self) -> None:
            state = getattr(getattr(self.session, "settings_manager", None), "get_bash_show_output", lambda: True)()
            self.session.settings_manager.set_bash_show_output(not state)
            self._write(f"Bash output: {'on' if not state else 'off'}.", "info")
            self._refresh_sidebar()

        def action_cycle_retry_mode(self) -> None:
            """Ctrl+R: cycle retry mode off → on → unlimited → off."""
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
            self.session.settings_manager.set_retry_mode(new_mode)
            self.session.set_auto_retry_enabled(new_mode in ("on", "unlimited"))
            self._write(f"Auto-retry set to {new_mode}.", "info")
            self._refresh_sidebar()

        def action_clear_stream(self) -> None:
            # Clear is a lifecycle barrier: apply pending state transitions
            # first, then intentionally discard the visible transcript.
            self._flush_pending_ui_events()
            stream_widget = self.query_one("#stream")
            stream_widget.update("")
            self._stream_lines = []
            self._rendered_stream_lines = ()
            with self._pending_ui_events_lock:
                self._pending_ui_events = []
                self._pending_assistant_deltas = ""
                self._ui_flush_wakeup_pending = False
            self._assistant_stream_open = False
            self._assistant_stream = ""
            self._assistant_has_live_delta = False
            self._assistant_live_start_idx = -1
            self._assistant_live_buffer = ""
            self._assistant_live_line_count = 0
            self._active_tool_block = None
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
            try:
                self.query_one("#shortcuts_overlay", Static).remove_class("visible")
            except Exception:
                pass

        def action_help(self) -> None:
            self._write(
                "/help /stats /state /status /tools /model /model-cycle /providers /thinking /thinking-cycle /theme /queue /steer /follow /compact /tree /navigate /fork /new /sessions [number|full exact name] /login [status|refresh <provider>|provider [apiKey] [model] [subscription]] /logout /reload /retry /retry-cycle /skill [list] /skill:<name> [args] /config /extui /cooperation /subagents /details-show /history /mcp /bash /paste-image /abort /clear /exit",
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
            if et == "message_start":
                msg = event.get("message", {})
                if msg.get("role") == "assistant":
                    # Discard any partial block left from a failed stream
                    # before resetting bookkeeping so the buffer is still
                    # available to calculate the block extent.
                    self._discard_live_assistant_block()
                    self._assistant_stream = ""
                    self._assistant_has_live_delta = False
                    self._assistant_live_start_idx = -1
                    self._assistant_live_buffer = ""
                    self._assistant_live_line_count = 0
                    self._assistant_stream_open = True
            elif et == "message_update":
                ae = event.get("assistantMessageEvent", {})
                if ae.get("type") == "text_delta":
                    delta = str(ae.get("delta", ""))
                    # Deltas are coalesced in the session listener.  Retain
                    # this path for manually-posted test/UI events.
                    self._last_delta_ts = time.monotonic()
                    if self._apply_assistant_deltas(delta):
                        self._render_stream()
            elif et == "message_end":
                msg = event.get("message", {})
                if msg.get("role") == "assistant":
                    # When the provider suppressed streamed content (tool JSON
                    # detected early), the message_end carries suppressed=True.
                    # Skip creating an assistant block — the tool call events
                    # below will render the response correctly.
                    suppressed = event.get("suppressed", False)
                    if not suppressed:
                        # If live deltas were already streamed, the content is
                        # visible in the stream — don't re-render it.
                        if self._assistant_has_live_delta:
                            end = self._assistant_live_start_idx + self._assistant_live_line_count
                            self._stream_lines.insert(max(0, end), "")
                            self._trim_stream()
                        else:
                            text = self._assistant_stream.strip()
                            if not text:
                                content = msg.get("content", "")
                                if isinstance(content, list):
                                    text = "".join(x.get("text", "") for x in content if x.get("type") == "text").strip()
                                else:
                                    text = str(content).strip()
                            final_text = text or "[empty response]"
                            self._write_chat_block("assistant", final_text)
                    # Always reset live state regardless of suppression.
                    self._assistant_stream = ""
                    self._assistant_has_live_delta = False
                    self._assistant_live_start_idx = -1
                    self._assistant_live_buffer = ""
                    self._assistant_live_line_count = 0
                    self._assistant_stream_open = False
            elif et == "thinking_delta":
                if self._apply_thinking_delta(str(event.get("delta", ""))):
                    self._render_stream()
            elif et == "turn_start":
                # A turn is in flight — arm the waiting spinner. This also
                # covers turns the session starts by itself (queued follow-ups
                # drained after the previous turn), which never pass through
                # on_input_submitted.
                self._turn_active = True
                self._finalize_thinking_block()
            elif et == "compaction_start":
                self._write_lifecycle_separator()
            elif et == "tool_call_start":
                tool_name = str(event.get("tool") or "tool")
                args_text = json.dumps(event.get("args", {}), ensure_ascii=False)
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
                start, end = self._write_tool_block(text)
                self._active_tool_block = (tool_name, start, end, text)
            elif et == "tool_approval_rejected":
                self._write(f"[Rejected] {event.get('tool')}: {event.get('reason', '')}", "warn")
            elif et == "ask_user":
                self._ask_user_pending = {"id": str(event.get("id") or "")}
                self._write_tool_block(f"agent asks: {event.get('question')}")
                self._toast("Agent waiting for response", severity="warning")
                try:
                    input_widget = self.query_one("#input", TextArea)
                    input_widget.placeholder = "Answer for agent:"
                    input_widget.focus()
                except Exception:
                    pass
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
                if not self._finish_tool_block(tool_name, status):
                    self._write_tool_block(f"tool {status}: {tool_name}")
                if (
                    tool_name != "finish"
                    and getattr(self.session.settings_manager, "get_bash_show_output", lambda: True)()
                ):
                    payload = event.get("result") or {}
                    if event.get("ok"):
                        out = str(payload.get("outputText") or payload.get("result") or "").rstrip()
                    else:
                        out = str(payload.get("error") or "").rstrip()
                    if out:
                        self._write_tool_block(out)
            elif et == "tool_call_nudge_start":
                self._finalize_thinking_block()
            elif et == "turn_end":
                self._retry_state = "idle"
                self._turn_active = False
                self._assistant_stream_open = False
                self._finalize_thinking_block()
                self._remove_thinking_line()
                self._render_stream()
                if event.get("ok") is False:
                    err = str(event.get("error") or "Unknown error").strip()
                    self._write(f"[error] {err}", "error")
            elif et == "auto_retry_end":
                self._retry_state = "idle"
                self._turn_active = False
                self._assistant_stream_open = False
                self._remove_thinking_line()
                self._render_stream()
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
                plan = event.get("plan", "")
                if plan:
                    self._write_tool_block(f"Plan:\n{plan}")
                else:
                    self._write_tool_block("Plan: cleared")

            # Delta batches repaint only the stream. Sidebar work stays tied
            # to lifecycle/state-changing events, never token rate.
            if not (
                et == "thinking_delta"
                or (et == "message_update" and event.get("assistantMessageEvent", {}).get("type") == "text_delta")
            ):
                self._refresh_sidebar()


class TuiMode:
    def __init__(self, runtime_host: Any, options: dict[str, Any] | None = None) -> None:
        self.runtime_host = runtime_host
        self.options = options or {}

    async def run(self) -> None:
        if not TEXTUAL_AVAILABLE:
            raise RuntimeError("TUI mode requires 'textual'. Install dependencies: pip install -e .")
        session = self.runtime_host.session
        app = _OneTextualApp(session, self.options, self.runtime_host)
        if (
            bool(self.options.get("cooperation"))
            or getattr(session.settings_manager, "get_tool_approval", lambda: False)()
        ):
            session.approval_callback = app._approval_prompt
        try:
            await app.run_async()
        finally:
            # Abort the session on Ctrl+Q (or any exit) so in-flight
            # provider calls, tool tasks, and bash subprocesses are
            # cancelled before the main cleanup path runs.
            await session.abort()
