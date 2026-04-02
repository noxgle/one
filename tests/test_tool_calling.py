from __future__ import annotations

import asyncio
import json
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


class _FailingProvider:
    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
    ) -> Any:
        raise RuntimeError("provider down")


class _SlowProvider:
    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
    ) -> Any:
        from one.providers.base import ChatResult

        await asyncio.sleep(0.1)
        return ChatResult(text="DONE", raw={}, usage={}, stop_reason="stop")


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


@pytest.mark.asyncio
async def test_turn_end_emitted_on_non_retryable_error(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"retry": {"enabled": False}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium")
    agent.providers = {"openai": _FailingProvider()}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    await agent.prompt("fail now")

    turn_end = [e for e in events if e.get("type") == "turn_end"]
    assert turn_end
    assert turn_end[-1]["ok"] is False
    assert "provider down" in turn_end[-1]["error"]

    assert any(e.get("type") == "agent_end" for e in events)


@pytest.mark.asyncio
async def test_queue_and_active_tools_introspection(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory()
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["ls", "read"])

    assert agent.active_tools == ["ls", "read"]

    await agent.steer("a")
    await agent.follow_up("b")
    queues = agent.get_pending_queues()
    assert queues["steering"] == ["a"]
    assert queues["followUp"] == ["b"]


@pytest.mark.asyncio
async def test_tool_error_payload_contains_contract_fields(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 2, "timeoutSec": 1}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["ls"])
    agent.providers = {"openai": _FakeProvider(['{"tool":"read","args":{"path":"x"}}', "done"])}

    await agent.prompt("trigger disabled tool")
    tool_msg = next(m for m in agent.messages if m.get("role") == "toolResult")
    payload = json.loads(tool_msg["content"])
    assert payload["ok"] is False
    assert payload["tool"] == "read"
    assert payload["errorType"] == "RuntimeError"
    assert "disabled" in payload["error"].lower()


@pytest.mark.asyncio
async def test_abort_stops_turn_with_abort_message(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"retry": {"enabled": False}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium")
    agent.providers = {"openai": _SlowProvider()}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    task = asyncio.create_task(agent.prompt("run and abort"))
    await asyncio.sleep(0.02)
    await agent.abort()
    await task

    assert agent.get_last_assistant_text() == "Request aborted."
    turn_end = [e for e in events if e.get("type") == "turn_end"]
    assert turn_end
    assert turn_end[-1]["aborted"] is True


@pytest.mark.asyncio
async def test_tool_result_message_payload_is_capped_for_context(tmp_path: Path):
    big_text = "x" * 20000
    (tmp_path / "big.txt").write_text(big_text, encoding="utf-8")

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 2, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["read"])
    agent.providers = {"openai": _FakeProvider(['{"tool":"read","args":{"path":"big.txt"}}', "done"])}

    await agent.prompt("read big")
    tool_msg = next(m for m in agent.messages if m.get("role") == "toolResult")
    payload = json.loads(tool_msg["content"])
    result = payload["result"]
    assert len(result) <= 13000
    assert "truncated to 12000 chars" in result
