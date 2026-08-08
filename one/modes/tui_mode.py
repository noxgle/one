from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
import textwrap
from typing import Any

from rich.markup import escape as rich_escape

try:
    from textual import events
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.message import Message
    from textual.widgets import Input, Static

    TEXTUAL_AVAILABLE = True
except Exception:  # pragma: no cover
    TEXTUAL_AVAILABLE = False


def build_sidebar_snapshot(
    session: Any,
    retry_state: str,
    last_tool_error: str,
    last_provider_error: str,
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
        "lastToolError": last_tool_error,
        "lastProviderError": last_provider_error,
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


if TEXTUAL_AVAILABLE:

    class SessionEvent(Message):
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload
            super().__init__()

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
        }

        #sidebar {
            width: 42;
            border: round #2f466e;
            background: #101a30;
            padding: 0 1;
        }
        """

        BINDINGS = [
            ("ctrl+c", "abort", "Abort"),
            ("ctrl+l", "clear_stream", "Clear"),
            ("ctrl+q", "quit", "Quit"),
            ("ctrl+a", "toggle_cooperation", "Toggle approval"),
            ("f1", "help", "Help"),
        ]

        def __init__(self, session: Any, options: dict[str, Any] | None = None) -> None:
            super().__init__()
            self.session = session
            self.options = options or {}
            self._theme = resolve_tui_theme(self.options.get("theme") or getattr(session.settings_manager, "get_theme", lambda: "default")())
            self._off_listener = None
            self._assistant_stream = ""
            self._stream_lines: list[str] = []
            self._retry_state = "idle"
            self._last_tool_error = "-"
            self._last_provider_error = "-"
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

        def compose(self) -> ComposeResult:
            with Horizontal(id="root"):
                with Vertical(id="main"):
                    with VerticalScroll(id="stream_container"):
                        yield Static("", id="stream")
                    yield Input(placeholder="Wpisz polecenie lub /help", id="input")
                yield Static(id="sidebar")

        def on_mount(self) -> None:
            self.query_one("#input", Input).focus()
            self._apply_theme(self._theme.name)
            # Drive the "waiting for the model" spinner while a turn is in
            # flight but no assistant text is currently streaming.
            self.set_interval(0.15, self._tick_waiting)

            def _listener(event: dict[str, Any]) -> None:
                try:
                    self.post_message(SessionEvent(event))
                except Exception:
                    self.call_from_thread(self.post_message, SessionEvent(event))

            self._off_listener = self.session.subscribe(_listener)
            self._write("one TUI v2 ready. /help", "info")
            self._refresh_sidebar()

        def on_unmount(self) -> None:
            if callable(self._off_listener):
                try:
                    self._off_listener()
                except Exception:
                    pass

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
            """Drop the animated 'waiting' line from the stream, if present."""
            for i in range(len(self._stream_lines) - 1, -1, -1):
                if self._stream_lines[i].startswith(_THINKING_MARK):
                    self._stream_lines.pop(i)
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
            self._stream_lines.append(text)
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
            content = (text or "").strip() or "[empty]"
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
                input_widget = self.query_one("#input", Input)
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
                last_tool_error=self._last_tool_error,
                last_provider_error=self._last_provider_error,
            )
            sidebar = (
                f"[b {self._theme.info}]Info[/]\n"
                f"Model: {s['model']}\n"
                f"Theme: {self._theme.name}\n"
                f"Thinking: {s['thinking']}\n"
                f"Ctx: {s['contextPercent']:.1f}%\n"
                f"Retry: {s['retry']}\n"
                f"CWD: {s['cwd']}\n"
                "\n"
                f"[b {self._theme.info}]Queue[/]\n"
                f"Steer: {s['queueSteer']}\n"
                f"Follow: {s['queueFollow']}\n"
                f"Total: {s['queueTotal']}\n"
                "\n"
                f"[b {self._theme.info}]Tokens[/]\n"
                f"In: {s['tokenInput']}\n"
                f"Out: {s['tokenOutput']}\n"
                f"Cache: r{s['tokenCacheRead']}/w{s['tokenCacheWrite']}\n"
                f"Total: {s['tokenTotal']}\n"
                f"Cost: {s['cost']:.6f}\n"
                "\n"
                f"[b {self._theme.info}]Errors[/]\n"
                f"Tool: {s['lastToolError']}\n"
                f"Provider: {s['lastProviderError']}\n"
                "\n"
                f"[b {self._theme.info}]Keys[/]\n"
                "Ctrl+C abort\nCtrl+L clear\nCtrl+Q quit\nF1 help\n"
                "Pretty chat view: Static blocks with theme backgrounds\n"
            )
            self.query_one("#sidebar", Static).update(sidebar)

        def _try_auto_copy_selected_stream_text(self) -> None:
            try:
                stream_widget = self.query_one("#stream")
            except Exception:
                return
            try:
                text = str(getattr(stream_widget, "selected_text", "") or "").strip()
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
            # Static chat view may not expose text selection APIs in all terminals.
            self.call_after_refresh(self._try_auto_copy_selected_stream_text)

        async def _handle_command(self, cmd: str) -> None:
            session = self.session
            if cmd in {"/exit", "/quit"}:
                self.exit()
                return
            if cmd == "/help":
                self._write(
                    "Commands: /help /status /model [provider/model] /thinking [level] /queue [clear] /theme [name] /abort /clear /exit",
                    "info",
                )
                return
            if cmd == "/clear":
                stream_widget = self.query_one("#stream")
                stream_widget.update("")
                self._stream_lines = []
            self._assistant_has_live_delta = False
            self._assistant_live_start_idx = -1
            self._assistant_live_buffer = ""
            if cmd == "/abort":
                await session.abort()
                self._write("[abort requested]", "warn")
                return
            if cmd == "/status":
                s = build_sidebar_snapshot(session, self._retry_state, self._last_tool_error, self._last_provider_error)
                self._write(json.dumps(s, ensure_ascii=False), "info")
                return
            if cmd == "/model":
                current = f"{session.model.provider}/{session.model.id}" if session.model else "none"
                self._write(f"Current model: {current}", "info")
                self._write("Usage: /model <provider>/<model-id>", "info")
                return
            if cmd.startswith("/model "):
                val = cmd[len("/model ") :].strip()
                if "/" not in val:
                    self._write("Usage: /model <provider>/<model-id>", "error")
                    return
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

            self._write(f"Unknown command: {cmd}. Use /help.", "error")

        async def on_input_submitted(self, event: Input.Submitted) -> None:
            text = event.value.strip()
            self.query_one("#input", Input).value = ""
            if self._approval_pending is not None:
                await self._handle_approval_answer(text)
                return
            if not text:
                return
            if text.startswith("/"):
                await self._handle_command(text)
                return

            self._write_chat_block("user", text)
            self._turn_active = True
            self._last_delta_ts = 0.0

            async def _run_prompt() -> None:
                try:
                    await self.session.prompt(text)
                except Exception as e:
                    self._last_provider_error = str(e).strip() or e.__class__.__name__
                    self._write(f"[error] {self._last_provider_error}", "error")
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
                    self.query_one("#input", Input).placeholder = "Powód odrzucenia:"
                except Exception:
                    pass
                self._write("[Approve] podaj powód odrzucenia", "warn")
                return
            # Any other non-empty text is treated as the rejection reason.
            queue.put_nowait(("no", text))

        async def _approval_prompt(self, tool_name: str, args: dict[str, Any]) -> tuple[bool, str]:
            """Cooperation mode callback: ask the user via the input widget."""
            self._approval_pending = {"tool": tool_name, "args": args, "stage": "answer"}
            self._approval_queue = asyncio.Queue()
            self._write(f"[Approve] {tool_name} {json.dumps(args, ensure_ascii=False)}", "warn")
            try:
                input_widget = self.query_one("#input", Input)
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
                    self.query_one("#input", Input).placeholder = "Wpisz polecenie lub /help"
                except Exception:
                    pass
                self._refresh_sidebar()
            if verdict == "yes":
                return True, ""
            return False, reason

        async def action_abort(self) -> None:
            await self.session.abort()
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

        def action_clear_stream(self) -> None:
            stream_widget = self.query_one("#stream")
            stream_widget.update("")
            self._stream_lines = []
            self._assistant_has_live_delta = False
            self._assistant_live_start_idx = -1
            self._assistant_live_buffer = ""

        def action_help(self) -> None:
            self._write("/help /status /model /thinking /queue /theme /abort /clear /exit", "info")

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
            elif et == "tool_call_end":
                status = "ok" if event.get("ok") else "err"
                if not event.get("ok"):
                    payload = event.get("result") or {}
                    tool_name = str(event.get("tool") or payload.get("tool") or "unknown")
                    err = str(payload.get("error") or "unknown tool error").strip()
                    self._last_tool_error = f"{tool_name}: {err}"
                tool_name = str(event.get("tool") or "tool")
                self._write_tool_block(f"tool {status}: {tool_name}")
            elif et == "turn_end" and event.get("ok") is False:
                err = str(event.get("error") or "Unknown error").strip()
                self._last_provider_error = err
                self._write(f"[error] {err}", "error")
            elif et == "auto_retry_start":
                self._retry_state = f"retry-{event.get('attempt')}"
                retry_err = str(event.get("errorMessage") or "").strip()
                if retry_err:
                    self._last_provider_error = retry_err
                self._write(
                    f"[retry] attempt {event.get('attempt')}/{event.get('maxAttempts')} in {event.get('delayMs')}ms",
                    "warn",
                )
            elif et in {"auto_retry_end", "turn_end"}:
                self._retry_state = "idle"
                self._turn_active = False
                self._remove_thinking_line()
                self._render_stream()

            self._refresh_sidebar()


class TuiMode:
    def __init__(self, runtime_host: Any, options: dict[str, Any] | None = None) -> None:
        self.runtime_host = runtime_host
        self.options = options or {}

    async def run(self) -> None:
        if not TEXTUAL_AVAILABLE:
            raise RuntimeError("TUI mode requires 'textual'. Install dependencies: pip install -e .")
        session = self.runtime_host.session
        app = _OneTextualApp(session, self.options)
        if bool(self.options.get("cooperation")) or getattr(session.settings_manager, "get_tool_approval", lambda: False)():
            session.approval_callback = app._approval_prompt
        await app.run_async()
