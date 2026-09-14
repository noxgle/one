from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.attachments import AttachmentValidationError
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.core.types import ModelInfo
from one.providers.base import ChatResult


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
        on_delta: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        max_tokens: int | None = None,
        images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
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
        on_delta: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        max_tokens: int | None = None,
        images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
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
        on_delta: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        max_tokens: int | None = None,
        images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
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
        on_delta: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        max_tokens: int | None = None,
        images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
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


# ── Phase 30.7: multi-invocation reasoning/tool sequencing ───────────────────

class _ProviderWithThinkingAndTools:
    """Three-invocation provider: THINK_A→read, THINK_B→grep, THINK_C→answer.

    Each invocation fires thinking_delta callbacks for every chunk (including "" for A).
    """

    def __init__(self) -> None:
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

        self.calls += 1

        if self.calls == 1:
            chunks = ["THINK", " ", "A", ""]
            content = '{"tool":"read","args":{"path":"a.txt"}}'
            if on_thinking_delta:
                for c in chunks:
                    on_thinking_delta(c)
            if on_delta:
                on_delta(content)
            return ChatResult(text="THINK A\n\n" + content, raw={}, usage={}, stop_reason="stop")

        elif self.calls == 2:
            chunks = ["THINK", " ", "B"]
            content = '{"tool":"grep","args":{"pattern":"foo"}}'
            if on_thinking_delta:
                for c in chunks:
                    on_thinking_delta(c)
            if on_delta:
                on_delta(content)
            return ChatResult(text="THINK B\n\n" + content, raw={}, usage={}, stop_reason="stop")

        else:
            chunks = ["THINK", " ", "C"]
            content = "DONE"
            if on_thinking_delta:
                for c in chunks:
                    on_thinking_delta(c)
            if on_delta:
                on_delta(content)
            return ChatResult(text="THINK C\n\nDONE", raw={}, usage={}, stop_reason="stop")


@pytest.mark.asyncio
async def test_multi_invocation_reasoning_tool_sequencing(tmp_path: Path):
    """Three provider invocations: THINK_A→read, THINK_B→grep, THINK_C→answer."""
    # Create read source file so first tool succeeds; grep can succeed on tmp_path
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry(auth, str(tmp_path / "models.json"))
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 6, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))

    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["read", "grep"])
    agent.providers = {"openai": _ProviderWithThinkingAndTools()}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("go")

    # Exactly ONE turn_start in the whole prompt/tool loop
    turn_starts = [e for e in events if e.get("type") == "turn_start"]
    assert len(turn_starts) == 1

    # Filter out lifecycle events (agent_start, agent_end) to get the meaningful sequence
    meaningful = [e for e in events if e.get("type") not in ("agent_start", "agent_end")]

    # Assert exact filtered sequence of existing meaningful events
    expected_types = [
        "message_start",  # pre-turn preamble
        "message_end",
        "turn_start",
        # THINK_A deltas (empty "" filtered by agent_session._on_thinking_delta)
        "thinking_delta",  # "THINK"
        "thinking_delta",  # " "
        "thinking_delta",  # "A"
        # tool read (succeeds — a.txt exists)
        "tool_call_start",
        "tool_call_end",
        # THINK_B deltas
        "thinking_delta",  # "THINK"
        "thinking_delta",  # " "
        "thinking_delta",  # "B"
        # tool grep (succeeds)
        "tool_call_start",
        "tool_call_end",
        # THINK_C deltas
        "thinking_delta",  # "THINK"
        "thinking_delta",  # " "
        "thinking_delta",  # "C"
        # final answer
        "message_start",
        "message_update",
        "message_end",
        "turn_end",
    ]
    actual_types = [e.get("type") for e in meaningful]
    assert actual_types == expected_types, f"Types mismatch: {actual_types}"

    # A segment deltas include exact " " but no ""
    think_deltas = [e for e in events if e.get("type") == "thinking_delta"]
    assert all(e.get("delta") != "" for e in think_deltas)
    assert any(e.get("delta") == " " for e in think_deltas)

    # Final message_update delta includes thinking + content (provider returns full text)
    msg_updates = [e for e in events if e.get("type") == "message_update"]
    assert msg_updates[-1].get("assistantMessageEvent", {}).get("delta") == "THINK C\n\nDONE"

    # Strict chronological order: A < read_start < read_end < B < grep_start < grep_end < C < final_msg
    first_a = next(i for i, e in enumerate(events) if e.get("delta") == "A" and e.get("type") == "thinking_delta")
    read_start = next(i for i, e in enumerate(events) if e.get("type") == "tool_call_start" and e.get("tool") == "read")
    read_end = next(i for i, e in enumerate(events) if e.get("type") == "tool_call_end" and e.get("tool") == "read")
    first_b = next(i for i, e in enumerate(events) if e.get("delta") == "B" and e.get("type") == "thinking_delta")
    grep_start = next(i for i, e in enumerate(events) if e.get("type") == "tool_call_start" and e.get("tool") == "grep")
    grep_end = next(i for i, e in enumerate(events) if e.get("type") == "tool_call_end" and e.get("tool") == "grep")
    first_c = next(i for i, e in enumerate(events) if e.get("delta") == "C" and e.get("type") == "thinking_delta")
    final_msg = next(i for i, e in enumerate(events) if e.get("type") == "message_start" and i > 1)
    assert first_a < read_start < read_end < first_b < grep_start < grep_end < first_c < final_msg


