"""LlamaCppAdapter unit tests.

All HTTP is faked (no network, no running llama-server).
"""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from one.providers.llama_cpp import LlamaCppAdapter
from one.providers.openai_compatible import OpenAICompatibleAdapter
from one.providers.registry import build_provider_registry


def _sse(*events: dict[str, Any]) -> list[str]:
    return [f"data: {json.dumps(e)}" for e in events]


class _StreamContext:
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


async def _stream_with(adapter: Any, lines: list[str]) -> tuple[Any, list[str], list[str]]:
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
            "key", "local", [{"role": "user", "content": "hello"}], "off",
            on_delta=visible.append, on_thinking_delta=thinking.append,
        )
    return result, visible, thinking


def test_adapter_defaults() -> None:
    adapter = LlamaCppAdapter("http://127.0.0.1:8080")
    assert adapter.name == "llama.cpp"
    assert adapter.supports_native_tools is True
    assert adapter.supports_reasoning_effort is False
    assert adapter.default_temperature is None
    assert isinstance(adapter, OpenAICompatibleAdapter)


def test_registry_registers_llama_cpp_adapter() -> None:
    registry = build_provider_registry()
    assert isinstance(registry["llama.cpp"], LlamaCppAdapter)
    assert registry["llama.cpp"].supports_native_tools is True


def test_with_base_url_returns_llama_cpp_adapter() -> None:
    adapter = LlamaCppAdapter("http://127.0.0.1:8080")
    changed = adapter.with_base_url("http://192.0.2.38:8089")
    assert isinstance(changed, LlamaCppAdapter)
    assert changed.base_url == "http://192.0.2.38:8089"
    assert changed.supports_native_tools is True


def test_payload_includes_native_tools_and_tool_choice_none() -> None:
    adapter = LlamaCppAdapter("http://127.0.0.1:8080")
    tools = [{"name": "read", "description": "Read a file", "parameters": {"type": "object", "properties": {}}}]
    messages = [{"role": "user", "content": "hi"}]
    payload = adapter._build_payload("local", messages, "off", tools=tools)
    assert payload["tools"] == [{"type": "function", "function": tools[0]}]
    assert "tool_choice" not in payload
    disabled = adapter._build_payload("local", messages, "off", tools=tools, disable_tool_calls=True)
    assert disabled["tool_choice"] == "none"
    assert disabled["tools"] == payload["tools"]


def test_payload_does_not_leak_private_metadata() -> None:
    adapter = LlamaCppAdapter("http://127.0.0.1:8080")
    messages = [
        {"role": "assistant", "content": "calling", "_nativeToolCalls": [
            {"id": "call_1", "name": "read", "arguments": {"path": "a.txt"}},
        ]},
        {"role": "user", "content": "result", "_nativeToolCallId": "call_1"},
    ]
    payload = adapter._build_payload("local", messages, "off")
    assert "_nativeToolCalls" not in repr(payload)
    assert "_nativeToolCallId" not in repr(payload)
    assert payload["messages"][0]["tool_calls"][0]["id"] == "call_1"
    assert payload["messages"][1] == {"role": "tool", "tool_call_id": "call_1", "content": "result"}


@pytest.mark.asyncio
async def test_streaming_native_tool_calls_assembled_from_fragments() -> None:
    adapter = LlamaCppAdapter("http://127.0.0.1:8080")
    result, _, _ = await _stream_with(adapter, _sse(
        {"choices": [{"delta": {"role": "assistant", "tool_calls": [
            {"index": 0, "id": "call_1", "type": "function", "function": {"name": "read", "arguments": ""}},
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"path":'}},
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '"a.txt"}'}},
        ]}, "finish_reason": "tool_calls"}]},
    ))
    assert result.had_native_tool_call is True
    assert result.native_tool_calls == [
        {"id": "call_1", "name": "read", "arguments": {"path": "a.txt"}}
    ]


