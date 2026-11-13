# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json

import httpx
import pytest

from one.providers.base import ChatResult
from one.providers.codex_responses import BASE_URL, CodexResponsesAdapter
from one.providers.native_compaction import NativeCompactionProtocolError


def test_native_capability_is_opt_in_for_exact_target() -> None:
    class FakeAdapter(CodexResponsesAdapter):
        def supports_native_compaction_for(self, model: str, endpoint: str) -> bool:
            return model == "synthetic-model" and endpoint == "https://example.test/responses"

    assert not CodexResponsesAdapter().supports_native_compaction_for("synthetic-model", f"{BASE_URL}/responses")
    adapter = FakeAdapter()
    assert adapter.supports_native_compaction_for("synthetic-model", "https://example.test/responses")
    assert not adapter.supports_native_compaction_for("other", "https://example.test/responses")
    assert not adapter.supports_native_compaction_for("synthetic-model", f"{BASE_URL}/responses")


def test_native_output_items_are_not_in_result_repr() -> None:
    result = ChatResult(text="", raw={}, usage={}, native_output_items=[{"encrypted_content": "secret-marker"}])
    assert "secret-marker" not in repr(result)
    assert result.native_output_items == [{"encrypted_content": "secret-marker"}]


class EnabledAdapter(CodexResponsesAdapter):
    def supports_native_compaction_for(self, model: str, endpoint: str) -> bool:
        return model == "fake" and endpoint == f"{BASE_URL}/responses"


def test_native_payload_gate_and_replay() -> None:
    items = [{"type": "compaction", "encrypted_content": "secret-marker"}]
    adapter = EnabledAdapter()
    payload = adapter._build_payload("fake", [], "off", True, native_compaction_threshold=100, native_input_items=items)
    assert payload["input"] == items
    assert payload["context_management"] == [{"type": "compaction", "compact_threshold": 100}]
    assert payload["store"] is False and "previous_response_id" not in payload
    legacy = adapter._build_payload("other", [], "off", True, native_compaction_threshold=100)
    assert "context_management" not in legacy
    with pytest.raises(NativeCompactionProtocolError, match="unsupported"):
        adapter._build_payload("other", [], "off", True, native_input_items=items)


@pytest.mark.asyncio
async def test_native_stream_order_replay_and_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [{"type": "compaction", "encrypted_content": "secret-one"},
             {"type": "compaction", "encrypted_content": "secret-two"}]
    events = [{"type": "response.output_item.done", "output_index": i, "item": item}
              for i, item in reversed(list(enumerate(items)))]
    events.append({"type": "response.completed", "response": {"status": "completed", "output": items}})
    requests = []
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, text="".join(f"data: {json.dumps(e)}\n\n" for e in events))
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    adapter = EnabledAdapter()
    result = await adapter.chat("key", "fake", [], "off", native_compaction_threshold=10)
    assert result.native_output_items == items
    assert "secret-one" not in repr(result) and "secret-one" not in repr(result.raw)
    await adapter.chat("key", "fake", [], "off", native_input_items=result.native_output_items)
    assert requests[1]["input"] == items and requests[1]["store"] is False
    assert "previous_response_id" not in requests[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("events", [
    ["not-json secret-marker"],
    [json.dumps({"type": ["secret-marker"]})],
    [json.dumps({"type": "response.completed", "response": {"status": "completed", "output": [{"type": []}]}})],
    [json.dumps({"type": "response.output_item.done", "output_index": 0, "item": {"type": "compaction", "encrypted_content": "secret-marker"}})],
    [json.dumps({"type": "response.completed", "response": {"status": "completed", "output": [{"type": "compaction", "encrypted_content": 3}]}})],
    [json.dumps({"type": "response.output_item.done", "output_index": 1, "item": {"type": "compaction", "encrypted_content": "secret-marker"}}),
     json.dumps({"type": "response.completed", "response": {"status": "completed", "output": []}})],
])
async def test_native_stream_invalid_is_content_free(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="".join(f"data: {e}\n\n" for e in events)))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    with pytest.raises(NativeCompactionProtocolError) as exc:
        await EnabledAdapter().chat("key", "fake", [], "off", native_compaction_threshold=10)
    assert "secret-marker" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 422, 429, 500])
async def test_native_http_errors_are_typed_and_safe(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    from one.core.compaction.native import native_retryable
    from one.providers.native_compaction import NativeCompactionHTTPError

    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text="encrypted-marker"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    for kwargs in ({"native_compaction_threshold": 10}, {"safe_errors": True}):
        with pytest.raises(NativeCompactionHTTPError) as exc:
            await EnabledAdapter().chat("key", "fake", [], "off", **kwargs)
        assert exc.value.status_code == status
        assert "encrypted-marker" not in str(exc.value)
        assert not isinstance(exc.value, NativeCompactionProtocolError)
        assert native_retryable(exc.value) == (status in (400, 422))
    with pytest.raises(RuntimeError, match="encrypted-marker"):
        await EnabledAdapter().chat("key", "fake", [], "off")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["final", "match", "mismatch", "missing_done"])
async def test_native_text_consistency_and_callbacks(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    item = {"type": "message", "content": [{"type": "output_text", "text": "final"}]}
    events = []
    if mode in ("match", "mismatch"):
        events.append({"type": "response.output_text.delta", "delta": "final" if mode == "match" else "wrong"})
    if mode != "missing_done":
        events.append({"type": "response.output_item.done", "output_index": 0, "item": item})
    events.append({"type": "response.completed", "response": {"status": "completed", "output": [item]}})
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, text="".join(f"data: {json.dumps(event)}\n\n" for event in events)))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    callbacks = []
    if mode in ("mismatch", "missing_done"):
        with pytest.raises(NativeCompactionProtocolError):
            await EnabledAdapter().chat("key", "fake", [], "off", on_delta=callbacks.append,
                                        native_compaction_threshold=10)
        assert callbacks == []
    else:
        result = await EnabledAdapter().chat("key", "fake", [], "off", on_delta=callbacks.append,
                                             native_compaction_threshold=10)
        assert result.text == "final" and callbacks == ["final"]


@pytest.mark.asyncio
async def test_native_incomplete_truncation_never_exposes_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    call = {"type": "function_call", "name": "tool", "call_id": "c", "arguments": "{}"}
    events = [{"type": "response.output_item.done", "output_index": 0, "item": call},
              {"type": "response.incomplete", "response": {"status": "incomplete",
               "incomplete_details": {"reason": "max_output_tokens"}, "output": [call]}}]
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, text="".join(f"data: {json.dumps(event)}\n\n" for event in events)))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    result = await EnabledAdapter().chat("key", "fake", [], "off", native_compaction_threshold=10)
    assert result.output_truncated and result.stop_reason == "max_output_tokens"
    assert not result.native_tool_calls and result.native_output_items is None
