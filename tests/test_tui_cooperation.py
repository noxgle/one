# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.support.tui import _mk_app_session, _submit


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
async def test_tui_disabling_cooperation_clears_pending_ask_user(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_toggle_cooperation()
        task = asyncio.create_task(session._ask_user({"question": "continue?"}))
        await pilot.pause()
        assert app._ask_user_pending is not None

        app.action_toggle_cooperation()
        await pilot.pause()
        assert app._ask_user_pending is None
        app._turn_active = True
        app._last_delta_ts = 0.0
        app._tick_waiting()
        assert "waiting for your response" not in "\n".join(app._stream_lines)
        await task


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

        # Approval is resolved only by its dedicated control.
        app.action_approve_tool()
        await task
        assert app._approval_pending is None


@pytest.mark.asyncio
async def test_tui_ctrl_c_rejects_pending_approval_without_aborting_session(tmp_path: Path) -> None:
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    abort_calls = 0

    async def abort() -> None:
        nonlocal abort_calls
        abort_calls += 1

    session.abort = abort  # type: ignore[method-assign]
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        approval = asyncio.create_task(app._approval_prompt("write", {"path": "secret.txt"}))
        await pilot.pause()
        assert app._approval_pending is not None

        await pilot.press("ctrl+c")

        assert await approval == (False, "aborted by user")
        assert abort_calls == 0


@pytest.mark.asyncio
async def test_tui_ctrl_c_without_pending_approval_aborts_session(tmp_path: Path) -> None:
    """The normal Ctrl+C binding remains active when no approval owns it."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    abort_calls = 0

    async def abort() -> None:
        nonlocal abort_calls
        abort_calls += 1

    session.abort = abort  # type: ignore[method-assign]
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert abort_calls == 1
        assert app._approval_pending is None


@pytest.mark.asyncio
async def test_tui_main_and_child_approvals_share_fifo_overlay(tmp_path: Path) -> None:
    """Main and child requests share one labeled fail-closed FIFO overlay."""
    from one.core.agent_session import AgentSession
    from one.core.session_manager import SessionManager
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        app.action_toggle_cooperation()
        callback = session.approval_callback
        assert callback is not None
        child = AgentSession(
            SessionManager.in_memory(str(tmp_path)),
            session.settings_manager,
            session.model_registry,
            session.resource_loader,
            session.model,
            session.thinking_level,
            tools=["write"],
            approval_callback=callback,
            cooperation_state=session._cooperation_state,
        )
        main_task = asyncio.create_task(session._run_tool_call("write", {"path": "main.txt", "content": "main"}))
        child_task = asyncio.create_task(child._run_tool_call("write", {"path": "child.txt", "content": "child"}))
        await pilot.pause()
        assert app._approval_pending is not None
        assert len(app._approval_waiters) == 2
        assert "Main agent" in str(app.query_one("#approval_summary").content)

        # Main prompt remains a normal prompt and cannot resolve either request.
        prompts: list[str] = []

        async def prompt(text: str, **_kwargs: object) -> None:
            prompts.append(text)

        session.prompt = prompt  # type: ignore[method-assign]
        await _submit(app, pilot, "ordinary prompt")
        assert prompts == ["ordinary prompt"]
        assert not child_task.done()
        await pilot.press("alt+a")
        assert (await asyncio.wait_for(main_task, timeout=2))["ok"] is True
        await pilot.pause()
        assert "Child " in str(app.query_one("#approval_summary").content)
        await pilot.press("alt+a")
        assert (await asyncio.wait_for(child_task, timeout=2))["ok"] is True


@pytest.mark.asyncio
async def test_tui_approval_controls_do_not_steal_normal_prompt_input(tmp_path: Path) -> None:
    from textual.containers import Vertical

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    prompts: list[str] = []

    async def prompt(text: str, **_kwargs: object) -> None:
        prompts.append(text)

    session.prompt = prompt  # type: ignore[method-assign]
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        approval = asyncio.create_task(app._approval_prompt("write", {"path": "x.txt", "content": "x"}))
        await pilot.pause()
        assert app.query_one("#approval_overlay", Vertical).has_class("visible")
        await _submit(app, pilot, "ordinary prompt")
        await pilot.pause()
        assert prompts == ["ordinary prompt"]
        assert not approval.done()
        await pilot.press("alt+a")
        assert await asyncio.wait_for(approval, timeout=2) == (True, "")


@pytest.mark.asyncio
async def test_tui_approval_reason_uses_its_own_field_and_enter_is_inert(tmp_path: Path) -> None:
    """Typing a reason, including Enter, cannot submit a prompt or approve."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    prompts: list[str] = []

    async def prompt(text: str, **_kwargs: object) -> None:
        prompts.append(text)

    session.prompt = prompt  # type: ignore[method-assign]
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        approval = asyncio.create_task(app._approval_prompt("write", {"path": "x.txt"}))
        await pilot.pause()
        await pilot.click("#approval-reason")
        await pilot.press(*"keep it")
        await pilot.press("enter")
        await pilot.press(*"unchanged")
        assert not approval.done()
        assert prompts == []
        await pilot.press("alt+r")
        assert await approval == (False, "keep itunchanged")


@pytest.mark.asyncio
async def test_tui_approval_reason_resets_for_next_request_but_not_theme_refresh(tmp_path: Path) -> None:
    """A theme refresh preserves a current draft; FIFO promotion starts blank."""
    from one.modes.tui_mode import _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test() as pilot:
        first = asyncio.create_task(app._approval_prompt("write", {"path": "one"}))
        second = asyncio.create_task(app._approval_prompt("bash", {"command": "two"}))
        await pilot.pause()
        await pilot.click("#approval-reason")
        await pilot.press(*"first reason")
        assert app.query_one("#approval-reason").value == "first reason"
        assert app._apply_theme("fallout")
        assert app.query_one("#approval-reason").value == "first reason"
        await pilot.press("alt+r")
        assert await first == (False, "first reason")
        await pilot.pause()
        assert app.query_one("#approval-reason").value == ""
        await pilot.press("alt+r")
        assert await second == (False, "Rejected by user")


@pytest.mark.asyncio
async def test_tui_approval_queue_serializes_and_cooperation_off_rejects_all(tmp_path: Path) -> None:
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        session.approval_callback = app._approval_prompt
        first = asyncio.create_task(app._approval_prompt("write", {"path": "one"}))
        second = asyncio.create_task(app._approval_prompt("bash", {"command": "two"}))
        await pilot.pause()
        assert app._approval_pending is not None
        assert app._approval_pending["tool"] == "write"
        assert len(app._approval_waiters) == 2
        await pilot.press("alt+a")
        assert await first == (True, "")
        await pilot.pause()
        assert app._approval_pending is not None
        assert app._approval_pending["tool"] == "bash"
        app.action_toggle_cooperation()
        assert await second == (False, "Cooperation disabled")
        assert app._approval_pending is None


@pytest.mark.asyncio
async def test_tui_rebind_rejects_queued_approvals_and_old_callback(tmp_path: Path) -> None:
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    replacement = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        app.action_toggle_cooperation()
        old_callback = session.approval_callback
        assert old_callback is not None
        approval = asyncio.ensure_future(old_callback("write", {"path": "one"}))
        queued = asyncio.ensure_future(old_callback("bash", {"command": "two"}))
        await pilot.pause()
        futures = [pending["future"] for pending in app._approval_waiters]
        assert app._bound_session is session
        assert len(futures) == 2

        app.session = replacement
        app._bind_session()
        assert [future.result() for future in futures] == [(False, "Session changed")] * 2
        assert await approval == (False, "Session changed")
        assert await queued == (False, "Session changed")
        assert app._approval_pending is None
        assert await asyncio.wait_for(old_callback("write", {"path": "late"}), timeout=1) == (False, "Session changed")


@pytest.mark.asyncio
async def test_tui_unmount_rejects_queued_approvals_and_hides_overlay(tmp_path: Path) -> None:
    from textual.containers import Vertical

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        first = asyncio.create_task(app._approval_prompt("write", {"path": "one"}))
        second = asyncio.create_task(app._approval_prompt("bash", {"command": "two"}))
        await pilot.pause()
        assert app.query_one("#approval_overlay", Vertical).has_class("visible")
        futures = [pending["future"] for pending in app._approval_waiters]
        assert len(futures) == 2

        app.on_unmount()
        assert [future.result() for future in futures] == [(False, "TUI closed")] * 2
        assert await first == (False, "TUI closed")
        assert await second == (False, "TUI closed")
        assert app._approval_pending is None
        assert not app.query_one("#approval_overlay", Vertical).has_class("visible")


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
