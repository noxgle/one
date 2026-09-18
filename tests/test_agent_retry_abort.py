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
