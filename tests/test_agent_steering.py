from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.tools.index import ToolDef, all_tools
from tests.support.agents import _FakeProvider, _Loader


class _SlowProvider:
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

        await asyncio.sleep(0.1)
        return ChatResult(text="DONE", raw={}, usage={}, stop_reason="stop")


class _FailThenStableProvider:
    def __init__(self, fail_calls: int = 1, success_delay_sec: float = 0.0) -> None:
        self.fail_calls = fail_calls
        self.success_delay_sec = success_delay_sec
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

        self.calls += 1
        if self.calls <= self.fail_calls:
            raise RuntimeError("transient")
        if self.success_delay_sec > 0:
            await asyncio.sleep(self.success_delay_sec)
        return ChatResult(text="OK", raw={}, usage={}, stop_reason="stop")


class _RecordingProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.requests: list[list[dict[str, Any]]] = []

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

        self.requests.append(messages)
        return ChatResult(text=self.responses[len(self.requests) - 1], raw={}, usage={}, stop_reason="stop")


class _TimeoutProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def chat(self, **kwargs: Any) -> Any:
        self.started.set()
        await asyncio.Event().wait()


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
async def test_abort_does_not_drop_queued_messages(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"retry": {"enabled": False}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium")
    agent.providers = {"openai": _SlowProvider()}

    task = asyncio.create_task(agent.prompt("long task"))
    await asyncio.sleep(0.02)
    await agent.steer("next-a")
    await agent.follow_up("next-b")
    await agent.abort()
    await task

    queues = agent.get_pending_queues()
    assert queues["steering"] == ["next-a"]
    assert queues["followUp"] == ["next-b"]


@pytest.mark.asyncio
async def test_steering_is_injected_fifo_between_tool_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Steers received during a tool run reach the immediately following requests."""
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    agent = AgentSession(
        SessionManager.in_memory(str(tmp_path)),
        SettingsManager.in_memory({"retry": {"enabled": False}, "tools": {"maxSteps": 4}}),
        registry,
        _Loader(),
        model,
        "medium",
        tools=["bash"],
    )
    provider = _RecordingProvider(
        [
            '{"tool":"bash","args":{"command":"first"}}',
            '{"tool":"bash","args":{"command":"second"}}',
            "DONE",
        ]
    )
    agent.providers = {"openai": provider}
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    runs = 0

    async def blocking_bash(cwd: str, command: str, timeout: int, prefix: str) -> dict[str, Any]:
        nonlocal runs
        runs += 1
        if runs == 1:
            first_started.set()
            await release_first.wait()
        return {"ok": True, "result": command}

    monkeypatch.setitem(all_tools, "bash", ToolDef("bash", "blocking test tool", blocking_bash))
    task = asyncio.create_task(agent.prompt("start"))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await agent.steer("first steer")
    await agent.steer("second steer")
    release_first.set()
    await task

    request_users = [[m["content"] for m in request if m["role"] == "user"] for request in provider.requests]
    assert request_users[0] == ["start"]
    assert request_users[1][-1] == "first steer"
    assert "second steer" not in request_users[1]
    assert request_users[2][-1] == "second steer"
    assert request_users[2].index("first steer") < request_users[2].index("second steer")
    assert agent.get_pending_queues() == {"steering": [], "followUp": []}


@pytest.mark.asyncio
async def test_terminal_provider_timeout_keeps_steering_queue(tmp_path: Path) -> None:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    agent = AgentSession(
        SessionManager.in_memory(str(tmp_path)),
        SettingsManager.in_memory({"retry": {"enabled": False}, "providers": {"timeoutSec": 1}}),
        registry,
        _Loader(),
        model,
        "medium",
    )
    provider = _TimeoutProvider()
    agent.providers = {"openai": provider}
    task = asyncio.create_task(agent.prompt("start"))
    await asyncio.wait_for(provider.started.wait(), timeout=1)
    await agent.steer("keep after timeout")
    await task

    assert agent.get_pending_queues() == {"steering": ["keep after timeout"], "followUp": []}


@pytest.mark.asyncio
async def test_retry_success_then_queue_drains_once_without_duplication(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"retry": {"enabled": True, "maxRetries": 2, "baseDelayMs": 1, "maxDelayMs": 1}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium")
    provider = _FailThenStableProvider(fail_calls=1, success_delay_sec=0.03)
    agent.providers = {"openai": provider}

    task = asyncio.create_task(agent.prompt("start"))
    await asyncio.sleep(0.005)
    await agent.steer("s1")
    await agent.follow_up("f1")
    await task

    user_messages = [m for m in agent.messages if m.get("role") == "user"]
    contents = [m.get("content") for m in user_messages]
    assert contents.count("start") == 1
    assert contents.count("s1") == 1
    assert contents.count("f1") == 1
    assert agent.get_pending_queues() == {"steering": [], "followUp": []}


@pytest.mark.asyncio
async def test_finish_does_not_auto_drain_queued_messages(tmp_path: Path) -> None:
    """A terminal finish leaves messages queued for an explicit next turn."""
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"retry": {"enabled": False}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium")
    provider = _FakeProvider(
        [
            '{"tool":"finish","args":{"summary":"done","goal_success":true}}',
            "queued response",
        ]
    )
    agent.providers = {"openai": provider}

    await agent.steer("do not run automatically either")
    await agent.follow_up("do not run automatically")
    await agent.prompt("complete this task")

    assert provider.calls == 1
    assert agent.get_pending_queues() == {
        "steering": ["do not run automatically either"],
        "followUp": ["do not run automatically"],
    }
