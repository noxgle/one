from __future__ import annotations

import json
from pathlib import Path

from one.cli.args import parse_args
from one.modes.tui_mode import BUILTIN_TUI_THEMES, discover_custom_tui_themes, resolve_tui_theme


class _Loader:
    def __init__(self, theme_paths: list[str]) -> None:
        self._theme_paths = theme_paths

    def get_themes(self) -> dict:
        return {"themes": [{"path": p} for p in self._theme_paths], "diagnostics": []}


def test_parse_args_accepts_tui_mode() -> None:
    parsed = parse_args(["--mode", "tui"])
    assert parsed.errors == []
    assert parsed.mode == "tui"


def test_resolve_builtin_tui_theme() -> None:
    theme = resolve_tui_theme("solarized")
    assert theme.name == "solarized"
    assert theme == BUILTIN_TUI_THEMES["solarized"]


def test_discover_custom_tui_theme_from_json(tmp_path: Path) -> None:
    p = tmp_path / "my_theme.json"
    p.write_text(
        json.dumps(
            {
                "name": "my-theme",
                "styles": {
                    "header": "bold white on blue",
                    "status": "white on black",
                    "body": "white on black",
                    "footer": "black on white",
                    "accent": "cyan",
                    "info": "green",
                    "warning": "yellow",
                    "error": "red",
                },
            }
        ),
        encoding="utf-8",
    )
    custom = discover_custom_tui_themes(_Loader([str(p)]))
    assert "my-theme" in custom
    resolved = resolve_tui_theme("my-theme", custom)
    assert resolved.name == "my-theme"
    assert resolved.accent == "cyan"
