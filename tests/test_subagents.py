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
from one.core.types import ModelInfo


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
async def test_spawn_subagent_single_delegates(tmp_path: Path):
    """spawn_subagent with a single task: subagent runs and finishes, parent sees the summary."""
    spawn_json = json.dumps({"tool": "spawn_subagent", "args": {"task": "sub task"}})
    finish_json = json.dumps({"tool": "finish", "args": {"summary": "sub done", "goal_success": True}})
    # repeat-last: call1→spawn, call2→finish (subagent), call3→finish (parent)
    agent = _mk_agent(tmp_path, tools=["spawn_subagent", "finish"])
    agent.providers = {"openai": _Provider([spawn_json, finish_json])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("go")

    # Parent should finish with the subagent summary
    assert "sub done" in agent.get_last_assistant_text()
    finish_result = agent.get_last_finish_result()
    assert finish_result["finished"] is True
    # Tool result message for spawn_subagent contains the subagent summary
    spawn_results = [m for m in agent.messages if m.get("role") == "toolResult" and json.loads(m.get("content", "{}")).get("tool") == "spawn_subagent"]
    assert len(spawn_results) == 1
    assert "sub done" in spawn_results[0].get("content", "")
    # subagent_start and subagent_end events emitted
    sub_events = [e for e in events if e.get("type") == "subagent_start"]
    assert len(sub_events) == 1
    assert sub_events[0]["task"] == "sub task"
    sub_ends = [e for e in events if e.get("type") == "subagent_end"]
    assert len(sub_ends) == 1
    assert sub_ends[0]["ok"] is True


@pytest.mark.asyncio
async def test_spawn_subagent_sets_parent_session_header(tmp_path: Path):
    """Subagent session header must contain parentSession and subagentDepth."""
    spawn_json = json.dumps({"tool": "spawn_subagent", "args": {"task": "sub task"}})
    finish_json = json.dumps({"tool": "finish", "args": {"summary": "sub done", "goal_success": True}})

    session_dir = str(tmp_path / "sessions")
    manager = SessionManager.create(str(tmp_path), session_dir)
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    agent = AgentSession(manager, settings, registry, _Loader(), model, "medium", tools=["spawn_subagent"])
    agent.providers = {"openai": _Provider([spawn_json, finish_json])}

    await agent.prompt("go")

    # Should have 2 files: parent + subagent
    jsonl_files = sorted(Path(session_dir).glob("*.jsonl"))
    assert len(jsonl_files) == 2

    # Find the subagent file (the one with parentSession in header)
    subagent_file = None
    for f in jsonl_files:
        lines = f.read_text(encoding="utf-8").splitlines()
        if lines:
            header = json.loads(lines[0])
            if "parentSession" in header and header.get("subagentDepth") == 1:
                subagent_file = f
                break
    assert subagent_file is not None, "Subagent session file not found with parentSession header"
    assert json.loads(subagent_file.read_text(encoding="utf-8").splitlines()[0])["subagentDepth"] == 1


@pytest.mark.asyncio
async def test_spawn_subagent_parallel_tasks(tmp_path: Path):
    """spawn_subagent with parallel tasks: two subagents run concurrently."""
    spawn_json = json.dumps({"tool": "spawn_subagent", "args": {"tasks": ["a", "b"]}})
    finish_json = json.dumps({"tool": "finish", "args": {"summary": "sub done", "goal_success": True}})
    # repeat-last: call1→spawn, call2→finish (sub a), call3→finish (sub b), call4→finish (parent)
    agent = _mk_agent(tmp_path, tools=["spawn_subagent", "finish"])
    agent.providers = {"openai": _Provider([spawn_json, finish_json])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("go")

    finish_result = agent.get_last_finish_result()
    # Parent should have results from both subagents in the spawn_subagent tool result
    # The visible tool result text contains both summaries (combined).
    spawn_results = [m for m in agent.messages if m.get("role") == "toolResult" and json.loads(m.get("content", "{}")).get("tool") == "spawn_subagent"]
    assert len(spawn_results) == 1
    content = json.loads(spawn_results[0].get("content", "{}"))
    assert content["result"].count("sub done") == 2
    # Structured per-subagent data lives in the tool_call_end event rawResult.
    end_events = [e for e in events if e.get("type") == "tool_call_end" and e.get("tool") == "spawn_subagent"]
    assert len(end_events) == 1
    raw = end_events[0]["result"]["rawResult"]
    results = raw.get("results", [])
    assert len(results) == 2
    for r in results:
        assert r["ok"] is True
    # Two subagent_start events
    sub_starts = [e for e in events if e.get("type") == "subagent_start"]
    assert len(sub_starts) == 2


@pytest.mark.asyncio
async def test_spawn_subagent_depth_limit(tmp_path: Path):
    """Subagent at maxDepth should get a depth error, not spawn further."""
    spawn_json = json.dumps({"tool": "spawn_subagent", "args": {"task": "inner"}})
    finish_json = json.dumps({"tool": "finish", "args": {"summary": "outer done", "goal_success": True}})
    # repeat-last: call1→spawn (parent), call2→spawn (subagent → depth error),
    #   call3→finish (subagent continues and finishes), call4→finish (parent finishes)
    session_dir = str(tmp_path / "sessions")
    manager = SessionManager.create(str(tmp_path), session_dir)
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory(
        {"tools": {"maxSteps": 4, "timeoutSec": 5}, "subagents": {"maxConcurrent": 2, "maxDepth": 1}}
    )
    agent = AgentSession(manager, settings, registry, _Loader(), model, "medium", tools=["spawn_subagent", "finish"])
    agent.providers = {"openai": _Provider([spawn_json, finish_json])}

    await agent.prompt("go")

    # Parent should have finished (subagent called finish)
    finish_result = agent.get_last_finish_result()
    assert finish_result["finished"] is True
    # The depth limit error should be visible in the subagent's session file
    jsonl_files = sorted(Path(session_dir).glob("*.jsonl"))
    assert len(jsonl_files) == 2
    # Find the subagent file and check for depth limit error
    subagent_content = None
    for f in jsonl_files:
        lines = f.read_text(encoding="utf-8").splitlines()
        if lines:
            header = json.loads(lines[0])
            if header.get("subagentDepth") == 1:
                subagent_content = f.read_text(encoding="utf-8")
                break
    assert subagent_content is not None
    assert "depth limit" in subagent_content.lower() or "depth_limit" in subagent_content.lower() or "depth" in subagent_content.lower()


@pytest.mark.asyncio
async def test_spawn_subagent_concurrency_limit(tmp_path: Path):
    """spawn_subagent with more tasks than maxConcurrent should error."""
    spawn_json = json.dumps({"tool": "spawn_subagent", "args": {"tasks": ["a", "b"]}})
    # repeat-last: call1→spawn (parent → too many tasks → error), call2→DONE
    agent = _mk_agent(
        tmp_path,
        tools=["spawn_subagent"],
        settings_override={"tools": {"maxSteps": 4, "timeoutSec": 5}, "subagents": {"maxConcurrent": 1, "maxDepth": 3}},
    )
    agent.providers = {"openai": _Provider([spawn_json, "DONE"])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("go")

    # Parent should have the error in messages
    tool_results = [m for m in agent.messages if m.get("role") == "toolResult"]
    assert len(tool_results) >= 1
    content = json.loads(tool_results[0].get("content", "{}"))
    error_text = content.get("error", "")
    assert "Too many parallel subagents" in error_text or "maxConcurrent" in error_text


@pytest.mark.asyncio
async def test_spawn_subagent_requires_task_or_tasks(tmp_path: Path):
    """spawn_subagent without task or tasks should error."""
    empty_json = json.dumps({"tool": "spawn_subagent", "args": {}})
    # repeat-last: call1→empty (error), call2→DONE
    agent = _mk_agent(tmp_path, tools=["spawn_subagent"])
    agent.providers = {"openai": _Provider([empty_json, "DONE"])}

    await agent.prompt("go")

    tool_results = [m for m in agent.messages if m.get("role") == "toolResult"]
    assert len(tool_results) >= 1
    content = json.loads(tool_results[0].get("content", "{}"))
    error_text = content.get("error", "")
    assert "requires 'task' or 'tasks'" in error_text


@pytest.mark.asyncio
async def test_spawn_subagent_unknown_tools_rejected(tmp_path: Path):
    """spawn_subagent with unknown tool names should error."""
    bad_tools_json = json.dumps({"tool": "spawn_subagent", "args": {"task": "x", "tools": ["nope"]}})
    # repeat-last: call1→bad tools (error), call2→DONE
    agent = _mk_agent(tmp_path, tools=["spawn_subagent"])
    agent.providers = {"openai": _Provider([bad_tools_json, "DONE"])}

    await agent.prompt("go")

    tool_results = [m for m in agent.messages if m.get("role") == "toolResult"]
    assert len(tool_results) >= 1
    content = json.loads(tool_results[0].get("content", "{}"))
    error_text = content.get("error", "")
    assert "Unknown tools: nope" in error_text


@pytest.mark.asyncio
async def test_spawn_subagent_disabled_raises(tmp_path: Path):
    """spawn_subagent must fail when subagents are disabled in settings."""
    spawn_json = json.dumps({"tool": "spawn_subagent", "args": {"task": "sub task"}})
    agent = _mk_agent(
        tmp_path,
        tools=["spawn_subagent", "finish"],
        settings_override={"tools": {"maxSteps": 4, "timeoutSec": 5}, "subagents": {"enabled": False}},
    )
    with pytest.raises(RuntimeError, match="Subagents disabled"):
        await agent._spawn_subagent({"task": "sub task"})


@pytest.mark.asyncio
async def test_set_model_fallback_context_window(tmp_path: Path):
    """set_model must assign a fallback context window for models without one."""
    agent = _mk_agent(tmp_path, tools=["finish"])
    dynamic = ModelInfo(provider="openai", id="dynamic-model", context_window=None)
    await agent.set_model(dynamic)
    assert agent.model.context_window == 128_000


def test_settings_subagents_and_bash_defaults() -> None:
    settings = SettingsManager.in_memory()
    assert settings.get_subagents_enabled() is True
    assert settings.get_bash_show_output() is True
    settings.set_subagents_enabled(False)
    settings.set_bash_show_output(False)
    assert settings.get_subagents_enabled() is False
    assert settings.get_bash_show_output() is False
    assert settings.merged()["subagents"]["enabled"] is False
    assert settings.merged()["bash"]["showOutput"] is False
