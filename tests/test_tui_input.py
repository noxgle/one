from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from one.modes.tui_mode import (
    _THINKING_MARK,
)
from tests.support.tui import _mk_app_session, _submit, _visible_text_area_text


@pytest.mark.asyncio
async def test_tui_prompt_queued_while_streaming(tmp_path: Path) -> None:
    """A plain prompt submitted while the agent is streaming is queued as a
    steer (default for followUpMode='queue') instead of failing with
    'streamingBehavior is required'."""
    import asyncio

    from one.modes.tui_mode import _OneTextualApp
    from one.providers.base import ChatResult

    class _SlowStreamProvider:
        async def chat(self, api_key, model, messages, thinking_level, headers=None, on_delta=None, on_thinking_delta=None, max_tokens=None, images=None, storage_dir=""):
            await asyncio.sleep(1.0)
            return ChatResult(text="DONE", raw={}, usage={}, stop_reason="stop")

    session = _mk_app_session(tmp_path, runtime_key="sk-test")
    session.providers = {"openai": _SlowStreamProvider()}
    session.settings_manager.set_retry_enabled(False)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "first prompt")
        for _ in range(100):
            await pilot.pause()
            if session.is_streaming:
                break
        assert session.is_streaming
        await _submit(app, pilot, "second prompt")
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "[error]" not in stream
        # Default followUpMode='queue' → normal input during streaming routes to steer
        assert "second prompt" in session.get_pending_queues()["steering"]
        for _ in range(200):
            await pilot.pause()
            if not app._turn_active and not session.is_streaming:
                break
        assert "second prompt" not in session.get_pending_queues()["steering"]


@pytest.mark.asyncio
async def test_tui_input_stays_responsive_during_rapid_streaming(tmp_path: Path) -> None:
    """Provider deltas must not starve keyboard events or queue one UI event each."""
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp
    from one.providers.base import ChatResult

    release_first_response = asyncio.Event()
    first_response_complete = asyncio.Event()
    deltas_finished = asyncio.Event()
    rapid_output = "rapid-stream-output-" + ("x" * 4000)

    class _RapidStreamProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(
            self,
            api_key,
            model,
            messages,
            thinking_level,
            headers=None,
            on_delta=None,
            on_thinking_delta=None,
            max_tokens=None,
            images=None,
            storage_dir="",
        ):
            self.calls += 1
            if self.calls == 1:
                for char in rapid_output:
                    assert on_delta is not None
                    on_delta(char)
                    # Simulate a fast async transport which yields frequently.
                    await asyncio.sleep(0)
                deltas_finished.set()
                await release_first_response.wait()
                first_response_complete.set()
                return ChatResult(text=rapid_output, raw={}, usage={}, stop_reason="stop")
            return ChatResult(text="follow-up complete", raw={}, usage={}, stop_reason="stop")

    session = _mk_app_session(tmp_path, runtime_key="sk-test")
    session.providers = {"openai": _RapidStreamProvider()}
    session.settings_manager.set_retry_enabled(False)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "first prompt")
        for _ in range(200):
            await pilot.pause()
            if app._assistant_stream:
                break
        assert app._assistant_stream

        # Use real pilot key events rather than assigning .text: this regresses
        # the starvation that previously made keyboard input wait for the stream.
        await pilot.press(*"second prompt")
        input_widget = app.query_one("#input", TextArea)
        assert input_widget.text == "second prompt"
        # Check the rendered TextArea, not merely its backing value: this is
        # the user-visible paint that regressed under UI event backlogs.
        await pilot.pause()
        assert "second prompt" in _visible_text_area_text(input_widget)
        assert not first_response_complete.is_set()
        await pilot.press("enter")
        await pilot.pause()
        assert "second prompt" in session.get_pending_queues()["steering"]
        for _ in range(200):
            await pilot.pause()
            if deltas_finished.is_set() and rapid_output in app._assistant_stream:
                break
        assert rapid_output == app._assistant_stream

        release_first_response.set()
        for _ in range(500):
            await pilot.pause()
            if not session.is_streaming and not app._turn_active:
                break
        stream = "\n".join(app._stream_lines)
        assert "rapid-stream-output-" in stream
        assert stream.count("x") >= 4000
        assert stream.index("rapid-stream-output-") < stream.index("> second prompt")


