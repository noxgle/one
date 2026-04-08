from __future__ import annotations

import asyncio
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
    ) -> Any:
        from one.providers.base import ChatResult

        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ChatResult(text=self.responses[idx], raw={}, usage={}, stop_reason="stop")


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
    ) -> Any:
        from one.providers.base import ChatResult

        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary")
        return ChatResult(text="DONE", raw={}, usage={}, stop_reason="stop")


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

        await asyncio.sleep(0.08)
        return ChatResult(text="DONE", raw={}, usage={}, stop_reason="stop")


def _mk_agent(tmp_path: Path, tools: list[str] | None = None, settings_override: dict[str, Any] | None = None) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory(settings_override or {"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    return AgentSession(session, settings, registry, _Loader(), model, "medium", tools=tools)


def _compact(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in events:
        item = {"type": e.get("type")}
        if "attempt" in e:
            item["attempt"] = e.get("attempt")
        if "ok" in e:
            item["ok"] = e.get("ok")
        if "reason" in e:
            item["reason"] = e.get("reason")
        if "willRetry" in e:
            item["willRetry"] = e.get("willRetry")
        if "aborted" in e:
            item["aborted"] = e.get("aborted")
        if e.get("type") == "tool_call_start":
            item["tool"] = e.get("tool")
        if e.get("type") == "tool_call_end":
            item["tool"] = e.get("tool")
            item["ok"] = e.get("ok")
        out.append(item)
    return out


@pytest.mark.asyncio
async def test_event_snapshot_tool_success(tmp_path: Path):
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    agent = _mk_agent(tmp_path, tools=["read"])
    agent.providers = {"openai": _Provider(['{"tool":"read","args":{"path":"a.txt"}}', "DONE"])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("go")
    assert _compact(events) == [
        {"type": "agent_start"},
        {"type": "message_start"},
        {"type": "message_end"},
        {"type": "turn_start", "attempt": 1},
        {"type": "tool_call_start", "tool": "read"},
        {"type": "tool_call_end", "ok": True, "tool": "read"},
        {"type": "message_start"},
        {"type": "message_update"},
        {"type": "message_end"},
        {"type": "turn_end", "attempt": 1, "ok": True, "reason": "stop", "aborted": False},
        {"type": "agent_end"},
    ]


@pytest.mark.asyncio
async def test_event_snapshot_retry_then_success(tmp_path: Path):
    agent = _mk_agent(tmp_path, settings_override={"retry": {"enabled": True, "maxRetries": 2, "baseDelayMs": 1, "maxDelayMs": 1}})
    agent.providers = {"openai": _FlakyProvider()}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("go")
    compact = _compact(events)
    assert {"type": "turn_end", "attempt": 1, "ok": False, "reason": "error", "willRetry": True} in compact
    assert {"type": "auto_retry_start", "attempt": 1} in compact
    assert {"type": "auto_retry_end", "attempt": 1, "willRetry": True, "aborted": False} in compact
    assert {"type": "turn_end", "attempt": 2, "ok": True, "reason": "stop", "aborted": False} in compact


@pytest.mark.asyncio
async def test_event_snapshot_abort_path(tmp_path: Path):
    agent = _mk_agent(tmp_path)
    agent.providers = {"openai": _SlowProvider()}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    task = asyncio.create_task(agent.prompt("go"))
    await asyncio.sleep(0.01)
    await agent.abort()
    await task

    compact = _compact(events)
    assert {"type": "turn_end", "attempt": 1, "ok": True, "reason": "abort", "aborted": True} in compact
    assert {"type": "agent_end"} in compact
