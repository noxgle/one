"""Regression tests for provider timeout, fail-closed wrapper, and idle watchdog.

These tests verify that:
1. Provider timeouts are enforced by the outer deadline in `_invoke_provider`
2. The fail-closed wrapper in `prompt()` resets state on unexpected exceptions
3. Idle watchdogs in providers abort streams on SSE idle
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager


class _Loader:
    """Minimal resource loader for tests."""

    def load_prompt(self, name: str) -> str:
        return ""


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
    settings = SettingsManager.in_memory(
        settings_override or {"tools": {"maxSteps": 4, "timeoutSec": 5}}
    )
    session = SessionManager.in_memory(str(tmp_path))
    return AgentSession(
        session, settings, registry, _Loader(), model, "medium", tools=tools
    )


# ---------------------------------------------------------------------------
# Fake provider that simulates timeout
# ---------------------------------------------------------------------------


class _SlowProvider:
    """Provider that never returns — simulates a hanging request."""

    def __init__(self, chat_delay: float = 10.0) -> None:
        self.chat_delay = chat_delay
        self.chat_call_count = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: Any,
        on_delta: Any = None,
        on_thinking_delta: Any = None,
        **kwargs: Any,
    ) -> Any:
        self.chat_call_count += 1
        await asyncio.sleep(self.chat_delay)  # Never returns
        return None  # unreachable


# ---------------------------------------------------------------------------
# Test: Outer deadline timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_timeout_emits_turn_end_and_resets_streaming(
    tmp_path: Path,
) -> None:
    """Provider timeout should emit turn_end, reset _is_streaming, and not hang."""
    agent = _mk_agent(
        tmp_path,
        settings_override={
            "tools": {"maxSteps": 4, "timeoutSec": 5},
            # Minimum timeout is 10s (enforced by get_provider_timeout_sec)
            "providers": {"timeoutSec": 10},
            # Single attempt, fast: maxRetries=0 yields willRetry=False.
            "retry": {"enabled": True, "maxRetries": 0, "baseDelayMs": 1, "maxDelayMs": 1},
        },
    )
    # Use 20s sleep to avoid race condition with 10s timeout
    agent.providers = {"openai": _SlowProvider(chat_delay=20.0)}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    # prompt() handles the timeout internally — emits turn_end/agent_end,
    # resets _is_streaming, and returns (never re-raises).  Use wait_for
    # as a regression backstop so a real hang fails the suite instead of
    # blocking forever.
    await asyncio.wait_for(agent.prompt("test timeout"), timeout=30)

    # Check that _is_streaming is reset
    assert agent._is_streaming is False

    # Check that turn_end was emitted with error
    turn_end_events = [e for e in events if e.get("type") == "turn_end"]
    assert len(turn_end_events) >= 1
    assert turn_end_events[-1].get("ok") is False

    # Check that agent_end was emitted (terminal event)
    agent_end_events = [e for e in events if e.get("type") == "agent_end"]
    assert len(agent_end_events) >= 1

    # Provider was called exactly once (no retries with maxRetries=0)
    assert agent.providers["openai"].chat_call_count == 1


# ---------------------------------------------------------------------------
# Test: Fail-closed wrapper
# ---------------------------------------------------------------------------


class _FailingProvider:
    """Provider that raises an unexpected exception."""

    def __init__(self, error_msg: str = "unexpected error") -> None:
        self.error_msg = error_msg
        self.chat_call_count = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: Any,
        on_delta: Any = None,
        on_thinking_delta: Any = None,
        **kwargs: Any,
    ) -> Any:
        self.chat_call_count += 1
        raise RuntimeError(self.error_msg)


@pytest.mark.asyncio
async def test_fail_closed_resets_state_on_unexpected_error(
    tmp_path: Path,
) -> None:
    """Unexpected exceptions should trigger fail-closed, resetting _is_streaming."""
    agent = _mk_agent(
        tmp_path,
        settings_override={
            "retry": {"enabled": True, "maxRetries": 0, "baseDelayMs": 1, "maxDelayMs": 1},
        },
    )
    agent.providers = {"openai": _FailingProvider("unexpected error")}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    # prompt() handles the unexpected exception internally — emits terminal
    # events, resets state, and returns.  No exception propagates to caller.
    await asyncio.wait_for(agent.prompt("test fail-closed"), timeout=30)

    # Check that _is_streaming is reset
    assert agent._is_streaming is False
    assert agent._retrying is False
    assert agent._abort_requested is False

    # Check that turn_end was emitted with error
    turn_end_events = [e for e in events if e.get("type") == "turn_end"]
    assert len(turn_end_events) >= 1
    assert turn_end_events[-1].get("ok") is False

    # Check that agent_end was emitted (terminal event)
    agent_end_events = [e for e in events if e.get("type") == "agent_end"]
    assert len(agent_end_events) >= 1


# ---------------------------------------------------------------------------
# Test: Provider timeout setting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_timeout_setting_respected(tmp_path: Path) -> None:
    """Provider timeout setting should be respected by the outer deadline."""
    agent = _mk_agent(
        tmp_path,
        settings_override={
            "tools": {"maxSteps": 4, "timeoutSec": 5},
            # Minimum timeout is 10s (enforced by get_provider_timeout_sec)
            "providers": {"timeoutSec": 10},
            # Single attempt, fast.
            "retry": {"enabled": True, "maxRetries": 0, "baseDelayMs": 1, "maxDelayMs": 1},
        },
    )
    agent.providers = {"openai": _SlowProvider(chat_delay=30.0)}

    import time

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    start = time.monotonic()
    await asyncio.wait_for(agent.prompt("test timeout"), timeout=30)
    elapsed = time.monotonic() - start

    # Should timeout in ~10 seconds, not 30 seconds
    # Allow some buffer for test overhead
    assert 8 < elapsed < 15, f"Timeout took {elapsed:.1f}s, expected ~10s"


# ---------------------------------------------------------------------------
# Test: Fail-closed wrapper emits terminal events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_closed_emits_terminal_events(tmp_path: Path) -> None:
    """Fail-closed wrapper should emit turn_end and agent_end on unexpected error."""
    agent = _mk_agent(
        tmp_path,
        settings_override={
            "retry": {"enabled": True, "maxRetries": 0, "baseDelayMs": 1, "maxDelayMs": 1},
        },
    )
    agent.providers = {"openai": _FailingProvider("unexpected error")}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    # prompt() handles the unexpected exception internally — emits terminal
    # events, resets state, and returns.  No exception propagates to caller.
    await asyncio.wait_for(agent.prompt("test fail-closed"), timeout=30)

    # Check that terminal events were emitted
    agent_end_events = [e for e in events if e.get("type") == "agent_end"]
    assert len(agent_end_events) >= 1

    turn_end_events = [e for e in events if e.get("type") == "turn_end"]
    assert len(turn_end_events) >= 1
    assert turn_end_events[-1].get("ok") is False

    # Provider was called exactly once (no retries with maxRetries=0)
    assert agent.providers["openai"].chat_call_count == 1


# ---------------------------------------------------------------------------
# Test: SSE idle watchdog (all four adapters)
# ---------------------------------------------------------------------------


def _make_fake_sse_stream(lines: list[str], hang_after: bool = True) -> Any:
    """Return a fake httpx Response with aiter_lines that yields *lines* then hangs.

    The stream context manager is async; aiter_lines yields each line, and
    after the last one (if *hang_after*) the async iterator never ends so
    the adapter's idle watchdog fires.
    """
    from unittest.mock import MagicMock

    resp_mock = MagicMock()
    resp_mock.is_error = False

    async def _aread() -> bytes:
        return b""

    resp_mock.aread = _aread

    line_iter = iter(lines)

    async def _aiter_lines():
        for line in line_iter:
            yield line
        if hang_after:
            # Never end — forces the idle watchdog to fire.
            await asyncio.sleep(9999)

    resp_mock.aiter_lines = _aiter_lines
    return resp_mock


class _FakeStreamContext:
    """Async context manager wrapping a fake httpx Response."""

    def __init__(self, resp: Any) -> None:
        self._resp = resp

    async def __aenter__(self) -> Any:
        return self._resp

    async def __aexit__(self, *args: Any) -> None:
        pass


@pytest.mark.asyncio
async def test_sse_idle_watchdog_openai_compatible() -> None:
    """openai_compatible adapter: idle watchdog fires on a stalled SSE stream."""
    from unittest.mock import patch

    from one.providers.openai_compatible import OpenAICompatibleAdapter

    # Two valid SSE lines, then the stream hangs forever (no [DONE]).
    lines = [
        "data: {\"choices\":[{\"delta\":{\"content\":\"hello\"}}]}",
        "data: {\"choices\":[{\"delta\":{\"content\":\" world\"}}]}",
    ]
    adapter = OpenAICompatibleAdapter("test", "http://localhost:9999")

    with patch(
        "httpx.AsyncClient.stream",
        return_value=_FakeStreamContext(_make_fake_sse_stream(lines, hang_after=True)),
    ):
        with patch.object(adapter, "_build_headers", return_value={}):
            # Patch _IDLE_SSE_TIMEOUT to 0.5 s for fast test execution.
            with patch("one.providers.openai_compatible._IDLE_SSE_TIMEOUT", 0.5):
                with pytest.raises(RuntimeError, match="idle"):
                    await adapter.chat(
                        api_key="x",
                        model="gpt-4",
                        messages=[],
                        thinking_level="medium",
                        on_delta=lambda d: None,
                    )


@pytest.mark.asyncio
async def test_sse_idle_watchdog_anthropic() -> None:
    """anthropic adapter: idle watchdog fires on a stalled SSE stream."""
    from unittest.mock import patch

    from one.providers.anthropic import AnthropicAdapter

    lines = [
        'data: {"type":"content_block_delta","delta":{"text":"hi"}}',
    ]
    adapter = AnthropicAdapter()

    with patch(
        "httpx.AsyncClient.stream",
        return_value=_FakeStreamContext(_make_fake_sse_stream(lines)),
    ):
        with patch("one.providers.anthropic._IDLE_SSE_TIMEOUT", 0.5):
            with pytest.raises(RuntimeError, match="idle"):
                await adapter.chat(
                    api_key="sk-ant-xxx",
                    model="claude-3",
                    messages=[],
                    thinking_level="medium",
                    on_delta=lambda d: None,
                )


@pytest.mark.asyncio
async def test_sse_idle_watchdog_gemini() -> None:
    """gemini adapter: idle watchdog fires on a stalled SSE stream."""
    from unittest.mock import patch

    from one.providers.gemini import GeminiAdapter

    lines = [
        'data: {"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}',
    ]
    adapter = GeminiAdapter()

    with patch(
        "httpx.AsyncClient.stream",
        return_value=_FakeStreamContext(_make_fake_sse_stream(lines)),
    ):
        with patch("one.providers.gemini._IDLE_SSE_TIMEOUT", 0.5):
            with pytest.raises(RuntimeError, match="idle"):
                await adapter.chat(
                    api_key="fake-key",
                    model="gemini-pro",
                    messages=[],
                    thinking_level="medium",
                    on_delta=lambda d: None,
                )


@pytest.mark.asyncio
async def test_sse_idle_watchdog_codex() -> None:
    """codex_responses adapter: idle watchdog fires on a stalled SSE stream."""
    from unittest.mock import patch

    from one.providers.codex_responses import CodexResponsesAdapter

    lines = [
        'data: {"type":"response.output_text.delta","delta":"starting"}',
    ]
    adapter = CodexResponsesAdapter()

    with patch(
        "httpx.AsyncClient.stream",
        return_value=_FakeStreamContext(_make_fake_sse_stream(lines)),
    ):
        with patch("one.providers.codex_responses._IDLE_SSE_TIMEOUT", 0.5):
            with pytest.raises(RuntimeError, match="idle"):
                await adapter.chat(
                    api_key="chatgpt-token",
                    model="codex",
                    messages=[],
                    thinking_level="medium",
                    on_delta=lambda d: None,
                )