@pytest.mark.asyncio
async def test_tui_input_paints_and_submits_while_provider_is_silent(tmp_path: Path) -> None:
    """A silent async provider wait must leave the TextArea paintable."""
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp
    from one.providers.base import ChatResult

    entered = asyncio.Event()
    release = asyncio.Event()

    class _GatedProvider:
        async def chat(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return ChatResult(text="silent complete", raw={}, usage={}, stop_reason="stop")

    session = _mk_app_session(tmp_path, runtime_key="sk-test")
    session.providers = {"openai": _GatedProvider()}
    session.settings_manager.set_retry_enabled(False)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await _submit(app, pilot, "first")
        for _ in range(50):
            await pilot.pause()
            if entered.is_set():
                break
        assert entered.is_set()
        await pilot.press(*"queued while silent")
        input_widget = app.query_one("#input", TextArea)
        await pilot.pause()
        assert "queued while silent" in _visible_text_area_text(input_widget)
        await pilot.press("enter")
        await pilot.pause()
        assert "queued while silent" in session.get_pending_queues()["steering"]
        release.set()


@pytest.mark.asyncio
async def test_tui_spinner_survives_queued_prompt_injected_before_next_request(tmp_path: Path) -> None:
    """A prompt submitted mid-turn is injected without losing the active spinner."""
    import asyncio

    from one.modes.tui_mode import _OneTextualApp
    from one.providers.base import ChatResult

    class _SlowStreamProvider:
        async def chat(self, api_key, model, messages, thinking_level, headers=None, on_delta=None, on_thinking_delta=None, max_tokens=None, images=None, storage_dir=""):
            await asyncio.sleep(1.0)
            return ChatResult(text="DONE", raw={}, usage={}, stop_reason="stop")

    session = _mk_app_session(tmp_path, runtime_key="sk-test")
    session.providers = {"openai": _SlowStreamProvider()}
    session.settings_manager.set_retry_enabled(False)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "first prompt")
        for _ in range(100):
            await pilot.pause()
            if session.is_streaming:
                break
        assert session.is_streaming
        # Queue a second prompt while the first turn is still in flight.
        await _submit(app, pilot, "second prompt")
        for _ in range(10):
            await pilot.pause()
        # The queued submission must NOT clear the turn state: the first turn
        # is still running, so the spinner stays armed.
        assert app._turn_active is True

        # The queued steer is consumed by the nudge request in this active
        # turn, so there need not be a second agent_start/spinner window.
        spinner_windows = 0
        spinner_visible = False
        for _ in range(300):
            await pilot.pause()
            any_spinner = any(line.startswith(_THINKING_MARK) for line in app._stream_lines)
            if any_spinner and not spinner_visible:
                spinner_windows += 1
                spinner_visible = True
            elif not any_spinner:
                spinner_visible = False
            if not app._turn_active and not session.is_streaming:
                break
        assert spinner_windows >= 1, "spinner must remain visible for the running turn"
        assert "second prompt" in [m.get("content") for m in session.messages if m.get("role") == "user"]


@pytest.mark.asyncio
async def test_extension_ui_cancel_via_empty_input(tmp_path: Path):
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        req = session.request_extension_ui(extension="x", ui_type="overlay", payload={})
        await pilot.pause()
        assert app._extension_ui_pending_request is not None

        input_widget = app.query_one("#input", TextArea)
        input_widget.text = ""
        await input_widget.action_submit()
        await pilot.pause()

        assert app._extension_ui_pending_request is None
        assert session._extension_ui_history[-1]["cancelled"] is True
        assert session._extension_ui_history[-1]["requestId"] == req["id"]


