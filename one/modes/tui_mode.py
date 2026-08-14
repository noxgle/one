from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
import textwrap
from typing import Any

from rich.markup import escape as rich_escape

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
    retry_state: str,
) -> dict[str, Any]:
    usage = session.get_context_usage() or {}
    queues = session.get_pending_queues() or {"steering": [], "followUp": []}
    stats = session.get_session_stats() or {}
    tokens = stats.get("tokens") or {}

    steer_count = len(queues.get("steering", []))
    follow_count = len(queues.get("followUp", []))

    return {
        "model": f"{session.model.provider}/{session.model.id}" if session.model else "none",
        "thinking": getattr(session, "thinking_level", "medium"),
        "cwd": getattr(getattr(session, "session_manager", None), "cwd", "."),
        "sessionId": getattr(session, "session_id", "-"),
        "streaming": bool(getattr(session, "is_streaming", False)),
        "compacting": bool(getattr(session, "is_compacting", False)),
        "retry": retry_state,
        "coop": "on" if getattr(session, "approval_callback", None) is not None else "off",
        "contextPercent": float(usage.get("percent") or 0.0),
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
    }


# Braille spinner frames shown in the chat stream while the model is working.
_THINKING_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# Sentinel prefix marking the animated "waiting" line inside _stream_lines so
# it can be located and removed reliably (even after list trimming). The
# __MK__: prefix is stripped at render time, so it never shows in the UI.
_THINKING_MARK = "__MK__:"


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
    "default": TuiPalette("default", "#0b1220", "#dbe7ff", "#0f1a2e", "#2f466e", "#152441", "#456ca8", "#e7f0ff", "#101a30", "#8fd3ff", "#ffd479", "#ff8a8a", "#9ff0c4", "#8fd3ff", "#ffd479", "#133a2b", "#15324a", "#3a3016"),
    "light": TuiPalette("light", "#f5f7fb", "#1b2433", "#ffffff", "#b8c6df", "#f1f5ff", "#8ea4d5", "#1d2a44", "#f8fbff", "#2f5ea8", "#a96a00", "#b22b2b", "#146c43", "#2f5ea8", "#946200", "#d7f4e7", "#dfe9ff", "#fff0d9"),
    "hacker": TuiPalette("hacker", "#020902", "#7cff7c", "#031003", "#1f6f1f", "#041804", "#2da62d", "#a5ff9e", "#031203", "#7cff7c", "#f0ff8c", "#ff7070", "#9bffb3", "#7cff7c", "#f0ff8c", "#0a2a0a", "#092209", "#2b2b08"),
    "solarized": TuiPalette("solarized", "#002b36", "#93a1a1", "#073642", "#2aa198", "#0a3b47", "#268bd2", "#eee8d5", "#0a3742", "#268bd2", "#b58900", "#dc322f", "#2aa198", "#268bd2", "#b58900", "#08453f", "#0b4050", "#4a4309"),
    "fallout": TuiPalette("fallout", "#12100b", "#ffd77a", "#1a170f", "#8f7b3f", "#211c12", "#b89a4d", "#ffe8a6", "#18140d", "#f4c96c", "#ffb347", "#ff7a62", "#ffd77a", "#f4c96c", "#ffb347", "#3b2f14", "#332811", "#4a3716"),
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
    "/exit", "/quit", "/help", "/stats", "/state", "/status", "/queue", "/tools",
    "/clear", "/abort", "/model", "/model-cycle", "/thinking", "/thinking-cycle",
    "/theme", "/steer", "/follow", "/compact", "/tree", "/navigate", "/fork",
    "/new", "/login", "/logout", "/retry", "/config", "/extui", "/cooperation",
    "/subagents", "/bash-show", "/mcp",
    "/bash",
)


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
            self._completion_prefix = ""
            self._completion_matches: list[str] = []
            self._completion_index = -1
            self._completion_locked = False  # locked after first completion

        BINDINGS = [
            ("ctrl+j", "submit", "Submit"),
            ("shift+enter", "newline", "New line"),
        ]

        async def _on_key(self, event: events.Key) -> None:
            # TextArea._on_key swallows Enter (inserts "\n") before widget
            # BINDINGS are ever consulted, so Enter-to-submit must be handled
            # here, ahead of the superclass. Shift+Enter stays a newline.
            if event.key == "enter":
                event.stop()
                event.prevent_default()
                self.post_message(InputSubmitted(self.text))
                return
            if event.key == "tab":
                if self._complete_slash_command():
                    event.stop()
                    event.prevent_default()
                    return
            else:
                # Any edit/navigation invalidates the current completion cycle.
                self._completion_prefix = ""
                self._completion_matches = []
                self._completion_index = -1
                self._completion_locked = False
            await super()._on_key(event)

        async def action_submit(self) -> None:
            self.post_message(InputSubmitted(self.text))

        def action_newline(self) -> None:
            self.insert("\n")

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
                self._completion_matches = [
                    c for c in _SLASH_COMMANDS if c.startswith("/" + prefix)
                ]
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
            if self.read_only:
                return
            text = _paste_from_system_clipboard()
            if not text:
                text = self.app.clipboard
            if not text:
                return
            if len(text) > self._PASTE_MAX_CHARS:
                text = text[: self._PASTE_MAX_CHARS]
            if result := self._replace_via_keyboard(text, *self.selection):
                self.move_cursor(result.end_location)


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

        #stream_container {
            height: 1fr;
            border: round #2f466e;
            background: #0f1a2e;
            overflow-y: auto;
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
            border: round #2f466e;
            background: #101a30;
            padding: 0 1;
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

        #ext_panel.visible, #ext_overlay.visible {
            display: block;
        }
        """

        BINDINGS = [
            ("ctrl+c", "abort", "Abort"),
            ("ctrl+l", "clear_stream", "Clear"),
            ("ctrl+q", "quit", "Quit"),
            # priority=True so it wins over the focused TextArea's ctrl+a (home).
            Binding("ctrl+a", "toggle_cooperation", "Toggle approval", priority=True),
            ("ctrl+s", "toggle_subagents", "Toggle subagents"),
            ("f1", "help", "Help"),
        ]

        def __init__(self, session: Any, options: dict[str, Any] | None = None, runtime_host: Any | None = None) -> None:
            super().__init__()
            self.session = session
            self.options = options or {}
            self.runtime_host = runtime_host
            self._theme = resolve_tui_theme(self.options.get("theme") or getattr(session.settings_manager, "get_theme", lambda: "default")())
            self._off_listener = None
            self._assistant_stream = ""
            self._stream_lines: list[str] = []
            self._retry_state = "idle"
            self._last_auto_copied = ""
            self._assistant_has_live_delta = False
            self._assistant_live_start_idx = -1
            self._assistant_live_buffer = ""
            self._turn_active = False
            self._last_delta_ts = 0.0
            self._thinking_frame = 0
            self._thinking_active = False
            self._approval_queue: asyncio.Queue | None = None
            self._approval_pending: dict[str, Any] | None = None
            self._extension_ui_pending_request: dict[str, Any] | None = None
            self._login_pending: dict[str, Any] | None = None
            self._ask_user_pending: dict[str, Any] | None = None

        def compose(self) -> ComposeResult:
            with Horizontal(id="root"):
                with Vertical(id="main"):
                    with VerticalScroll(id="stream_container"):
                        yield Static("", id="stream")
                    yield Static("", id="ext_panel")
                    yield Static("", id="ext_overlay")
                    yield _CommandTextArea(placeholder="Wpisz polecenie lub /help", id="input", soft_wrap=True)
                yield Static(id="sidebar")

        def on_mount(self) -> None:
            self.query_one("#input", TextArea).focus()
            self._apply_theme(self._theme.name)
            # Drive the "waiting for the model" spinner while a turn is in
            # flight but no assistant text is currently streaming.
            self.set_interval(0.15, self._tick_waiting)
            self._bind_session()
            self._write("one TUI v2 ready. /help", "info")
            self._refresh_sidebar()

        def _bind_session(self) -> None:
            """(Re)subscribe to session events.

            Used on mount and after /fork, which swaps the underlying session
            for a fresh one — the old listener must be detached first.
            """
            if callable(self._off_listener):
                try:
                    self._off_listener()
                except Exception:
                    pass
                self._off_listener = None

            def _listener(event: dict[str, Any]) -> None:
                try:
                    self.post_message(SessionEvent(event))
                except Exception:
                    self.call_from_thread(self.post_message, SessionEvent(event))

            self._off_listener = self.session.subscribe(_listener)

        def on_unmount(self) -> None:
            if callable(self._off_listener):
                try:
                    self._off_listener()
                except Exception:
                    pass
                self._off_listener = None

        def _render_stream(self) -> None:
            stream_widget = self.query_one("#stream")
            rendered: list[str] = []
            for line in self._stream_lines:
                if line.startswith("__MK__:"):
                    rendered.append(line[len("__MK__:") :])
                else:
                    rendered.append(rich_escape(line))
            stream_widget.update("\n".join(rendered))
            try:
                self.query_one("#stream_container", VerticalScroll).scroll_end(animate=False)
            except Exception:
                pass

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

        def _tick_waiting(self) -> None:
            """Animate the in-stream spinner while the model is working."""
            now = time.monotonic()
            if not evaluate_waiting(self._turn_active, self._last_delta_ts, now):
                if self._thinking_active:
                    self._remove_thinking_line()
                    self._render_stream()
                return
            self._thinking_frame = advance_thinking_frame(self._thinking_frame)
            frame = _THINKING_FRAMES[self._thinking_frame]
            line = f"{_THINKING_MARK}[{self._theme.info}]{frame} Ctrl+C abort[/]"
            if self._thinking_active:
                # Rewrite the existing spinner line in place.
                for i in range(len(self._stream_lines) - 1, -1, -1):
                    if self._stream_lines[i].startswith(_THINKING_MARK):
                        self._stream_lines[i] = line
                        break
                else:
                    # The line was trimmed away; re-append it.
                    self._stream_lines.append("")
                    self._stream_lines.append(line)
                    self._stream_lines.append("")
                    if len(self._stream_lines) > 500:
                        self._stream_lines = self._stream_lines[-500:]
            else:
                self._stream_lines.append("")
                self._stream_lines.append(line)
                self._stream_lines.append("")
                if len(self._stream_lines) > 500:
                    self._stream_lines = self._stream_lines[-500:]
                self._thinking_active = True
            self._render_stream()

        def _write(self, text: str, kind: str = "normal") -> None:
            self._stream_lines.append(sanitize_display_text(text))
            if len(self._stream_lines) > 500:
                self._stream_lines = self._stream_lines[-500:]
            self._render_stream()

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
                self._assistant_live_start_idx = len(self._stream_lines)
                self._assistant_live_buffer = ""
                self._assistant_has_live_delta = True
            self._assistant_live_buffer += delta
            panel_lines = self._format_chat_panel("assistant", self._assistant_live_buffer)
            if self._assistant_live_start_idx >= 0:
                self._stream_lines = self._stream_lines[: self._assistant_live_start_idx] + panel_lines
            else:
                self._stream_lines.extend(panel_lines)
            if len(self._stream_lines) > 500:
                self._stream_lines = self._stream_lines[-500:]
            self._render_stream()

        def _write_chat_block(self, role: str, text: str) -> None:
            self._remove_thinking_line()
            self._stream_lines.append("")
            rendered_text = text
            if role == "user":
                rendered_text = f"> {text}"
            self._stream_lines.extend(self._format_chat_panel(role, rendered_text))
            self._stream_lines.append("")
            if len(self._stream_lines) > 500:
                self._stream_lines = self._stream_lines[-500:]
            self._render_stream()

        def _write_tool_block(self, text: str) -> None:
            self._remove_thinking_line()
            self._stream_lines.append("")
            self._stream_lines.extend(self._format_chat_panel("tool", text, pad_y=0))
            self._stream_lines.append("")
            if len(self._stream_lines) > 500:
                self._stream_lines = self._stream_lines[-500:]
            self._render_stream()

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
                    subprocess.run(cmd, input=payload, text=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

        def _refresh_sidebar(self) -> None:
            s = build_sidebar_snapshot(
                self.session,
                retry_state=self._retry_state,
            )
            if s["compacting"]:
                status = "compacting…"
            elif self._retry_state != "idle":
                status = "retrying…"
            elif s["streaming"]:
                status = "working…"
            else:
                status = "idle"
            sidebar = (
                f"[b {self._theme.info}]Info[/]\n"
                f"Model: {s['model']}\n"
                f"Theme: {self._theme.name}\n"
                f"Thinking: {s['thinking']}\n"
                f"Ctx: {s['contextPercent']:.1f}%\n"
                f"Retry: {s['retry']}\n"
                f"Status: {status}\n"
                f"Coop: {s['coop']} (Ctrl+A)\n"
                f"Subagents: {'on' if s['subagents'] else 'off'} (Ctrl+S)\n"
                f"Bash: {'on' if s['bashOutput'] else 'off'}\n"
                f"CWD: {s['cwd']}\n"
                f"Session: {s['sessionId']}\n"
                "\n"
                f"[b {self._theme.info}]Keys[/]\n"
                "Ctrl+C abort\nCtrl+L clear\nCtrl+Q quit\nCtrl+A coop\nCtrl+V paste\n"
            )
            sidebar = sanitize_display_text(sidebar)
            self.query_one("#sidebar", Static).update(sidebar)

        def _try_auto_copy_selected_stream_text(self) -> None:
            # Textual 8 keeps arbitrary text selections in `screen.selections`
            # (not on the widget); `Static` has no `selected_text` attribute.
            try:
                text = str(self.screen.get_selected_text() or "").strip()
            except Exception:
                return
            if not text:
                return
            if text == self._last_auto_copied:
                return
            status, _backend = self._copy_to_clipboard(text)
            if status == "copied":
                self._last_auto_copied = text
                self._toast("selection copied", severity="information")
            elif status == "attempted":
                self._toast("clipboard request sent to terminal (OSC52). terminal may ignore it.", severity="warning")
            else:
                self._toast("no clipboard backend. Install wl-clipboard/xclip/xsel or pyperclip.", severity="warning")

        def on_mouse_up(self, event: events.MouseUp) -> None:
            # After a drag the screen retains the selection; after a plain
            # click it is cleared, so this only fires for real selections.
            self.call_after_refresh(self._try_auto_copy_selected_stream_text)

        async def _handle_command(self, cmd: str) -> None:
            session = self.session
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
            if cmd == "/help":
                self._write("/exit /quit | /help | /stats | /state /status | /queue | /tools | /clear | /abort", "info")
                self._write(
                    "/model [provider/model] | /model-cycle | /thinking [level] | /thinking-cycle | /theme [name]",
                    "info",
                )
                self._write(
                    "/steer <text> | /follow <text> | /compact [instructions] | /tree | /navigate <id> [--summary <text>] | /fork <id> | /new | /login [status|provider [apiKey] [model]] | /logout <provider>",
                    "info",
                )
                self._write("/retry <on|off> | /config [key] [value] | /extui <list|request|respond|cancel|clear>", "info")
                self._write("/cooperation [on|off] | /subagents [on|off] | /bash-show [on|off] | /mcp [list|enable|disable] | /bash <command>", "info")
                return
            if cmd == "/clear":
                stream_widget = self.query_one("#stream")
                stream_widget.update("")
                self._stream_lines = []
                self._assistant_has_live_delta = False
                self._assistant_live_start_idx = -1
                self._assistant_live_buffer = ""
                return
            self._assistant_has_live_delta = False
            self._assistant_live_start_idx = -1
            self._assistant_live_buffer = ""
            if cmd == "/abort":
                await session.abort()
                self._turn_active = False
                self._remove_thinking_line()
                self._render_stream()
                self._write("[abort requested]", "warn")
                return
            if cmd == "/status":
                s = build_sidebar_snapshot(session, self._retry_state)
                self._write(json.dumps(s, ensure_ascii=False), "info")
                return
            if cmd == "/stats":
                self._write(json.dumps(session.get_session_stats(), ensure_ascii=False), "info")
                return
            if cmd == "/state":
                self._write(
                    json.dumps(
                        {
                            "model": {"provider": session.model.provider, "id": session.model.id} if session.model else None,
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
            if cmd == "/model":
                current = f"{session.model.provider}/{session.model.id}" if session.model else "none"
                providers = session.model_registry.providers()
                self._write(
                    json.dumps({"current": current, "providers": providers, "usage": "/model <provider>/<model-id>"}, ensure_ascii=False),
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
                self._write(f"Thinking level set to {level}", "info")
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
                if not self._apply_theme(name):
                    self._write("Unknown theme. Available: " + ", ".join(sorted(BUILTIN_TUI_THEMES.keys())), "error")
                    return
                session.settings_manager.set_theme(name)
                self._write(f"Theme set to {name}", "info")
                self._refresh_sidebar()
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
                self._write(json.dumps(result, ensure_ascii=False), "info")
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
                # Fresh session: drop stale stream content from the old one.
                stream_widget = self.query_one("#stream")
                stream_widget.update("")
                self._stream_lines = []
                self._assistant_has_live_delta = False
                self._assistant_live_start_idx = -1
                self._assistant_live_buffer = ""
                self._clear_extension_ui_state()
                self._write("New session started.", "info")
                self._refresh_sidebar()
                return
            if cmd.startswith("/login"):
                rest = cmd[len("/login") :].strip()
                parts = rest.split() if rest else []
                if parts and parts[0] == "status":
                    if len(parts) == 2:
                        self._write(json.dumps(session.model_registry.get_provider_auth_status(parts[1]), ensure_ascii=False), "info")
                    else:
                        providers = session.model_registry.providers()
                        statuses = [session.model_registry.get_provider_auth_status(p) for p in providers]
                        self._write(json.dumps({"providers": statuses}, ensure_ascii=False), "info")
                    return
                provider = parts[0] if len(parts) >= 1 else ""
                if not provider:
                    self._write("Provider is required. Usage: /login <provider> [apiKey] [model]", "error")
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
                session.model_registry.remove_stored_api_key(provider)
                self._write(f"Removed stored key for {provider}.", "info")
                return
            if cmd.startswith("/retry "):
                mode = cmd[len("/retry ") :].strip().lower()
                if mode not in {"on", "off"}:
                    self._write("Usage: /retry <on|off>", "error")
                    return
                session.set_auto_retry_enabled(mode == "on")
                self._write(f"Auto-retry set to {mode}.", "info")
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
            if cmd == "/bash-show":
                state = getattr(session.settings_manager, "get_bash_show_output", lambda: True)()
                self._write(f"Bash output: {'on' if state else 'off'}", "info")
                self._write("Usage: /bash-show <on|off>", "info")
                return
            if cmd.startswith("/bash-show "):
                mode = cmd[len("/bash-show ") :].strip().lower()
                if mode not in {"on", "off"}:
                    self._write("Usage: /bash-show <on|off>", "error")
                    return
                session.settings_manager.set_bash_show_output(mode == "on")
                self._write(f"Bash output set to {mode}.", "info")
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
                        self._write(f"- {s['name']}: {state} [{tools_list}]", "info")
                    self._write("Usage: /mcp list | /mcp enable <name> | /mcp disable <name>", "info")
                    return
                if sub == "enable":
                    if not parts or len(parts) < 2:
                        self._write("Usage: /mcp list | /mcp enable <name> | /mcp disable <name>", "error")
                        return
                    server_name = parts[1]
                    cfg = session.settings_manager.get_mcp_servers().get(server_name)
                    if not cfg or not cfg.get("command"):
                        self._write(f"No MCP config for '{server_name}'. Add mcpServers.<name> to settings.json (see README).", "error")
                        return
                    manager = getattr(session, "_mcp_manager", None)
                    if manager is None:
                        self._write("MCP not available (started with --no-mcp).", "error")
                        return
                    try:
                        added = await manager.enable_server(server_name, str(cfg["command"]), cfg.get("args") or [], cfg.get("env") or {})
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
                        removed = await manager.disable_server(server_name)
                        if removed:
                            self._write(f"Server '{server_name}' disabled. Removed tools: {', '.join(removed)}", "info")
                            session.sync_mcp_tools()
                            session.settings_manager.set_mcp_server_enabled(server_name, False)
                        else:
                            self._write(f"No running server named '{server_name}'.", "warn")
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
                        resp = session.respond_extension_ui(request_id=request_id, payload=payload, cancelled=sub == "cancel")
                    except ValueError as e:
                        self._write(str(e), "error")
                        return
                    self._write(json.dumps(resp, ensure_ascii=False), "info")
                    return
                self._write("Usage: /extui <list|request|respond|cancel|clear>", "error")
                return
            if cmd.strip() == "/cooperation":
                enabled = session.approval_callback is not None
                self._write(json.dumps({"enabled": enabled, "tools": sorted(session._approval_tools)}, ensure_ascii=False), "info")
                return
            if cmd.startswith("/cooperation "):
                mode = cmd[len("/cooperation ") :].strip().lower()
                if mode in {"on", "enable", "yes", "1", "true"}:
                    session.approval_callback = self._approval_prompt
                    self._write("[Cooperation] enabled: mutating tools (bash/write/edit) ask first", "info")
                elif mode in {"off", "disable", "no", "0", "false"}:
                    session.approval_callback = None
                    self._write("[Cooperation] disabled: all tools run freely", "info")
                else:
                    self._write("Usage: /cooperation [on|off]", "error")
                    return
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
                    if getattr(session.settings_manager, "get_bash_show_output", lambda: True)() and output:
                        self._write_tool_block(output)
                    else:
                        self._write(f"[bash] exitCode={result.get('exitCode')}", "info")
                except asyncio.CancelledError:
                    self._write("[bash] przerwano", "warn")
                except Exception as e:
                    self._write(str(e), "error")
                return

            self._write(f"Unknown command: {cmd}. Use /help.", "error")

        async def on_input_submitted(self, event: InputSubmitted) -> None:
            text = event.value.strip()
            self.query_one("#input", TextArea).text = ""
            if self._approval_pending is not None:
                await self._handle_approval_answer(text)
                return
            # Slash commands always take priority over pending prompts so that
            # /new, /fork, /abort etc. work even when extension UI is waiting.
            if text.startswith("/"):
                await self._handle_command(text)
                return
            if self._extension_ui_pending_request is not None:
                await self._handle_extension_ui_answer(text)
                return
            if self._login_pending is not None:
                await self._handle_login_answer(text)
                return
            if self._ask_user_pending is not None:
                await self._handle_ask_user_answer(text)
                return
            if not text:
                return

            self._write_chat_block("user", text)
            self._turn_active = True
            self._last_delta_ts = 0.0

            async def _run_prompt() -> None:
                try:
                    await self.session.prompt(text)
                except Exception as e:
                    self._write(f"[error] {e}", "error")
                    self._refresh_sidebar()
                finally:
                    self._turn_active = False
                    self._remove_thinking_line()
                    self._render_stream()
                    self._refresh_sidebar()

            asyncio.create_task(_run_prompt())

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
                    self._write("[Approve] powód jest wymagany — wpisz powód lub 'y' aby zatwierdzić", "warn")
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
                    self.query_one("#input", TextArea).placeholder = "Powód odrzucenia:"
                except Exception:
                    pass
                self._write("[Approve] podaj powód odrzucenia", "warn")
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
                input_widget.placeholder = "Wpisz polecenie lub /help"
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
                input_widget.placeholder = "Wpisz polecenie lub /help"
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
            try:
                input_widget = self.query_one("#input", TextArea)
                input_widget.placeholder = "Akceptuj (Enter) / n + powód"
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
                    self.query_one("#input", TextArea).placeholder = "Wpisz polecenie lub /help"
                except Exception:
                    pass
                self._refresh_sidebar()
            if verdict == "yes":
                return True, ""
            return False, reason

        async def action_abort(self) -> None:
            await self.session.abort()
            self._turn_active = False
            self._remove_thinking_line()
            self._render_stream()
            self._write("[abort requested]", "warn")
            self._refresh_sidebar()

        def action_toggle_cooperation(self) -> None:
            """Ctrl+A: toggle ask-before-running (cooperation) mode.

            Affects future tool calls only; a pending approval prompt keeps
            waiting for its answer.
            """
            if self.session.approval_callback is None:
                self.session.approval_callback = self._approval_prompt
                self._write("[Cooperation] enabled: mutating tools (bash/write/edit) ask first", "info")
            else:
                self.session.approval_callback = None
                self._write("[Cooperation] disabled: all tools run freely", "info")
            self._refresh_sidebar()

        def action_toggle_subagents(self) -> None:
            enabled = getattr(getattr(self.session, "settings_manager", None), "get_subagents_enabled", lambda: True)()
            self.session.settings_manager.set_subagents_enabled(not enabled)
            self._write(f"Subagents {'enabled' if not enabled else 'disabled'}.", "info")
            self._refresh_sidebar()

        def action_clear_stream(self) -> None:
            stream_widget = self.query_one("#stream")
            stream_widget.update("")
            self._stream_lines = []
            self._assistant_has_live_delta = False
            self._assistant_live_start_idx = -1
            self._assistant_live_buffer = ""

        def action_help(self) -> None:
            self._write("/help /stats /state /status /tools /model /model-cycle /thinking /thinking-cycle /theme /queue /steer /follow /compact /tree /navigate /fork /new /login /logout /retry /config /extui /cooperation /subagents /bash-show /mcp /bash /abort /clear /exit", "info")

        def _show_extension_panel(self, req: dict[str, Any]) -> None:
            """Render an extension widget/overlay payload as a TUI component."""
            ui_type = str(req.get("uiType") or "widget")
            title = str(req.get("title") or "")
            payload = req.get("payload") or {}
            lines = [f"[b]{title or req.get('extension', 'extension')}[/b]"]
            lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
            rendered = "\n".join(lines)
            try:
                if ui_type == "overlay":
                    overlay = self.query_one("#ext_overlay", Static)
                    overlay.update(rendered)
                    overlay.add_class("visible")
                else:
                    panel = self.query_one("#ext_panel", Static)
                    panel.update(rendered)
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
                self.query_one("#input", TextArea).placeholder = "Wpisz polecenie lub /help"
            except Exception:
                pass

        async def on_session_event(self, message: SessionEvent) -> None:
            event = message.payload
            et = event.get("type")
            if et == "message_start":
                msg = event.get("message", {})
                if msg.get("role") == "assistant":
                    self._assistant_stream = ""
                    self._assistant_has_live_delta = False
                    self._assistant_live_start_idx = -1
                    self._assistant_live_buffer = ""
            elif et == "message_update":
                ae = event.get("assistantMessageEvent", {})
                if ae.get("type") == "text_delta":
                    delta = str(ae.get("delta", ""))
                    self._last_delta_ts = time.monotonic()
                    self._assistant_stream += delta
                    self._append_assistant_delta(delta)
            elif et == "message_end":
                msg = event.get("message", {})
                if msg.get("role") == "assistant":
                    text = self._assistant_stream.strip()
                    if not text:
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            text = "".join(x.get("text", "") for x in content if x.get("type") == "text").strip()
                        else:
                            text = str(content).strip()
                    final_text = text or "[empty response]"
                    if not self._assistant_has_live_delta:
                        self._write_chat_block("assistant", final_text)
                    else:
                        self._stream_lines.append("")
                        if len(self._stream_lines) > 500:
                            self._stream_lines = self._stream_lines[-500:]
                        self._render_stream()
                    self._assistant_stream = ""
                    self._assistant_has_live_delta = False
                    self._assistant_live_start_idx = -1
                    self._assistant_live_buffer = ""
            elif et == "tool_call_start":
                tool_name = str(event.get("tool") or "tool")
                args_text = json.dumps(event.get("args", {}), ensure_ascii=False)
                self._write_tool_block(f"tool start: {tool_name} {args_text}")
            elif et == "tool_approval_rejected":
                self._write(f"[Rejected] {event.get('tool')}: {event.get('reason', '')}", "warn")
            elif et == "ask_user":
                self._ask_user_pending = {"id": str(event.get("id") or "")}
                self._write_tool_block(f"agent pyta: {event.get('question')}")
                try:
                    input_widget = self.query_one("#input", TextArea)
                    input_widget.placeholder = "Odpowiedź dla agenta:"
                    input_widget.focus()
                except Exception:
                    pass
            elif et == "ask_user_answered":
                self._write(f"[AskUser] odpowiedź: {event.get('answer')}", "info")
            elif et == "extension_ui_request":
                ext = str(event.get("extension") or "unknown")
                ui_type = str(event.get("uiType") or "widget")
                title = str(event.get("title") or "")
                req_id = str(event.get("id") or "")
                heading = f"[ExtUI] {ext} ({ui_type})" + (f" - {title}" if title else "")
                self._write_tool_block(heading)
                self._write(json.dumps(event.get("payload") or {}, ensure_ascii=False, indent=2), "info")
                self._extension_ui_pending_request = dict(event)
                try:
                    input_widget = self.query_one("#input", TextArea)
                    input_widget.placeholder = "Odpowiedź dla rozszerzenia (JSON lub tekst; puste = anuluj)"
                    input_widget.focus()
                except Exception:
                    pass
                self._show_extension_panel(dict(event))
            elif et == "extension_ui_response":
                rid = str(event.get("requestId") or "")
                cancelled = bool(event.get("cancelled"))
                resp_payload = event.get("payload") or {}
                self._write(
                    f"[ExtUI] response {rid} cancelled={cancelled} "
                    + json.dumps(resp_payload, ensure_ascii=False),
                    "info",
                )
                if self._extension_ui_pending_request is not None and str(self._extension_ui_pending_request.get("id") or "") == rid:
                    self._clear_extension_ui_state()
            elif et == "tool_call_end":
                status = "ok" if event.get("ok") else "err"
                tool_name = str(event.get("tool") or "tool")
                self._write_tool_block(f"tool {status}: {tool_name}")
                if tool_name != "finish" and getattr(self.session.settings_manager, "get_bash_show_output", lambda: True)():
                    payload = event.get("result") or {}
                    if event.get("ok"):
                        out = str(payload.get("outputText") or payload.get("result") or "").rstrip()
                    else:
                        out = str(payload.get("error") or "").rstrip()
                    if out:
                        self._write_tool_block(out)
            elif et == "turn_end":
                self._retry_state = "idle"
                self._turn_active = False
                self._remove_thinking_line()
                self._render_stream()
                if event.get("ok") is False:
                    err = str(event.get("error") or "Unknown error").strip()
                    self._write(f"[error] {err}", "error")
            elif et == "auto_retry_end":
                self._retry_state = "idle"
                self._turn_active = False
                self._remove_thinking_line()
                self._render_stream()
            elif et == "auto_retry_start":
                self._retry_state = f"retry-{event.get('attempt')}"
                self._write(
                    f"[retry] attempt {event.get('attempt')}/{event.get('maxAttempts')} in {event.get('delayMs')}ms",
                    "warn",
                )

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
        if bool(self.options.get("cooperation")) or getattr(session.settings_manager, "get_tool_approval", lambda: False)():
            session.approval_callback = app._approval_prompt
        await app.run_async()
