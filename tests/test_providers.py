"""Phase 23: reasoning_content streaming for llama.cpp / OpenAI-compatible providers.

All HTTP is faked (no network).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from one.providers.anthropic import AnthropicAdapter
from one.providers.codex_responses import CodexResponsesAdapter
from one.providers.gemini import GeminiAdapter
from one.providers.ollama import OllamaCloudAdapter
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

    async def __aenter__(self) -> _StreamContext:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b"error"

    def raise_for_status(self) -> None:
        pass


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


async def _stream_with(adapter: Any, lines: list[str]) -> tuple[Any, list[str], list[str]]:
    """Run an adapter against fake SSE and return result/text/thinking deltas."""
    visible: list[str] = []
    thinking: list[str] = []
    stream_ctx = _StreamContext(lines)
    with patch.object(httpx, "AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = lambda *a, **k: stream_ctx
        MockClient.return_value = mock_client
        result = await adapter.chat(
            "key", "test-model", [{"role": "user", "content": "hello"}], "medium",
            on_delta=visible.append, on_thinking_delta=thinking.append,
        )
    return result, visible, thinking


@pytest.mark.asyncio
async def test_anthropic_stream_routes_thinking_blocks_separately() -> None:
    result, visible, thinking = await _stream_with(AnthropicAdapter(), _sse(
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hidden"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "answer"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
    ))
    assert thinking == ["hidden"]
    assert visible == ["answer"]
    assert result.text == "answer"


@pytest.mark.asyncio
async def test_gemini_stream_routes_thought_parts_separately() -> None:
    result, visible, thinking = await _stream_with(GeminiAdapter(), _sse(
        {"candidates": [{"content": {"parts": [{"text": "hidden", "thought": True}, {"text": "answer"}]}, "finishReason": "STOP"}]},
    ))
    assert thinking == ["hidden"]
    assert visible == ["answer"]
    assert result.text == "answer"


@pytest.mark.asyncio
async def test_codex_responses_stream_routes_reasoning_summary_delta_separately() -> None:
    result, visible, thinking = await _stream_with(CodexResponsesAdapter(), _sse(
        {
            "type": "response.reasoning_summary_text.delta",
            "item_id": "rs_1",
            "output_index": 0,
            "summary_index": 0,
            "delta": "plan",
        },
        {
            "type": "response.reasoning_summary_text.done",
            "item_id": "rs_1",
            "output_index": 0,
            "summary_index": 0,
            "text": "plan",
        },
        {
            "type": "response.reasoning_summary_part.done",
            "item_id": "rs_2",
            "output_index": 0,
            "summary_index": 1,
            "part": {"type": "summary_text", "text": "fallback"},
        },
        {
            "type": "response.reasoning_text.delta",
            "item_id": "r_1",
            "output_index": 1,
            "delta": " useful",
        },
        {"type": "response.reasoning_summary_part.added", "item_id": "rs_3"},
        {"type": "response.output_text.delta", "delta": "answer"},
        {"type": "response.completed", "response": {"status": "completed", "usage": {"output_tokens": 2}}},
    ))
    assert thinking == ["plan", "fallback", " useful"]
    assert visible == ["answer"]
    assert result.text == "answer"
    assert result.stop_reason == "completed"


@pytest.mark.asyncio
async def test_codex_responses_stream_emits_summary_text_done_without_delta() -> None:
    result, visible, thinking = await _stream_with(CodexResponsesAdapter(), _sse(
        {
            "type": "response.reasoning_summary_text.done",
            "item_id": "rs_1",
            "output_index": 0,
            "summary_index": 0,
            "text": "complete plan",
        },
        {"type": "response.completed", "response": {"status": "completed"}},
    ))
    assert thinking == ["complete plan"]
    assert visible == []
    assert result.text == ""


@pytest.mark.asyncio
async def test_codex_responses_stream_raises_on_failed_event() -> None:
    with pytest.raises(RuntimeError, match="responses failed"):
        await _stream_with(CodexResponsesAdapter(), _sse(
            {"type": "response.failed", "response": {"error": {"message": "nope"}}},
        ))


@pytest.mark.asyncio
async def test_codex_responses_stream_raises_on_incomplete_event() -> None:
    with pytest.raises(RuntimeError, match="responses incomplete.*max_output_tokens"):
        await _stream_with(CodexResponsesAdapter(), _sse(
            {
                "type": "response.incomplete",
                "response": {"incomplete_details": {"reason": "max_output_tokens"}},
            },
        ))


@pytest.mark.asyncio
async def test_codex_responses_non_stream_emits_summary_but_not_result_text() -> None:
    adapter = CodexResponsesAdapter()
    post_resp = _PostResp({
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": [
                {"type": "summary_text", "text": "first"}, " second",
            ]},
            {"type": "function_call", "name": "read", "arguments": "{}"},
            {"type": "message", "content": [{"type": "output_text", "text": "answer"}]},
        ],
    })
    thinking: list[str] = []
    with patch.object(httpx, "AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=post_resp)
        mock_client_class.return_value = mock_client
        result = await adapter.chat(
            "key", "test-model", [{"role": "user", "content": "hello"}], "medium",
            on_thinking_delta=thinking.append,
        )
    assert thinking == ["first", " second"]
    assert result.text == "answer"


@pytest.mark.asyncio
async def test_openrouter_stream_routes_reasoning_fields_without_final_text() -> None:
    adapter = OpenAICompatibleAdapter(
        "openrouter", "https://openrouter.ai/api", reasoning_mode="openrouter"
    )
    result, visible, thinking = await _stream_with(adapter, _sse(
        {"choices": [{"delta": {"reasoning": "plan"}}]},
        {"choices": [{"delta": {"reasoning_content": " more"}}]},
        {"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]},
    ))
    assert thinking == ["plan", " more"]
    assert visible == ["answer"]
    assert result.text == "answer"


@pytest.mark.asyncio
async def test_ollama_cloud_stream_routes_message_thinking_separately() -> None:
    result, visible, thinking = await _stream_with(OllamaCloudAdapter("https://ollama.com"), [
        json.dumps({"message": {"thinking": "plan", "content": ""}, "done": False}),
        json.dumps({"message": {"content": "answer"}, "done": True}),
    ])
    assert thinking == ["plan"]
    assert visible == ["answer"]
    assert result.text == "answer"
    assert result.stop_reason == "stop"


@pytest.mark.asyncio
async def test_ollama_cloud_non_stream_routes_message_thinking_separately() -> None:
    adapter = OllamaCloudAdapter("https://ollama.com")
    post_resp = _PostResp({"message": {"thinking": "plan", "content": "answer"}, "done": True})
    thinking: list[str] = []
    with patch.object(httpx, "AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=post_resp)
        mock_client_class.return_value = mock_client
        result = await adapter.chat(
            "key", "glm-5:cloud", [{"role": "user", "content": "hello"}], "high",
            on_thinking_delta=thinking.append,
        )
    assert thinking == ["plan"]
    assert result.text == "answer"


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
    """Reasoning is routed to thinking callbacks, not parseable result text."""

    reasoning_text = "Let me think about this carefully."
    content_text = "Here is the answer."

    sse_lines = _sse(
        {"choices": [{"delta": {"content": None, "role": "assistant"}}]},
        {"choices": [{"delta": {"reasoning_content": reasoning_text}}]},
        {"choices": [{"delta": {"content": content_text}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )

    captured_deltas: list[str] = []
    thinking_deltas: list[str] = []

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
            on_thinking_delta=thinking_deltas.append,
        )

    assert thinking_deltas == [reasoning_text]
    assert reasoning_text not in captured_deltas
    assert content_text in captured_deltas

    assert result.text == content_text


@pytest.mark.asyncio
async def test_non_streaming_includes_reasoning_content(adapter: OpenAICompatibleAdapter):
    """Non-streaming reasoning is emitted separately and cannot corrupt parsing."""

    thinking = "I will solve this step by step."
    content = "The answer is 42."
    thinking_deltas: list[str] = []

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
            on_thinking_delta=thinking_deltas.append,
        )

    assert thinking_deltas == [thinking]
    assert result.text == content
    assert result.had_thinking is True


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
    assert result.had_thinking is False


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
    assert result.had_thinking is False


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

    assert result.text == "Part A B"


@pytest.mark.asyncio
async def test_non_streaming_only_reasoning_no_content(adapter: OpenAICompatibleAdapter):
    """Edge case: only reasoning_content, empty content field."""

    thinking = "I thought about it."
    thinking_deltas: list[str] = []

    post_resp = _PostResp(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
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
            on_thinking_delta=thinking_deltas.append,
        )

    assert result.text == ""
    assert thinking_deltas == [thinking]
    assert result.had_thinking is True


@pytest.mark.asyncio
async def test_streaming_fragmented_reasoning_with_empty_content_is_classified(
    adapter: OpenAICompatibleAdapter,
):
    """llama.cpp-style fragmented reasoning remains display-only with empty text."""

    result, visible, thinking = await _stream_with(adapter, _sse(
        {"choices": [{"delta": {"role": "assistant", "reasoning_content": "I will "}}]},
        {"choices": [{"delta": {"reasoning_content": "use a tool", "content": ""}}]},
        {"choices": [{"delta": {"content": None}, "finish_reason": "stop"}]},
    ))

    assert result.text == ""
    assert result.had_thinking is True
    assert visible == []
    assert thinking == ["I will ", "use a tool"]


@pytest.mark.asyncio
async def test_streaming_json_content_is_not_reasoning(adapter: OpenAICompatibleAdapter):
    """Normal content, including a JSON-in-text tool call, keeps its prior path."""

    tool_json = '{"tool":"read","args":{"path":"a.txt"}}'
    result, visible, thinking = await _stream_with(adapter, _sse(
        {"choices": [{"delta": {"role": "assistant", "content": tool_json[:17]}}]},
        {"choices": [{"delta": {"content": tool_json[17:]}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ))

    assert result.text == tool_json
    assert result.had_thinking is False
    assert result.had_native_tool_call is False
    assert visible == [tool_json[:17], tool_json[17:]]
    assert thinking == []


@pytest.mark.asyncio
async def test_streaming_native_tool_calls_are_classified(adapter: OpenAICompatibleAdapter):
    result, _, _ = await _stream_with(adapter, _sse(
        {"choices": [{"delta": {"role": "assistant", "tool_calls": [
            {"index": 0, "id": "call_1", "type": "function", "function": {"name": "read", "arguments": ""}},
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"path":"a.txt"}'}},
        ]}, "finish_reason": "tool_calls"}]},
    ))

    assert result.had_native_tool_call is True


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
    assert result.text == content_text


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
    assert result.text == ""
    assert result.had_thinking is True


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
    assert result.text == ""


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
    assert result.text == ""


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
    assert result.text == ""
    # All 3 chunks were attempted; chunk 2 raised but was swallowed
    assert call_count == 3
