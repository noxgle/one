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
from one.providers.base import ChatResult
from one.tools.index import all_tools
from tests.support.agents import _FakeProvider, _Loader


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
async def test_deferred_action_response_gets_tool_nudge(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["read"])
    provider = _FakeProvider(
        [
            "Sprawdzę to i zacznę od diagnostyki.",
            '{"tool":"read","args":{"path":"a.txt"}}',
            "DONE",
        ]
    )
    agent.providers = {"openai": provider}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    await agent.prompt("sprawdź")

    assert agent.get_last_assistant_text() == "DONE"
    tool_results = [m for m in agent.messages if m.get("role") == "toolResult"]
    assert tool_results
    nudge_start = [e for e in events if e.get("type") == "tool_call_nudge_start"]
    nudge_end = [e for e in events if e.get("type") == "tool_call_nudge_end"]
    assert len(nudge_start) == 1
    assert len(nudge_end) == 1
    assert nudge_end[0].get("used") is True


@pytest.mark.asyncio
async def test_nudge_propagates_provider_tool_call_id(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    agent = AgentSession(
        SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory({"tools": {"maxSteps": 3}}),
        registry, _Loader(), model, "medium", tools=["read"],
    )
    agent.providers = {"openai": _FakeProvider([
        "I will inspect that.",
        ChatResult(
            text='{"tool":"read","args":{"path":"a.txt"}}',
            raw={"choices": [{"message": {"tool_calls": [
                {"id": "call_nudged", "function": {"name": "read", "arguments": '{"path":"a.txt"}'}}
            ]}}]}, usage={}, stop_reason="tool_calls",
        ),
        "DONE",
    ])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("inspect")

    start = next(event for event in events if event["type"] == "tool_call_start")
    assert start["toolCallId"] == "call_nudged"


@pytest.mark.asyncio
async def test_nudge_fire_convert_counts(tmp_path: Path):
    """Short prose at step 0 fires nudge; nudge yields tool call → count=1 fire, 1 conversion true."""
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["read"])
    provider = _FakeProvider(
        [
            "Sprawdzę to i zacznę od diagnostyki.",  # short prose → triggers nudge
            '{"tool":"read","args":{"path":"a.txt"}}',  # nudge yields tool call
            "DONE",
        ]
    )
    agent.providers = {"openai": provider}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    await agent.prompt("sprawdź")

    assert agent.get_last_assistant_text() == "DONE"

    start_events = [e for e in events if e.get("type") == "tool_call_nudge_start"]
    end_events = [e for e in events if e.get("type") == "tool_call_nudge_end"]
    assert len(start_events) == 1
    assert len(end_events) == 1
    assert start_events[0].get("fireCount") == 1
    assert end_events[0].get("used") is True
    assert end_events[0].get("fireCount") == 1

    # Session stats should reflect the nudge
    stats = agent.get_session_stats()
    assert stats["nudge"]["fires"] == 1
    assert stats["nudge"]["conversions"]["true"] == 1
    assert stats["nudge"]["conversions"]["false"] == 0


@pytest.mark.asyncio
async def test_nudge_fire_no_convert_counts(tmp_path: Path):
    """Short prose at step 0 fires nudge; nudge yields more short prose (no tool) →
    turn ends (loop breaks when tool_call stays None). Counters: 1 fire, 1 conversion false."""
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["read"])
    provider = _FakeProvider(
        [
            "Sprawdzę to i zacznę od diagnostyki.",  # short prose → triggers nudge
            "Nie mam dostępu do tych danych.",  # short prose → nudge doesn't convert
        ]
    )
    agent.providers = {"openai": provider}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    await agent.prompt("sprawdź")

    # Turn ends because tool_call stays None after nudge (loop breaks at line 1667)
    assert agent.get_last_assistant_text() == "Nie mam dostępu do tych danych."

    start_events = [e for e in events if e.get("type") == "tool_call_nudge_start"]
    end_events = [e for e in events if e.get("type") == "tool_call_nudge_end"]
    assert len(start_events) == 1
    assert len(end_events) == 1
    assert start_events[0].get("fireCount") == 1
    assert end_events[0].get("used") is False
    assert end_events[0].get("fireCount") == 1

    stats = agent.get_session_stats()
    assert stats["nudge"]["fires"] == 1
    assert stats["nudge"]["conversions"]["false"] == 1
    assert stats["nudge"]["conversions"]["true"] == 0


