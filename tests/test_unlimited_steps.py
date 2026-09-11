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
    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
        return "You are a coding agent."


class _FakeProvider:
    """Provider that returns sequential responses."""

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
        on_delta: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        max_tokens: int | None = None,
        images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
    ) -> Any:
        from one.providers.base import ChatResult

        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ChatResult(text=self.responses[idx], raw={}, usage={}, stop_reason="stop")


def _mk_agent(
    tmp_path: Path,
    tools: list[str] | None = None,
    settings_override: dict | None = None,
) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory(settings_override or {"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    return AgentSession(session, settings, registry, _Loader(), model, "medium", tools=tools)


# ---------------------------------------------------------------------------
# Settings: maxSteps=0 acceptance / rejection
# ---------------------------------------------------------------------------


def test_set_tool_settings_accepts_zero() -> None:
    """maxSteps=0 is a valid value (unlimited)."""
    settings = SettingsManager.in_memory()
    settings.set_tool_settings(max_steps=0)
    assert settings.get_tool_max_steps() == 0


def test_set_tool_settings_accepts_positive() -> None:
    """Positive maxSteps values still work."""
    settings = SettingsManager.in_memory()
    settings.set_tool_settings(max_steps=10)
    assert settings.get_tool_max_steps() == 10


def test_set_tool_settings_rejects_negative() -> None:
    """Negative maxSteps must raise ValueError."""
    settings = SettingsManager.in_memory()
    with pytest.raises(ValueError, match="maxSteps must be >= 0"):
        settings.set_tool_settings(max_steps=-1)


def test_set_tool_settings_rejects_negative_via_setter() -> None:
    """Negative maxSteps through set_tool_settings must raise ValueError."""
    settings = SettingsManager.in_memory()
    with pytest.raises(ValueError, match="maxSteps must be >= 0"):
        settings.set_tool_settings(max_steps=-5)


def test_get_tool_max_steps_default_is_six() -> None:
    """Default maxSteps is 6."""
    settings = SettingsManager.in_memory()
    assert settings.get_tool_max_steps() == 6


def test_get_tool_max_steps_zero_from_initial() -> None:
    """maxSteps=0 from initial settings is read as 0."""
    settings = SettingsManager.in_memory(initial={"tools": {"maxSteps": 0}})
    assert settings.get_tool_max_steps() == 0


# ---------------------------------------------------------------------------
# Bounded mode: tool_step_limit still emitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bounded_mode_emits_tool_step_limit(tmp_path: Path) -> None:
    """With maxSteps=2, the agent hits the step limit and gets tool_step_limit."""
    agent = _mk_agent(
        tmp_path,
        tools=["ls"],
        settings_override={"tools": {"maxSteps": 2, "timeoutSec": 5}},
    )
    # Provider keeps returning tool calls — never finishes, never stops calling tools.
    agent.providers = {"openai": _FakeProvider(['{"tool":"ls","args":{"path":"."}}'] * 10)}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("Loop tools")
    last = events[-1]
    assert last["type"] == "agent_end"

    # The final assistant message should have stopReason=tool_step_limit.
    assistant_msgs = [m for m in agent.messages if m.get("role") == "assistant"]
    assert assistant_msgs
    final = assistant_msgs[-1]
    assert final.get("stopReason") == "tool_step_limit"
    assert "reached the step limit" in (final.get("content", [{}])[0].get("text", "") if isinstance(final.get("content"), list) else str(final.get("content", "")))


# ---------------------------------------------------------------------------
# Unlimited mode (maxSteps=0): more tools than default, ends via finish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unlimited_mode_executes_many_steps_then_finish(tmp_path: Path) -> None:
    """With maxSteps=0, the agent can execute more than the default 6 steps,
    ending via the finish tool."""
    agent = _mk_agent(
        tmp_path,
        tools=["ls", "read", "finish"],
        settings_override={"tools": {"maxSteps": 0, "timeoutSec": 5}},
    )
    # 5 tool calls (ls) + 1 finish call = 6 provider calls, then done.
    responses = ['{"tool":"ls","args":{"path":"."}}'] * 5 + ['{"tool":"finish","args":{"summary":"Done"}}']
    agent.providers = {"openai": _FakeProvider(responses)}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("Run many tools")

    # Count tool calls: should be 5 ls + 1 finish = 6.
    tool_starts = [e for e in events if e.get("type") == "tool_call_start"]
    assert len(tool_starts) == 6

    # The last tool should be finish.
    assert tool_starts[-1]["tool"] == "finish"

    # No tool_step_limit event should be emitted.
    tool_step_events = [e for e in events if e.get("type") == "message_end"]
    final_assistant = [m for m in agent.messages if m.get("role") == "assistant"][-1]
    assert final_assistant.get("stopReason") == "completed"

    # Should NOT have tool_step_limit stop reason.
    assert "tool_step_limit" not in str(final_assistant.get("stopReason", ""))


@pytest.mark.asyncio
async def test_unlimited_mode_exceeds_default_6_limit(tmp_path: Path) -> None:
    """With maxSteps=0, the agent can execute more than 6 tool steps.
    This proves it's not bounded by the default limit."""
    agent = _mk_agent(
        tmp_path,
        tools=["ls", "finish"],
        settings_override={"tools": {"maxSteps": 0, "timeoutSec": 5}},
    )
    # 7 tool calls (ls) + 1 finish = 8 provider calls.
    responses = ['{"tool":"ls","args":{"path":"."}}'] * 7 + ['{"tool":"finish","args":{"summary":"Done after 7 ls"}}']
    agent.providers = {"openai": _FakeProvider(responses)}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("Run 8 steps")

    tool_starts = [e for e in events if e.get("type") == "tool_call_start"]
    assert len(tool_starts) == 8

    # No tool_step_limit in the final message.
    final_assistant = [m for m in agent.messages if m.get("role") == "assistant"][-1]
    assert final_assistant.get("stopReason") == "completed"


# ---------------------------------------------------------------------------
# Unlimited mode: abort still terminates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unlimited_mode_abort_terminates(tmp_path: Path) -> None:
    """In unlimited mode, abort() must still terminate the tool loop."""
    import asyncio

    class _SlowProvider:
        async def chat(self, api_key, model, messages, thinking_level, headers=None, on_delta=None, on_thinking_delta=None, max_tokens=None, images=None, storage_dir=""):
            from one.providers.base import ChatResult
            await asyncio.sleep(60)  # blocks forever
            return ChatResult(text="DONE", raw={}, usage={}, stop_reason="stop")

    agent = _mk_agent(
        tmp_path,
        tools=["ls", "finish"],
        settings_override={"tools": {"maxSteps": 0, "timeoutSec": 5}},
    )
    agent.providers = {"openai": _SlowProvider()}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    task = asyncio.create_task(agent.prompt("go"))
    await asyncio.sleep(0.01)
    await agent.abort()
    await task

    # Should have abort in the final message.
    final_assistant = [m for m in agent.messages if m.get("role") == "assistant"][-1]
    assert final_assistant.get("stopReason") == "abort"


# ---------------------------------------------------------------------------
# Unlimited mode: budget still terminates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unlimited_mode_budget_terminates(tmp_path: Path) -> None:
    """In unlimited mode, budget limits must still terminate the tool loop."""

    class _ProviderWithUsage:
        def __init__(self, response: str) -> None:
            self.response = response
            self.calls = 0

        async def chat(self, api_key, model, messages, thinking_level, headers=None, on_delta=None, on_thinking_delta=None, max_tokens=None, images=None, storage_dir=""):
            from one.providers.base import ChatResult

            self.calls += 1
            # Each call adds 100 tokens; budget is 50 → first call exceeds it.
            return ChatResult(
                text=self.response,
                raw={},
                usage={"prompt_tokens": 100, "completion_tokens": 0},
                stop_reason="stop",
            )

    agent = _mk_agent(
        tmp_path,
        tools=["ls", "finish"],
        settings_override={"tools": {"maxSteps": 0, "timeoutSec": 5}, "budget": {"maxTokens": 50}},
    )
    agent.providers = {"openai": _ProviderWithUsage('{"tool":"ls","args":{"path":"."}}')}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("Run tools with budget")

    # Only 1 provider call before budget is exceeded.
    provider_calls = agent.providers["openai"].calls
    assert provider_calls == 1

    # Final assistant should have budget_exceeded.
    final_assistant = [m for m in agent.messages if m.get("role") == "assistant"][-1]
    assert final_assistant.get("stopReason") == "budget_exceeded"


# ---------------------------------------------------------------------------
# Unlimited mode: no-tool-call ends turn normally
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unlimited_mode_no_tool_ending(tmp_path: Path) -> None:
    """When the model returns no tool call, the turn ends with that response
    (not tool_step_limit), even in unlimited mode."""
    agent = _mk_agent(
        tmp_path,
        tools=["ls", "finish"],
        settings_override={"tools": {"maxSteps": 0, "timeoutSec": 5}},
    )
    # First call: no tool call → turn ends immediately.
    agent.providers = {"openai": _FakeProvider(["Here is the answer"])}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("Just answer")

    # No tool calls at all.
    tool_starts = [e for e in events if e.get("type") == "tool_call_start"]
    assert len(tool_starts) == 0

    # Final message is the assistant text, not tool_step_limit.
    final_assistant = [m for m in agent.messages if m.get("role") == "assistant"][-1]
    assert final_assistant.get("stopReason") != "tool_step_limit"
