from __future__ import annotations

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
    def get_system_prompt(self) -> str:
        return "You are a coding agent."


class _RecordingProvider:
    """Fake provider that records every request's messages for assertions."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0
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
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ChatResult(text=self.responses[idx], raw={}, usage={}, stop_reason="stop")


def _make_agent(
    tmp_path: Path,
    responses: list[str],
    tools: list[str] | None = None,
    approval_callback=None,
) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 6, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(
        session,
        settings,
        registry,
        _Loader(),
        model,
        "medium",
        tools=tools or ["read", "plan", "finish"],
        approval_callback=approval_callback,
    )
    agent.providers = {"openai": _RecordingProvider(responses)}  # type: ignore[assignment]
    return agent


def _plan_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter entries for customType=plan messages."""
    out: list[dict[str, Any]] = []
    for e in entries:
        msg = e.get("message", {})
        if msg.get("customType") == "plan":
            out.append(msg)
    return out


# ---------------------------------------------------------------------------
# Session-level: plan call sets state, emits events, injects into system prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_sets_session_plan_and_system_prompt(tmp_path: Path):
    """Calling plan sets session._plan, emits plan_update, and the next system prompt contains the plan."""
    agent = _make_agent(
        tmp_path,
        [
            '{"tool":"plan","args":{"plan":"1. read file\\n2. edit content\\n3. finish"}}',
            '{"tool":"finish","args":{"summary":"done","goal_success":true}}',
        ],
    )

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    await agent.prompt("Plan and finish.")

    # plan_update event emitted with the plan text
    plan_events = [e for e in events if e.get("type") == "plan_update"]
    assert len(plan_events) >= 1
    assert plan_events[0]["plan"] == "1. read file\n2. edit content\n3. finish"

    # Next provider call's system prompt contains the plan
    provider = agent.providers["openai"]  # type: ignore[index]
    assert len(provider.requests) >= 2  # type: ignore[attr-defined]
    second_req = provider.requests[1]  # type: ignore[attr-defined]
    system_text = ""
    for m in second_req:
        if m.get("role") == "system":
            system_text += str(m.get("content", ""))
    assert "# Active Plan" in system_text
    assert "1. read file" in system_text


@pytest.mark.asyncio
async def test_plan_finish_clears_plan_and_emits_empty_update(tmp_path: Path):
    """Calling finish after a plan clears session._plan and emits plan_update with empty text."""
    agent = _make_agent(
        tmp_path,
        [
            '{"tool":"plan","args":{"plan":"step one"}}',
            '{"tool":"finish","args":{"summary":"done","goal_success":true}}',
        ],
    )

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    await agent.prompt("Plan and finish.")

    # finish cleared the plan
    assert agent._plan is None

    # last plan_update event has empty plan
    plan_events = [e for e in events if e.get("type") == "plan_update"]
    assert len(plan_events) >= 2
    assert plan_events[-1]["plan"] == ""


# ---------------------------------------------------------------------------
# Persistence: plan message in jsonl, not in provider messages, survives reload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_persists_as_custom_type_message(tmp_path: Path):
    """After a plan call, the session jsonl contains a customType=plan message;
    self.messages does NOT contain it."""
    agent = _make_agent(
        tmp_path,
        [
            '{"tool":"plan","args":{"plan":"persist me"}}',
        ],
        tools=["read", "plan", "finish"],
    )
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    # Don't call prompt — just simulate a plan call by calling _run_tool_call directly
    result = await agent._run_tool_call("plan", {"plan": "persist me"})
    assert result["ok"] is True

    # The plan message is in the session manager's jsonl entries
    entries = agent.session_manager.get_entries()
    plan_entries = _plan_entries(entries)
    assert len(plan_entries) >= 1
    assert plan_entries[-1]["content"] == "persist me"

    # Plan messages are NOT in self.messages
    assert not any(m.get("customType") == "plan" for m in agent.messages)


@pytest.mark.asyncio
async def test_plan_restored_on_session_reload(tmp_path: Path):
    """Creating a new session over the same jsonl restores _plan from the last plan message."""
    # Create agent and set plan directly (no provider needed)
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 6, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    loader = _Loader()

    agent1 = AgentSession(session, settings, registry, loader, model, "medium", tools=["read", "plan", "finish"])
    await agent1._run_tool_call("plan", {"plan": "restored plan"})
    assert agent1._plan == "restored plan"

    # Plan messages are not in messages
    assert not any(m.get("customType") == "plan" for m in agent1.messages)

    # Create a new session over the same session_manager
    agent2 = AgentSession(session, settings, registry, loader, model, "medium")
    assert agent2._plan == "restored plan"
    # Plan messages still not in the provider message list
    assert not any(m.get("customType") == "plan" for m in agent2.messages)


@pytest.mark.asyncio
async def test_plan_cleared_on_finish_and_restored_as_none(tmp_path: Path):
    """After finish clears the plan, reloading restores _plan as None."""
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 6, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    loader = _Loader()

    agent1 = AgentSession(session, settings, registry, loader, model, "medium", tools=["read", "plan", "finish"])
    # Set plan
    await agent1._run_tool_call("plan", {"plan": "will be cleared"})
    assert agent1._plan == "will be cleared"

    # Call finish to clear the plan
    await agent1._run_tool_call("finish", {"summary": "done", "goal_success": True})
    assert agent1._plan is None

    # Verify a cleared marker was written
    entries = agent1.session_manager.get_entries()
    plan_entries = _plan_entries(entries)
    assert plan_entries[-1]["content"] == ""

    # Reload - _plan should be None
    agent2 = AgentSession(session, settings, registry, loader, model, "medium")
    assert agent2._plan is None


@pytest.mark.asyncio
async def test_plan_finish_goal_success_false_clears_plan_and_preserves_flag(tmp_path: Path):
    """finish(goal_success=false) is terminal: clears plan, emits empty plan_update,
    and the final assistant message carries goalSuccess=False."""
    agent = _make_agent(
        tmp_path,
        [
            '{"tool":"plan","args":{"plan":"step one"}}',
            '{"tool":"finish","args":{"summary":"failed task","goal_success":false}}',
        ],
    )

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    await agent.prompt("Plan and finish (fail).")

    # finish cleared the plan
    assert agent._plan is None

    # last plan_update event has empty plan
    plan_events = [e for e in events if e.get("type") == "plan_update"]
    assert len(plan_events) >= 2
    assert plan_events[-1]["plan"] == ""

    # Final assistant message contains goalSuccess=false
    assistant_events = [e for e in events if e.get("type") == "message_end"]
    assert len(assistant_events) >= 1
    # The last assistant message is the finish summary
    last_assistant = assistant_events[-1].get("message", {})
    assert last_assistant.get("goalSuccess") is False


@pytest.mark.asyncio
async def test_plan_finish_goal_success_false_restored_none(tmp_path: Path):
    """After finish(goal_success=false) clears the plan, reloading restores _plan as None."""
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 6, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    loader = _Loader()

    agent1 = AgentSession(session, settings, registry, loader, model, "medium", tools=["read", "plan", "finish"])
    await agent1._run_tool_call("plan", {"plan": "will be cleared"})
    assert agent1._plan == "will be cleared"

    await agent1._run_tool_call("finish", {"summary": "failed", "goal_success": False})
    assert agent1._plan is None

    # Verify a cleared marker was written
    entries = agent1.session_manager.get_entries()
    plan_entries = _plan_entries(entries)
    assert plan_entries[-1]["content"] == ""

    # Reload - _plan should be None
    agent2 = AgentSession(session, settings, registry, loader, model, "medium")
    assert agent2._plan is None
