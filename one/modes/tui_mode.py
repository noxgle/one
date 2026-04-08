from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console, Group
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text


@dataclass(frozen=True)
class TuiTheme:
    name: str
    header: str
    status: str
    body: str
    footer: str
    accent: str
    info: str
    warning: str
    error: str


BUILTIN_TUI_THEMES: dict[str, TuiTheme] = {
    "default": TuiTheme(
        name="default",
        header="bold white on blue",
        status="white on rgb(45,45,45)",
        body="white on black",
        footer="black on rgb(200,200,200)",
        accent="cyan",
        info="bright_cyan",
        warning="yellow",
        error="bright_red",
    ),
    "light": TuiTheme(
        name="light",
        header="bold black on rgb(210,230,255)",
        status="black on rgb(240,240,240)",
        body="black on white",
        footer="black on rgb(230,230,230)",
        accent="blue",
        info="blue",
        warning="dark_orange",
        error="red",
    ),
    "hacker": TuiTheme(
        name="hacker",
        header="bold black on green",
        status="green on rgb(10,10,10)",
        body="green on black",
        footer="black on green",
        accent="bright_green",
        info="green",
        warning="yellow",
        error="red",
    ),
    "solarized": TuiTheme(
        name="solarized",
        header="bold rgb(38,139,210) on rgb(0,43,54)",
        status="rgb(131,148,150) on rgb(7,54,66)",
        body="rgb(131,148,150) on rgb(0,43,54)",
        footer="rgb(101,123,131) on rgb(7,54,66)",
        accent="rgb(42,161,152)",
        info="rgb(38,139,210)",
        warning="rgb(181,137,0)",
        error="rgb(220,50,47)",
    ),
}


def _as_theme(name: str, payload: dict[str, Any]) -> TuiTheme | None:
    required = {"header", "status", "body", "footer", "accent", "info", "warning", "error"}
    if not required.issubset(set(payload.keys())):
        return None
    return TuiTheme(
        name=name,
        header=str(payload["header"]),
        status=str(payload["status"]),
        body=str(payload["body"]),
        footer=str(payload["footer"]),
        accent=str(payload["accent"]),
        info=str(payload["info"]),
        warning=str(payload["warning"]),
        error=str(payload["error"]),
    )


def discover_custom_tui_themes(resource_loader: Any) -> dict[str, TuiTheme]:
    out: dict[str, TuiTheme] = {}
    themes = resource_loader.get_themes().get("themes", [])
    for entry in themes:
        path = entry.get("path")
        if not path:
            continue
        p = Path(path)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue

        if isinstance(raw, dict) and "styles" in raw and isinstance(raw["styles"], dict):
            theme_name = str(raw.get("name") or p.stem).strip().lower()
            maybe = _as_theme(theme_name, raw["styles"])
            if maybe:
                out[theme_name] = maybe
            continue

        if isinstance(raw, dict):
            theme_name = str(raw.get("name") or p.stem).strip().lower()
            maybe = _as_theme(theme_name, raw)
            if maybe:
                out[theme_name] = maybe
    return out


def resolve_tui_theme(theme_name: str | None, custom_themes: dict[str, TuiTheme] | None = None) -> TuiTheme:
    custom = custom_themes or {}
    if theme_name:
        key = theme_name.strip().lower()
        if key in custom:
            return custom[key]
        if key in BUILTIN_TUI_THEMES:
            return BUILTIN_TUI_THEMES[key]
    return BUILTIN_TUI_THEMES["default"]