@pytest.mark.asyncio
async def test_tui_paste_from_system_clipboard(tmp_path: Path, monkeypatch):
    from one.modes import tui_mode

    session = _mk_app_session(tmp_path)
    app = tui_mode._OneTextualApp(session)
    monkeypatch.setattr(tui_mode, "_paste_from_system_clipboard", lambda: "pasted-text")
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert input_widget.text == "pasted-text"


@pytest.mark.asyncio
async def test_tui_paste_fallback_app_clipboard(tmp_path: Path, monkeypatch):
    from one.modes import tui_mode

    session = _mk_app_session(tmp_path)
    app = tui_mode._OneTextualApp(session)
    monkeypatch.setattr(tui_mode, "_paste_from_system_clipboard", lambda: None)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._clipboard = "in-app-text"
        input_widget = app.query_one("#input")
        input_widget.action_paste()
        await pilot.pause()
        assert input_widget.text == "in-app-text"


@pytest.mark.asyncio
async def test_tui_paste_multiline_preserved(tmp_path: Path, monkeypatch):
    """Multi-line paste stays multi-line: the input field is now a TextArea."""
    from one.modes import tui_mode

    session = _mk_app_session(tmp_path)
    app = tui_mode._OneTextualApp(session)
    monkeypatch.setattr(tui_mode, "_paste_from_system_clipboard", lambda: "line1\nline2\nline3")
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        input_widget.action_paste()
        await pilot.pause()
        assert input_widget.text == "line1\nline2\nline3"


@pytest.mark.asyncio
async def test_tui_paste_long_text_truncated(tmp_path: Path, monkeypatch):
    """Huge single-line pastes are capped so the UI does not blow up."""
    from one.modes import tui_mode

    session = _mk_app_session(tmp_path)
    app = tui_mode._OneTextualApp(session)
    monkeypatch.setattr(tui_mode, "_paste_from_system_clipboard", lambda: "x" * 50_000)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        input_widget.action_paste()
        await pilot.pause()
        assert len(input_widget.text) == tui_mode._CommandTextArea._PASTE_MAX_CHARS
        assert input_widget.text == "x" * tui_mode._CommandTextArea._PASTE_MAX_CHARS


@pytest.mark.asyncio
async def test_tui_paste_via_event_inserts_once(tmp_path: Path, monkeypatch):
    """Dispatching a real events.Paste must insert exactly once."""
    from textual import events
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget: TextArea = app.query_one("#input", TextArea)
        paste_event = events.Paste("paste-from-event")
        input_widget.post_message(paste_event)
        await pilot.pause()
        # Must appear exactly once — no double-insert.
        assert input_widget.text == "paste-from-event"
        assert input_widget.text.count("paste-from-event") == 1


@pytest.mark.asyncio
async def test_tui_app_paste_after_focus_loss_refocuses_input_and_inserts_once(tmp_path: Path):
    """A driver-routed terminal paste reaches the app when focus was cleared."""
    from textual import events
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget: TextArea = app.query_one("#input", TextArea)
        app.screen.set_focus(None)
        assert app.focused is None
        app.post_message(events.Paste("paste-after-focus-loss"))
        await pilot.pause()
        assert app.focused is input_widget
        assert input_widget.text == "paste-after-focus-loss"
        assert input_widget.text.count("paste-after-focus-loss") == 1


@pytest.mark.asyncio
async def test_tui_app_empty_paste_after_focus_loss_does_not_refocus_or_insert(tmp_path: Path):
    """An empty app-level paste is consumed without changing focus."""
    from textual import events
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget: TextArea = app.query_one("#input", TextArea)
        app.screen.set_focus(None)
        assert app.focused is None
        app.post_message(events.Paste(""))
        await pilot.pause()
        assert app.focused is None
        assert input_widget.text == ""


