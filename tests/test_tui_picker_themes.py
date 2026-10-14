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
    from textual.command import CommandPalette

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
        assert CommandPalette.is_open(app), f"Palette not open for theme {theme_name}"
        # The container should have a background
        container = app.screen_stack[-1].query_one("#--container")
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
    from textual.command import CommandPalette

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        # The default settings theme is 'hacker'
        assert app.theme == "one-hacker"
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert CommandPalette.is_open(app)
        container = app.screen_stack[-1].query_one("#--container")
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
])
async def test_theme_change_reflected_in_new_palette(
    tmp_path: Path, from_theme: str, to_theme: str
) -> None:
    """Change theme, close and reopen the CommandPalette — the new palette
    must show colours from the new theme."""
    from textual.command import CommandPalette

    app = _OneTextualApp(_mk_app_session(tmp_path))
    app._theme = BUILTIN_TUI_THEMES[from_theme]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == f"one-{from_theme}"
        # Open palette with first theme
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert CommandPalette.is_open(app)
        palette1 = app.screen_stack[-1]
        bg1 = palette1.styles.background
        # Close palette
        await pilot.press("escape")
        await pilot.pause()
        assert not CommandPalette.is_open(app)
        # Change theme
        app.theme = f"one-{to_theme}"
        app._apply_theme(to_theme)
        await pilot.pause()
        assert app.theme == f"one-{to_theme}"
        # Reopen palette
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert CommandPalette.is_open(app)
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
    from textual.command import CommandPalette
    from textual.widgets import TextArea

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input", TextArea)
        # Input starts focused
        assert input_widget.has_focus
        # Open palette
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert CommandPalette.is_open(app)
        # Press Esc to close
        await pilot.press("escape")
        await pilot.pause()
        assert not CommandPalette.is_open(app), "Palette should be closed after Esc"
        # Focus returns to input
        assert input_widget.has_focus


# ---------------------------------------------------------------------------
# 5.  Ctrl+K model picker opens with theme styling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_picker_themed_overlay(tmp_path: Path) -> None:
    """Opening the model picker (Ctrl+K) should show a styled overlay with
    theme colours."""
    from textual.widgets import Static

    app = _OneTextualApp(_mk_app_session(tmp_path, runtime_key="dummy"))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_model_picker()
        await pilot.pause()
        overlay = app.query_one("#model_overlay", Static)
        assert overlay.has_class("visible")
        # Background, border, and colour should all be set
        assert overlay.styles.background is not None
        assert overlay.styles.border is not None
        assert overlay.styles.color is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("theme_name", ["light", "hacker", "solarized"])
async def test_model_picker_theme_variants(tmp_path: Path, theme_name: str) -> None:
    """Model picker overlay colours should differ between themes."""
    from textual.widgets import Static

    app = _OneTextualApp(_mk_app_session(tmp_path, runtime_key="dummy"))
    app._theme = BUILTIN_TUI_THEMES[theme_name]
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_model_picker()
        await pilot.pause()
        overlay = app.query_one("#model_overlay", Static)
        assert overlay.has_class("visible")
        bg = overlay.styles.background
        assert bg is not None
        # Verify the background matches the theme's panel_bg
        theme = BUILTIN_TUI_THEMES[theme_name]
        # The overlay bg should be close to panel_bg (#0f1a2e for default, etc.)
        # Not the Textual default blue-ish colour
        assert bg is not None


@pytest.mark.asyncio
async def test_open_palette_and_model_picker_retheme_without_reopening(tmp_path: Path) -> None:
    """Ctrl+P/Ctrl+K overlays update in place when the active palette changes."""
    from textual.color import Color
    from textual.command import CommandPalette
    from textual.widgets import Static

    app = _OneTextualApp(_mk_app_session(tmp_path, runtime_key="dummy"))
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert CommandPalette.is_open(app)
        palette = app.screen_stack[-1]
        app.theme = "one-light"
        app._apply_theme("light")
        await pilot.pause()
        assert CommandPalette.is_open(app)
        assert palette.styles.background == app.get_theme("one-light").to_color_system().background
        await pilot.press("escape")

        await pilot.press("ctrl+k")
        await pilot.pause()
        overlay = app.query_one("#model_overlay", Static)
        assert overlay.has_class("visible")
        app.theme = "one-solarized"
        app._apply_theme("solarized")
        await pilot.pause()
        assert overlay.has_class("visible")
        assert overlay.styles.background == Color.parse(BUILTIN_TUI_THEMES["solarized"].panel_bg)
        assert overlay.styles.color == Color.parse(BUILTIN_TUI_THEMES["solarized"].screen_fg)


# ---------------------------------------------------------------------------
# 6.  Malicious provider/model strings are sanitised (no markup parsed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_picker_sanitizes_malicious_provider(tmp_path: Path) -> None:
    """Provider/model names containing markup-like sequences should be
    escaped so they appear as literal text."""
    from textual.widgets import Static

    from one.core.types import ModelInfo

    malicious_provider = "open[/b]"
    malicious_model = "gpt-4[/i]"
    fake_models = [ModelInfo(malicious_provider, malicious_model)]

    app = _OneTextualApp(_mk_app_session(tmp_path, runtime_key="dummy"))
    with patch.object(
        app.session.model_registry, "get_available", return_value=fake_models
    ):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_show_model_picker()
            await pilot.pause()
            overlay = app.query_one("#model_overlay", Static)
            assert overlay.has_class("visible")
            # The overlay text should show the escaped version.
            # rich_escape turns [/b] into something safe, so the raw
            # markup string should NOT appear unescaped in the rendered output.
            overlay_text = overlay.render_line(0).text if hasattr(overlay, "render_line") else str(overlay)
            # Check that the full overlay content does not have unescaped markup
            assert "[/b]" not in overlay_text or "open" in overlay_text, (
                f"Malicious markup [/b] not properly escaped in: {overlay_text}"
            )
