# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.resources.resource_loader import DefaultResourceLoader


class _Loader:
    def __init__(self) -> None:
        self.selected_tools: list[str] | None = None

    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
        self.selected_tools = selected_tools
        return "You are a coding agent."


class _Provider:
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
    ) -> Any:
        from one.providers.base import ChatResult

        self.requests.append(messages)
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ChatResult(text=self.responses[idx], raw={}, usage={}, stop_reason="stop")


def _mk_agent(
    tmp_path: Path,
    tools: list[str] | None = None,
    settings_override: dict[str, Any] | None = None,
    cooperation: bool = False,
) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory(settings_override or {"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=tools)
    if cooperation:
        async def approve(_tool: str, _args: dict[str, Any]) -> tuple[bool, str]:
            return True, ""
        agent.approval_callback = approve
    return agent


@pytest.mark.asyncio
async def test_ask_user_returns_answer(tmp_path: Path):
    """Provider calls ask_user, session waits, we answer, then finish."""
    ask_user_json = json.dumps({"tool": "ask_user", "args": {"question": "which dir?"}})
    finish_json = json.dumps({"tool": "finish", "args": {"summary": "done", "goal_success": True}})

    agent = _mk_agent(tmp_path, tools=["ask_user", "finish"], cooperation=True)
    agent.providers = {"openai": _Provider([ask_user_json, finish_json])}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    question_event = asyncio.Event()
    qid = None

    def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "ask_user":
            nonlocal qid
            qid = event["id"]
            question_event.set()

    agent.subscribe(on_event)

    prompt_task = asyncio.create_task(agent.prompt("go"))
    await question_event.wait()

    assert qid is not None
    ask_user_events = [e for e in events if e.get("type") == "ask_user"]
    assert len(ask_user_events) == 1
    assert ask_user_events[0]["question"] == "which dir?"

    agent.answer_question(qid, "the answer")
    await prompt_task

    # Tool result for ask_user contains the answer
    tool_results = [m for m in agent.messages if m.get("role") == "toolResult"]
    ask_user_result = next(m for m in tool_results if json.loads(m["content"]).get("tool") == "ask_user")
    content = json.loads(ask_user_result["content"])
    assert "the answer" in content.get("result", "")

    assert agent.get_last_finish_result()["finished"] is True

    answered_events = [e for e in events if e.get("type") == "ask_user_answered"]
    assert len(answered_events) == 1
    assert answered_events[0]["answer"] == "the answer"


@pytest.mark.asyncio
async def test_cooperative_text_prompt_resumes_with_safe_read_after_answer(tmp_path: Path) -> None:
    """Text-only calls receive the dynamic guidance and resume the same task."""
    (tmp_path / "chosen.txt").write_text("confirmed\n", encoding="utf-8")
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    loader = DefaultResourceLoader(
        cwd=str(tmp_path), agent_dir=str(tmp_path / "agent"), settings_manager=SettingsManager.in_memory()
    )
    agent = AgentSession(
        SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory(), registry, loader, model, "medium",
        tools=["read", "ask_user", "finish"],
    )

    async def approve(_tool: str, _args: dict[str, Any]) -> tuple[bool, str]:
        return True, ""

    agent.approval_callback = approve
    provider = _Provider([
        json.dumps({"tool": "ask_user", "args": {"question": "Which file? I recommend chosen.txt."}}),
        json.dumps({"tool": "read", "args": {"path": "chosen.txt"}}),
        json.dumps({"tool": "finish", "args": {"summary": "read it", "goal_success": True}}),
    ])
    agent.providers = {"openai": provider}  # type: ignore[assignment]
    question = asyncio.Event()
    question_id: str | None = None

    def answer(event: dict[str, Any]) -> None:
        nonlocal question_id
        if event.get("type") == "ask_user":
            question_id = str(event["id"])
            question.set()

    agent.subscribe(answer)
    task = asyncio.create_task(agent.prompt("inspect the chosen file"))
    await question.wait()
    assert question_id is not None
    agent.answer_question(question_id, "chosen.txt")
    await task

    assert "Cooperation is ON. The ask_user tool is active." in provider.requests[0][0]["content"]
    assert any("confirmed" in message.get("content", "") for message in agent.messages if message["role"] == "toolResult")
    assert agent.get_last_finish_result()["goalSuccess"] is True


@pytest.mark.asyncio
async def test_ask_user_timeout(tmp_path: Path):
    """No answer provided within timeout; tool_call_end shows error, session continues."""
    ask_user_json = json.dumps({"tool": "ask_user", "args": {"question": "timeout?"}})
    finish_json = json.dumps({"tool": "finish", "args": {"summary": "done", "goal_success": True}})

    agent = _mk_agent(
        tmp_path,
        tools=["ask_user", "finish"],
        settings_override={"tools": {"maxSteps": 4, "timeoutSec": 5}, "askUser": {"timeoutSec": 1}},
        cooperation=True,
    )
    agent.providers = {"openai": _Provider([ask_user_json, finish_json])}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    question_event = asyncio.Event()
    qid = None

    def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "ask_user":
            nonlocal qid
            qid = event["id"]
            question_event.set()

    agent.subscribe(on_event)

    await agent.prompt("go")

    # No answer was ever provided; timeout should have triggered
    ask_user_end = [e for e in events if e.get("type") == "tool_call_end" and e.get("tool") == "ask_user"]
    assert len(ask_user_end) >= 1
    assert ask_user_end[-1]["ok"] is False
    assert "No answer received" in ask_user_end[-1].get("result", {}).get("error", "")

    assert agent.get_last_finish_result()["finished"] is True


@pytest.mark.asyncio
async def test_ask_user_requires_question(tmp_path: Path):
    """ask_user with empty args should error."""
    empty_json = json.dumps({"tool": "ask_user", "args": {}})
    finish_json = json.dumps({"tool": "finish", "args": {"summary": "done", "goal_success": True}})

    agent = _mk_agent(tmp_path, tools=["ask_user", "finish"], cooperation=True)
    agent.providers = {"openai": _Provider([empty_json, finish_json])}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("go")

    ask_user_end = [e for e in events if e.get("type") == "tool_call_end" and e.get("tool") == "ask_user"]
    assert len(ask_user_end) >= 1
    assert ask_user_end[-1]["ok"] is False
    assert "non-empty" in ask_user_end[-1].get("result", {}).get("error", "")


@pytest.mark.asyncio
async def test_ask_user_off_returns_deterministic_result_without_event_or_wait(tmp_path: Path):
    agent = _mk_agent(tmp_path, tools=["ask_user", "finish"])
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    result = await asyncio.wait_for(agent._run_tool_call("ask_user", {"question": "stale context?"}), timeout=0.1)

    assert result["ok"] is True
    assert result["result"] == "Cooperation is disabled; proceed with best judgment without asking the user."
    assert agent.get_pending_questions() == []
    assert not [event for event in events if event["type"] == "ask_user"]


def test_ask_user_is_hidden_from_prompt_when_cooperation_is_off(tmp_path: Path):
    agent = _mk_agent(tmp_path, tools=["read", "ask_user", "finish"])
    loader = agent.resource_loader

    agent._build_runtime_system_prompt()
    assert loader.selected_tools == ["read", "finish"]

    async def approve(_tool: str, _args: dict[str, Any]) -> tuple[bool, str]:
        return True, ""

    agent.approval_callback = approve
    agent._build_runtime_system_prompt()
    assert loader.selected_tools == ["read", "ask_user", "finish"]


def test_default_prompt_has_mode_aware_cooperation_guidance(tmp_path: Path) -> None:
    """The default prompt never contradicts the current ask_user capability."""
    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    loader = DefaultResourceLoader(
        cwd=str(tmp_path), agent_dir=str(tmp_path / "agent"), settings_manager=SettingsManager.in_memory()
    )
    agent = AgentSession(
        SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory(), registry, loader, model, "medium",
        tools=["read", "ask_user", "finish"],
    )

    off_prompt = agent._build_runtime_system_prompt()
    assert "Autonomous mode: do not ask the user." not in off_prompt
    assert "Cooperation is OFF. ask_user is unavailable" in off_prompt
    assert "- ask_user " not in off_prompt

    async def approve(_tool: str, _args: dict[str, Any]) -> tuple[bool, str]:
        return True, ""

    agent.approval_callback = approve
    on_prompt = agent._build_runtime_system_prompt()
    assert "Cooperation is ON. The ask_user tool is active." in on_prompt
    assert "- ask_user {question, timeoutSec?}" in on_prompt
    assert '"tool":"ask_user"' in on_prompt
    assert "Never put a request for missing information in finish.summary" in on_prompt


def test_cooperation_prompt_handles_tools_excluding_ask_user_and_custom_loader(tmp_path: Path) -> None:
    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    class _CustomLoader:
        def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
            return "CUSTOM SYSTEM PROMPT"

    agent = AgentSession(
        SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory(), registry, _CustomLoader(), model, "medium",
        tools=["read", "finish"],
    )

    async def approve(_tool: str, _args: dict[str, Any]) -> tuple[bool, str]:
        return True, ""

    agent.approval_callback = approve
    prompt = agent._build_runtime_system_prompt()
    assert "CUSTOM SYSTEM PROMPT" in prompt
    assert "Cooperation is ON, but ask_user is unavailable" in prompt
    assert "Do not call a nonexistent human-question tool." in prompt

    active_agent = AgentSession(
        SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory(), registry, _CustomLoader(), model, "medium",
        tools=["read", "ask_user", "finish"],
    )
    active_agent.approval_callback = approve
    active_prompt = active_agent._build_runtime_system_prompt()
    assert "CUSTOM SYSTEM PROMPT" in active_prompt
    assert "Cooperation is ON. The ask_user tool is active." in active_prompt


@pytest.mark.asyncio
async def test_disabling_cooperation_releases_pending_question(tmp_path: Path):
    agent = _mk_agent(tmp_path, tools=["ask_user", "finish"], cooperation=True)
    released = asyncio.Event()
    agent.subscribe(lambda event: released.set() if event["type"] == "ask_user_released" else None)

    task = asyncio.create_task(agent._ask_user({"question": "continue?"}))
    await asyncio.sleep(0)
    assert len(agent.get_pending_questions()) == 1

    agent.approval_callback = None
    result = await task

    assert released.is_set()
    assert result["output"] == "Cooperation is disabled; proceed with best judgment without asking the user."
    assert agent.get_pending_questions() == []


@pytest.mark.asyncio
async def test_answer_question_ignores_released_or_duplicate_answers(tmp_path: Path):
    agent = _mk_agent(tmp_path, tools=["ask_user", "finish"], cooperation=True)
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    task = asyncio.create_task(agent._ask_user({"question": "continue?"}))
    await asyncio.sleep(0)
    question_id = agent.get_pending_questions()[0]["id"]

    agent.approval_callback = None
    agent.answer_question(question_id, "late answer")
    result = await task

    assert result["output"] == "Cooperation is disabled; proceed with best judgment without asking the user."
    assert not [event for event in events if event["type"] == "ask_user_answered"]

    async def approve(_tool: str, _args: dict[str, Any]) -> tuple[bool, str]:
        return True, ""

    agent.approval_callback = approve
    duplicate_task = asyncio.create_task(agent._ask_user({"question": "again?"}))
    await asyncio.sleep(0)
    duplicate_id = agent.get_pending_questions()[0]["id"]
    agent.answer_question(duplicate_id, "first answer")
    agent.answer_question(duplicate_id, "second answer")
    duplicate_result = await duplicate_task

    assert duplicate_result["output"] == "first answer"
    answered_events = [event for event in events if event["type"] == "ask_user_answered"]
    assert answered_events == [{"type": "ask_user_answered", "id": duplicate_id, "answer": "first answer"}]


@pytest.mark.asyncio
async def test_ask_user_cancellation_clears_pending_question(tmp_path: Path):
    agent = _mk_agent(tmp_path, tools=["ask_user", "finish"], cooperation=True)
    task = asyncio.create_task(agent._ask_user({"question": "cancel?"}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent.get_pending_questions() == []


@pytest.mark.asyncio
async def test_answer_question_unknown_id(tmp_path: Path):
    """Calling answer_question with a nonexistent id raises ValueError."""
    agent = _mk_agent(tmp_path, tools=["ask_user", "finish"], cooperation=True)
    with pytest.raises(ValueError):
        agent.answer_question("nope", "x")


@pytest.mark.asyncio
async def test_get_pending_questions(tmp_path: Path):
    """get_pending_questions returns the current pending ask_user questions."""
    ask_user_json = json.dumps({"tool": "ask_user", "args": {"question": "which dir?"}})
    finish_json = json.dumps({"tool": "finish", "args": {"summary": "done", "goal_success": True}})

    agent = _mk_agent(tmp_path, tools=["ask_user", "finish"], cooperation=True)
    agent.providers = {"openai": _Provider([ask_user_json, finish_json])}

    question_event = asyncio.Event()
    qid = None

    def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "ask_user":
            nonlocal qid
            qid = event["id"]
            question_event.set()

    agent.subscribe(on_event)

    prompt_task = asyncio.create_task(agent.prompt("go"))
    await question_event.wait()

    assert agent.get_pending_questions() == [{"id": qid, "question": "which dir?"}]

    agent.answer_question(qid, "the answer")
    await prompt_task


@pytest.mark.asyncio
async def test_ask_user_abort_cancels_wait(tmp_path: Path):
    """Aborting the session during a pending ask_user cancels the wait."""
    ask_user_json = json.dumps({"tool": "ask_user", "args": {"question": "halt?"}})
    finish_json = json.dumps({"tool": "finish", "args": {"summary": "done", "goal_success": True}})

    agent = _mk_agent(tmp_path, tools=["ask_user", "finish"], cooperation=True)
    agent.providers = {"openai": _Provider([ask_user_json, finish_json])}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    question_event = asyncio.Event()
    qid = None

    def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "ask_user":
            nonlocal qid
            qid = event["id"]
            question_event.set()

    agent.subscribe(on_event)

    prompt_task = asyncio.create_task(agent.prompt("go"))
    await question_event.wait()

    await agent.abort()
    await prompt_task

    assert agent.get_last_finish_result()["finished"] is False

    ask_user_end = [e for e in events if e.get("type") == "tool_call_end" and e.get("tool") == "ask_user"]
    assert len(ask_user_end) >= 1
    assert ask_user_end[-1].get("aborted") is True
