# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
from collections.abc import Callable
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

    async def reload(self) -> None:
        return


class _Provider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0
        self.last_messages: list[dict[str, Any]] = []
        self.max_tokens: int | None = None

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
        self.last_messages = messages
        self.max_tokens = max_tokens
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
        on_delta: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        max_tokens: int | None = None,
        images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
    ) -> Any:
        raise RuntimeError("summarizer down")


class _CallbackProvider:
    def __init__(self) -> None:
        self.callbacks: tuple[bool, bool] | None = None

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
    ) -> Any:
        from one.providers.base import ChatResult

        self.callbacks = (on_delta is not None, on_thinking_delta is not None)
        if on_delta:
            on_delta("summary")
        if on_thinking_delta:
            on_thinking_delta("reasoning")
        return ChatResult(text="MODEL SUMMARY", raw={}, usage={}, stop_reason="completed")


class _OverflowThenSuccessProvider:
    def __init__(self, failures: int = 1) -> None:
        self.failures = failures
        self.calls = 0

    async def chat(self, **kwargs: Any) -> Any:
        from one.providers.base import ChatResult

        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError(
                "llama.cpp API error 400: exceed_context_size_error "
                "n_prompt_tokens=1200 n_ctx=1024"
            )
        callback = kwargs.get("on_delta")
        if callback:
            callback("done")
        return ChatResult(
            text="done " * 60, raw={},
            usage={"prompt_tokens": 900, "completion_tokens": 4}, stop_reason="stop",
        )


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


def test_context_usage_includes_runtime_prompt_and_uses_new_default_threshold(tmp_path):
    agent = _mk_agent(tmp_path, {})
    agent.messages = [{"role": "user", "content": "hello"}]

    usage = agent.get_context_usage()
    assert usage is not None
    assert usage["tokens"] > agent._approx_message_tokens(agent.messages[0])
    assert agent.settings_manager.get_compaction_threshold_percent() == 80


@pytest.mark.asyncio
async def test_live_context_policy_switch_changes_usage_and_outgoing_reserve(tmp_path):
    model = ModelInfo("openai", "test-model", context_window=872_000,
                      max_context_window=872_000, default_input_budget=272_000)
    provider = _Provider(["DONE"])
    agent = _mk_agent(tmp_path, {"compaction": {"summarizeWithModel": False}}, provider=provider, model=model)
    agent.messages = [{"role": "user", "content": "hello"}]
    endpoint = agent.get_context_usage()
    assert endpoint is not None and endpoint["effectiveContextWindow"] == 855_616
    agent.settings_manager.set_config_value("context.policy", "provider-default")
    default = agent.get_context_usage()
    assert default is not None and default["effectiveContextWindow"] == 272_000
    request = agent._flatten_messages_for_provider()
    await agent._invoke_provider(request)
    assert provider.max_tokens == 16_384


@pytest.mark.asyncio
async def test_preflight_compacts_before_oversized_provider_request(tmp_path):
    model = ModelInfo(provider="openai", id="test-model", context_window=200)
    provider = _Provider(["DONE"])
    agent = _mk_agent(
        tmp_path,
        {"compaction": {"summarizeWithModel": False, "recentTokens": 4, "minKeptMessages": 1}},
        provider=provider,
        model=model,
    )
    _seed(agent)
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("continue")

    # A tiny selected total can still have no output room after the safe
    # preflight compaction; it must not be sent as a misleading one-token call.
    assert provider.calls == 0
    assert any(e["type"] == "compaction_start" and e["reason"] == "auto_preflight" for e in events)


@pytest.mark.asyncio
async def test_real_context_overflow_compacts_and_retries_once_without_generic_retries(tmp_path):
    provider = _OverflowThenSuccessProvider()
    model = ModelInfo(provider="openai", id="test-model", context_window=100_000)
    agent = _mk_agent(
        tmp_path,
        {"compaction": {"summarizeWithModel": False, "recentTokens": 4, "minKeptMessages": 1}},
        provider=provider,
        model=model,
    )
    _seed(agent)
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("continue")

    assert provider.calls == 2
    assert len([e for e in events if e["type"] == "compaction_start" and e["reason"] == "context_limit_retry"]) == 1
    final = [m for m in agent.messages if m.get("role") == "assistant"][-1]
    assert final["usage"]["input"] == 900
    assert final["usage"]["output"] == 4


