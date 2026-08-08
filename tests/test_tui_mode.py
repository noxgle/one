from __future__ import annotations

from pathlib import Path

import pytest

from one.cli.args import parse_args
from one.modes.tui_mode import (
    BUILTIN_TUI_THEMES,
    _THINKING_FRAMES,
    advance_thinking_frame,
    build_sidebar_snapshot,
    evaluate_waiting,
    resolve_tui_theme,
)


class _DummyModel:
    provider = "openrouter"
    id = "google/gemma-4-31b-it:free"


class _DummySessionManager:
    cwd = "/tmp/project"


class _DummySession:
    def __init__(self) -> None:
        self.model = _DummyModel()
        self.thinking_level = "medium"
        self.session_manager = _DummySessionManager()
        self.session_id = "s-test"
        self.is_streaming = False
        self.is_compacting = False

    def get_context_usage(self) -> dict:
        return {"percent": 12.5}

    def get_pending_queues(self) -> dict:
        return {"steering": [1, 2], "followUp": [3]}

    def get_session_stats(self) -> dict:
        return {
            "tokens": {
                "input": 100,
                "output": 50,
                "cacheRead": 10,
                "cacheWrite": 5,
                "total": 165,
            },
            "cost": 0.0123,
        }


def test_parse_args_accepts_tui_mode() -> None:
    parsed = parse_args(["--mode", "tui"])
    assert parsed.errors == []
    assert parsed.mode == "tui"


def test_resolve_tui_theme_includes_fallout() -> None:
    fallout = resolve_tui_theme("fallout")
    assert fallout.name == "fallout"
    assert "fallout" in BUILTIN_TUI_THEMES
    assert resolve_tui_theme("no-such-theme").name == "default"


def test_build_sidebar_snapshot_contains_runtime_details() -> None:
    snapshot = build_sidebar_snapshot(
        _DummySession(),
        retry_state="retry-1",
        last_tool_error="bash: timeout",
        last_provider_error="400 Bad Request",
    )

    assert snapshot["model"] == "openrouter/google/gemma-4-31b-it:free"
    assert snapshot["thinking"] == "medium"
    assert snapshot["cwd"] == "/tmp/project"
    assert snapshot["contextPercent"] == 12.5
    assert snapshot["queueSteer"] == 2
    assert snapshot["queueFollow"] == 1
    assert snapshot["queueTotal"] == 3
    assert snapshot["tokenInput"] == 100
    assert snapshot["tokenOutput"] == 50
    assert snapshot["tokenTotal"] == 165
    assert snapshot["cost"] == 0.0123
    assert snapshot["coop"] == "off"
    assert snapshot["lastToolError"] == "bash: timeout"
    assert snapshot["lastProviderError"] == "400 Bad Request"


def test_thinking_frames_are_single_width() -> None:
    assert len(_THINKING_FRAMES) >= 2
    # Each braille frame must occupy exactly one terminal cell.
    for frame in _THINKING_FRAMES:
        assert len(frame) == 1


def test_evaluate_waiting_rule() -> None:
    now = 100.0
    # No active turn -> never waiting.
    assert evaluate_waiting(False, now - 10.0, now) is False
    # Active turn, deltas streaming recently -> not waiting.
    assert evaluate_waiting(True, now - 0.2, now) is False
    # Active turn, no deltas for a while -> waiting (first token / between
    # tool calls / tool execution / retry delay).
    assert evaluate_waiting(True, now - 2.0, now) is True
    # Custom idle threshold.
    assert evaluate_waiting(True, now - 0.5, now, idle_threshold=0.4) is True


def test_advance_thinking_frame_wraps() -> None:
    n = len(_THINKING_FRAMES)
    assert advance_thinking_frame(0) == 1
    assert advance_thinking_frame(n - 1) == 0
    # A few steps keep cycling.
    frame = 0
    for _ in range(3 * n):
        frame = advance_thinking_frame(frame)
    assert frame == 0


# ---------------------------------------------------------------------------
# Headless TUI app tests (extension UI flow).
# ---------------------------------------------------------------------------


class _FakeLoader:
    cwd = "/tmp/project"

    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
        return "You are a coding agent."


def _mk_app_session(tmp_path: Path):
    from one.core.agent_session import AgentSession
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.settings_manager import SettingsManager

    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session_manager = SessionManager.in_memory(str(tmp_path))
    return AgentSession(session_manager, settings, registry, _FakeLoader(), model, "medium")


@pytest.mark.asyncio
async def test_extension_ui_request_renders_and_answer_routes(tmp_path: Path):
    from textual.widgets import Input

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        req = session.request_extension_ui(extension="test-ext", ui_type="widget", payload={"q": 1}, title="Wizard")
        await pilot.pause()

        assert app._extension_ui_pending_request is not None
        stream = "\n".join(app._stream_lines)
        assert "test-ext (widget)" in stream
        assert "Wizard" in stream
        assert '"q": 1' in stream

        input_widget = app.query_one("#input", Input)
        input_widget.value = '{"answer": 42}'
        await input_widget.action_submit()
        await pilot.pause()

        assert app._extension_ui_pending_request is None
        assert session._extension_ui_history[-1]["payload"] == {"answer": 42}
        assert session._extension_ui_history[-1]["requestId"] == req["id"]
        stream = "\n".join(app._stream_lines)
        assert f"[ExtUI] response {req['id']} cancelled=False" in stream


@pytest.mark.asyncio
async def test_extension_ui_cancel_via_empty_input(tmp_path: Path):
    from textual.widgets import Input

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        req = session.request_extension_ui(extension="x", ui_type="overlay", payload={})
        await pilot.pause()
        assert app._extension_ui_pending_request is not None

        input_widget = app.query_one("#input", Input)
        input_widget.value = ""
        await input_widget.action_submit()
        await pilot.pause()

        assert app._extension_ui_pending_request is None
        assert session._extension_ui_history[-1]["cancelled"] is True
        assert session._extension_ui_history[-1]["requestId"] == req["id"]


@pytest.mark.asyncio
async def test_extension_ui_external_response_clears_pending(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        req = session.request_extension_ui(extension="x", ui_type="widget", payload={})
        await pilot.pause()
        assert app._extension_ui_pending_request is not None

        session.respond_extension_ui(request_id=req["id"], payload={"ok": True})
        await pilot.pause()

        assert app._extension_ui_pending_request is None
        stream = "\n".join(app._stream_lines)
        assert f"[ExtUI] response {req['id']} cancelled=False" in stream