@pytest.mark.asyncio
async def test_no_nudge_normal_tool_call(tmp_path: Path):
    """Model returns JSON directly at step 0 → no nudge events, counters stay zero."""
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["read"])
    provider = _FakeProvider(
        [
            '{"tool":"read","args":{"path":"a.txt"}}',  # normal tool JSON → no nudge
            "DONE",
        ]
    )
    agent.providers = {"openai": provider}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    await agent.prompt("sprawdź")

    assert agent.get_last_assistant_text() == "DONE"

    nudge_start = [e for e in events if e.get("type") == "tool_call_nudge_start"]
    nudge_end = [e for e in events if e.get("type") == "tool_call_nudge_end"]
    assert len(nudge_start) == 0
    assert len(nudge_end) == 0

    stats = agent.get_session_stats()
    assert stats["nudge"]["fires"] == 0
    assert stats["nudge"]["conversions"]["true"] == 0
    assert stats["nudge"]["conversions"]["false"] == 0


def test_finish_tool_registered_in_defaults() -> None:

    assert "finish" in all_tools
    # AgentSession defaults its active tools to all_tools keys when none are passed.
    assert "finish" in list(all_tools.keys())


def test_plan_in_default_tool_names() -> None:
    from one.tools.index import DEFAULT_TOOL_NAMES

    assert "plan" in DEFAULT_TOOL_NAMES
    # Every name in DEFAULT_TOOL_NAMES must exist in all_tools.
    for name in DEFAULT_TOOL_NAMES:
        assert name in all_tools, f"{name!r} in DEFAULT_TOOL_NAMES but missing from all_tools"


@pytest.mark.asyncio
async def test_finish_tool_ends_turn_with_summary(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["read", "finish"])
    provider = _FakeProvider(['{"tool":"finish","args":{"summary":"All done.","goal_success":true}}'])
    agent.providers = {"openai": provider}

    seen_turn_end: dict[str, Any] = {}

    def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "turn_end":
            seen_turn_end.update(event)

    agent.subscribe(on_event)
    await agent.prompt("Finish the task.")

    # finish is terminal: no further provider calls, final message is the summary.
    assert provider.calls == 1
    assert agent.get_last_assistant_text() == "All done."
    last = agent.messages[-1]
    assert last.get("goalSuccess") is True
    assert last.get("stopReason") == "completed"
    assert seen_turn_end.get("ok") is True
    assert seen_turn_end.get("reason") == "completed"
    tool_results = seen_turn_end.get("toolResults", [])
    assert len(tool_results) == 1
    assert tool_results[0]["tool"] == "finish"
    assert tool_results[0]["ok"] is True


@pytest.mark.asyncio
async def test_finish_tool_flat_args_format(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["finish"])
    provider = _FakeProvider(['{"tool":"finish","summary":"Flat done.","goal_success":false}'])
    agent.providers = {"openai": provider}

    await agent.prompt("Finish.")
    assert provider.calls == 1
    assert agent.get_last_assistant_text() == "Flat done."
    assert agent.messages[-1].get("goalSuccess") is False
    assert agent.messages[-1].get("stopReason") == "completed"


@pytest.mark.asyncio
async def test_finish_tool_disabled_raises_and_continues(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    # finish is not in the active set here.
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["ls"])
    provider = _FakeProvider(['{"tool":"finish","args":{"summary":"nope","goal_success":true}}', "DONE"])
    agent.providers = {"openai": provider}

    await agent.prompt("Try finish.")

    assert agent.get_last_assistant_text() == "DONE"
    assert provider.calls == 2
    tool_results = [m for m in agent.messages if m.get("role") == "toolResult"]
    assert tool_results
    assert "disabled" in tool_results[0]["content"]
