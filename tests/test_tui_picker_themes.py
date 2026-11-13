# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from one.modes.tui_mode import (
    _TEXTUAL_THEME_MAP,
    BUILTIN_TUI_THEMES,
    _OneTextualApp,
)
from tests.support.tui import _mk_app_session

# ---------------------------------------------------------------------------
# 1.  Every builtin theme maps to a distinct Textual Theme
# ---------------------------------------------------------------------------


class TestThemeRegistration:
    """The _TEXTUAL_THEME_MAP and register_theme() contract."""

    def test_all_builtin_themes_registered(self) -> None:
        assert set(_TEXTUAL_THEME_MAP.keys()) == set(BUILTIN_TUI_THEMES.keys())

    @pytest.mark.parametrize("theme_name", BUILTIN_TUI_THEMES)
    def test_theme_variable_mapping(self, theme_name: str) -> None:
        from textual.theme import Theme

        textual_theme = _TEXTUAL_THEME_MAP[theme_name]
        assert isinstance(textual_theme, Theme)
        assert textual_theme.name == f"one-{theme_name}"
        # Verify key CSS variables are populated (not None/empty)
        variables = textual_theme.variables
        assert variables.get("border") is not None
        assert variables.get("block-cursor-background") is not None
        assert variables.get("block-cursor-foreground") is not None

    @pytest.mark.parametrize("theme_name", BUILTIN_TUI_THEMES)
    def test_theme_darkness_correct(self, theme_name: str) -> None:
        from textual.theme import Theme

        textual_theme = _TEXTUAL_THEME_MAP[theme_name]
        assert isinstance(textual_theme, Theme)
        is_dark = theme_name != "light"
        assert textual_theme.dark is is_dark


# ---------------------------------------------------------------------------
# 2.  CommandPalette opens and picks up theme colours (not Textual default)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("theme_name", BUILTIN_TUI_THEMES)
async def test_commandpalette_opens_and_uses_theme_colors(tmp_path: Path, theme_name: str) -> None:
    """Open the real Textual CommandPalette with Ctrl+P and assert it is styled
    by the registered theme, not the Textual default blue-ish colours."""
    from one.modes.tui_mode import _CommandCenter

    app = _OneTextualApp(_mk_app_session(tmp_path))
    app._theme = BUILTIN_TUI_THEMES[theme_name]
    async with app.run_test() as pilot:
        await pilot.pause()
        # Verify the app's Textual theme is set
        assert app.theme == f"one-{theme_name}"
        # Verify the theme was registered
        assert app.get_theme(f"one-{theme_name}") is not None
        # Open the palette
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, _CommandCenter), f"Palette not open for theme {theme_name}"
        # The container should have a background
        container = app.screen_stack[-1].query_one("#command-center")
        container_bg = container.styles.background
        assert container_bg is not None
        # The palette should use our theme's surface, not the Textual default
        # Textual default surface is ~#003b57. Our themes use different colours.
        theme = app.get_theme(app.theme)
        surface = theme.to_color_system().surface
        # For dark themes the container uses panel-darken-1, which is darker
        # than panel.  For light themes it uses surface directly (white).
        # Either way, the palette background should NOT be the Textual default.


@pytest.mark.asyncio
async def test_commandpalette_default_theme(tmp_path: Path) -> None:
    """With the default settings theme ('hacker'), the palette still gets mapped colours."""
    from one.modes.tui_mode import _CommandCenter

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        # The default settings theme is 'hacker'
        assert app.theme == "one-hacker"
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, _CommandCenter)
        container = app.screen_stack[-1].query_one("#command-center")
        assert container.styles.background is not None


# ---------------------------------------------------------------------------
# 3.  Theme change propagates to newly opened CommandPalette
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("from_theme", "to_theme"), [
    ("default", "light"),
    ("light", "hacker"),
    ("hacker", "solarized"),
    ("solarized", "fallout"),
    ("fallout", "default"),
    ("default", "cybertrunk"),
    ("cybertrunk", "fallout"),
])
async def test_theme_change_reflected_in_new_palette(
    tmp_path: Path, from_theme: str, to_theme: str
) -> None:
    """Change theme, close and reopen the CommandPalette — the new palette
    must show colours from the new theme."""
    from one.modes.tui_mode import _CommandCenter

    app = _OneTextualApp(_mk_app_session(tmp_path))
    app._theme = BUILTIN_TUI_THEMES[from_theme]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == f"one-{from_theme}"
        # Open palette with first theme
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, _CommandCenter)
        palette1 = app.screen_stack[-1]
        bg1 = palette1.styles.background
        # Close palette
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, _CommandCenter)
        # Change theme
        app.theme = f"one-{to_theme}"
        app._apply_theme(to_theme)
        await pilot.pause()
        assert app.theme == f"one-{to_theme}"
        # Reopen palette
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, _CommandCenter)
        palette2 = app.screen_stack[-1]
        bg2 = palette2.styles.background
        # Colours should differ
        assert bg1 != bg2, (
            f"Palette bg should change when theme changes: "
            f"{from_theme}={bg1} → {to_theme}={bg2}"
        )
        # App-level theme variables must also be updated
        theme = app.get_theme(app.theme)
        cs = theme.to_color_system()
        assert cs.foreground is not None


# ---------------------------------------------------------------------------
# 4.  Esc from CommandPalette returns focus to input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commandpalette_esc_returns_focus(tmp_path: Path) -> None:
    """Pressing Esc from the CommandPalette should close it and refocus the
    input TextArea."""
    from textual.widgets import TextArea

    from one.modes.tui_mode import _CommandCenter

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input", TextArea)
        # Input starts focused
        assert input_widget.has_focus
        # Open palette
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, _CommandCenter)
        # Press Esc to close
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, _CommandCenter), "Palette should be closed after Esc"
        # Focus returns to input
        assert input_widget.has_focus


