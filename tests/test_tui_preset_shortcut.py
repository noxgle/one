# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from textual.screen import ModalScreen
from textual.widgets import Button, TextArea

from one.modes.tui_mode import TuiSessionView, _OneTextualApp
from tests.support.tui import _mk_app_session


@pytest.mark.asyncio
async def test_shift_tab_cycles_presets_preserving_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(tmp_path))
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "reviewer.md").write_text(
        "---\nversion: 1\nid: reviewer\nname: Reviewer\n---\nReview carefully.\n"
    )
    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one("#input", TextArea)
        editor.text = "unfinished\nmessage"
        editor.move_cursor((0, 4))
        for expected in ("readonly", "reviewer", "default"):
            await pilot.press("shift+tab")
            await pilot.pause()
            assert session.preset_id == expected
            assert editor.text == "unfinished\nmessage"
            assert editor.cursor_location == (0, 4)
            assert app.focused is editor
        assert "Preset set to Reviewer" in "\n".join(app._stream_lines)
        assert "bash" in session.active_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["_is_streaming", "_is_compacting", "_active_tool_tasks"])
async def test_shift_tab_rejects_busy_session(tmp_path, monkeypatch, busy):
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(tmp_path))
    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        old = getattr(session, busy)
        setattr(session, busy, {"tool": object()} if busy == "_active_tool_tasks" else True)
        try:
            await pilot.press("shift+tab")
            await pilot.pause()
            assert session.preset_id == "default"
            assert "Cannot change preset while work is active" in "\n".join(app._stream_lines)
        finally:
            setattr(session, busy, old)


@pytest.mark.asyncio
async def test_shift_tab_keeps_modal_focus_navigation(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(tmp_path))

    class Dialog(ModalScreen):
        def compose(self):
            yield Button("First", id="first")
            yield Button("Second", id="second")

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(Dialog())
        await pilot.pause()
        app.screen.query_one("#second").focus()
        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.focused.id == "first"
        assert session.preset_id == "default"


@pytest.mark.asyncio
async def test_shift_tab_does_not_cycle_with_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(tmp_path))
    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_shortcuts()
        await pilot.press("shift+tab")
        await pilot.pause()
        assert session.preset_id == "default"


@pytest.mark.asyncio
async def test_shift_tab_uses_detached_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(tmp_path))

    class Facade:
        def __init__(self):
            self.preset = "default"
            self.operations = []

        def subscribe(self, listener, **kwargs):
            return lambda: None

        async def command(self, name, payload=None):
            if name == "ui":
                self.operations.append(payload)
                if payload["operation"] == "preset_list":
                    return {"result": {"presets": [{"id": "default"}, {"id": "readonly"}]}}
                assert payload["operation"] == "set_preset"
                self.preset = payload["payload"]["id"]
            return {"result": {"epoch": 0, "sidebar": {
                "preset": self.preset, "presetName": self.preset.title(),
            }, "messages": []}}

    facade = Facade()
    app = _OneTextualApp(TuiSessionView(facade))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.session.preset_id == "readonly"
        assert [op["operation"] for op in facade.operations] == ["preset_list", "set_preset"]


@pytest.mark.asyncio
async def test_preset_discovery_failure_is_displayed(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(tmp_path))
    view = TuiSessionView(object())
    app = _OneTextualApp(view)
    view.command = AsyncMock(side_effect=ValueError("Invalid preset file"))
    app._write = lambda text, level: errors.append((text, level))
    errors = []
    await app._cycle_preset()
    assert errors == [("Invalid preset file", "error")]
    assert view.preset_id == "default"
