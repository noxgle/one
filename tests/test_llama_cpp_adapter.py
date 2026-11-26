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


def test_chat_signature_stays_introspectable() -> None:
    """AgentSession gates request features on inspect.signature(provider.chat).

    A bare ``*args/**kwargs`` override erased every gate: no streaming, no
    tools, no temperature, no images, no max_tokens. That silently downgraded
    every llama.cpp request.
    """
    import inspect

    from one.providers.openai_compatible import OpenAICompatibleAdapter

    llama = inspect.signature(LlamaCppAdapter.chat).parameters
    base = inspect.signature(OpenAICompatibleAdapter.chat).parameters
    assert list(llama) == list(base), "override must expose the inherited signature"
    for gate in ("on_delta", "max_tokens", "tools", "images", "temperature",
                 "storage_dir", "disable_tool_calls", "on_thinking_delta"):
        assert gate in llama, f"{gate} gate lost"


@pytest.mark.asyncio
async def test_agent_session_sends_full_request_to_llama(tmp_path) -> None:
    """End-to-end gate check through AgentSession, not the bare adapter.

    Captures the final wire payload, which is the only place that proves
    streaming, native tools, temperature and max_tokens all survived the
    ``inspect.signature`` gates in ``_invoke_provider_impl``.
    """
    from unittest.mock import AsyncMock, patch

    from one.core.agent_session import AgentSession
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.settings_manager import SettingsManager
    from one.core.types import ModelInfo

    captured: dict[str, Any] = {}

    class Loader:
        def get_system_prompt(self, selected_tools=None):  # noqa: ARG002
            return "sys"

        async def reload(self) -> None:
            return

    class StreamCtx:
        is_error = False
        status_code = 200
        text = ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> None:
            pass

        async def aread(self) -> bytes:
            return b""

        async def aiter_lines(self):
            yield "data: " + json.dumps({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
            yield "data: [DONE]"

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("llama.cpp", "dummy")
    registry = ModelRegistry.create(auth)
    session = SessionManager.in_memory(str(tmp_path))
    model = ModelInfo("llama.cpp", "local", base_url="http://127.0.0.1:8080")
    agent = AgentSession(session, SettingsManager.in_memory({}), registry, Loader(), model, "medium")
    agent.providers = {"llama.cpp": LlamaCppAdapter("http://127.0.0.1:8080")}
    session.append_message({"role": "user", "content": "hi"})
    agent.messages = session.build_session_context()["messages"]

    def stream(payload: dict[str, Any]) -> StreamCtx:
        captured["payload"] = payload
        return StreamCtx()

    with patch.object(httpx, "AsyncClient") as client_cls:
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.stream = lambda *a, **k: stream(k["json"])
        client_cls.return_value = client
        await agent._invoke_provider(agent._flatten_messages_for_provider(), allow_live_stream=True)

    payload = captured.get("payload")
    assert payload is not None, "the streaming path must be used (idle-timeout SSE), not a blocking POST"
    assert payload.get("stream") is True
    assert payload.get("tools"), "native tool definitions must reach llama.cpp"
    assert payload.get("temperature") is not None, "temperature must reach llama.cpp"
    assert payload.get("max_tokens") is not None, "max_tokens must be enforced for llama.cpp"
    assert payload.get("stream_options") == {"include_usage": True}


class _JsonResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.is_error = status_code >= 400
        self.text = json.dumps(payload)

    def json(self) -> Any:
        return self._payload


class _RouterClient:
    """Fake httpx client emulating a router-mode llama-server.

    ``/props`` returns the router stub (``n_ctx: 0``); ``/slots`` and
    ``/v1/models`` carry the real per-model slot size. ``/apply-template`` and
    ``/tokenize`` reject requests that omit the model in the JSON body.
    """

    def __init__(self, model: str, n_ctx: int) -> None:
        self.model = model
        self.n_ctx = n_ctx
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> _RouterClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass

    async def get(self, url: str, params: dict[str, Any] | None = None, headers: Any = None) -> _JsonResponse:
        if url.endswith("/props"):
            return _JsonResponse({"role": "router", "default_generation_settings": {"n_ctx": 0}})
        if url.endswith("/slots"):
            if not params or params.get("model") != self.model:
                return _JsonResponse({"error": {"message": "model name is missing"}}, 400)
            return _JsonResponse([{"id": 0, "n_ctx": self.n_ctx}])
        if url.endswith("/v1/models"):
            return _JsonResponse({"data": [{"id": self.model, "memory": {"effective": {"n_ctx": self.n_ctx}}}]})
        return _JsonResponse({"error": {"message": "not found"}}, 404)

    async def post(self, url: str, json: dict[str, Any] | None = None, headers: Any = None) -> _JsonResponse:
        self.posts.append((url, json or {}))
        if not json or json.get("model") != self.model:
            return _JsonResponse({"error": {"message": "model name is missing from the request"}}, 400)
        if url.endswith("/apply-template"):
            return _JsonResponse({"prompt": "<|im_start|>user\nhi<|im_end|>"})
        if url.endswith("/tokenize"):
            return _JsonResponse({"tokens": [1, 2, 3, 4, 5]})
        return _JsonResponse({"error": {"message": "not found"}}, 404)


@pytest.mark.asyncio
async def test_count_request_tokens_sends_model_in_body_for_router_mode() -> None:
    """Router-mode llama-server rejects /apply-template and /tokenize without a body model."""
    adapter = LlamaCppAdapter("http://router:8089")
    client = _RouterClient("iq3s-mtp", 49152)
    with patch.object(httpx, "AsyncClient", return_value=client):
        count = await adapter.count_request_tokens("", "iq3s-mtp", [{"role": "user", "content": "hi"}], "off")
    assert count == 5
    assert all(body.get("model") == "iq3s-mtp" for _, body in client.posts)


@pytest.mark.asyncio
async def test_get_context_window_falls_back_to_slots_in_router_mode() -> None:
    """A router's /props reports n_ctx 0; the real slot size comes from /slots."""
    adapter = LlamaCppAdapter("http://router:8089")
    client = _RouterClient("iq3s-mtp", 49152)
    with patch.object(httpx, "AsyncClient", return_value=client):
        window = await adapter.get_context_window("", "iq3s-mtp")
    assert window == 49152


@pytest.mark.asyncio
async def test_get_context_window_falls_back_to_models_catalogue() -> None:
    """When /slots is unavailable, memory.effective.n_ctx from /v1/models is used."""
    adapter = LlamaCppAdapter("http://router:8089")

    class _NoSlots(_RouterClient):
        async def get(self, url, params=None, headers=None):
            if url.endswith("/slots"):
                return _JsonResponse({"error": {"message": "not found"}}, 404)
            return await super().get(url, params, headers)

    client = _NoSlots("longctx", 131072)
    with patch.object(httpx, "AsyncClient", return_value=client):
        window = await adapter.get_context_window("", "longctx")
    assert window == 131072


@pytest.mark.asyncio
async def test_get_context_window_prefers_props_when_present() -> None:
    """A single-model server's /props value still wins over the fallbacks."""
    adapter = LlamaCppAdapter("http://single:8080")

    class _Single(_RouterClient):
        async def get(self, url, params=None, headers=None):
            if url.endswith("/props"):
                return _JsonResponse({"default_generation_settings": {"n_ctx": 65536}})
            return _JsonResponse({"error": {"message": "unexpected"}}, 500)

    client = _Single("local", 49152)
    with patch.object(httpx, "AsyncClient", return_value=client):
        window = await adapter.get_context_window("", "local")
    assert window == 65536