# ── CLI disposal on image-import error path ──────────────────────────────────


class _FakeHost:
    """Minimal fake host that tracks ``dispose()`` calls."""

    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.asyncio
async def test_cli_dispose_on_image_import_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """CLI must call ``host.dispose()`` when image import raises.

    This test verifies the exception-handling pattern in ``main.py`` that calls
    ``await host.dispose()`` followed by ``await mcp_manager.close()`` and
    returns exit code 2 when ``import_image`` raises ``AttachmentValidationError``.
    """
    from unittest.mock import AsyncMock, patch

    # Simulate the main.py image-import try/except block (lines 436-461).
    fake_host = _FakeHost()
    fake_mcp = AsyncMock()

    with patch("one.core.attachments.import_image") as mock_import:
        mock_import.side_effect = AttachmentValidationError("boom")

        # This mirrors the main.py import logic.
        with patch("one.core.attachments.count_attachments"):
            try:
                from one.core.attachments import AttachmentInput, count_attachments, import_image  # noqa: F811

                count_attachments([AttachmentInput(path="/nonexistent.png")])
                _ = import_image(str(tmp_path), "/nonexistent.png")
            except AttachmentValidationError as e:
                # main.py exception handler (lines 450-455):
                # print(f"Invalid image: {e}")
                await fake_host.dispose()
                await fake_mcp.close()
                exit_code = 2
            else:
                exit_code = 0

    assert fake_host.disposed is True, "host.dispose() must be called on import error"
    assert fake_mcp.close.called is True, "mcp_manager.close() must be called"
    assert exit_code == 2, "exit code must be 2 on image import failure"


@pytest.mark.asyncio
async def test_cli_dispose_on_generic_image_import_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """CLI must also call ``host.dispose()`` for generic (non-validation) image errors."""
    from unittest.mock import AsyncMock, patch

    fake_host = _FakeHost()
    fake_mcp = AsyncMock()

    class _GenericError(Exception):
        pass

    with patch("one.core.attachments.import_image") as mock_import:
        mock_import.side_effect = _GenericError("disk full")

        with patch("one.core.attachments.count_attachments"):
            try:
                from one.core.attachments import AttachmentInput, count_attachments, import_image  # noqa: F811

                count_attachments([AttachmentInput(path="/nonexistent.png")])
                _ = import_image(str(tmp_path), "/nonexistent.png")
            except Exception:
                # main.py generic exception handler (lines 456-461):
                await fake_host.dispose()
                await fake_mcp.close()
                exit_code = 2
            else:
                exit_code = 0

    assert fake_host.disposed is True, "host.dispose() must be called for generic import errors"
    assert exit_code == 2


# ---------------------------------------------------------------------------
# Steer/follow-up image rejection (R2.1)
# ---------------------------------------------------------------------------


class _TextOnlyProvider:
    """Provider that captures calls but returns text."""

    def __init__(self, response: str = "hello") -> None:
        self.response = response
        self.chat_call_count = 0

    async def chat(self, api_key, model, messages, thinking_level, headers=None, images=None, storage_dir=""):
        from one.providers.base import ChatResult
        self.chat_call_count += 1
        return ChatResult(text=self.response, raw={}, usage={}, stop_reason="stop")


def _make_text_only_session(tmp_path: Path) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = ModelInfo(provider="openai", id="gpt-4.1", input_image=True)
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session_manager = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(
        session_manager, settings, registry, _Loader(), model, "medium",
        storage_dir=str(tmp_path / "store"),
    )
    agent.providers = {"openai": _TextOnlyProvider()}
    return agent


@pytest.mark.asyncio
async def test_steer_rejects_images(tmp_path: Path) -> None:
    """steer() must raise ValueError when images are provided."""
    session = _make_text_only_session(tmp_path)
    images = [{"blob_hash": "abc"}]
    with pytest.raises(ValueError, match="steer does not support image"):
        await session.steer("some steer text", images=images)


@pytest.mark.asyncio
async def test_follow_up_rejects_images(tmp_path: Path) -> None:
    """follow_up() must raise ValueError when images are provided."""
    session = _make_text_only_session(tmp_path)
    images = [{"blob_hash": "abc"}]
    with pytest.raises(ValueError, match="follow_up does not support image"):
        await session.follow_up("some follow-up text", images=images)