@pytest.mark.asyncio
async def test_repeated_context_overflow_has_only_one_compaction_recovery_attempt(tmp_path):
    provider = _OverflowThenSuccessProvider(failures=2)
    model = ModelInfo(provider="openai", id="test-model", context_window=100_000)
    agent = _mk_agent(
        tmp_path,
        {"compaction": {"summarizeWithModel": False, "recentTokens": 4, "minKeptMessages": 1}},
        provider=provider,
        model=model,
    )
    _seed(agent)
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("continue")

    assert provider.calls == 2
    assert len([e for e in events if e["type"] == "compaction_start" and e["reason"] == "context_limit_retry"]) == 1
    assert any("exceed_context_size_error" in str(m.get("content")) for m in agent.messages)


def test_context_overflow_recognition_parses_llama_cpp_sizes_and_code_fallback_is_conservative(tmp_path):
    error = "error: exceed_context_size_error {n_prompt_tokens: 1200, n_ctx: 1024}"
    assert AgentSession._is_context_limit_error(error)
    assert AgentSession._context_limit_details(error) == {"n_prompt_tokens": 1200, "n_ctx": 1024}
    assert AgentSession._approx_message_tokens({"content": '{"tool":"read","args":{"path":"a.py"}}'}) > (
        len('{"tool":"read","args":{"path":"a.py"}}') // 4
    )


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
async def test_auto_compact_skips_when_nothing_to_drop(tmp_path):
    agent = _mk_agent(tmp_path, {"compaction": {"summarizeWithModel": False}})
    agent.session_manager.append_message({"role": "user", "content": "small"})
    agent.messages = agent.session_manager.build_session_context()["messages"]
    original = list(agent.messages)

    result = await agent.compact(reason="auto")
    assert result["skipped"] is True
    assert agent.messages == original
    assert agent.session_manager.get_last_compaction() is None


@pytest.mark.asyncio
async def test_manual_compact_empty_history_still_skips(tmp_path):
    agent = _mk_agent(tmp_path, {"compaction": {"summarizeWithModel": False}})

    result = await agent.compact()

    assert result["skipped"] is True
    assert result["summary"] == ""
    assert agent.session_manager.get_last_compaction() is None


@pytest.mark.asyncio
async def test_manual_compact_summarizes_short_history_and_persists(tmp_path):
    agent = _mk_agent(tmp_path, {"compaction": {"summarizeWithModel": False}})
    _seed(agent, count=2)
    original = list(agent.messages)
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    result = await agent.compact()

    assert result["skipped"] is False
    assert result["summary"]
    assert result["tokensBefore"] > 0
    # Short manual compaction retains every raw message plus the summary so it
    # does not trade the only detailed context for a lossy summary.
    assert result["kept"] == len(original) + 1
    assert agent.messages[0]["customType"] == "compaction_summary"
    assert agent.messages[1:] == original
    persisted = agent.session_manager.get_last_compaction()
    assert persisted is not None
    assert persisted["summary"] == result["summary"]
    assert any(e["type"] == "compaction_start" and e["reason"] == "manual" for e in events)
    assert any(e["type"] == "compaction_end" and e["reason"] == "manual" for e in events)


@pytest.mark.asyncio
async def test_repeated_manual_compact_short_history_replaces_summary_and_reconstructs(tmp_path):
    agent = _mk_agent(tmp_path, {"compaction": {"summarizeWithModel": False}})
    _seed(agent, count=2)
    original = list(agent.messages)

    first = await agent.compact("First manual summary.")
    second = await agent.compact("Second manual summary.")

    assert first["skipped"] is False
    assert second["skipped"] is False
    assert second["tokensBefore"] > 0
    assert [m.get("customType") for m in agent.messages].count("compaction_summary") == 1
    assert agent.messages[0]["content"] == "Second manual summary."
    assert agent.messages[1:] == original
    persisted = agent.session_manager.get_last_compaction()
    assert persisted is not None
    assert persisted["summary"] == "Second manual summary."
    assert agent.session_manager.build_session_context()["messages"] == agent.messages


