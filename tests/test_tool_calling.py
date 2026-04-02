from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager


class _Loader:
    def get_system_prompt(self) -> str:
        return "You are a coding agent."


class _FakeProvider:
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
    ) -> Any:
        from one.providers.base import ChatResult

        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ChatResult(text=self.responses[idx], raw={}, usage={}, stop_reason="stop")


@pytest.mark.asyncio
async def test_tool_calling_multistep_cycle(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["read", "ls"])
    agent.providers = {
        "openai": _FakeProvider(
            [
                '{"tool":"read","args":{"path":"a.txt"}}',
                "DONE",
            ]
        )
    }

    seen_turn_end: dict[str, Any] = {}

    def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "turn_end":
            seen_turn_end.update(event)

    agent.subscribe(on_event)
    await agent.prompt("Read file and finish.")

    assert agent.get_last_assistant_text() == "DONE"
    assert len(seen_turn_end.get("toolResults", [])) == 1
    payload = seen_turn_end["toolResults"][0]
    assert payload["ok"] is True
    assert payload["tool"] == "read"
    assert "hello" in payload["result"]


@pytest.mark.asyncio
async def test_tool_calling_step_limit_message(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 2, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["ls"])
    agent.providers = {"openai": _FakeProvider(['{"tool":"ls","args":{"path":"."}}'])}

    await agent.prompt("Loop tools forever")
    assert "limit kroków" in (agent.get_last_assistant_text() or "")


@pytest.mark.asyncio
async def test_tool_timeout_surfaces_error(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 2, "timeoutSec": 1}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["bash"])
    agent.providers = {
        "openai": _FakeProvider(
            [
                '{"tool":"bash","args":{"command":"sleep 1 && echo ok","timeout":0.01}}',
                "done",
            ]
        )
    }

    await agent.prompt("Run command")
    # Assistant finishes second step; tool result should carry timeout error.
    tool_results = [m for m in agent.messages if m.get("role") == "toolResult"]
    assert tool_results
    assert "timeout" in tool_results[0]["content"].lower()