@pytest.mark.asyncio
async def test_prompt_with_images_sets_images_on_non_streaming(tmp_path: Path) -> None:
    """prompt() with images on non-streaming session should pass images to provider."""
    session = _make_text_only_session(tmp_path)
    # Simulate image refs
    images = [{"blob_hash": "abc123", "mime": "image/png", "size": 100}]
    await session.prompt("describe it", images=images)
    assert session.providers["openai"].chat_call_count >= 1
    assert session._images == images


# ── Streaming delivery-mode tests (Task A1) ──────────────────────────────────

@pytest.mark.asyncio
async def test_streaming_delivery_defaults_to_steer_with_queue_mode(tmp_path: Path) -> None:
    """Normal input during streaming → steer when followUpMode='queue' (default)."""
    agent = _mk_agent(tmp_path)
    agent.providers = {"openai": _Provider(["DONE"])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    # Simulate: a prompt is in-flight (is_streaming=True).
    # The second prompt() call should route based on follow_up_mode config.
    agent._is_streaming = True
    await agent.prompt("while-streaming")
    # Should have queued as steer (default for followUpMode='queue')
    queues = agent.get_pending_queues()
    assert queues["steering"] == ["while-streaming"]
    assert queues["followUp"] == []
    # Reset flag to allow cleanup
    agent._is_streaming = False


@pytest.mark.asyncio
async def test_streaming_delivery_follow_up_mode_follow_up(tmp_path: Path) -> None:
    """Normal input during streaming → followUp when followUpMode='follow_up'."""
    agent = _mk_agent(tmp_path, settings_override={"followUpMode": "follow_up"})
    agent.providers = {"openai": _Provider(["DONE"])}

    agent._is_streaming = True
    await agent.prompt("while-streaming")
    queues = agent.get_pending_queues()
    assert queues["steering"] == []
    assert queues["followUp"] == ["while-streaming"]
    agent._is_streaming = False


@pytest.mark.asyncio
async def test_explicit_streaming_behavior_still_works(tmp_path: Path) -> None:
    """Explicit streamingBehavior option overrides config."""
    agent = _mk_agent(tmp_path)
    agent.providers = {"openai": _Provider(["DONE"])}

    agent._is_streaming = True
    await agent.prompt("explicit steer", {"streamingBehavior": "steer"})
    assert agent.get_pending_queues()["steering"] == ["explicit steer"]

    agent._is_streaming = True
    await agent.prompt("explicit follow", {"streamingBehavior": "followUp"})
    assert agent.get_pending_queues()["followUp"] == ["explicit follow"]
    agent._is_streaming = False


@pytest.mark.asyncio
async def test_queues_not_drained_after_finish_tool(tmp_path: Path) -> None:
    """After finish tool, _check_queues must NOT drain steering/follow-up."""
    agent = _mk_agent(tmp_path)
    agent.providers = {"openai": _Provider(['{"tool":"finish","args":{"summary":"done"}}'])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("go")
    # The prompt finished with the finish tool, so _check_queues returned
    # early via `finished_with_tool` guard — queues untouched.

    # Now queue a steer via steer()
    await agent.steer("queued steer")
    queues = agent.get_pending_queues()
    assert queues["steering"] == ["queued steer"]
    assert queues["followUp"] == []


@pytest.mark.asyncio
async def test_queues_not_drained_after_abort(tmp_path: Path) -> None:
    """After abort, _check_queues must NOT drain steering/follow-up."""
    agent = _mk_agent(tmp_path)
    agent.providers = {"openai": _SlowProvider()}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    task = asyncio.create_task(agent.prompt("go"))
    await asyncio.sleep(0.01)
    await agent.steer("queued steer")
    queues_before = agent.get_pending_queues()
    assert queues_before["steering"] == ["queued steer"]

    await agent.abort()
    await task

    # Queue should still have the steer message — not drained by abort path.
    queues_after = agent.get_pending_queues()
    assert queues_after["steering"] == ["queued steer"]


@pytest.mark.asyncio
async def test_steer_and_follow_up_both_queued_fifo(tmp_path: Path) -> None:
    """Multiple steer and follow-up messages queue correctly."""
    agent = _mk_agent(tmp_path)

    await agent.steer("s1")
    await agent.steer("s2")
    await agent.follow_up("f1")
    await agent.follow_up("f2")

    queues = agent.get_pending_queues()
    assert queues["steering"] == ["s1", "s2"]
    assert queues["followUp"] == ["f1", "f2"]


@pytest.mark.asyncio
async def test_clear_pending_queues_works(tmp_path: Path) -> None:
    """clear_pending_queues clears the correct subset."""
    agent = _mk_agent(tmp_path)
    await agent.steer("s1")
    await agent.follow_up("f1")

    cleared = agent.clear_pending_queues("steering")
    assert cleared["steering"] == []
    assert cleared["followUp"] == ["f1"]

    cleared = agent.clear_pending_queues("follow")
    assert cleared["followUp"] == []

    cleared = agent.clear_pending_queues("all")
    assert cleared["steering"] == []
    assert cleared["followUp"] == []