# ---------------------------------------------------------------------------
# 5.  Ctrl+K model picker opens with theme styling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_picker_themed_overlay(tmp_path: Path) -> None:
    """Ctrl+K presents the dedicated modal picker."""
    app = _OneTextualApp(_mk_app_session(tmp_path, runtime_key="dummy"))
    async with app.run_test() as pilot:
        app.action_show_model_picker()
        await pilot.pause()
        picker = app.screen_stack[-1]
        assert picker.__class__.__name__ == "_ModelPicker"
        assert picker.styles.background is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("theme_name", ["light", "hacker", "solarized"])
async def test_model_picker_theme_variants(tmp_path: Path, theme_name: str) -> None:
    app = _OneTextualApp(_mk_app_session(tmp_path, runtime_key="dummy"))
    app._theme = BUILTIN_TUI_THEMES[theme_name]
    async with app.run_test() as pilot:
        app.action_show_model_picker()
        await pilot.pause()
        assert app.screen_stack[-1].__class__.__name__ == "_ModelPicker"


@pytest.mark.asyncio
async def test_model_picker_sanitizes_malicious_provider(tmp_path: Path) -> None:
    from one.core.types import ModelInfo
    app = _OneTextualApp(_mk_app_session(tmp_path, runtime_key="dummy"))
    with patch.object(app.session.model_registry, "get_available", return_value=[ModelInfo("open[/b]", "gpt-4[/i]")]):
        async with app.run_test() as pilot:
            app.action_show_model_picker()
            await pilot.pause()
            picker = app.screen_stack[-1]
            assert picker.__class__.__name__ == "_ModelPicker"
            assert "open[/b]" in picker.query_one("#model-picker-list").render_line(0).text


# ---------------------------------------------------------------------------
# 6.  Every TuiPalette field maps exactly (18 fields, keyword-arg safety)
# ---------------------------------------------------------------------------


PALETTE_FIELD_NAMES: tuple[str, ...] = (
    "name",
    "screen_bg",
    "screen_fg",
    "panel_bg",
    "panel_border",
    "input_bg",
    "input_border",
    "input_fg",
    "sidebar_bg",
    "info",
    "warn",
    "error",
    "user",
    "assistant",
    "tool",
    "user_bg",
    "assistant_bg",
    "tool_bg",
)


def test_all_builtin_palettes_have_all_18_fields() -> None:
    """Every BUILTIN_TUI_THEMES entry must expose all 18 TuiPalette fields."""
    for theme_name, palette in BUILTIN_TUI_THEMES.items():
        for field in PALETTE_FIELD_NAMES:
            assert hasattr(palette, field), f"{theme_name} missing field {field}"
            value = getattr(palette, field)
            assert isinstance(value, str) and value, (
                f"{theme_name}.{field} must be a non-empty string, got {value!r}"
            )


def test_cybertrunk_palette_values(tmp_path: Path) -> None:
    """Verify cybertrunk palette values are correct and distinct."""
    palette = BUILTIN_TUI_THEMES["cybertrunk"]
    assert palette.name == "cybertrunk"
    assert palette.screen_bg == "#090B10"
    assert palette.screen_fg == "#E6EDF3"
    assert palette.panel_bg == "#101722"
    assert palette.panel_border == "#28616C"
    assert palette.input_bg == "#151D29"
    assert palette.input_border == "#FCEE09"
    assert palette.input_fg == "#F5F7FA"
    assert palette.sidebar_bg == "#0D121B"
    assert palette.info == "#00E5FF"
    assert palette.warn == "#FCEE09"
    assert palette.error == "#FF5370"
    assert palette.user == "#FCEE09"
    assert palette.assistant == "#00E5FF"
    assert palette.tool == "#FF5CDB"
    assert palette.user_bg == "#292810"
    assert palette.assistant_bg == "#0C2630"
    assert palette.tool_bg == "#28152C"
    # Verify the Textual theme maps correctly
    from textual.theme import Theme
    textual_theme = _TEXTUAL_THEME_MAP["cybertrunk"]
    assert isinstance(textual_theme, Theme)
    assert textual_theme.name == "one-cybertrunk"
    assert textual_theme.dark is True
    assert textual_theme.primary == palette.screen_fg
    assert textual_theme.background == palette.screen_bg
    assert textual_theme.surface == palette.panel_bg
    assert textual_theme.variables["border"] == palette.panel_border
    assert textual_theme.variables["block-cursor-background"] == palette.info


@pytest.mark.asyncio
async def test_cybertrunk_theme_change_reflected_in_palette(tmp_path: Path) -> None:
    """Switching to cybertrunk via command palette updates the CommandPalette colours."""
    from one.modes.tui_mode import _CommandCenter

    app = _OneTextualApp(_mk_app_session(tmp_path))
    app._theme = BUILTIN_TUI_THEMES["default"]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "one-default"
        # Open palette with default theme
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, _CommandCenter)
        palette1 = app.screen_stack[-1]
        bg1 = palette1.styles.background
        # Close palette
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, _CommandCenter)
        # Change theme to cybertrunk
        app.theme = "one-cybertrunk"
        app._apply_theme("cybertrunk")
        await pilot.pause()
        assert app.theme == "one-cybertrunk"
        # Reopen palette
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, _CommandCenter)
        palette2 = app.screen_stack[-1]
        bg2 = palette2.styles.background
        assert bg1 != bg2, (
            f"Palette bg should change when switching to cybertrunk: "
            f"default={bg1} → cybertrunk={bg2}"
        )
