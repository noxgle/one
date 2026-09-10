from __future__ import annotations

import json
import time
from collections.abc import Callable
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
        on_delta: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        max_tokens: int | None = None,
        images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
    ) -> Any:
        from one.providers.base import ChatResult

        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ChatResult(text=self.responses[idx], raw={}, usage={}, stop_reason="stop")


class _ProviderWithUsage:
    """Provider that returns a response with specific usage counts."""

    def __init__(self, responses: list[str], usage: dict[str, int] | None = None) -> None:
        self.responses = responses
        self.usage = usage or {"prompt_tokens": 0, "completion_tokens": 0}
        self.calls = 0

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        max_tokens: int | None = None,
        images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
    ) -> Any:
        from one.providers.base import ChatResult

        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ChatResult(
            text=self.responses[idx],
            raw={},
            usage={"prompt_tokens": self.usage["prompt_tokens"], "completion_tokens": self.usage["completion_tokens"]},
            stop_reason="stop",
        )


def _mk_agent(
    tmp_path: Path,
    tools: list[str] | None = None,
    settings_override: dict[str, Any] | None = None,
) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory(settings_override or {"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    return AgentSession(session, settings, registry, _Loader(), model, "medium", tools=tools)


def test_budget_settings_defaults() -> None:
    """Budget limits default to 0 (unlimited)."""
    settings = SettingsManager.in_memory()
    assert settings.get_budget_max_tokens() == 0
    assert settings.get_budget_max_time_sec() == 0


def test_budget_settings_from_file(tmp_path: Path) -> None:
    """Budget values read from settings.json override defaults."""
    settings_dir = tmp_path / ".one"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps({"budget": {"maxTokens": 5000, "maxTimeSec": 120}}),
        encoding="utf-8",
    )
    settings = SettingsManager.create(cwd=str(tmp_path), agent_dir=str(settings_dir))
    assert settings.get_budget_max_tokens() == 5000
    assert settings.get_budget_max_time_sec() == 120


@pytest.mark.asyncio
async def test_token_budget_stops_turn(tmp_path: Path) -> None:
    """When total tokens exceed maxTokens, the turn ends with budget_exceeded."""
    agent = _mk_agent(tmp_path, settings_override={"tools": {"maxSteps": 4, "timeoutSec": 5}, "budget": {"maxTokens": 1}})
    provider = _ProviderWithUsage(
        responses=[
            json.dumps({"tool": "bash", "args": {"command": "echo hi"}}),
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 0},
    )
    agent.providers = {"openai": provider}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("do it")

    # The provider was called exactly once (step 1: 0 tokens < 1, makes tool call).
    assert provider.calls == 1

    # The final assistant message has stopReason "budget_exceeded".
    assistant_msgs = [m for m in agent.messages if m.get("role") == "assistant"]
    assert len(assistant_msgs) >= 1
    last_assistant = assistant_msgs[-1]
    assert last_assistant.get("stopReason") == "budget_exceeded"
    last_text = last_assistant.get("content", [{}])[0].get("text", "") if isinstance(last_assistant.get("content"), list) else ""
    assert "Token budget reached" in last_text

    # A budget_exceeded event was emitted.
    budget_events = [e for e in events if e.get("type") == "budget_exceeded"]
    assert len(budget_events) >= 1
    assert budget_events[-1]["kind"] == "token_budget"


@pytest.mark.asyncio
async def test_time_budget_stops_turn(tmp_path: Path) -> None:
    """When elapsed time exceeds maxTimeSec, the turn ends without calling the provider."""
    agent = _mk_agent(tmp_path, settings_override={"tools": {"maxSteps": 4, "timeoutSec": 5}, "budget": {"maxTimeSec": 1}})
    provider = _ProviderWithUsage(
        responses=[
            "final answer",
        ],
        usage={"prompt_tokens": 0, "completion_tokens": 0},
    )
    agent.providers = {"openai": provider}

    # Simulate that the session "started" 10 seconds ago.
    agent._session_started_at = time.monotonic() - 10

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("go")

    # The provider should NOT have been called.
    assert provider.calls == 0

    # The final assistant message has stopReason "budget_exceeded".
    assistant_msgs = [m for m in agent.messages if m.get("role") == "assistant"]
    assert len(assistant_msgs) >= 1
    last_assistant = assistant_msgs[-1]
    assert last_assistant.get("stopReason") == "budget_exceeded"
    last_text = last_assistant.get("content", [{}])[0].get("text", "") if isinstance(last_assistant.get("content"), list) else ""
    assert "Time budget reached" in last_text

    # A budget_exceeded event was emitted.
    budget_events = [e for e in events if e.get("type") == "budget_exceeded"]
    assert len(budget_events) >= 1
    assert budget_events[-1]["kind"] == "time_budget"
