# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from textual.widgets import Input, TextArea

from one.modes.tui_mode import _CommandCenter, _OneTextualApp
from tests.support.tui import _mk_app_session


async def search(picker, pilot, text):
    field = picker.query_one("#command-search", Input)
    field.value = text
    await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 40), (80, 24), (60, 20)])
async def test_center_groups_keyboard_and_preserves_draft(tmp_path: Path, size):
    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test(size=size) as pilot:
        draft = app.query_one("#input", TextArea)
        draft.text = "unfinished\nmessage"
        await pilot.press("ctrl+p")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, _CommandCenter)
        assert picker.query_one("#command-center").region.bottom <= size[1]
        await pilot.press("down", "pagedown", "pageup")
        await search(picker, pilot, "ctrl+k")
        assert [a.id for a in picker._visible] == ["model"]
        await pilot.press("escape")
        assert draft.text == "unfinished\nmessage"
        assert draft.has_focus


@pytest.mark.asyncio
async def test_center_theme_submenu_back_and_apply(tmp_path: Path):
    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        await pilot.pause()
        picker = app.screen
        await search(picker, pilot, "motyw")
        await pilot.press("enter")
        await pilot.pause()
        assert picker._page == "theme"
        assert any(a.title.startswith("●") for a in picker._actions)
        await pilot.press("escape")
        assert app.screen is picker and picker._page == ""
        await search(picker, pilot, "theme")
        await pilot.press("enter")
        await pilot.pause()
        await search(picker, pilot, "fallout")
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, _CommandCenter)
        assert app._theme.name == "fallout"
        assert session.settings_manager.get_theme() == "fallout"


@pytest.mark.asyncio
async def test_center_model_transition_and_blocked_action(tmp_path: Path):
    session = _mk_app_session(tmp_path, runtime_key="dummy")
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        await pilot.pause()
        await search(app.screen, pilot, "/model")
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "_ModelPicker"
        await pilot.press("escape")
        session._subagents_hard_disabled = True
        await pilot.press("ctrl+p")
        await pilot.pause()
        picker = app.screen
        await search(picker, pilot, "subagents")
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is picker
        assert "--no-subagents" in str(picker.query_one("#command-description").content)


@pytest.mark.asyncio
async def test_center_executes_once_and_preserves_draft(tmp_path: Path):
    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test() as pilot:
        draft = app.query_one("#input", TextArea)
        draft.text = "keep me"
        await pilot.press("ctrl+p")
        await pilot.pause()
        await search(app.screen, pilot, "/stats")
        with patch.object(app, "_handle_command") as command:
            await pilot.press("enter")
            await pilot.pause()
            command.assert_awaited_once_with("/stats")
        assert draft.text == "keep me"
