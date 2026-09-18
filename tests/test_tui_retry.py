from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from one.modes.tui_mode import (
    _THINKING_MARK,
)
from tests.support.tui import _mk_app_session, _submit, _visible_text_area_text


class _ErrorProvider:
    async def chat(self, api_key, model, messages, thinking_level, headers=None, on_delta=None, on_thinking_delta=None, max_tokens=None, images=None, storage_dir=""):
        raise RuntimeError("All connection attempts failed")


class _RetryingProvider:
    """Provider that emits deltas then raises on *failures_left* attempts."""

    def __init__(
        self,
        deltas: list[str],
        failures_left: int = 1,
        success_text: str = "",
    ) -> None:
        self.deltas = deltas
        self.failures_left = failures_left
        self.success_text = success_text
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
        from one.providers.base import ChatResult

        self.calls += 1
        # Always emit deltas so the session can render them.
        for d in self.deltas:
            if on_delta:
                on_delta(d)
            await asyncio.sleep(0.005)
        if self.failures_left > 0:
            self.failures_left -= 1
            raise RuntimeError("connection reset")
        return ChatResult(text=self.success_text, raw={}, usage={}, stop_reason="stop")


@pytest.mark.asyncio
async def test_tui_thinking_batches_preserve_output_and_retry_boundary(tmp_path: Path) -> None:
    """Thinking storms are coalesced without losing order or queued input."""
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp
    from one.providers.base import ChatResult

    thinking_emitted = asyncio.Event()
    release = asyncio.Event()

    class _ThinkingThenRetryProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, *args, on_delta=None, on_thinking_delta=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                for index in range(200):
                    assert on_thinking_delta is not None
                    on_thinking_delta(f"think-{index};")
                    await asyncio.sleep(0)
                thinking_emitted.set()
                await release.wait()
                assert on_delta is not None
                on_delta("partial-retry-output")
                raise RuntimeError("retry boundary")
            if on_delta is not None:
                on_delta("final-retry-output")
            return ChatResult(text="final-retry-output", raw={}, usage={}, stop_reason="stop")

    session = _mk_app_session(
        tmp_path,
        runtime_key="sk-test",
        settings_override={"retry": {"enabled": True, "maxRetries": 1, "baseDelayMs": 1, "maxDelayMs": 1}},
    )
    provider = _ThinkingThenRetryProvider()
    session.providers = {"openai": provider}
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await _submit(app, pilot, "first")
        for _ in range(300):
            await pilot.pause()
            if thinking_emitted.is_set() and "think-199;" in app._thinking_buffer:
                break
        assert thinking_emitted.is_set()
        await pilot.press(*"queued during thinking")
        input_widget = app.query_one("#input", TextArea)
        await pilot.pause()
        assert "queued during thinking" in _visible_text_area_text(input_widget)
        await pilot.press("enter")
        await pilot.pause()
        assert "queued during thinking" in session.get_pending_queues()["steering"]
        release.set()
        for _ in range(300):
            await pilot.pause()
            if provider.calls == 2 and not session.is_streaming:
                break
        stream = "\n".join(app._stream_lines)
        assert "think-0;" in stream and "think-199;" in stream
        assert "partial-retry-output" not in stream
        assert stream.count("final-retry-output") == 1


@pytest.mark.asyncio
async def test_tui_error_turn_resets_spinner(tmp_path: Path):
    """A turn ending with an error must not leave the 'Ctrl+C abort' spinner running forever."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path, runtime_key="sk-test")
    session.providers = {"openai": _ErrorProvider()}
    session.settings_manager.set_retry_enabled(False)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "check server")
        # Wait until the error turn fully settles.
        for _ in range(100):
            await pilot.pause()
            if not app._turn_active and not session.is_streaming:
                break
        stream = "\n".join(app._stream_lines)
        assert "All connection attempts failed" in stream
        assert app._turn_active is False
        assert not any(line.startswith(_THINKING_MARK) for line in app._stream_lines)


@pytest.mark.asyncio
async def test_tui_ctrl_c_after_finished_error_turn(tmp_path: Path):
    """Ctrl+C after a finished (error) turn must acknowledge and keep the spinner dead."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path, runtime_key="sk-test")
    session.providers = {"openai": _ErrorProvider()}
    session.settings_manager.set_retry_enabled(False)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "check server")
        for _ in range(100):
            await pilot.pause()
            if not app._turn_active:
                break
        await pilot.press("ctrl+c")
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "[abort requested]" in stream
        assert app._turn_active is False
        assert not any(line.startswith(_THINKING_MARK) for line in app._stream_lines)


@pytest.mark.asyncio
async def test_tui_retry_sidebar_on_off(tmp_path: Path):
    """Sidebar Retry field reflects session.auto_retry_enabled (on/off),
    not the transient _retry_state."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Default is enabled (on).
        assert app.session.settings_manager.get_retry_enabled() is True
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        s = str(sidebar.content)
        assert "Retry: on" in s

        # Toggle off.
        app.session.set_auto_retry_enabled(False)
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        s = str(sidebar.content)
        assert "Retry: off" in s

        # Toggle on again.
        app.session.set_auto_retry_enabled(True)
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        s = str(sidebar.content)
        assert "Retry: on" in s


@pytest.mark.asyncio
async def test_tui_cycle_retry_mode_action_transitions(tmp_path: Path):
    """Ctrl+R (action_cycle_retry_mode) cycles off → on → unlimited → off."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Start: default is "on"
        assert session.settings_manager.get_retry_mode() == "on"

        # First cycle: on → unlimited
        app.action_cycle_retry_mode()
        await pilot.pause()
        assert session.settings_manager.get_retry_mode() == "unlimited"

        # Second cycle: unlimited → off
        app.action_cycle_retry_mode()
        await pilot.pause()
        assert session.settings_manager.get_retry_mode() == "off"

        # Third cycle: off → on
        app.action_cycle_retry_mode()
        await pilot.pause()
        assert session.settings_manager.get_retry_mode() == "on"


