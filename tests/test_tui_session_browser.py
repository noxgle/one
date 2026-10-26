# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from textual.widgets import Input, TextArea

from one.core.session_manager import SessionInfo
from one.modes.tui_mode import _OneTextualApp, _SessionBrowser
from tests.support.tui import _mk_app_session


def infos():
    return [SessionInfo(f"/scratch/{i}.jsonl", f"exact-id-{i}", "/scratch", "Same title" if i < 2 else f"Task {i}",
                        datetime(2026, 10, 20), datetime(2026, 10, 20, 12, i), i) for i in range(25)]


async def open_browser(app, pilot):
    await pilot.press("ctrl+p")
    await pilot.pause()
    center = app.screen
    center.query_one("#command-search", Input).value = "sesje"
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, _SessionBrowser)
    return center, app.screen


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 40), (80, 24), (60, 20)])
async def test_browser_search_back_numbering_and_layout(tmp_path: Path, size):
    app = _OneTextualApp(_mk_app_session(tmp_path))
    with patch.object(app, "_browser_session_infos", AsyncMock(return_value=infos())):
        async with app.run_test(size=size) as pilot:
            app.query_one("#input", TextArea).text = "keep draft"
            center, browser = await open_browser(app, pilot)
            assert len(browser._filtered) == 25
            assert browser.query_one("#session-load").region.bottom <= size[1]
            await pilot.press("pagedown", "down")
            browser.query_one("#session-search", Input).value = "exact-id-1"
            await pilot.pause()
            assert any(info.id == "exact-id-1" for info in browser._filtered)
            assert app._session_list_snapshot is None
            await pilot.press("escape")
            assert app.screen is center
            assert center.query_one("#command-search", Input).value == "sesje"
            await pilot.press("escape")
            assert app.query_one("#input", TextArea).text == "keep draft"


@pytest.mark.asyncio
async def test_browser_load_exact_id_draft_confirmation_and_error(tmp_path: Path):
    app = _OneTextualApp(_mk_app_session(tmp_path))
    with patch.object(app, "_browser_session_infos", AsyncMock(return_value=infos())), patch.object(
        app, "_load_browser_session", AsyncMock(side_effect=ValueError("gone"))
    ) as load:
        async with app.run_test() as pilot:
            app.query_one("#input", TextArea).text = "draft"
            center, browser = await open_browser(app, pilot)
            browser.query_one("#session-search", Input).value = "exact-id-0"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            load.assert_not_awaited()
            assert "Keep your draft" in str(browser.query_one("#session-notice").content)
            await pilot.press("enter")
            await pilot.pause()
            assert load.await_args.args[0].id == "exact-id-0"
            assert app.screen is browser
            assert "Unable to load" in str(browser.query_one("#session-notice").content)
            assert app.query_one("#input", TextArea).text == "draft"
            load.side_effect = None
            await pilot.press("enter")
            await pilot.pause()
            assert len(app.screen_stack) == 1


@pytest.mark.asyncio
async def test_browser_revalidates_removed_identity(tmp_path: Path):
    app = _OneTextualApp(_mk_app_session(tmp_path))
    with patch.object(app, "_browser_session_infos", AsyncMock(side_effect=[infos(), []])), patch.object(
        app, "_load_browser_session", AsyncMock()
    ) as load:
        async with app.run_test() as pilot:
            _, browser = await open_browser(app, pilot)
            await pilot.press("enter")
            await pilot.pause()
            load.assert_not_awaited()
            assert "no longer available" in str(browser.query_one("#session-notice").content)