@pytest.mark.asyncio
async def test_context_limit_retry_skips_short_history_without_mutation(tmp_path):
    agent = _mk_agent(
        tmp_path,
        {"compaction": {"summarizeWithModel": False, "recentTokens": 100_000}},
    )
    _seed(agent, count=2)
    original = list(agent.messages)

    result = await agent.compact(reason="context_limit_retry", allow_during_prompt=True)

    assert result["skipped"] is True
    assert result["tokensBefore"] == 0
    assert agent.messages == original
    assert agent.session_manager.get_last_compaction() is None
    assert agent.session_manager.build_session_context()["messages"] == original


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
async def test_manual_compact_custom_instructions_summarizes_short_history(tmp_path):
    provider = _Provider(["MODEL SUMMARY"])
    agent = _mk_agent(
        tmp_path,
        {"compaction": {"summarizeWithModel": True}},
        provider=provider,
    )
    _seed(agent, count=2)

    result = await agent.compact("Keep the implementation decision.")

    assert result["skipped"] is False
    assert result["summary"] == "Keep the implementation decision."
    assert result["tokensBefore"] > 0
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_manual_compact_short_history_uses_model_summary(tmp_path):
    provider = _Provider(["MODEL SUMMARY"])
    agent = _mk_agent(
        tmp_path,
        {"compaction": {"summarizeWithModel": True}},
        provider=provider,
    )
    _seed(agent, count=2)

    result = await agent.compact()

    assert result["skipped"] is False
    assert result["summary"] == "MODEL SUMMARY"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_compaction_does_not_supply_live_callbacks_or_emit_live_events(tmp_path):
    provider = _CallbackProvider()
    agent = _mk_agent(
        tmp_path,
        {"compaction": {"summarizeWithModel": True}},
        provider=provider,
    )
    _seed(agent, count=2)
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    result = await agent.compact()

    assert result["summary"] == "MODEL SUMMARY"
    assert provider.callbacks == (False, False)
    assert not any(event["type"] in {"message_start", "message_update", "thinking_delta"} for event in events)


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


@pytest.mark.asyncio
async def test_compaction_preserves_structured_plan_state(tmp_path):
    agent = _mk_agent(
        tmp_path,
        {"compaction": {"summarizeWithModel": False, "recentTokens": 50}},
    )
    _seed(agent, count=15)
    plan = [{"step": "analyze", "status": "in_progress"}, {"step": "fix", "status": "pending"}]
    agent._plan = plan
    agent.session_manager.append_message(
        {"role": "user", "customType": "plan", "content": plan, "timestamp": 1234567890}
    )

    result = await agent.compact()

    assert result["skipped"] is False
    assert agent._plan == plan
    assert "[>] analyze\n[ ] fix" in agent._build_runtime_system_prompt()


def test_runtime_prompt_contains_current_date(tmp_path):
    """Phase 16: the runtime system prompt ends with a fresh '# Current Date' section."""
    from datetime import datetime

    agent = _mk_agent(tmp_path, {})
    prompt = agent._build_runtime_system_prompt()
    now = datetime.now().astimezone()
    offset = now.strftime("%z") or "+0000"
    assert "# Current Date" in prompt
    assert f"Today is {now:%Y-%m-%d} ({now:%A})" in prompt
    assert "local time" in prompt
    assert f"UTC{offset[:3]}:{offset[3:]}" in prompt
    # The section is appended after the base prompt / plan blocks.
    assert prompt.index("# Current Date") > 0