@pytest.mark.asyncio
async def test_streaming_multiple_tool_calls_preserve_indexes() -> None:
    adapter = LlamaCppAdapter("http://127.0.0.1:8080")
    result, _, _ = await _stream_with(adapter, _sse(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c0", "function": {"name": "read", "arguments": '{"path":"a"}'}},
            {"index": 1, "id": "c1", "function": {"name": "write", "arguments": '{"path":"b"}'}},
        ]}, "finish_reason": "tool_calls"}]},
    ))
    assert result.native_tool_calls == [
        {"id": "c0", "name": "read", "arguments": {"path": "a"}},
        {"id": "c1", "name": "write", "arguments": {"path": "b"}},
    ]


@pytest.mark.asyncio
async def test_streaming_malformed_arguments_kept_as_string() -> None:
    adapter = LlamaCppAdapter("http://127.0.0.1:8080")
    result, _, _ = await _stream_with(adapter, _sse(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c0", "function": {"name": "read", "arguments": "{not json"}},
        ]}, "finish_reason": "tool_calls"}]},
    ))
    assert result.native_tool_calls == [
        {"id": "c0", "name": "read", "arguments": "{not json"}
    ]


@pytest.mark.asyncio
async def test_non_stream_native_tool_calls() -> None:
    adapter = LlamaCppAdapter("http://127.0.0.1:8080")
    post_resp = _PostResp({
        "choices": [{
            "message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": '{"path":"a.txt"}'}},
            ]},
            "finish_reason": "tool_calls",
        }],
        "usage": {},
    })
    with patch.object(httpx, "AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=post_resp)
        MockClient.return_value = mock_client
        result = await adapter.chat("key", "local", [{"role": "user", "content": "hi"}], "off")
    assert result.had_native_tool_call is True
    assert result.native_tool_calls == [
        {"id": "call_1", "name": "read", "arguments": {"path": "a.txt"}}
    ]


@pytest.mark.asyncio
async def test_streaming_reasoning_separated_from_text() -> None:
    adapter = LlamaCppAdapter("http://127.0.0.1:8080")
    result, visible, thinking = await _stream_with(adapter, _sse(
        {"choices": [{"delta": {"role": "assistant", "reasoning_content": "I will "}}]},
        {"choices": [{"delta": {"reasoning_content": "use a tool", "content": ""}}]},
        {"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]},
    ))
    assert result.text == "answer"
    assert result.had_thinking is True
    assert visible == ["answer"]
    assert thinking == ["I will ", "use a tool"]


class _PostResp:
    def __init__(self, json_data: dict[str, Any], *, status_code: int = 200) -> None:
        self._data = json_data
        self.status_code = status_code

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def json(self) -> dict[str, Any]:
        return self._data


def test_usage_falls_back_to_timings_when_usage_is_absent() -> None:
    """llama-server builds may omit ``usage`` but always report ``timings``."""
    adapter = LlamaCppAdapter("http://127.0.0.1:8080")
    assert adapter._usage_from_timings({"timings": {"prompt_n": 1234, "predicted_n": 56}}) == {
        "prompt_tokens": 1234, "completion_tokens": 56,
    }
    # Missing or invalid timings must not fabricate usage.
    assert adapter._usage_from_timings({}) == {}
    assert adapter._usage_from_timings({"timings": {"prompt_n": 0}}) == {}
    assert adapter._usage_from_timings({"timings": {"prompt_n": True}}) == {}
    assert adapter._usage_from_timings({"timings": "nope"}) == {}
    assert adapter._usage_from_timings(None) == {}


@pytest.mark.asyncio
async def test_streaming_chat_derives_usage_from_final_timings() -> None:
    adapter = LlamaCppAdapter("http://127.0.0.1:8080")
    result, _, _ = await _stream_with(adapter, _sse(
        {"choices": [{"delta": {"content": "answer"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "timings": {"prompt_n": 4321, "predicted_n": 17}},
    ))
    assert result.usage == {"prompt_tokens": 4321, "completion_tokens": 17}


@pytest.mark.asyncio
async def test_streaming_chat_keeps_reported_usage_over_timings() -> None:
    adapter = LlamaCppAdapter("http://127.0.0.1:8080")
    result, _, _ = await _stream_with(adapter, _sse(
        {"choices": [{"delta": {"content": "answer"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 2},
         "timings": {"prompt_n": 4321, "predicted_n": 17}},
    ))
    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 2}
