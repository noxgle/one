from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from one.providers.base import ChatResult
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


class _AlwaysFailProvider:
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
        self.calls += 1
        raise RuntimeError("always-fail")


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
        if e.get("type") == "tool_call_error":
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


@pytest.mark.asyncio
async def test_event_snapshot_tool_error_lifecycle(tmp_path: Path):
    agent = _mk_agent(tmp_path, tools=["ls"])
    agent.providers = {"openai": _Provider(['{"tool":"read","args":{"path":"missing.txt"}}', "DONE"])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("go")
    assert _compact(events) == [
        {"type": "agent_start"},
        {"type": "message_start"},
        {"type": "message_end"},
        {"type": "turn_start", "attempt": 1},
        {"type": "tool_call_start", "tool": "read"},
        {"type": "tool_call_error", "tool": "read"},
        {"type": "tool_call_end", "ok": False, "tool": "read"},
        {"type": "message_start"},
        {"type": "message_update"},
        {"type": "message_end"},
        {"type": "turn_end", "attempt": 1, "ok": True, "reason": "stop", "aborted": False},
        {"type": "agent_end"},
    ]


@pytest.mark.asyncio
async def test_event_snapshot_abort_during_retry_keeps_queue(tmp_path: Path):
    agent = _mk_agent(
        tmp_path,
        settings_override={"retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 50, "maxDelayMs": 50}},
    )
    agent.providers = {"openai": _AlwaysFailProvider()}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    task = asyncio.create_task(agent.prompt("go"))
    await asyncio.sleep(0.01)
    await agent.steer("queued-steer")
    await agent.follow_up("queued-follow")
    await agent.abort()
    await task

    compact = _compact(events)
    # First failing turn should be marked retryable.
    assert {"type": "turn_end", "attempt": 1, "ok": False, "reason": "error", "willRetry": True} in compact
    # Retry cycle should start and end as aborted (without consuming queued messages).
    assert {"type": "auto_retry_start", "attempt": 1} in compact
    assert {"type": "auto_retry_end", "attempt": 1, "willRetry": False, "aborted": True} in compact
    assert {"type": "turn_end", "attempt": 1, "ok": True, "reason": "abort", "aborted": True} in compact
    assert agent.get_pending_queues() == {"steering": ["queued-steer"], "followUp": ["queued-follow"]}


@pytest.mark.asyncio
async def test_abort_cancels_in_flight_provider_call(tmp_path: Path):
    """abort() must cancel the in-flight provider request, not just set a flag."""
    agent = _mk_agent(tmp_path)
    cancelled = asyncio.Event()

    class _CancelAwareProvider:
        async def chat(
            self,
            api_key: str,
            model: str,
            messages: list[dict[str, Any]],
            thinking_level: str,
            headers: dict[str, str] | None = None,
        ) -> Any:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return ChatResult(text="DONE", raw={}, usage={}, stop_reason="stop")

    agent.providers = {"openai": _CancelAwareProvider()}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    task = asyncio.create_task(agent.prompt("go"))
    await asyncio.sleep(0.01)
    await agent.abort()
    await task

    assert cancelled.is_set()
    assert not agent.is_streaming
    compact = _compact(events)
    assert {"type": "turn_end", "attempt": 1, "ok": True, "reason": "abort", "aborted": True} in compact
    assert {"type": "agent_end"} in compact


@pytest.mark.asyncio
async def test_event_snapshot_abort_during_retry_backoff_is_instant(tmp_path: Path):
    """abort() during a long retry backoff must return immediately, not wait out the delay."""
    agent = _mk_agent(
        tmp_path,
        settings_override={"retry": {"enabled": True, "maxRetries": 2, "baseDelayMs": 60000, "maxDelayMs": 60000}},
    )
    agent.providers = {"openai": _FlakyProvider()}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    started = time.monotonic()
    task = asyncio.create_task(agent.prompt("go"))
    await asyncio.sleep(0.01)
    await agent.abort()
    await task
    elapsed = time.monotonic() - started

    assert elapsed < 5.0  # 60s backoff was interrupted
    compact = _compact(events)
    assert {"type": "turn_end", "attempt": 1, "ok": False, "reason": "error", "willRetry": True} in compact
    assert {"type": "auto_retry_start", "attempt": 1} in compact
    assert {"type": "auto_retry_end", "attempt": 1, "willRetry": False, "aborted": True} in compact
    assert {"type": "turn_end", "attempt": 1, "ok": True, "reason": "abort", "aborted": True} in compact
    # retry never happened
    assert not any(e.get("attempt") == 2 for e in compact)


# ── Phase 24: thinking_delta event ───────────────────────────────────────────

class _ProviderWithThinking:
    """Provider that fires thinking_delta events for reasoning_content."""

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
        on_delta: Any = None,
        on_thinking_delta: Any = None,
        max_tokens: int | None = None,
    ) -> Any:
        from one.providers.base import ChatResult

        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        text = self.responses[idx]
        # Split text into "thinking" (before ---) and "content" (after ---)
        if "---" in text:
            thinking, content = text.split("---", 1)
            thinking = thinking.strip()
            content = content.strip()
            # Fire thinking_delta events
            if on_thinking_delta and thinking:
                # Emit in chunks of 4 chars to simulate streaming
                for i in range(0, len(thinking), 4):
                    on_thinking_delta(thinking[i : i + 4])
            # Fire on_delta for content
            if on_delta and content:
                on_delta(content)
            return ChatResult(text=thinking + "\n\n" + content, raw={}, usage={}, stop_reason="stop")
        return ChatResult(text=text, raw={}, usage={}, stop_reason="stop")


@pytest.mark.asyncio
async def test_thinking_delta_event_emitted(tmp_path: Path):
    """Provider fires thinking_delta events for reasoning_content."""
    agent = _mk_agent(tmp_path)
    agent.providers = {"openai": _ProviderWithThinking(["I am thinking about this\n---\nHere is the answer"])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("go")

    # Verify thinking_delta events were emitted
    thinking_events = [e for e in events if e.get("type") == "thinking_delta"]
    assert len(thinking_events) > 0

    # All chunks should be part of the thinking text
    all_thinking = "".join(e.get("delta", "") for e in thinking_events)
    assert "thinking" in all_thinking

    # The full text should contain both thinking and content
    message_updates = [e for e in events if e.get("type") == "message_update"]
    assert any("answer" in str(e) for e in message_updates)
