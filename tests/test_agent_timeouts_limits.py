from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from tests.support.agents import _FakeProvider, _Loader


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
    assert "reached the step limit" in (agent.get_last_assistant_text() or "")


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

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

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
    payload = json.loads(tool_results[0]["content"])
    assert payload["ok"] is False
    assert payload["timedOut"] is True
    assert payload["errorType"] == "TimeoutError"
    assert payload["exitCode"] is not None and payload["exitCode"] != 0
    assert "timed out" in payload["error"].lower()

    # Event assertions: tool_call_end for bash has ok:false, result.timedOut true, no aborted true
    tool_call_ends = [e for e in events if e.get("type") == "tool_call_end"]
    bash_end = next((e for e in tool_call_ends if e.get("tool") == "bash"), None)
    assert bash_end is not None
    assert bash_end["ok"] is False
    assert bash_end["result"]["timedOut"] is True
    assert bash_end.get("aborted") is not True


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


@pytest.mark.asyncio
async def test_bash_model_timeout_honored_above_settings(tmp_path: Path):
    """Model's explicit 'timeout' arg overrides the settings default — command must NOT be cancelled."""
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
                '{"tool":"bash","args":{"command":"sleep 2 && echo ok","timeout":3}}',
                "done",
            ]
        )
    }

    await agent.prompt("Run command")
    tool_results = [m for m in agent.messages if m.get("role") == "toolResult"]
    assert len(tool_results) == 1
    payload = json.loads(tool_results[0]["content"])
    assert payload["ok"] is True
    assert "ok" in payload.get("result", "")


@pytest.mark.asyncio
async def test_bash_no_timeout_uses_settings_default(tmp_path: Path):
    """When model omits 'timeout', the settings default applies — command is timed out."""
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
                '{"tool":"bash","args":{"command":"sleep 3"}}',
                "done",
            ]
        )
    }

    await agent.prompt("Run command")
    tool_results = [m for m in agent.messages if m.get("role") == "toolResult"]
    assert tool_results
    content = tool_results[0]["content"].lower()
    assert "timed out" in content
    assert "(cancelled)" not in content


@pytest.mark.asyncio
async def test_mcp_timeout_precedence_is_emitted_and_propagated(tmp_path: Path):
    """MCP uses the same per-call/default timeout in its event and client call."""
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    class _Mcp:
        def __init__(self) -> None:
            self.timeouts: list[int | float | None] = []

        def tools(self) -> list[object]:
            return [type("Tool", (), {"name": "remote"})()]

        def has_tool(self, name: str) -> bool:
            return name == "remote"

        async def call_tool(self, name: str, args: dict[str, Any], timeout: int | float | None) -> dict[str, Any]:
            self.timeouts.append(timeout)
            return {"ok": True, "output": "remote result"}

    mcp = _Mcp()
    agent = AgentSession(
        SessionManager.in_memory(str(tmp_path)),
        SettingsManager.in_memory({"tools": {"timeoutSec": 7}}),
        registry,
        _Loader(),
        model,
        "medium",
        tools=[],
        mcp_manager=mcp,  # type: ignore[arg-type]
    )
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent._run_tool_call("remote", {}, timeout_sec=None)
    await agent._run_tool_call("remote", {"timeout": 11}, timeout_sec=None)

    starts = [event for event in events if event["type"] == "tool_call_start"]
    assert [event["effectiveTimeout"] for event in starts] == [7, 11]
    assert mcp.timeouts == [7, 11]


def test_mcp_timeout_compatibility_fallback_when_tools_default_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    class _Mcp:
        def tools(self) -> list[object]:
            return []

        def has_tool(self, name: str) -> bool:
            return name == "remote"

    agent = AgentSession(
        SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory(), registry, _Loader(), model, "medium", tools=[], mcp_manager=_Mcp()  # type: ignore[arg-type]
    )
    monkeypatch.setattr(agent.settings_manager, "get_tool_timeout_sec", lambda: (_ for _ in ()).throw(ValueError()))
    assert agent._effective_tool_timeout("remote", {}, None) == 120
