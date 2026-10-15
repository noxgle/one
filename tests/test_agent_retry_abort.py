# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from tests.support.agents import _Loader


class _FailingProvider:
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
        raise RuntimeError("provider down")


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


class _FlakyProvider:
    def __init__(self) -> None:
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
        if self.calls == 1:
            raise RuntimeError("temporary provider issue")
        return ChatResult(text="RECOVERED", raw={}, usage={}, stop_reason="stop")


class _SequenceProvider:
    """Offline provider that returns strings or raises queued exceptions."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def chat(self, *args: Any, **kwargs: Any) -> Any:
        from one.providers.base import ChatResult

        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return ChatResult(text=str(outcome), raw={}, usage={}, stop_reason="stop")


def _agent_with_retry(tmp_path: Path, retry: dict[str, Any]) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    return AgentSession(
        SessionManager.in_memory(str(tmp_path)),
        SettingsManager.in_memory({"retry": retry}),
        registry,
        _Loader(),
        model,
        "medium",
    )


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
    await agent.prompt("fail now", {"requestId": "exception-request"})

    turn_end = [e for e in events if e.get("type") == "turn_end"]
    assert turn_end
    assert turn_end[-1]["ok"] is False
    assert turn_end[-1]["reason"] == "error"
    assert "provider down" in turn_end[-1]["error"]

    agent_end = next(e for e in events if e.get("type") == "agent_end")
    assert agent_end["requestId"] == "exception-request"
    assert agent_end["turnId"] == "turn-1"
    assert agent._active_turn_id is None and agent._active_request_id is None


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

    task = asyncio.create_task(agent.prompt("run and abort", {"requestId": "abort-request"}))
    await asyncio.sleep(0.02)
    await agent.abort()
    await task

    assert agent.get_last_assistant_text() == "Request aborted."
    turn_end = [e for e in events if e.get("type") == "turn_end"]
    assert turn_end
    assert turn_end[-1]["aborted"] is True
    assert turn_end[-1]["reason"] == "abort"
    agent_end = next(e for e in events if e.get("type") == "agent_end")
    assert agent_end["requestId"] == "abort-request"
    assert agent_end["turnId"] == "turn-1"
    assert agent._active_turn_id is None and agent._active_request_id is None


@pytest.mark.asyncio
async def test_retry_then_success_emits_reason_completed(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"retry": {"enabled": True, "maxRetries": 2, "baseDelayMs": 1, "maxDelayMs": 1}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium")
    agent.providers = {"openai": _FlakyProvider()}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    await agent.prompt("retry once", {"requestId": "retry-request"})

    assert agent.get_last_assistant_text() == "RECOVERED"
    turn_ends = [e for e in events if e.get("type") == "turn_end"]
    assert len(turn_ends) >= 2
    assert turn_ends[0]["ok"] is False
    assert turn_ends[-1]["ok"] is True
    assert turn_ends[-1]["reason"] in {"stop", "completed"}
    agent_end = next(e for e in events if e.get("type") == "agent_end")
    assert agent_end["requestId"] == "retry-request"
    assert agent_end["turnId"] == "turn-1"
    assert agent._active_turn_id is None and agent._active_request_id is None


@pytest.mark.asyncio
async def test_retry_on_uses_finite_default_and_explicit_zero_disables(tmp_path: Path) -> None:
    agent = _agent_with_retry(tmp_path, {"enabled": True, "mode": "on", "baseDelayMs": 1, "maxDelayMs": 1})
    provider = _SequenceProvider([httpx.ConnectError("All connection attempts failed"), '{"tool":"finish","args":{"summary":"Recovered","goal_success":true}}'])
    agent.providers = {"openai": provider}
    await agent.prompt("recover")
    assert provider.calls == 2
    assert agent.get_last_assistant_text() == "Recovered"

    disabled = _agent_with_retry(tmp_path, {"enabled": True, "mode": "on", "maxRetries": 0, "baseDelayMs": 1, "maxDelayMs": 1})
    disabled_provider = _SequenceProvider([RuntimeError("connection failed"), "MUST NOT RUN"])
    disabled.providers = {"openai": disabled_provider}
    await disabled.prompt("do not recover")
    assert disabled_provider.calls == 1


@pytest.mark.asyncio
async def test_retry_on_default_exhausts_after_three_retries(tmp_path: Path) -> None:
    agent = _agent_with_retry(tmp_path, {"enabled": True, "mode": "on", "baseDelayMs": 1, "maxDelayMs": 1})
    provider = _SequenceProvider([RuntimeError("connection failed")] * 5)
    agent.providers = {"openai": provider}
    await agent.prompt("exhaust retries")
    # Initial request plus the documented finite default of three retries.
    assert provider.calls == 4


@pytest.mark.asyncio
async def test_retry_unlimited_continues_past_finite_cap_and_off_does_not_retry(tmp_path: Path) -> None:
    agent = _agent_with_retry(tmp_path, {"enabled": True, "mode": "unlimited", "maxRetries": 1, "baseDelayMs": 1, "maxDelayMs": 1})
    provider = _SequenceProvider([RuntimeError("connection failed")] * 4 + ['{"tool":"finish","args":{"summary":"Recovered","goal_success":true}}'])
    agent.providers = {"openai": provider}
    await agent.prompt("recover eventually")
    assert provider.calls == 5
    assert agent.get_last_assistant_text() == "Recovered"

    off = _agent_with_retry(tmp_path, {"enabled": True, "mode": "off", "maxRetries": 9, "baseDelayMs": 1, "maxDelayMs": 1})
    off_provider = _SequenceProvider([RuntimeError("connection failed"), "MUST NOT RUN"])
    off.providers = {"openai": off_provider}
    await off.prompt("do not recover")
    assert off_provider.calls == 1


@pytest.mark.asyncio
async def test_retry_unlimited_can_abort_after_the_finite_cap(tmp_path: Path) -> None:
    agent = _agent_with_retry(tmp_path, {"enabled": True, "mode": "unlimited", "maxRetries": 1, "baseDelayMs": 1, "maxDelayMs": 1})
    provider = _SequenceProvider([RuntimeError("connection failed")] * 20)
    agent.providers = {"openai": provider}
    task = asyncio.create_task(agent.prompt("retry until cancelled"))
    for _ in range(100):
        if provider.calls > 2:
            break
        await asyncio.sleep(0.01)
    assert provider.calls > 2
    await agent.abort()
    await task
    assert agent.get_last_assistant_text() == "Request aborted."


@pytest.mark.asyncio
async def test_retry_unlimited_stops_for_permanent_provider_error(tmp_path: Path) -> None:
    agent = _agent_with_retry(tmp_path, {"enabled": True, "mode": "unlimited", "maxRetries": 1, "baseDelayMs": 1, "maxDelayMs": 1})
    provider = _SequenceProvider([RuntimeError("invalid API key"), "MUST NOT RUN"])
    agent.providers = {"openai": provider}
    await agent.prompt("do not spin")
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_retry_after_repair_timeout_does_not_replay_completed_tool(tmp_path: Path) -> None:
    agent = _agent_with_retry(tmp_path, {"enabled": True, "mode": "on", "maxRetries": 2, "baseDelayMs": 1, "maxDelayMs": 1})
    provider = _SequenceProvider([
        '{"tool":"write","args":{"path":"output.txt","content":"created once"}}',
        '{"tool":"read","args":',
        httpx.ReadTimeout(""),
        '{"tool":"finish","args":{"summary":"Recovered","goal_success":true}}',
    ])
    agent.providers = {"openai": provider}
    writes = 0
    original_execute = agent._execute_tool_by_name

    async def execute(name: str, args: dict[str, Any], timeout_sec: int | None = None) -> dict[str, Any]:
        nonlocal writes
        if name == "write":
            writes += 1
            return {"ok": True, "output": "written"}
        return await original_execute(name, args, timeout_sec)

    agent._execute_tool_by_name = execute  # type: ignore[method-assign]
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    await agent.prompt("recover malformed tool output")
    assert agent.get_last_assistant_text() == "Recovered"
    assert [event.get("tool") for event in events if event.get("type") == "tool_call_start"].count("write") == 1
    assert writes == 1


@pytest.mark.asyncio
async def test_live_retry_off_stops_generic_retry_after_backoff(tmp_path: Path) -> None:
    agent = _agent_with_retry(tmp_path, {"enabled": True, "mode": "on", "maxRetries": 3, "baseDelayMs": 30, "maxDelayMs": 30})
    provider = _SequenceProvider([RuntimeError("connection failed"), "MUST NOT RUN"])
    agent.providers = {"openai": provider}

    def disable_on_backoff(event: dict[str, Any]) -> None:
        if event.get("type") == "auto_retry_start":
            agent.settings_manager.set_retry_enabled(False)

    agent.subscribe(disable_on_backoff)
    await agent.prompt("stop retry")
    assert provider.calls == 1
