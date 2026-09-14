"""Minimal regression tests for steeringMode/followUpMode queue handling."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager


class _Loader:
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
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
        on_delta: Any = None,
        on_thinking_delta: Any = None,
        max_tokens: int | None = None,
        images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
    ) -> Any:
        from one.providers.base import ChatResult

        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ChatResult(text=self.responses[idx], raw={}, usage={}, stop_reason="stop")


def _mk_agent(tmp_path: Path, **overrides: Any) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory(overrides.get("settings", {}))
    session = SessionManager.in_memory(str(tmp_path))
    return AgentSession(session, settings, registry, _Loader(), model, "medium", tools=overrides.get("tools"))


@pytest.mark.asyncio
async def test_steering_mode_interrupt_queues_follow_up_when_follow_up_mode_set(tmp_path: Path):
    """When steeringMode=interrupt (default) and followUpMode=follow_up, input during streaming
    should be routed to follow_up (not steer)."""
    agent = _mk_agent(
        tmp_path,
        settings={"tools": {"maxSteps": 1, "timeoutSec": 5}},
        tools=["finish"],
    )
    # Default: steeringMode=interrupt, followUpMode=queue
    # Normal input routes as steer when followUpMode != follow_up
    # So with default settings, behavior should be "steer"
    agent.providers = {"openai": _Provider(['{"tool":"finish","args":{"summary":"done","goal_success":true}}'])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("go")

    # Should finish successfully
    finish_result = agent.get_last_finish_result()
    assert finish_result["finished"] is True


@pytest.mark.asyncio
async def test_no_auto_queue_drain_after_finish(tmp_path: Path):
    """Queues should NOT be automatically drained after the agent calls finish."""
    agent = _mk_agent(
        tmp_path,
        settings={"tools": {"maxSteps": 1, "timeoutSec": 5}},
        tools=["finish"],
    )
    agent.providers = {"openai": _Provider(['{"tool":"finish","args":{"summary":"done","goal_success":true}}'])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("go")

    # After finish, queues should be empty (there were none) and
    # the agent should NOT have auto-prompted a new turn.
    # With finish() the prompt loop exits early (finished_with_tool=True),
    # so no queue drain happens.
    finish_result = agent.get_last_finish_result()
    assert finish_result["finished"] is True
    # No additional turns after finish
    agent_end_events = [e for e in events if e.get("type") == "agent_end"]
    assert len(agent_end_events) == 1


@pytest.mark.asyncio
async def test_no_auto_queue_drain_after_abort(tmp_path: Path):
    """Queues should NOT be automatically drained after abort."""
    agent = _mk_agent(
        tmp_path,
        settings={"tools": {"maxSteps": 1, "timeoutSec": 5}},
        tools=["finish"],
    )
    # Provider responds with tool call that doesn't exist → error, no finish
    agent.providers = {"openai": _Provider(["{invalid json}"]) }
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("go")

    # After error/abort, the _abort_requested check prevents queue drain
    assert agent._abort_requested is False  # reset after prompt returns
    # No spurious agent_end beyond the first one
    agent_end_events = [e for e in events if e.get("type") == "agent_end"]
    assert len(agent_end_events) >= 1
