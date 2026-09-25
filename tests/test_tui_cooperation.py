from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.tui import _mk_app_session


@pytest.mark.asyncio
async def test_tui_ctrl_z_toggles_cooperation(tmp_path: Path):
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Focus is on the Input widget; the app's priority binding for
        # ctrl+z toggles cooperation regardless of widget focus.
        input_widget = app.query_one("#input")
        assert input_widget.has_focus
        header = app.query_one("#header", Static)
        assert "COOP: OFF" in str(header.content)
        await pilot.press("ctrl+z")
        assert session.approval_callback is not None
        assert session.settings_manager.get_tool_approval() is True
        assert "COOP: ON" in str(header.content)
        await pilot.press("ctrl+z")
        assert session.approval_callback is None
        assert session.settings_manager.get_tool_approval() is False
        assert "COOP: OFF" in str(header.content)


@pytest.mark.asyncio
async def test_tui_ctrl_a_no_longer_toggles_cooperation(tmp_path: Path):
    """Ctrl+A must NOT toggle cooperation anymore; it falls through to the
    focused widget's default binding (home / cursor-to-start-of-line)."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Cooperation starts OFF (approval_callback is None).
        assert session.approval_callback is None
        # Press ctrl+a — should NOT toggle cooperation.
        await pilot.press("ctrl+a")
        await pilot.pause()
        # Still OFF: ctrl+a no longer triggers toggle_cooperation.
        assert session.approval_callback is None
        # Now toggle via ctrl+z and verify it works.
        await pilot.press("ctrl+z")
        assert session.approval_callback is not None
        await pilot.press("ctrl+z")
        assert session.approval_callback is None


@pytest.mark.asyncio
async def test_tui_spinner_paused_shows_approval_wait(tmp_path: Path):
    """Approval pending no longer shows a paused label — normal spinner
    advances (approval gate is handled by the approval prompt widget)."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Arm the spinner: turn active + stale delta so evaluate_waiting is True.
        app._turn_active = True
        app._last_delta_ts = 0.0  # very old → waiting
        start_frame = app._thinking_frame
        # Set approval pending — should NOT pause the spinner anymore.
        app._approval_pending = {"tool": "bash", "args": {}}
        # tick_waiting is sync (not async), call directly.
        app._tick_waiting()
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        # No "zatwierdzenie" in the stream; frame should have advanced.
        assert "zatwierdzenie" not in stream
        # Frame must have advanced (spinner animates normally).
        assert app._thinking_frame != start_frame


@pytest.mark.asyncio
async def test_tui_spinner_paused_shows_ask_user_wait(tmp_path: Path):
    """When ask_user is pending and the spinner ticks, it shows a paused label."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._turn_active = True
        app._last_delta_ts = 0.0
        app._ask_user_pending = {"id": "x"}
        app._tick_waiting()
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "waiting for your response" in stream


@pytest.mark.asyncio
async def test_tui_approval_prompt_toasts(tmp_path: Path):
    """_approval_prompt must call notify (toast) with a message containing 'Approve:'
    and timeout=8.0."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Capture notify calls.
        notified: list[tuple[str, dict]] = []
        original_notify = app.notify

        def capture_notify(msg, **kwargs):
            notified.append((str(msg), kwargs))
            # Also call the original so the UI works.
            try:
                original_notify(msg, **kwargs)
            except Exception:
                pass

        app.notify = capture_notify  # type: ignore[method-assign]

        # Start the approval prompt (it will block on queue.get()).
        import asyncio

        task = asyncio.create_task(app._approval_prompt("bash", {"cmd": "rm -rf /"}))
        await pilot.pause()

        # The prompt should have called notify already.
        assert len(notified) >= 1
        assert any("Approve: bash" in n for n, _ in notified)
        header = app.query_one("#header", Static)
        assert "COOP: ON (PENDING)" in str(header.content)
        assert "awaiting approval" in str(header.content)
        # Verify timeout=8.0 is passed.
        # Find the actual call with the "Approve:" message
        approve_call = [kwargs for msg, kwargs in notified if "Approve: bash" in msg]
        assert len(approve_call) >= 1
        assert approve_call[0].get("timeout") == 8.0

        # Answer the prompt to unblock.
        assert app._approval_queue is not None
        app._approval_queue.put_nowait(("yes", ""))
        await task
        assert app._approval_pending is None


@pytest.mark.asyncio
async def test_tui_ask_user_event_toasts(tmp_path: Path):
    """ask_user event must trigger a toast notification."""
    from one.modes.tui_mode import SessionEvent, _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        notified: list[str] = []
        original_notify = app.notify

        def capture_notify(msg, **kwargs):
            notified.append(str(msg))
            try:
                original_notify(msg, **kwargs)
            except Exception:
                pass

        app.notify = capture_notify  # type: ignore[method-assign]

        # Fire the ask_user event directly.
        await app.on_session_event(
            SessionEvent(
                {
                    "type": "ask_user",
                    "id": "q1",
                    "question": "What is your name?",
                }
            )
        )
        await pilot.pause()
        assert any("Agent waiting for response" in n for n in notified)
        assert app._ask_user_pending is not None