class TuiMode:
    def __init__(self, runtime_host: Any, options: dict[str, Any] | None = None) -> None:
        self.runtime_host = runtime_host
        self.options = options or {}
        self.console = Console()
        self._lines: list[Text] = []
        self._max_lines = 240
        self._assistant_stream = ""
        self._theme_name = str(self.options.get("theme") or "default")
        self._custom_themes: dict[str, TuiTheme] = {}
        self._theme = BUILTIN_TUI_THEMES["default"]
        self._retry_state = "idle"

    def _append_line(self, text: str, style: str | None = None) -> None:
        line = Text(text, style=style) if style else Text(text)
        self._lines.append(line)
        if len(self._lines) > self._max_lines:
            self._lines = self._lines[-self._max_lines :]

    def _switch_theme(self, name: str) -> bool:
        chosen = resolve_tui_theme(name, self._custom_themes)
        requested = (name or "").strip().lower()
        if requested and requested not in self._custom_themes and requested not in BUILTIN_TUI_THEMES:
            return False
        self._theme_name = chosen.name
        self._theme = chosen
        return True

    def _status_line(self, session: Any) -> str:
        model = f"{session.model.provider}/{session.model.id}" if session.model else "no-model"
        usage = session.get_context_usage()
        ctx = f"{usage['percent']:.1f}%" if usage else "n/a"
        q = session.get_pending_queues()
        return (
            f"model={model}  thinking={session.thinking_level}  ctx={ctx}  "
            f"retry={self._retry_state}  queue=s{len(q['steering'])}/f{len(q['followUp'])}  "
            f"cwd={session.session_manager.cwd}"
        )

    def _build_layout(self, session: Any) -> Layout:
        layout = Layout(name="root")
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="status", size=3),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=3),
        )
        header = Panel(
            Text(f"one TUI v1  |  theme={self._theme.name}", style=self._theme.accent),
            border_style=self._theme.accent,
            style=self._theme.header,
        )
        status = Panel(
            Text(self._status_line(session), style=self._theme.info),
            border_style=self._theme.accent,
            style=self._theme.status,
        )
        body = Panel(
            Group(*self._lines[-80:]) if self._lines else Text("No events yet.", style=self._theme.info),
            border_style=self._theme.accent,
            style=self._theme.body,
            title="Event Stream",
        )
        footer = Panel(
            Text(
                "/help /theme [name] /clear /abort /exit",
                style=self._theme.accent,
            ),
            border_style=self._theme.accent,
            style=self._theme.footer,
        )
        layout["header"].update(header)
        layout["status"].update(status)
        layout["body"].update(body)
        layout["footer"].update(footer)
        return layout

    async def run(self) -> None:
        session = self.runtime_host.session
        self._custom_themes = discover_custom_tui_themes(session.resource_loader)
        preferred = self.options.get("theme") or session.settings_manager.get_theme() or "default"
        self._theme = resolve_tui_theme(preferred, self._custom_themes)
        self._theme_name = self._theme.name

        def on_event(event: dict[str, Any]) -> None:
            et = event.get("type")
            if et == "message_start":
                msg = event.get("message", {})
                if msg.get("role") == "assistant":
                    self._assistant_stream = ""
            elif et == "message_update":
                ae = event.get("assistantMessageEvent", {})
                if ae.get("type") == "text_delta":
                    self._assistant_stream += str(ae.get("delta", ""))
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
                    self._append_line(f"assistant> {text or '[empty response]'}", self._theme.info)
                    self._assistant_stream = ""
            elif et == "tool_call_start":
                self._append_line(f"[tool] {event.get('tool')} {json.dumps(event.get('args', {}), ensure_ascii=False)}", self._theme.warning)
            elif et == "tool_call_end":
                status = "ok" if event.get("ok") else "err"
                style = self._theme.info if event.get("ok") else self._theme.error
                self._append_line(f"[tool:{status}] {event.get('tool')}", style)
            elif et == "turn_end" and event.get("ok") is False:
                self._append_line(f"[error] {event.get('error') or 'Unknown error'}", self._theme.error)
            elif et == "auto_retry_start":
                self._retry_state = f"retry-{event.get('attempt')}"
                self._append_line(
                    f"[retry] attempt {event.get('attempt')}/{event.get('maxAttempts')} in {event.get('delayMs')}ms",
                    self._theme.warning,
                )
            elif et in {"auto_retry_end", "turn_end"}:
                self._retry_state = "idle"

        session.subscribe(on_event)
        self._append_line("TUI mode ready. Type /help for commands.", self._theme.info)

        while True:
            self.console.print(self._build_layout(session))
            try:
                line = await asyncio.to_thread(self.console.input, f"[{self._theme.accent}]> [/{self._theme.accent}]")
            except EOFError:
                break
            except KeyboardInterrupt:
                if session.is_streaming:
                    await session.abort()
                    self._append_line("[abort requested]", self._theme.warning)
                    continue
                break

            if not line.strip():
                continue
            cmd = line.strip()
            if cmd in {"/exit", "/quit"}:
                break
            if cmd == "/help":
                self._append_line("Commands: /help /theme [name] /clear /abort /exit", self._theme.info)
                self._append_line(
                    "Built-in themes: " + ", ".join(sorted(BUILTIN_TUI_THEMES.keys())),
                    self._theme.info,
                )
                if self._custom_themes:
                    self._append_line(
                        "Custom themes: " + ", ".join(sorted(self._custom_themes.keys())),
                        self._theme.info,
                    )
                continue
            if cmd == "/clear":
                self._lines = []
                continue
            if cmd == "/abort":
                await session.abort()
                self._append_line("[abort requested]", self._theme.warning)
                continue
            if cmd == "/theme":
                self._append_line(f"Current theme: {self._theme.name}", self._theme.info)
                self._append_line("Available: " + ", ".join(sorted(set(BUILTIN_TUI_THEMES) | set(self._custom_themes))), self._theme.info)
                continue
            if cmd.startswith("/theme "):
                requested = cmd[len("/theme ") :].strip()
                if self._switch_theme(requested):
                    try:
                        session.settings_manager.set_theme(self._theme.name)
                    except Exception:
                        pass
                    self._append_line(f"Theme switched to {self._theme.name}", self._theme.info)
                else:
                    self._append_line(f"Unknown theme: {requested}", self._theme.error)
                continue

            self._append_line(f"user> {line}", self._theme.accent)
            await session.prompt(line)