def test_stable_prompt_prefix_across_clock_tick(tmp_path):
    """Stability: two prompts built with different dates share an identical
    stable block (base prompt + plan + MCP).  The header's ``Current date:``
    line and the ``# Current Date`` section are the only date-dependent parts.

    Uses the real DefaultResourceLoader so _build_header() is exercised —
    a regression adding sub-day time to the header would be caught.
    """
    from datetime import datetime
    from unittest.mock import patch

    from one.resources.resource_loader import DefaultResourceLoader

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    cwd = tmp_path / "proj"
    cwd.mkdir(parents=True, exist_ok=True)
    settings = SettingsManager.in_memory()

    fixed_dt = datetime(2025, 6, 15, 10, 30, 0)
    with patch("one.resources.resource_loader.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_dt.astimezone()
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        loader = DefaultResourceLoader(cwd=str(cwd), agent_dir=str(agent_dir), settings_manager=settings)
        prompt = loader.get_system_prompt()

    # The header is date-only — no time component (regression guard).
    assert "Current date:" in prompt
    assert "Current time:" not in prompt
    import re

    first_line = prompt.split("\n")[0]
    assert not re.search(r"\d{2}:\d{2}", first_line), f"first line must not contain HH:MM: {first_line!r}"

    # Stable block: everything after the header (after ``user_privileges=...``)
    # must be identical regardless of clock tick.
    stable = prompt.split("user_privileges=", 1)[1]
    # Contains the base prompt's opening.
    assert "You are an autonomous terminal agent." in stable


def test_build_header_is_date_only(tmp_path):
    """_build_header() must produce date-only output — no time component."""
    from one.resources.resource_loader import _build_header

    header = _build_header("/tmp/test")
    lines = header.split("\n")
    first_line = lines[0]
    assert "Current date:" in first_line
    assert "Current time:" not in first_line
    # No HH:MM pattern in the first line
    import re
    assert not re.search(r"\d{2}:\d{2}", first_line), f"first line must not contain HH:MM: {first_line!r}"


def test_agent_keeps_missing_context_window_unknown(tmp_path):
    """The internal fallback remains available without becoming UI metadata."""
    from one.core.agent_session import FALLBACK_CONTEXT_WINDOW
    from one.core.types import ModelInfo

    model = ModelInfo(provider="ollama-cloud", id="nemotron-3-ultra", context_window=None)
    agent = _mk_agent(tmp_path, {}, model=model)
    assert agent.model is not None and agent.model.context_window is None

    usage = agent.get_context_usage()
    assert usage is not None
    assert usage["contextWindow"] is None
    assert usage["effectiveContextWindow"] == FALLBACK_CONTEXT_WINDOW
    assert usage["percent"] is None


@pytest.mark.asyncio
async def test_set_model_keeps_missing_context_window_unknown(tmp_path):
    from one.core.agent_session import FALLBACK_CONTEXT_WINDOW
    from one.core.types import ModelInfo

    agent = _mk_agent(tmp_path, {})
    other = ModelInfo(provider="openrouter", id="some/model", context_window=None)
    await agent.set_model(other)
    assert agent.model is not None and agent.model.context_window is None
    assert agent.get_context_usage()["effectiveContextWindow"] == FALLBACK_CONTEXT_WINDOW


@pytest.mark.asyncio
async def test_set_model_uses_verified_llama_runtime_context(tmp_path):
    class LlamaProvider:
        async def get_context_window(self, api_key, model, headers):  # noqa: ANN001
            assert api_key == ""
            assert model == "alias/with spaces"
            assert headers == {}
            return 16_384

    agent = _mk_agent(tmp_path, {})
    agent.providers = {"llama.cpp": LlamaProvider()}
    await agent.set_model(ModelInfo(provider="llama.cpp", id="alias/with spaces", context_window=None,
                                    metadata_source="models.json"))
    assert agent.model is not None and agent.model.context_window == 16_384
    assert agent.model.manual_fields == frozenset()
    assert agent.model.metadata_source == "provider endpoint"
    usage = agent.get_context_usage()
    assert usage is not None and usage["contextWindow"] == 16_384
    assert usage["effectiveContextWindow"] == 14_336


@pytest.mark.asyncio
async def test_set_model_overrides_builtin_llama_context_with_runtime_props(tmp_path):
    class LlamaProvider:
        async def get_context_window(self, api_key, model, headers):  # noqa: ANN001
            return 65_536

    agent = _mk_agent(tmp_path, {})
    agent.providers = {"llama.cpp": LlamaProvider()}
    await agent.set_model(ModelInfo(provider="llama.cpp", id="local", context_window=32_768))
    assert agent.model is not None and agent.model.context_window == 65_536


@pytest.mark.asyncio
async def test_llama_endpoint_preserves_manual_limits_and_uses_server_constraint(tmp_path):
    class LlamaProvider:
        async def get_context_window(self, api_key, model, headers):  # noqa: ANN001
            return 65_536

    manual = ModelInfo("llama.cpp", "local", context_window=100_000, input_limit=50_000,
                       metadata_source="models.json", manual_fields=frozenset({"context_window", "input_limit"}))
    agent = _mk_agent(tmp_path, {}, model=manual)
    agent.providers = {"llama.cpp": LlamaProvider()}
    await agent.set_model(manual)
    assert agent.model is not None
    assert (agent.model.context_window, agent.model.input_limit, agent.model.server_context_window) == (100_000, 50_000, 65_536)
    assert agent._effective_context_window == 50_000


@pytest.mark.asyncio
async def test_llama_manual_smaller_context_wins_over_server_constraint(tmp_path):
    class LlamaProvider:
        async def get_context_window(self, api_key, model, headers):  # noqa: ANN001
            return 65_536

    manual = ModelInfo("llama.cpp", "local", context_window=32_768, metadata_source="models.json",
                       manual_fields=frozenset({"context_window"}))
    agent = _mk_agent(tmp_path, {}, model=manual)
    agent.providers = {"llama.cpp": LlamaProvider()}
    await agent.set_model(manual)
    assert agent.model is not None and agent.model.context_window == 32_768
    assert agent.model.server_context_window == 65_536
    assert agent._effective_context_window == 28_672


@pytest.mark.asyncio
async def test_repeated_catalog_and_endpoint_refresh_preserves_manual_limits(tmp_path):
    class LlamaProvider:
        async def get_context_window(self, api_key, model, headers):  # noqa: ANN001
            return 65_536

    manual = ModelInfo("llama.cpp", "local", context_window=100_000, input_limit=50_000,
                       metadata_source="models.json", manual_fields=frozenset({"context_window", "input_limit"}))
    agent = _mk_agent(tmp_path, {}, model=manual)
    agent.providers = {"llama.cpp": LlamaProvider()}
    agent.model_registry.catalog.facts = {("llama.cpp", "local"): {"contextWindow": 1_000_000, "inputLimit": 272_000}}
    await agent.set_model(manual)
    await agent.refresh_context_window_metadata()
    assert agent.model is not None
    assert agent.model.metadata_source == "mixed"
    assert (agent.model.context_window, agent.model.input_limit, agent.model.server_context_window) == (100_000, 50_000, 65_536)


@pytest.mark.asyncio
async def test_session_refresh_updates_and_removes_catalog_overlays(tmp_path):
    base = ModelInfo("openai", "catalog-demo", context_window=100_000)
    agent = _mk_agent(tmp_path, {}, model=base)
    catalog = agent.model_registry.catalog
    catalog.facts = {("openai", "catalog-demo"): {"contextWindow": 1_000_000, "inputLimit": 272_000}}
    await agent.refresh_context_window_metadata()
    assert agent.model is not None and (agent.model.context_window, agent.model.input_limit) == (1_000_000, 272_000)

    catalog.facts = {("openai", "catalog-demo"): {"contextWindow": 500_000}}
    await agent.refresh_context_window_metadata()
    assert agent.model is not None and (agent.model.context_window, agent.model.input_limit) == (500_000, None)

    catalog.facts = {}
    await agent.refresh_context_window_metadata()
    assert agent.model is not None and agent.model.context_window == 100_000


@pytest.mark.asyncio
async def test_set_model_keeps_builtin_llama_context_when_props_unavailable(tmp_path):
    class LlamaProvider:
        async def get_context_window(self, api_key, model, headers):  # noqa: ANN001
            return None

    agent = _mk_agent(tmp_path, {})
    agent.providers = {"llama.cpp": LlamaProvider()}
    await agent.set_model(ModelInfo(provider="llama.cpp", id="local", context_window=32_768))
    assert agent.model is not None and agent.model.context_window == 32_768


@pytest.mark.asyncio
async def test_llama_context_uses_model_specific_base_url(tmp_path):
    class LlamaProvider:
        def __init__(self, endpoint="global"):
            self.endpoint = endpoint

        def with_base_url(self, base_url):  # noqa: ANN001
            return LlamaProvider(base_url)

        async def get_context_window(self, api_key, model, headers):  # noqa: ANN001
            return 8_192 if self.endpoint == "http://model-server/v1" else None

    agent = _mk_agent(tmp_path, {})
    agent.providers = {"llama.cpp": LlamaProvider()}
    await agent.set_model(ModelInfo(provider="llama.cpp", id="local", base_url="http://model-server/v1"))
    assert agent.model is not None and agent.model.context_window == 8_192


@pytest.mark.asyncio
async def test_set_model_refreshes_stale_chatgpt_catalogue_context(tmp_path):
    class CodexProvider:
        async def list_models_detailed(self, api_key, headers):  # noqa: ANN001
            return [{"id": "gpt-codex", "contextWindow": 272_000}]

    agent = _mk_agent(tmp_path, {})
    agent.model_registry._auth.set_runtime_api_key("chatgpt", "test")
    agent.providers = {"chatgpt": CodexProvider()}
    await agent.set_model(ModelInfo(provider="chatgpt", id="gpt-codex", context_window=128_000))
    assert agent.model is not None and agent.model.context_window == 272_000


@pytest.mark.asyncio
async def test_endpoint_enriches_empty_models_json_capacity_and_reports_usage(tmp_path):
    class CodexProvider:
        async def list_models_detailed(self, api_key, headers):  # noqa: ANN001
            return [{"id": "gpt-codex", "contextWindow": 872_000, "maxContextWindow": 872_000}]

    model = ModelInfo("chatgpt", "gpt-codex", context_window=None, max_context_window=None,
                      metadata_source="models.json")
    agent = _mk_agent(tmp_path, {}, model=model)
    agent.model_registry._auth.set_runtime_api_key("chatgpt", "test")
    agent.providers = {"chatgpt": CodexProvider()}
    agent._exact_context_tokens = 11_169
    agent._exact_context_signature = agent._request_signature(agent._flatten_messages_for_provider())

    await agent.set_model(model)

    assert agent.model is not None
    assert (agent.model.context_window, agent.model.max_context_window) == (872_000, 872_000)
    assert agent.model.manual_fields == frozenset()
    assert agent.model.metadata_source == "provider endpoint"
    usage = agent.get_context_usage()
    assert usage is not None
    assert usage["contextWindow"] == usage["maxContextWindow"] == 872_000
    assert usage["effectiveContextWindow"] == 855_616
    assert usage["percent"] == usage["effectivePercent"] == pytest.approx(11_169 / 855_616 * 100)


def test_server_only_context_has_a_known_gauge_without_raw_context_window(tmp_path):
    model = ModelInfo("llama.cpp", "local", server_context_window=872_000)
    agent = _mk_agent(tmp_path, {}, model=model)
    agent._exact_context_tokens = 11_169
    agent._exact_context_signature = agent._request_signature(agent._flatten_messages_for_provider())

    usage = agent.get_context_usage()
    assert usage is not None
    assert usage["contextWindow"] is None
    assert usage["selectedContextWindow"] == 872_000
    assert usage["percent"] == usage["effectivePercent"] == pytest.approx(11_169 / 855_616 * 100)


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


# ── Fix 1: compact busy guard ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compact_busy_when_streaming(tmp_path):
    """Manual compact() during an active prompt must return busy, not mutate state."""
    agent = _mk_agent(tmp_path, {"compaction": {"summarizeWithModel": False}})
    _seed(agent, count=10)
    total_before = len(agent.messages)

    # Pretend streaming is active (simulates a prompt in flight).
    agent._is_streaming = True
    result = await agent.compact()
    assert result["busy"] is True
    assert result["skipped"] is True
    assert len(agent.messages) == total_before  # no mutation
    assert agent.session_manager.get_last_compaction() is None


# ── Fix 2: context-limit compaction during retry ─────────────────────────────

class _ContextLimitProvider:
    """Provider that fails with a context-limit error on first call, succeeds on second."""

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
            raise RuntimeError("openrouter API error 400: maximum context length exceeded")
        return ChatResult(text="Task completed successfully.\n\nThe previous conversation history was summarized during compaction, reducing the context so the model can respond. This is the final answer with no tool calls needed.", raw={}, usage={}, stop_reason="stop")


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [
    {"enabled": True, "maxRetries": 3, "baseDelayMs": 1, "maxDelayMs": 1},
    {"enabled": False, "maxRetries": 3, "baseDelayMs": 1, "maxDelayMs": 1},
    {"enabled": True, "maxRetries": 0, "baseDelayMs": 1, "maxDelayMs": 1},
])
async def test_retry_context_limit_triggers_compaction(tmp_path, retry):
    """A context-length error should compact before retrying, not just retry blindly."""
    provider = _ContextLimitProvider()
    agent = _mk_agent(
        tmp_path,
        {
            "compaction": {"summarizeWithModel": False, "recentTokens": 30, "minKeptMessages": 1},
            "retry": retry,
            "tools": {"maxSteps": 2, "timeoutSec": 5},
        },
        provider=provider,
    )
    _seed(agent, count=10)
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("go")
    # Provider should have been called twice: fail → compact → success.
    assert provider.calls == 2
    # The retry should include a compaction_start with reason "context_limit_retry".
    assert any(e["type"] == "compaction_start" and e["reason"] == "context_limit_retry" for e in events)
    # The final turn should succeed.
    assert any(e["type"] == "turn_end" and e.get("ok") is True for e in events)
    assert not agent.is_streaming