@pytest.mark.asyncio
async def test_tui_ctrl_r_cycle_retry_mode_via_pilot(tmp_path: Path):
    """ctrl+r keypress triggers action_cycle_retry_mode via pilot."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Start: default is "on"
        assert session.settings_manager.get_retry_mode() == "on"

        # Press ctrl+r: on → unlimited
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert session.settings_manager.get_retry_mode() == "unlimited"

        # Press ctrl+r again: unlimited → off
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert session.settings_manager.get_retry_mode() == "off"

        # Press ctrl+r again: off → on
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert session.settings_manager.get_retry_mode() == "on"


@pytest.mark.asyncio
async def test_tui_retry_unlimited_accepted(tmp_path: Path):
    """/retry unlimited is accepted and persisted."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/retry unlimited")
        stream = "\n".join(app._stream_lines)
        assert "Auto-retry set to unlimited." in stream
        assert session.settings_manager.get_retry_mode() == "unlimited"
        assert session.settings_manager.get_retry_enabled() is True


@pytest.mark.asyncio
async def test_tui_retry_sidebar_shows_unlimited_mode(tmp_path: Path):
    """Sidebar Retry field reflects the actual retry mode string (off/on/unlimited)."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Start: "on"
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        s = str(sidebar.content)
        assert "Retry: on" in s

        # Set to unlimited
        session.settings_manager.set_retry_mode("unlimited")
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        s = str(sidebar.content)
        assert "Retry: unlimited" in s

        # Set to off
        session.settings_manager.set_retry_mode("off")
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        s = str(sidebar.content)
        assert "Retry: off" in s


@pytest.mark.asyncio
async def test_tui_retry_discards_partial_block_one_fail(tmp_path: Path):
    """Provider streams a multiline response, then raises on attempt 1.
    The retry succeeds.  Each distinctive line must appear EXACTLY ONCE
    in the final stream — no 2× duplication.

    Note: _format_chat_panel wraps long lines, so we search for unique
    substrings that identify each line (the full line may be split across
    two wrapped lines with a newline in between).
    """
    from one.modes.tui_mode import _OneTextualApp

    # Docker/Playwright-style response with distinctive lines.
    # Use short, unique tokens that survive wrapping.
    lines = [
        "docker ps",
        "CONTAINER  IMAGE          STATUS",
        "docker logs -f playwright-mcp",
        "http://localhost:8931/sse",
    ]
    # Unique substrings that identify each line.
    markers = [
        "docker ps",
        "CONTAINER  IMAGE",
        "playwright-mcp",
        "localhost:8931",
    ]
    # Interleave newlines to simulate realistic streaming.
    deltas = []
    for line in lines:
        deltas.append(line)
        deltas.append("\n")

    session = _mk_app_session(tmp_path, runtime_key="sk-test", settings_override={"retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 5, "maxDelayMs": 50}})
    session.providers = {"openai": _RetryingProvider(deltas, failures_left=1)}
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "run")
        # Wait for the turn to settle.
        for _ in range(300):
            await pilot.pause()
            if not session.is_streaming and not app._turn_active:
                break
        stream = "\n".join(app._stream_lines)
        # Every distinctive marker must appear exactly once.
        for marker in markers:
            count = stream.count(marker)
            assert count == 1, f"Expected '{marker}' exactly once, found {count} times in stream"
        # The retry indicator must be present.
        assert "[retry]" in stream or "retry" in stream.lower()
        # Final answer visible.
        assert "localhost:8931" in stream

@pytest.mark.asyncio
async def test_tui_retry_discards_partial_block_two_fails(tmp_path: Path):
    """Provider fails TWICE then succeeds on attempt 3 (the reported
    2-3× duplication scenario).  Each line must still appear EXACTLY ONCE."""
    from one.modes.tui_mode import _OneTextualApp

    lines = [
        "docker ps",
        "CONTAINER  IMAGE          STATUS",
        "docker logs -f playwright-mcp",
        "http://localhost:8931/sse",
    ]
    markers = [
        "docker ps",
        "CONTAINER  IMAGE",
        "playwright-mcp",
        "localhost:8931",
    ]
    deltas = []
    for line in lines:
        deltas.append(line)
        deltas.append("\n")

    session = _mk_app_session(tmp_path, runtime_key="sk-test", settings_override={"retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 5, "maxDelayMs": 50}})
    session.providers = {"openai": _RetryingProvider(deltas, failures_left=2)}
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "run")
        for _ in range(500):
            await pilot.pause()
            if not session.is_streaming and not app._turn_active:
                break
        stream = "\n".join(app._stream_lines)
        for marker in markers:
            count = stream.count(marker)
            assert count == 1, (
                f"Expected '{marker}' exactly once after 2 retries, "
                f"found {count} times in stream"
            )
        # The retry indicator must be present.
        assert "retry" in stream.lower()
        # Final answer visible.
        assert "localhost:8931" in stream