@pytest.mark.asyncio
async def test_tui_app_paste_does_not_steal_other_widget_focus(tmp_path: Path):
    """The no-focus fallback leaves paste owned by another focused widget."""
    from textual import events
    from textual.widgets import Button, TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget: TextArea = app.query_one("#input", TextArea)
        button = Button("Other", id="other-focus")
        await app.mount(button)
        button.focus()
        await pilot.pause()
        assert app.focused is button
        app.post_message(events.Paste("other-widget-paste"))
        await pilot.pause()
        assert app.focused is button
        assert input_widget.text == ""


@pytest.mark.asyncio
async def test_tui_ctrl_shift_v_uses_terminal_paste_event_once(tmp_path: Path, monkeypatch):
    """SSH terminal paste must not consult a clipboard backend or invoke image paste."""
    from textual import events
    from textual.widgets import TextArea

    from one.modes import tui_mode

    session = _mk_app_session(tmp_path)
    app = tui_mode._OneTextualApp(session)
    monkeypatch.setattr(tui_mode, "_paste_from_system_clipboard", lambda: pytest.fail("clipboard read"))
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget: TextArea = app.query_one("#input", TextArea)
        input_widget.post_message(events.Paste("ssh-terminal-paste"))
        await pilot.pause()
        assert input_widget.text == "ssh-terminal-paste"
        assert input_widget.text.count("ssh-terminal-paste") == 1


@pytest.mark.asyncio
async def test_tui_ctrl_alt_v_dispatches_image_paste(tmp_path: Path, monkeypatch):
    """Ctrl+Alt+V is the only app binding that dispatches image paste."""
    from one.modes import tui_mode

    session = _mk_app_session(tmp_path)
    app = tui_mode._OneTextualApp(session)
    calls: list[None] = []
    monkeypatch.setattr(app, "action_paste_image", lambda: calls.append(None))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+alt+v")
        await pilot.pause()
        assert calls == [None]
        assert all(getattr(binding, "key", None) != "ctrl+shift+v" for binding in app.BINDINGS)


@pytest.mark.asyncio
async def test_tui_paste_then_enter_submits_once(tmp_path: Path, monkeypatch):
    """Paste text then press Enter must submit one prompt containing the
    pasted text exactly once (no duplicate from double-insert)."""
    from textual import events
    from textual.widgets import TextArea

    from one.modes import tui_mode
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    monkeypatch.setattr(tui_mode, "_paste_from_system_clipboard", lambda: "submit-paste")
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget: TextArea = app.query_one("#input", TextArea)
        # Dispatch a real Paste event (not direct action call).
        paste_event = events.Paste("submit-paste")
        input_widget.post_message(paste_event)
        await pilot.pause()
        assert input_widget.text == "submit-paste"
        # Press Enter to submit.
        await pilot.press("enter")
        await pilot.pause()
        # The input should be cleared after submission.
        assert input_widget.text == ""


@pytest.mark.asyncio
async def test_tui_multiline_paste_via_event_preserved(tmp_path: Path, monkeypatch):
    """Multi-line paste via events.Paste stays multi-line (one paste = one
    insertion of the full multi-line string)."""
    from textual import events
    from textual.widgets import TextArea

    from one.modes import tui_mode
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    monkeypatch.setattr(tui_mode, "_paste_from_system_clipboard", lambda: None)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget: TextArea = app.query_one("#input", TextArea)
        multiline = "line1\nline2\nline3"
        paste_event = events.Paste(multiline)
        input_widget.post_message(paste_event)
        await pilot.pause()
        # The text must be present exactly once.
        assert input_widget.text.count("line1") == 1
        assert input_widget.text.count("line2") == 1
        assert input_widget.text.count("line3") == 1


@pytest.mark.asyncio
async def test_tui_paste_truncation_still_correct_via_event(tmp_path: Path, monkeypatch):
    """Huge paste via events.Paste is still capped to _PASTE_MAX_CHARS."""
    from textual import events
    from textual.widgets import TextArea

    from one.modes import tui_mode
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    monkeypatch.setattr(tui_mode, "_paste_from_system_clipboard", lambda: None)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget: TextArea = app.query_one("#input", TextArea)
        huge_text = "x" * 50_000
        paste_event = events.Paste(huge_text)
        input_widget.post_message(paste_event)
        await pilot.pause()
        assert len(input_widget.text) == tui_mode._CommandTextArea._PASTE_MAX_CHARS
        assert input_widget.text == "x" * tui_mode._CommandTextArea._PASTE_MAX_CHARS


