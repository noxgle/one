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


@pytest.mark.asyncio
async def test_ask_user_returns_answer(tmp_path: Path):
    """Provider calls ask_user, session waits, we answer, then finish."""
    ask_user_json = json.dumps({"tool": "ask_user", "args": {"question": "which dir?"}})
    finish_json = json.dumps({"tool": "finish", "args": {"summary": "done", "goal_success": True}})

    agent = _mk_agent(tmp_path, tools=["ask_user", "finish"])
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
async def test_ask_user_timeout(tmp_path: Path):
    """No answer provided within timeout; tool_call_end shows error, session continues."""
    ask_user_json = json.dumps({"tool": "ask_user", "args": {"question": "timeout?"}})
    finish_json = json.dumps({"tool": "finish", "args": {"summary": "done", "goal_success": True}})

    agent = _mk_agent(
        tmp_path,
        tools=["ask_user", "finish"],
        settings_override={"tools": {"maxSteps": 4, "timeoutSec": 5}, "askUser": {"timeoutSec": 1}},
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

    agent = _mk_agent(tmp_path, tools=["ask_user", "finish"])
    agent.providers = {"openai": _Provider([empty_json, finish_json])}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("go")

    ask_user_end = [e for e in events if e.get("type") == "tool_call_end" and e.get("tool") == "ask_user"]
    assert len(ask_user_end) >= 1
    assert ask_user_end[-1]["ok"] is False
    assert "non-empty" in ask_user_end[-1].get("result", {}).get("error", "")


@pytest.mark.asyncio
async def test_answer_question_unknown_id(tmp_path: Path):
    """Calling answer_question with a nonexistent id raises ValueError."""
    agent = _mk_agent(tmp_path, tools=["ask_user", "finish"])
    with pytest.raises(ValueError):
        agent.answer_question("nope", "x")


@pytest.mark.asyncio
async def test_get_pending_questions(tmp_path: Path):
    """get_pending_questions returns the current pending ask_user questions."""
    ask_user_json = json.dumps({"tool": "ask_user", "args": {"question": "which dir?"}})
    finish_json = json.dumps({"tool": "finish", "args": {"summary": "done", "goal_success": True}})

    agent = _mk_agent(tmp_path, tools=["ask_user", "finish"])
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

    agent = _mk_agent(tmp_path, tools=["ask_user", "finish"])
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
