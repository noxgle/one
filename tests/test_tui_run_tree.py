# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio

import pytest
from textual.app import App
from textual.widgets import OptionList, Static

from one.core.run_context import RunContext, RunLimits
from one.modes.tui_mode import TuiSessionView, _OneTextualApp, _RunTreeInspector
from tests.support.tui import _mk_app_session


class Facade:
    def __init__(self):
        self.context = RunContext("root", RunLimits())
        child = self.context.register(self.context.root_id, "child")
        self.grandchild = self.context.register(child.node_id, "grandchild")
        for i in range(22):
            self.context.register(self.context.root_id, f"extra-{i}")
        self.calls = []

    async def run_tree_page(self, epoch, offset, limit):
        return {"result": {"epoch": epoch, "available": True,
                           **self.context.tree_snapshot(offset=offset, limit=limit)}}

    async def cancel_run_subtree(self, epoch, run_id, node_id):
        self.calls.append((epoch, run_id, node_id))
        return {"result": {"accepted": True}}

    async def cancel_run(self, epoch, run_id):
        self.calls.append((epoch, run_id))
        return {"result": {"accepted": False}}


class Harness(App):
    def __init__(self, view):
        super().__init__()
        self.view = view

    def on_mount(self):
        self.push_screen(_RunTreeInspector(self.view))


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, "invalid", []])
async def test_cancel_invalid_result_shows_friendly_error(result):
    facade = Facade()

    async def cancel_run(epoch, run_id):
        return {"result": result}

    facade.cancel_run = cancel_run
    app = Harness(TuiSessionView(facade))
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await pilot.press("w", "y")
        await pilot.pause()
        notice = str(app.screen.query_one("#run-tree-notice", Static).content)
        assert "Cancellation failed: invalid response from runtime." in notice


@pytest.mark.asyncio
async def test_tree_render_pagination_and_cancellation():
    facade = Facade()
    view = TuiSessionView(facade)
    app = Harness(view)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, _RunTreeInspector)
        assert screen.page["nodes"][2]["depth"] == 2
        await pilot.press("down", "down")
        await pilot.pause()
        assert "depth 2" in str(screen.query_one("#run-tree-details", Static).content)
        assert "unknown tokens" in str(screen.query_one("#run-tree-details", Static).content)
        assert "remaining unlimited" in str(screen.query_one("#run-tree-summary", Static).content)
        await pilot.press("c")
        assert "irreversible" in str(screen.query_one("#run-tree-notice", Static).content)
        assert not facade.calls
        await pilot.press("y")
        await pilot.pause()
        assert facade.calls == [(0, facade.context.run_id, facade.grandchild.node_id)]
        await pilot.press("n")
        await pilot.pause()
        assert screen.page_offset == 20
        await pilot.press("p")
        await pilot.pause()
        assert screen.page_offset == 0
        await pilot.press("w", "y")
        await pilot.pause()
        assert facade.calls[-1] == (0, facade.context.run_id)
        assert "rejected" in str(screen.query_one("#run-tree-notice", Static).content)
        assert screen.query_one("#run-tree-list", OptionList).option_count == 20


@pytest.mark.asyncio
async def test_open_from_agents_panel(tmp_path):
    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        app.action_show_agents()
        # Replace only after startup; the modal needs a detached facade-backed view.
        original = app.session
        app.session = TuiSessionView(Facade())
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, _RunTreeInspector)
        screen = app.screen
        await pilot.press("n")
        await pilot.pause()
        assert screen.page_offset == 20
        await pilot.press("w", "y")
        await pilot.pause()
        assert len(screen.session._facade.calls) == 1
        await pilot.press("escape")
        app.session = original
        await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revision", "run"])
async def test_pagination_rejects_changed_snapshot(change):
    facade = Facade()
    app = Harness(TuiSessionView(facade))
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        screen = app.screen
        original = screen.page
        if change == "revision":
            facade.context.register(facade.context.root_id, "new")
        else:
            facade.context = RunContext("new-root", RunLimits())
        await screen.action_next_page()
        assert screen.page is original
        assert "revision changed" in str(screen.query_one("#run-tree-notice", Static).content)
        await screen.action_refresh_tree()
        assert screen.page_offset == 0
        assert screen.page is not original


@pytest.mark.asyncio
async def test_previous_page_rejects_new_revision():
    facade = Facade()
    app = Harness(TuiSessionView(facade))
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        await screen.action_next_page()
        original = screen.page
        facade.context.register(facade.context.root_id, "new")
        await screen.action_previous_page()
        assert screen.page is original
        assert screen.page_offset == 20
        assert screen.previous == [0]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["epoch", "newer"])
async def test_delayed_response_cannot_replace_view(change):
    facade = Facade()
    view = TuiSessionView(facade)
    app = Harness(view)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        original = screen.page
        ready, release = asyncio.Event(), asyncio.Event()
        fetch = facade.run_tree_page

        async def delayed(epoch, offset, limit):
            reply = await fetch(epoch, offset, limit)
            ready.set()
            await release.wait()
            return reply

        facade.run_tree_page = delayed
        task = asyncio.create_task(screen._load(20))
        await ready.wait()
        if change == "epoch":
            view._snapshot["epoch"] += 1
        else:
            facade.run_tree_page = fetch
            await screen.action_refresh_tree()
            original = screen.page
        release.set()
        assert not await task
        assert screen.page is original
        assert screen.page_offset == 0
