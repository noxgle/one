"""Golden-file snapshot tests for the TUI stream.

These tests drive the real ``_OneTextualApp`` headlessly (``run_test``) and
compare the rendered ``_stream_lines`` against committed fixtures. They pin
the full-turn render contract: user block, tool blocks, assistant block and
the blank-line separation between them. Update the fixtures only when the
render intentionally changes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager

_SNAPSHOTS = Path(__file__).parent / "snapshots"


class _Loader:
    cwd = "/tmp/project"

    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
        return "You are a coding agent."


class _Provider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict],
        thinking_level: str,
        headers: dict[str, str] | None = None,
    ) -> object:
        from one.providers.base import ChatResult

        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ChatResult(text=self.responses[idx], raw={}, usage={}, stop_reason="stop")


def _mk_app_session(tmp_path: Path) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory(
        {"tools": {"maxSteps": 4, "timeoutSec": 5}, "retry": {"enabled": False}}
    )
    session_manager = SessionManager.in_memory(str(tmp_path))
    return AgentSession(session_manager, settings, registry, _Loader(), model, "medium", tools=["read"])


async def _wait_idle(pilot, session: AgentSession, timeout_iters: int = 300) -> None:
    for _ in range(timeout_iters):
        if not session.is_streaming:
            break
        await pilot.pause(0.02)
    await pilot.pause()


@pytest.mark.asyncio
async def test_tui_full_turn_snapshot(tmp_path: Path):
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    (tmp_path / "a.txt").write_text("AAA\n", encoding="utf-8")
    session = _mk_app_session(tmp_path)
    session.providers = {"openai": _Provider(['{"tool":"read","args":{"path":"a.txt"}}', "DONE"])}
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input", TextArea)
        input_widget.text = "check the file"
        await input_widget.action_submit()
        await _wait_idle(pilot, session)

        expected = json.loads((_SNAPSHOTS / "tui_full_turn.json").read_text(encoding="utf-8"))
        assert app._stream_lines == expected


@pytest.mark.asyncio
async def test_tui_clear_resets_stream(tmp_path: Path):
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input", TextArea)
        input_widget.text = "/clear"
        await input_widget.action_submit()
        await pilot.pause()

        assert app._stream_lines == []