@pytest.mark.asyncio
async def test_tui_backspace_deletes_single_char(tmp_path: Path):
    """One backspace press deletes exactly one char and moves cursor one."""
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget: TextArea = app.query_one("#input", TextArea)
        input_widget.text = "hello"
        await pilot.pause()
        input_widget.move_cursor((0, 5))
        await pilot.pause()
        await pilot.press("backspace")
        await pilot.pause()
        assert input_widget.text == "hell"
        assert input_widget.cursor_location == (0, 4)


@pytest.mark.asyncio
async def test_tui_backspace_at_start_noop(tmp_path: Path):
    """Backspace at (0, 0) changes nothing."""
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget: TextArea = app.query_one("#input", TextArea)
        input_widget.text = "hello"
        await pilot.pause()
        input_widget.move_cursor((0, 0))
        await pilot.pause()
        await pilot.press("backspace")
        await pilot.pause()
        assert input_widget.text == "hello"
        assert input_widget.cursor_location == (0, 0)


@pytest.mark.asyncio
async def test_tui_delete_forward_single_char(tmp_path: Path):
    """One delete press removes exactly one char forward."""
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget: TextArea = app.query_one("#input", TextArea)
        input_widget.text = "hello"
        await pilot.pause()
        input_widget.move_cursor((0, 0))
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()
        assert input_widget.text == "ello"
        assert input_widget.cursor_location == (0, 0)


@pytest.mark.asyncio
async def test_tui_arrows_single_step(tmp_path: Path):
    """Arrow keys move the cursor by exactly one position."""
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget: TextArea = app.query_one("#input", TextArea)
        input_widget.text = "hello"
        await pilot.pause()
        input_widget.move_cursor((0, 5))
        await pilot.pause()
        await pilot.press("left")
        await pilot.pause()
        assert input_widget.cursor_location == (0, 4)
        await pilot.press("right")
        await pilot.pause()
        assert input_widget.cursor_location == (0, 5)


@pytest.mark.asyncio
async def test_tui_printable_inserts_once(tmp_path: Path):
    """A printable key inserts exactly one char (widget double-fire guard)."""
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget: TextArea = app.query_one("#input", TextArea)
        await pilot.press("a")
        await pilot.pause()
        assert input_widget.text == "a"


@pytest.mark.asyncio
async def test_tui_backspace_multiline_join_once(tmp_path: Path):
    """Backspace at line start joins lines exactly once."""
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget: TextArea = app.query_one("#input", TextArea)
        input_widget.text = "ab\ncd"
        await pilot.pause()
        input_widget.move_cursor((1, 0))
        await pilot.pause()
        await pilot.press("backspace")
        await pilot.pause()
        assert input_widget.text == "abcd"


@pytest.mark.asyncio
async def test_tui_backspace_selection_exact(tmp_path: Path):
    """Backspace with a selection deletes only the selection."""
    from textual.widgets import TextArea
    from textual.widgets._text_area import Selection

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget: TextArea = app.query_one("#input", TextArea)
        input_widget.text = "hello"
        await pilot.pause()
        input_widget.selection = Selection((0, 1), (0, 4))
        await pilot.pause()
        await pilot.press("backspace")
        await pilot.pause()
        assert input_widget.text == "ho"


@pytest.mark.asyncio
async def test_tui_backspace_repeat_three_times(tmp_path: Path):
    """Three backspace presses delete exactly three chars."""
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget: TextArea = app.query_one("#input", TextArea)
        input_widget.text = "hello"
        await pilot.pause()
        input_widget.move_cursor((0, 5))
        await pilot.pause()
        for _ in range(3):
            await pilot.press("backspace")
            await pilot.pause()
        assert input_widget.text == "he"
