from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.support.tui import _mk_app_session, _submit


class _FakeRuntimeHost:
    def __init__(self, session: Any, new_session: Any | None = None) -> None:
        self.session = session
        self._new_session = new_session

    async def fork(self, entry_id: str) -> dict:
        if self._new_session is not None:
            self.session = self._new_session
        return {"cancelled": False, "entryId": entry_id}

    async def new_session(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._new_session is not None:
            self.session = self._new_session
        return {"cancelled": False}


@pytest.mark.asyncio
async def test_tui_command_fork_rebinds_session(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    old_session = _mk_app_session(tmp_path)
    new_session = _mk_app_session(tmp_path)
    app = _OneTextualApp(old_session, runtime_host=_FakeRuntimeHost(old_session, new_session=new_session))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/fork entry-1")
        stream = "\n".join(app._stream_lines)
        assert '"cancelled": false' in stream
        assert app.session is new_session
        assert callable(app._off_listener)
        # Events from the new session must now be delivered to the app.
        new_session.request_extension_ui(extension="post-fork", ui_type="widget", payload={})
        await pilot.pause()
        assert app._extension_ui_pending_request is not None
        assert app._extension_ui_pending_request["extension"] == "post-fork"


@pytest.mark.asyncio
async def test_tui_command_new_rebinds_session(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    old_session = _mk_app_session(tmp_path)
    new_session = _mk_app_session(tmp_path)
    app = _OneTextualApp(old_session, runtime_host=_FakeRuntimeHost(old_session, new_session=new_session))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/new")
        stream = "\n".join(app._stream_lines)
        assert "New session started." in stream
        assert app.session is new_session
        assert callable(app._off_listener)


@pytest.mark.asyncio
async def test_tui_new_clears_extension_ui_state(tmp_path: Path):
    """P1-8: /new must clear pending extension request, hide panels, and reset placeholder."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    old_session = _mk_app_session(tmp_path)
    new_session = _mk_app_session(tmp_path)
    app = _OneTextualApp(old_session, runtime_host=_FakeRuntimeHost(old_session, new_session=new_session))
    async with app.run_test() as pilot:
        await pilot.pause()

        # Create a widget extension request on the old session.
        old_session.request_extension_ui(extension="stale-ext", ui_type="widget", payload={"x": 1}, title="Stale")
        await pilot.pause()

        panel = app.query_one("#ext_panel", Static)
        assert panel.has_class("visible")
        assert app._extension_ui_pending_request is not None
        assert app._extension_ui_pending_request["extension"] == "stale-ext"
        from textual.widgets import TextArea

        input_widget = app.query_one("#input", TextArea)
        assert input_widget.placeholder == "Answer for extension (JSON or text; empty = cancel)"

        # Submit /new.
        await _submit(app, pilot, "/new")
        await pilot.pause()

        # After /new: panel hidden, pending cleared, default placeholder, session swapped.
        assert not panel.has_class("visible")
        assert app._extension_ui_pending_request is None
        assert input_widget.placeholder == "Type a command or /help"
        assert app.session is new_session


@pytest.mark.asyncio
async def test_tui_fork_clears_extension_ui_state(tmp_path: Path):
    """P1-8: /fork must clear pending extension request, hide panels, and reset placeholder."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    old_session = _mk_app_session(tmp_path)
    new_session = _mk_app_session(tmp_path)
    app = _OneTextualApp(old_session, runtime_host=_FakeRuntimeHost(old_session, new_session=new_session))
    async with app.run_test() as pilot:
        await pilot.pause()

        # Trigger an overlay request in the old session.
        old_session.request_extension_ui(extension="fork-ext", ui_type="overlay", payload={"key": "val"}, title="Fork")
        await pilot.pause()

        overlay = app.query_one("#ext_overlay", Static)
        assert overlay.has_class("visible")
        assert app._extension_ui_pending_request is not None
        assert app._extension_ui_pending_request["extension"] == "fork-ext"
        from textual.widgets import TextArea

        input_widget = app.query_one("#input", TextArea)
        assert input_widget.placeholder == "Answer for extension (JSON or text; empty = cancel)"

        # Execute /fork.
        await _submit(app, pilot, "/fork entry-1")
        await pilot.pause()

        # After /fork: overlay hidden, pending cleared, default placeholder, session swapped.
        assert not overlay.has_class("visible")
        assert app._extension_ui_pending_request is None
        assert input_widget.placeholder == "Type a command or /help"
        assert app.session is new_session


@pytest.mark.asyncio
async def test_tui_command_fork_without_runtime_host(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/fork abc")
        stream = "\n".join(app._stream_lines)
        assert "Runtime host unavailable" in stream
        assert app.session is session


@pytest.mark.asyncio
async def test_tui_command_navigate_unknown_id_error(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/navigate nope-123")
        stream = "\n".join(app._stream_lines)
        assert "not found" in stream


@pytest.mark.asyncio
async def test_tui_shift_enter_inserts_newline(tmp_path: Path):
    """Shift+Enter inserts a newline; Enter submits (see _submit tests)."""
    from one.modes import tui_mode

    session = _mk_app_session(tmp_path)
    app = tui_mode._OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        input_widget.focus()
        await pilot.press("a", "b")
        await pilot.press("shift+enter")
        await pilot.press("c")
        await pilot.pause()
        assert input_widget.text == "ab\nc"


@pytest.mark.asyncio
async def test_extension_panels_hide_on_new_session(tmp_path: Path):
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session.request_extension_ui(extension="ext", ui_type="widget", payload={"x": 1}, title="P")
        await pilot.pause()
        panel = app.query_one("#ext_panel", Static)
        assert panel.has_class("visible")

        # Simulate the /new reset path: pending cleared + panels hidden.
        app._extension_ui_pending_request = None
        app._hide_extension_panels()
        await pilot.pause()
        assert not panel.has_class("visible")


@pytest.mark.asyncio
async def test_tui_history_records_commands_only(tmp_path: Path):
    """Submit /theme then /history — stream contains "1. /theme".
    Plain prompt "hello" must NOT appear in /history output."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/theme fallout")
        await _submit(app, pilot, "hello")
        await _submit(app, pilot, "/help")
        await _submit(app, pilot, "/history")
        stream = "\n".join(app._stream_lines)
        assert "1. /theme fallout" in stream
        assert "2. /help" in stream
        # "hello" must NOT be in the numbered history entries
        assert "1. hello" not in stream
        assert "2. hello" not in stream
        assert "3. hello" not in stream


@pytest.mark.asyncio
async def test_tui_history_populate_input(tmp_path: Path):
    """Submit /theme, then /history 1 — input widget value == "/theme", no turn started."""
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/theme hacker")
        await _submit(app, pilot, "/history 1")
        input_widget = app.query_one("#input", TextArea)
        assert input_widget.text == "/theme hacker"


@pytest.mark.asyncio
async def test_tui_history_out_of_range(tmp_path: Path):
    """Submit /history 99 — error line."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/history 99")
        stream = "\n".join(app._stream_lines)
        assert "Index out of range" in stream


@pytest.mark.asyncio
async def test_tui_history_empty(tmp_path: Path):
    """With no commands yet, /history prints 'No commands yet.'"""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/history")
        stream = "\n".join(app._stream_lines)
        assert "No commands yet." in stream


@pytest.mark.asyncio
async def test_tui_history_invalid_number(tmp_path: Path):
    """Submit /history abc — error line."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/history abc")
        stream = "\n".join(app._stream_lines)
        assert "Invalid number" in stream


@pytest.mark.asyncio
async def test_tui_history_bash_command_recorded(tmp_path: Path):
    """Submit /bash echo hi — it should appear in /history."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/bash echo hi")
        await _submit(app, pilot, "/history")
        stream = "\n".join(app._stream_lines)
        assert "1. /bash echo hi" in stream


@pytest.mark.asyncio
async def test_tui_command_help_includes_history(tmp_path: Path):
    """/help output must contain /history token."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/help")
        stream = "\n".join(app._stream_lines)
        assert "/history [n]" in stream


@pytest.mark.asyncio
async def test_tui_history_capped_at_200(tmp_path: Path):
    """Submitting more than 200 slash commands caps the history at 200.
    /history displays the last 50; lookup is on the SAME window."""
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        for i in range(205):
            await app._handle_command(f"/config test{i} val{i}")
        await pilot.pause()
        # 205 submitted, cap at 200 → first 5 dropped → history = test5..test204
        assert len(app._command_history) == 200
        # /history displays last 50 (test155..test204) numbered 1..50
        await app._handle_command("/history")
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "1. /config test155 val155" in stream
        assert "50. /config test204 val204" in stream
        # /history 1 → oldest of the displayed window (test155)
        await app._handle_command("/history 1")
        await pilot.pause()
        input_widget = app.query_one("#input", TextArea)
        assert input_widget.text == "/config test155 val155"
        # /history 50 → newest of the displayed window (test204)
        await app._handle_command("/history 50")
        await pilot.pause()
        input_widget = app.query_one("#input", TextArea)
        assert input_widget.text == "/config test204 val204"
        # /history 51 → out of range (window is 1..50)
        await app._handle_command("/history 51")
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "out of range" in stream


@pytest.mark.asyncio
async def test_tui_history_numbering_matches_lookup_beyond_50(tmp_path: Path):
    """With >50 commands, /history shows last 50 numbered 1..50; lookup 1→oldest of last 50,
    lookup 50→newest of last 50."""
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Append 60 fake commands directly to the history list
        for i in range(1, 61):
            app._command_history.append(f"/model m{i}")
        # /history → displays last 50 numbered 1..50
        # Entry 1 should be the oldest of the last 50 = /model m11 (index 10 of the full list)
        await app._handle_command("/history")
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "1. /model m11" in stream  # oldest of the last 50
        assert "50. /model m60" in stream  # newest of the last 50
        # /history 1 → should fill /model m11 (oldest of the sliced window)
        await app._handle_command("/history 1")
        await pilot.pause()
        input_widget = app.query_one("#input", TextArea)
        assert input_widget.text == "/model m11"
        # /history 50 → should fill /model m60 (newest of the sliced window)
        await app._handle_command("/history 50")
        await pilot.pause()
        input_widget = app.query_one("#input", TextArea)
        assert input_widget.text == "/model m60"


@pytest.mark.asyncio
async def test_tui_history_zero_index_out_of_range(tmp_path: Path):
    """Submit /history 0 → error line contains 'out of range'."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Add at least 1 entry so the history window is non-empty
        app._command_history.append("/test")
        await app._handle_command("/history 0")
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "out of range" in stream


@pytest.mark.asyncio
async def test_tui_history_records_alias_as_typed(tmp_path: Path):
    """Submit '/h' (alias of /help), then /history — output must contain '1. /h', not '/help'."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._handle_command("/h")
        await pilot.pause()
        await app._handle_command("/history")
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "1. /h" in stream


@pytest.mark.asyncio
async def test_tui_shortcuts_overlay_contains_new_shortcuts(tmp_path: Path):
    """The shortcuts panel (Ctrl+F1) must show Ctrl+Shift+V and Ctrl+R."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_shortcuts()
        await pilot.pause()
        overlay = app.query_one("#shortcuts_overlay")
        content = str(overlay.content)
        assert "Ctrl+Shift+V" in content
        assert "paste image from the system clipboard" in content
        assert "Ctrl+R" in content
        assert "cycle retry mode" in content
