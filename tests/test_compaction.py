from __future__ import annotations

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

    async def reload(self) -> None:
        return


class _Provider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0
        self.last_messages: list[dict[str, Any]] = []

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
        self.last_messages = messages
        idx = min(self.calls - 1, len(self.responses) - 1)
        return ChatResult(text=self.responses[idx], raw={}, usage={}, stop_reason="stop")


class _FailProvider:
    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
    ) -> Any:
        raise RuntimeError("summarizer down")


def _mk_agent(
    tmp_path,
    settings_override: dict[str, Any] | None = None,
    provider: Any = None,
    model: Any = None,
) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    if model is None:
        model = registry.find("openai", "gpt-4.1")
        assert model is not None
    settings = SettingsManager.in_memory(settings_override or {"compaction": {"summarizeWithModel": False}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium")
    if provider is not None:
        agent.providers = {"openai": provider}
    return agent


def _seed(agent: AgentSession, count: int = 10, prefix: str = "user message number {} with some extra padding text to make it longer here") -> None:
    sm = agent.session_manager
    for i in range(count):
        sm.append_message({"role": "user", "content": prefix.format(i)})
        sm.append_message({"role": "assistant", "content": f"answer {i}"})
    agent.messages = sm.build_session_context()["messages"]


@pytest.mark.asyncio
async def test_compact_keeps_recent_window_and_summary_message(tmp_path):
    agent = _mk_agent(
        tmp_path,
        {"compaction": {"summarizeWithModel": False, "recentTokens": 40, "minKeptMessages": 2}},
    )
    _seed(agent)
    total = len(agent.messages)
    assert total == 20

    result = await agent.compact()
    assert result["skipped"] is False
    assert result["summary"].startswith("Compacted previous")
    assert result["tokensBefore"] > 0
    # Rolling two-tier context: summary at front + recent raw window.
    assert agent.messages[0]["customType"] == "compaction_summary"
    assert agent.messages[0]["content"] == result["summary"]
    assert 3 <= len(agent.messages) < total
    assert agent.messages[-1]["content"] == "answer 9"

    entry = agent.session_manager.get_last_compaction()
    assert entry is not None
    assert entry["summary"] == result["summary"]
    # The persisted tree reconstructs exactly the live (summary + window) context.
    ctx = agent.session_manager.build_session_context()
    assert [m.get("content") for m in ctx["messages"]] == [m.get("content") for m in agent.messages]


@pytest.mark.asyncio
async def test_compact_skips_when_nothing_to_drop(tmp_path):
    agent = _mk_agent(tmp_path, {"compaction": {"summarizeWithModel": False}})
    agent.session_manager.append_message({"role": "user", "content": "small"})
    agent.messages = agent.session_manager.build_session_context()["messages"]

    result = await agent.compact()
    assert result["skipped"] is True
    assert agent.session_manager.get_last_compaction() is None


@pytest.mark.asyncio
async def test_compact_custom_instructions_skips_model(tmp_path):
    provider = _Provider(["MODEL SUMMARY"])
    agent = _mk_agent(
        tmp_path,
        {"compaction": {"summarizeWithModel": True, "recentTokens": 30, "minKeptMessages": 2}},
        provider=provider,
    )
    _seed(agent, count=15)

    result = await agent.compact("My custom instructions summary")
    assert result["summary"] == "My custom instructions summary"
    assert provider.calls == 0  # model summarizer never invoked


@pytest.mark.asyncio
async def test_compact_summarize_rolling_with_previous_summary(tmp_path):
    provider = _Provider(["FIRST SUMMARY", "SECOND SUMMARY"])
    agent = _mk_agent(
        tmp_path,
        {"compaction": {"summarizeWithModel": True, "recentTokens": 40, "minKeptMessages": 2}},
        provider=provider,
    )
    _seed(agent)

    r1 = await agent.compact()
    assert r1["summary"] == "FIRST SUMMARY"
    last_user = [m for m in provider.last_messages if m.get("role") == "user"][-1]
    assert "Previous summary" in last_user["content"]
    assert "History since last compaction" in last_user["content"]
    assert "user message number 0" in last_user["content"]

    # Continue the conversation, then compact again: the rolling schema feeds the
    # previous summary forward.
    sm = agent.session_manager
    for i in range(5):
        sm.append_message({"role": "user", "content": f"follow-up message {i} with some extra padding text to make it longer here"})
        sm.append_message({"role": "assistant", "content": f"fa {i}"})
    agent.messages = sm.build_session_context()["messages"]

    r2 = await agent.compact()
    assert r2["summary"] == "SECOND SUMMARY"
    last_user2 = [m for m in provider.last_messages if m.get("role") == "user"][-1]
    assert "FIRST SUMMARY" in last_user2["content"]


@pytest.mark.asyncio
async def test_compact_summarize_fallback_literal_on_error(tmp_path):
    agent = _mk_agent(
        tmp_path,
        {"compaction": {"summarizeWithModel": True, "recentTokens": 40, "minKeptMessages": 2}},
        provider=_FailProvider(),
    )
    _seed(agent)

    result = await agent.compact()
    assert result["summary"].startswith("Compacted previous")
    assert result["summary"] != ""


@pytest.mark.asyncio
async def test_auto_compaction_triggers_above_threshold(tmp_path):
    provider = _Provider(["DONE"])
    agent = _mk_agent(
        tmp_path,
        {"compaction": {"enabled": True, "thresholdPercent": 0.0, "recentTokens": 4, "minKeptMessages": 1, "summarizeWithModel": False}},
        provider=provider,
    )
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("a" * 40)
    assert any(e["type"] == "compaction_start" and e["reason"] == "auto" for e in events)
    assert any(e["type"] == "compaction_end" and e["reason"] == "auto" for e in events)
    assert agent.messages[0]["customType"] == "compaction_summary"
    assert agent.is_compacting is False
    assert agent.session_manager.get_last_compaction() is not None


@pytest.mark.asyncio
async def test_auto_compaction_respects_disabled(tmp_path):
    provider = _Provider(["DONE"])
    agent = _mk_agent(
        tmp_path,
        {"compaction": {"enabled": False, "thresholdPercent": 0.0, "recentTokens": 4, "minKeptMessages": 1, "summarizeWithModel": False}},
        provider=provider,
    )
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("a" * 40)
    assert not any(e["type"] == "compaction_start" for e in events)
    assert not any(m.get("customType") == "compaction_summary" for m in agent.messages)
    assert agent.session_manager.get_last_compaction() is None


@pytest.mark.asyncio
async def test_compaction_preserves_plan_state(tmp_path):
    """A plan set before compaction must survive: _plan stays set and the system
    prompt still contains the plan after compaction."""
    provider = _Provider(["DONE"])
    agent = _mk_agent(
        tmp_path,
        {"compaction": {"summarizeWithModel": False, "recentTokens": 50}},
        provider=provider,
    )
    # Seed enough messages to trigger compaction
    _seed(agent, count=15)

    # Set a plan (simulating what happens after a plan tool call)
    agent._plan = "1. analyze\n2. fix\n3. verify"
    # Also append a plan message so it survives compaction
    agent.session_manager.append_message({
        "role": "user",
        "customType": "plan",
        "content": "1. analyze\n2. fix\n3. verify",
        "timestamp": 1234567890,
    })

    # Compact
    result = await agent.compact()
    assert result["skipped"] is False

    # Plan is still set
    assert agent._plan == "1. analyze\n2. fix\n3. verify"

    # The plan message is still in the jsonl tree
    entries = agent.session_manager.get_entries()
    plan_entries = [
        e for e in entries
        if e.get("type") == "message" and e.get("message", {}).get("customType") == "plan"
    ]
    # Should also find plan messages stored via append_message (type: "message")
    if not plan_entries:
        plan_entries = [
            e for e in entries
            if isinstance(e, dict) and e.get("customType") == "plan"
        ]
    assert len(plan_entries) >= 1
    content = plan_entries[-1].get("message", {}).get("content") or plan_entries[-1].get("content", "")
    assert content == "1. analyze\n2. fix\n3. verify"

    # The next system prompt (built from messages) should still contain the plan
    prompt = agent._build_runtime_system_prompt()
    assert "# Active Plan" in prompt
    assert "1. analyze" in prompt


def test_runtime_prompt_contains_current_date(tmp_path):
    """Phase 16: the runtime system prompt ends with a fresh '# Current Date' section."""
    from datetime import datetime

    agent = _mk_agent(tmp_path, {})
    prompt = agent._build_runtime_system_prompt()
    now = datetime.now().astimezone()
    offset = now.strftime("%z") or "+0000"
    assert "# Current Date" in prompt
    assert f"Today is {now:%Y-%m-%d} ({now:%A})" in prompt
    assert f"{now:%H:%M} local time" in prompt
    assert f"UTC{offset[:3]}:{offset[3:]}" in prompt
    # The section is appended after the base prompt / plan blocks.
    assert prompt.index("# Current Date") > 0


def test_agent_normalizes_missing_context_window(tmp_path):
    """Phase 17: a model resolved at runtime without a context window (e.g.
    fetched via /login) still gets the fallback window so the ctx gauge and
    compaction work — both via __init__ and set_model."""
    from one.core.agent_session import FALLBACK_CONTEXT_WINDOW
    from one.core.types import ModelInfo

    model = ModelInfo(provider="ollama-cloud", id="nemotron-3-ultra", context_window=None)
    agent = _mk_agent(tmp_path, {}, model=model)
    assert agent.model is not None and agent.model.context_window == FALLBACK_CONTEXT_WINDOW

    usage = agent.get_context_usage()
    assert usage is not None
    assert usage["contextWindow"] == FALLBACK_CONTEXT_WINDOW


@pytest.mark.asyncio
async def test_set_model_normalizes_missing_context_window(tmp_path):
    from one.core.agent_session import FALLBACK_CONTEXT_WINDOW
    from one.core.types import ModelInfo

    agent = _mk_agent(tmp_path, {})
    other = ModelInfo(provider="openrouter", id="some/model", context_window=None)
    await agent.set_model(other)
    assert agent.model is not None and agent.model.context_window == FALLBACK_CONTEXT_WINDOW


@pytest.mark.asyncio
async def test_auto_compaction_not_below_threshold(tmp_path):
    provider = _Provider(["DONE"])
    agent = _mk_agent(
        tmp_path,
        {"compaction": {"thresholdPercent": 100.0, "recentTokens": 4, "minKeptMessages": 1, "summarizeWithModel": False}},
        provider=provider,
    )
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("a" * 40)
    assert not any(e["type"] == "compaction_start" for e in events)
    assert not any(m.get("customType") == "compaction_summary" for m in agent.messages)
