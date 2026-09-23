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
from one.providers.base import ChatResult


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


class _StreamingProvider:
    """Controllable fake provider for AgentSession's token-idle supervisor."""

    def __init__(self, pieces: list[tuple[float, str, bool]], finish_delay: float = 0) -> None:
        self.pieces = pieces
        self.finish_delay = finish_delay
        self.cancelled = False

    async def chat(self, messages: list[dict[str, Any]], *, on_delta: Any = None,
                   on_thinking_delta: Any = None, **kwargs: Any) -> ChatResult:
        try:
            for delay, piece, thinking in self.pieces:
                await asyncio.sleep(delay)
                (on_thinking_delta if thinking else on_delta)(piece)
            await asyncio.sleep(self.finish_delay)
            return ChatResult(text="", raw={}, usage={}, stop_reason="stop")
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _short_idle(agent: AgentSession, seconds: float = 0.05) -> None:
    agent.settings_manager.get_provider_timeout_sec = lambda: seconds  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_streaming_activity_extends_idle_window_and_cleans_up(tmp_path: Path) -> None:
    agent = _mk_agent(tmp_path)
    _short_idle(agent)
    provider = _StreamingProvider([(0.03, "a", False), (0.03, "b", False), (0.03, "c", False)], 0.03)
    agent.providers = {"openai": provider}

    await agent._invoke_provider([])

    assert not agent._active_chat_tasks


@pytest.mark.asyncio
async def test_streaming_idle_ignores_whitespace_but_reasoning_resets_it(tmp_path: Path) -> None:
    agent = _mk_agent(tmp_path)
    _short_idle(agent)
    provider = _StreamingProvider([(0.03, "reasoning", True), (0.01, "   ", False)], 0.01)
    agent.providers = {"openai": provider}

    await agent._invoke_provider([])


@pytest.mark.asyncio
async def test_whitespace_only_stream_hits_meaningful_token_idle_timeout(tmp_path: Path) -> None:
    """Whitespace from the first delta must not extend the central idle window."""
    agent = _mk_agent(tmp_path)
    _short_idle(agent)
    stalled = _StreamingProvider([(0, "   ", False)], finish_delay=1)
    agent.providers = {"openai": stalled}

    with pytest.raises(RuntimeError, match=r"provider timed out after 0.05s"):
        await agent._invoke_provider([])

    assert stalled.cancelled
    assert not agent._active_chat_tasks


@pytest.mark.asyncio
async def test_stream_activity_precedes_slow_event_callback(tmp_path: Path) -> None:
    agent = _mk_agent(tmp_path)
    _short_idle(agent)
    agent.subscribe(lambda event: __import__("time").sleep(0.08) if event["type"] == "message_update" else None)
    agent.providers = {"openai": _StreamingProvider([(0, "meaningful streamed text", False)])}

    await agent._invoke_provider([])


@pytest.mark.asyncio
async def test_streaming_abort_cancels_task_and_keeps_abort_signal(tmp_path: Path) -> None:
    agent = _mk_agent(tmp_path)
    _short_idle(agent, 1)
    provider = _StreamingProvider([], finish_delay=10)
    agent.providers = {"openai": provider}
    invocation = asyncio.create_task(agent._invoke_provider([]))
    await asyncio.sleep(0.01)
    await agent.abort()
    with pytest.raises(Exception) as exc_info:
        await invocation
    assert exc_info.value.__class__.__name__ == "_AbortSignal"
    assert provider.cancelled
    assert not agent._active_chat_tasks


@pytest.mark.asyncio
async def test_non_streaming_provider_keeps_absolute_deadline(tmp_path: Path) -> None:
    class _NonStreaming:
        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ChatResult:
            await asyncio.sleep(1)
            return ChatResult(text="", raw={}, usage={}, stop_reason="stop")

    agent = _mk_agent(tmp_path)
    _short_idle(agent)
    agent.providers = {"openai": _NonStreaming()}
    with pytest.raises(RuntimeError, match=r"provider timed out after 0.05s"):
        await agent._invoke_provider([])
    assert not agent._active_chat_tasks


@pytest.mark.asyncio
async def test_retry_after_stream_idle_gets_a_fresh_window(tmp_path: Path) -> None:
    class _RetryProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages: list[dict[str, Any]], *, on_delta: Any = None,
                       **kwargs: Any) -> ChatResult:
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(1)
            if on_delta is not None:
                on_delta("successful retry output")
            return ChatResult(text="successful retry output", raw={}, usage={}, stop_reason="stop")

    agent = _mk_agent(
        tmp_path,
        settings_override={"retry": {"enabled": True, "maxRetries": 1, "baseDelayMs": 1, "maxDelayMs": 1}},
    )
    _short_idle(agent)
    provider = _RetryProvider()
    agent.providers = {"openai": provider}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("retry after idle")

    assert provider.calls >= 2
    assert any(event["type"] == "auto_retry_start" for event in events)
    assert events[-1]["type"] == "agent_end"


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
@pytest.mark.parametrize(
    "retry",
    [
        {"enabled": False, "mode": "on", "maxRetries": 3},
        {"enabled": True, "mode": "off", "maxRetries": 3},
    ],
)
async def test_disabled_retry_settings_prevent_retry_attempts(tmp_path: Path, retry: dict[str, Any]) -> None:
    """The retry loop uses the same enabled/mode semantics as the getter."""
    agent = _mk_agent(
        tmp_path,
        settings_override={
            "retry": {**retry, "baseDelayMs": 1, "maxDelayMs": 1},
        },
    )
    provider = _FailingProvider()
    agent.providers = {"openai": provider}

    await agent.prompt("test retry mode off")

    assert provider.chat_call_count == 1


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


@pytest.mark.asyncio
async def test_sse_idle_watchdog_ollama() -> None:
    """Ollama's finite transport watchdog remains available as a fallback."""
    from unittest.mock import patch

    from one.providers.ollama import OllamaCloudAdapter

    adapter = OllamaCloudAdapter("http://localhost:11434")
    with patch(
        "httpx.AsyncClient.stream",
        return_value=_FakeStreamContext(_make_fake_sse_stream(['{"message":{"content":"hi"}}'])),
    ):
        with patch("one.providers.ollama._IDLE_SSE_TIMEOUT", 0.5):
            with pytest.raises(RuntimeError, match="idle"):
                await adapter.chat(
                    api_key="",
                    model="test",
                    messages=[],
                    thinking_level="medium",
                    on_delta=lambda d: None,
                )
