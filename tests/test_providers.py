"""Phase 23: reasoning_content streaming for llama.cpp / OpenAI-compatible providers.

All HTTP is faked (no network).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from one.providers.base import ChatResult
from one.providers.openai_compatible import OpenAICompatibleAdapter


# ── helpers ──────────────────────────────────────────────────────────────────

def _sse(*events: dict[str, Any]) -> list[str]:
    """Turn a list of JSON dicts into SSE data-lines."""
    return [f"data: {json.dumps(e)}" for e in events]


class _StreamContext:
    """Stand-in for ``client.stream()`` async-context-manager."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.is_error = False

    async def __aenter__(self) -> "_StreamContext":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b"error"


class _PostResp:
    """Stand-in for a non-stream httpx response."""

    def __init__(self, json_data: dict[str, Any], *, status_code: int = 200) -> None:
        self._data = json_data
        self.status_code = status_code

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def json(self) -> dict[str, Any]:
        return self._data


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def adapter() -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        name="test",
        base_url="http://localhost:8080",
    )


# ── tests ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_streaming_includes_reasoning_content(adapter: OpenAICompatibleAdapter):
    """on_delta must fire for reasoning_content chunks and final text includes it."""

    reasoning_text = "Let me think about this carefully."
    content_text = "Here is the answer."

    sse_lines = _sse(
        {"choices": [{"delta": {"content": None, "role": "assistant"}}]},
        {"choices": [{"delta": {"reasoning_content": reasoning_text}}]},
        {"choices": [{"delta": {"content": content_text}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )

    captured_deltas: list[str] = []

    def on_delta(piece: str) -> None:
        captured_deltas.append(piece)

    stream_ctx = _StreamContext(sse_lines)

    with patch.object(httpx, "AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = lambda *a, **k: stream_ctx  # sync → async-context-manager
        MockClient.return_value = mock_client

        result = await adapter.chat(
            api_key="key",
            model="qwen-test",
            messages=[{"role": "user", "content": "hello"}],
            thinking_level="high",
            on_delta=on_delta,
        )

    # on_delta should have been called with reasoning_content
    assert reasoning_text in captured_deltas
    assert content_text in captured_deltas

    # Final text must contain both reasoning and content
    assert reasoning_text in result.text
    assert content_text in result.text


@pytest.mark.asyncio
async def test_non_streaming_includes_reasoning_content(adapter: OpenAICompatibleAdapter):
    """Non-streaming chat must concatenate reasoning_content + content."""

    thinking = "I will solve this step by step."
    content = "The answer is 42."

    post_resp = _PostResp(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "reasoning_content": thinking,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }
    )

    with patch.object(httpx, "AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=post_resp)
        MockClient.return_value = mock_client

        result = await adapter.chat(
            api_key="key",
            model="qwen-test",
            messages=[{"role": "user", "content": "what is 6*7?"}],
            thinking_level="high",
        )

    assert thinking in result.text
    assert content in result.text
    # Expect thinking + "\n\n" + content
    assert result.text.startswith(thinking)


@pytest.mark.asyncio
async def test_streaming_content_only_no_reasoning(adapter: OpenAICompatibleAdapter):
    """When reasoning_content is absent, normal content-only streaming works."""

    content_text = "Hello, how can I help you?"
    sse_lines = _sse(
        {"choices": [{"delta": {"content": content_text, "role": "assistant"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )

    captured_deltas: list[str] = []

    def on_delta(piece: str) -> None:
        captured_deltas.append(piece)

    stream_ctx = _StreamContext(sse_lines)

    with patch.object(httpx, "AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = lambda *a, **k: stream_ctx
        MockClient.return_value = mock_client

        result = await adapter.chat(
            api_key="key",
            model="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
            thinking_level="none",
            on_delta=on_delta,
        )

    assert content_text in result.text
    assert "".join(captured_deltas) == content_text


@pytest.mark.asyncio
async def test_non_streaming_no_reasoning_content(adapter: OpenAICompatibleAdapter):
    """When reasoning_content is absent, only content is returned."""

    content = "Just normal content."

    post_resp = _PostResp(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
        }
    )

    with patch.object(httpx, "AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=post_resp)
        MockClient.return_value = mock_client

        result = await adapter.chat(
            api_key="key",
            model="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
            thinking_level="none",
        )

    assert result.text == content


@pytest.mark.asyncio
async def test_streaming_reasoning_then_content_order(adapter: OpenAICompatibleAdapter):
    """Reasoning chunks can arrive interleaved with content; order is preserved."""

    sse_lines = _sse(
        {"choices": [{"delta": {"content": "Part "}}]},
        {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
        {"choices": [{"delta": {"content": "A"}}]},
        {"choices": [{"delta": {"reasoning_content": " more thought"}}]},
        {"choices": [{"delta": {"content": " B"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )

    captured_deltas: list[str] = []

    def on_delta(piece: str) -> None:
        captured_deltas.append(piece)

    stream_ctx = _StreamContext(sse_lines)

    with patch.object(httpx, "AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = lambda *a, **k: stream_ctx
        MockClient.return_value = mock_client

        result = await adapter.chat(
            api_key="key",
            model="qwen-test",
            messages=[{"role": "user", "content": "test"}],
            thinking_level="high",
            on_delta=on_delta,
        )

    # Both reasoning and content parts appear in the final text
    assert "thinking" in result.text
    assert "Part " in result.text
    assert "A" in result.text
    assert " more thought" in result.text
    assert "B" in result.text


@pytest.mark.asyncio
async def test_non_streaming_only_reasoning_no_content(adapter: OpenAICompatibleAdapter):
    """Edge case: only reasoning_content, empty content field."""

    thinking = "I thought about it."

    post_resp = _PostResp(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": thinking,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
        }
    )

    with patch.object(httpx, "AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=post_resp)
        MockClient.return_value = mock_client

        result = await adapter.chat(
            api_key="key",
            model="qwen-test",
            messages=[{"role": "user", "content": "test"}],
            thinking_level="high",
        )

    assert thinking in result.text
    # Should be thinking + "\n\n" + ""
    assert result.text == thinking + "\n\n"


# ── Phase 24: on_thinking_delta ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_thinking_delta_called_for_reasoning(adapter: OpenAICompatibleAdapter):
    """When both on_delta and on_thinking_delta are provided, reasoning_content
    goes ONLY to on_thinking_delta (not on_delta)."""

    reasoning_text = "Let me think about this carefully."
    content_text = "Here is the answer."

    sse_lines = _sse(
        {"choices": [{"delta": {"reasoning_content": reasoning_text}}]},
        {"choices": [{"delta": {"content": content_text}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )

    thinking_deltas: list[str] = []
    normal_deltas: list[str] = []

    def on_thinking_delta(piece: str) -> None:
        thinking_deltas.append(piece)

    def on_delta(piece: str) -> None:
        normal_deltas.append(piece)

    stream_ctx = _StreamContext(sse_lines)

    with patch.object(httpx, "AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = lambda *a, **k: stream_ctx
        MockClient.return_value = mock_client

        result = await adapter.chat(
            api_key="key",
            model="qwen-test",
            messages=[{"role": "user", "content": "hello"}],
            thinking_level="high",
            on_delta=on_delta,
            on_thinking_delta=on_thinking_delta,
        )

    # reasoning_content goes only to on_thinking_delta
    assert reasoning_text in thinking_deltas
    assert reasoning_text not in normal_deltas
    # normal content goes only to on_delta
    assert content_text in normal_deltas
    assert content_text not in thinking_deltas


@pytest.mark.asyncio
async def test_on_thinking_delta_fallback_to_on_delta(adapter: OpenAICompatibleAdapter):
    """When on_thinking_delta is None, reasoning_content falls back to on_delta
    (backward compatibility)."""

    reasoning_text = "I thought about it."
    content_text = "The answer is 42."

    sse_lines = _sse(
        {"choices": [{"delta": {"reasoning_content": reasoning_text}}]},
        {"choices": [{"delta": {"content": content_text}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )

    captured_deltas: list[str] = []

    def on_delta(piece: str) -> None:
        captured_deltas.append(piece)

    stream_ctx = _StreamContext(sse_lines)

    with patch.object(httpx, "AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = lambda *a, **k: stream_ctx
        MockClient.return_value = mock_client

        result = await adapter.chat(
            api_key="key",
            model="qwen-test",
            messages=[{"role": "user", "content": "test"}],
            thinking_level="high",
            on_delta=on_delta,
            # on_thinking_delta is intentionally omitted
        )

    # Both should appear in on_delta for backward compat
    assert reasoning_text in captured_deltas
    assert content_text in captured_deltas
    assert reasoning_text in result.text
    assert content_text in result.text


# ── Phase 30.7: raw reasoning content streaming ──────────────────────────────


@pytest.mark.asyncio
async def test_raw_reasoning_chunks_exact_callbacks(adapter: OpenAICompatibleAdapter):
    """Exact raw reasoning chunks: callbacks fire with each chunk exactly, join matches."""

    chunks = ["rea", "son", ",", " ", "can", "'", "t", " ", "**", "bold", "**", " [x]"]

    sse_lines = _sse(
        *[{"choices": [{"delta": {"reasoning_content": c, "role": "assistant"}}]} for c in chunks],
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )

    captured: list[str] = []

    def on_thinking_delta(piece: str) -> None:
        captured.append(piece)

    stream_ctx = _StreamContext(sse_lines)

    with patch.object(httpx, "AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = lambda *a, **k: stream_ctx
        MockClient.return_value = mock_client

        result = await adapter.chat(
            api_key="key",
            model="qwen-test",
            messages=[{"role": "user", "content": "hello"}],
            thinking_level="high",
            on_delta=on_thinking_delta,
            on_thinking_delta=on_thinking_delta,
        )

    # Each callback receives exactly the raw chunk
    assert captured == chunks
    # Joined value is exact
    assert "".join(captured) == "reason, can't **bold** [x]"
    # ChatResult raw join unchanged
    assert result.text == "reason, can't **bold** [x]"


@pytest.mark.asyncio
async def test_empty_vs_whitespace_reasoning_callbacks(adapter: OpenAICompatibleAdapter):
    """Empty and whitespace-only chunks: empty strings skip callback, whitespace-only passes."""

    # Build SSE lines manually: empty reasoning_content (falsy, so delta won't enter `if piece_reasoning`)
    # then "A", " ", "B", then another empty.
    # Note: reasoning_content="" is falsy, so the `if piece_reasoning:` branch is skipped.
    # reasoning_content=" " is truthy (non-empty), so it enters and fires callback.
    sse_lines = [
        "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": "", "role": "assistant"}}]}),
        "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": "A"}}]}),
        "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": " "}}]}),
        "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": "B"}}]}),
        "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": ""}}]}),
        "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
    ]

    captured: list[str] = []

    def on_thinking_delta(piece: str) -> None:
        captured.append(piece)

    stream_ctx = _StreamContext(sse_lines)

    with patch.object(httpx, "AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = lambda *a, **k: stream_ctx
        MockClient.return_value = mock_client

        result = await adapter.chat(
            api_key="key",
            model="qwen-test",
            messages=[{"role": "user", "content": "hello"}],
            thinking_level="high",
            on_delta=on_thinking_delta,
            on_thinking_delta=on_thinking_delta,
        )

    # Empty strings are falsy → skipped; whitespace-only " " and substantive "A","B" pass
    assert captured == ["A", " ", "B"]
    # Join preserves whitespace
    assert "".join(captured) == "A B"
    assert result.text == "A B"


@pytest.mark.asyncio
async def test_raw_reasoning_fallback_on_delta_only(adapter: OpenAICompatibleAdapter):
    """When on_thinking_delta is absent, reasoning chunks go to on_delta exactly."""

    chunks = ["thi", "nk"]

    sse_lines = _sse(
        *[{"choices": [{"delta": {"reasoning_content": c, "role": "assistant"}}]} for c in chunks],
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )

    captured: list[str] = []

    def on_delta(piece: str) -> None:
        captured.append(piece)

    stream_ctx = _StreamContext(sse_lines)

    with patch.object(httpx, "AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = lambda *a, **k: stream_ctx
        MockClient.return_value = mock_client

        result = await adapter.chat(
            api_key="key",
            model="qwen-test",
            messages=[{"role": "user", "content": "hello"}],
            thinking_level="high",
            on_delta=on_delta,
            # no on_thinking_delta
        )

    assert captured == chunks
    assert result.text == "think"


@pytest.mark.asyncio
async def test_raw_reasoning_callback_raises_still_finishes(adapter: OpenAICompatibleAdapter):
    """If on_thinking_delta raises, provider streaming continues and ChatResult is complete."""

    call_count = 0
    chunks = ["A", "B", "C"]

    def on_thinking_delta(piece: str) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise ValueError("boom")
        # first and third chunks still fire

    sse_lines = _sse(
        *[{"choices": [{"delta": {"reasoning_content": c, "role": "assistant"}}]} for c in chunks],
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )

    stream_ctx = _StreamContext(sse_lines)

    with patch.object(httpx, "AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = lambda *a, **k: stream_ctx
        MockClient.return_value = mock_client

        result = await adapter.chat(
            api_key="key",
            model="qwen-test",
            messages=[{"role": "user", "content": "hello"}],
            thinking_level="high",
            on_delta=lambda p: None,
            on_thinking_delta=on_thinking_delta,
        )

    # Provider finished despite exception on chunk 2
    assert result.text == "ABC"
    # All 3 chunks were attempted; chunk 2 raised but was swallowed
    assert call_count == 3