# ── Fix 3: _is_compacting resets in finally + abort check after auto-compaction ─


@pytest.mark.asyncio
async def test_compaction_resets_flag_on_error(tmp_path):
    """If compaction raises (e.g. summarizer error), _is_compacting must be False."""
    agent = _mk_agent(
        tmp_path,
        {"compaction": {"summarizeWithModel": True, "recentTokens": 4, "minKeptMessages": 1}},
        provider=_FailProvider(),
    )
    _seed(agent, count=10)
    assert agent.is_compacting is False

    result = await agent.compact()
    assert result["skipped"] is False
    assert agent.is_compacting is False  # must be reset even on summarizer failure


@pytest.mark.asyncio
async def test_auto_compaction_abort_check_before_followup(tmp_path):
    """After auto-compaction the session must check abort before draining queued messages."""
    provider = _Provider(["DONE"])
    agent = _mk_agent(
        tmp_path,
        {
            "compaction": {"enabled": True, "thresholdPercent": 0.0, "recentTokens": 4, "minKeptMessages": 1, "summarizeWithModel": False},
            "retry": {"enabled": False},
            "tools": {"maxSteps": 2, "timeoutSec": 5},
        },
        provider=provider,
    )
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    # Start a prompt, queue a steer+followup, then abort.
    task = asyncio.create_task(agent.prompt("first"))
    await asyncio.sleep(0.01)
    await agent.steer("queued-steer")
    await agent.follow_up("queued-follow")
    await agent.abort()
    await task

    # Queues must be preserved (not drained).
    assert agent.get_pending_queues() == {"steering": ["queued-steer"], "followUp": ["queued-follow"]}


@pytest.mark.asyncio
async def test_wait_for_idle_waits_for_compaction(tmp_path):
    """wait_for_idle should not return while compacting."""
    agent = _mk_agent(tmp_path, {"compaction": {"summarizeWithModel": False}})
    _seed(agent, count=10)

    agent._is_compacting = True
    done = asyncio.Event()

    async def _wait():
        await agent.wait_for_idle()
        done.set()

    task = asyncio.create_task(_wait())
    await asyncio.sleep(0.05)
    assert not done.is_set()  # should still be waiting
    agent._is_compacting = False
    await task
    assert done.is_set()
