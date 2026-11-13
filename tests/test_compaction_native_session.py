# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio

import pytest

from one.core.agent_session import AgentSession, _AbortSignal
from one.core.auth_storage import AuthStorage
from one.core.compaction.native import NativeCompactionView
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.core.types import ModelInfo
from one.providers.base import ChatResult
from one.providers.codex_responses import BASE_URL, CodexResponsesAdapter
from one.providers.native_compaction import NativeCompactionProtocolError


class Loader:
    def get_system_prompt(self, selected_tools=None):
        return "system"


class FakeCodex(CodexResponsesAdapter):
    enabled = True
    failure = False

    def supports_native_compaction_for(self, model, endpoint):
        return self.enabled and model == "fake" and endpoint == f"{BASE_URL}/responses"

    async def chat(self, api_key, model, messages, thinking_level, headers=None, max_tokens=None,
                   tools=None, native_compaction_threshold=None, native_input_items=None):
        self.call = locals()
        if self.failure:
            raise NativeCompactionProtocolError("secret-marker")
        self.output = [{"type": "compaction", "encrypted_content": "secret-marker"}]
        return ChatResult(text="answer", raw={}, usage={}, native_output_items=self.output)


def make_agent(tmp_path, adapter):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("chatgpt", "dummy")
    registry = ModelRegistry.create(auth)
    settings = SettingsManager.in_memory({"compaction": {"enabled": True, "experimentalV2": True}})
    storage = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(storage, settings, registry, Loader(), ModelInfo("chatgpt", "fake", context_window=100000), "off")
    agent.providers = {"chatgpt": adapter}
    storage.append_message({"role": "user", "content": "hello"})
    agent.messages = storage.build_session_context()["messages"]
    return agent


@pytest.mark.asyncio
async def test_native_request_and_private_output(tmp_path, monkeypatch):
    adapter = FakeCodex()
    agent = make_agent(tmp_path, adapter)
    seen = []
    original = NativeCompactionView.prepare

    def prepare(view, messages, ids, settings, **kwargs):
        seen.append((messages, ids))
        return original(view, messages, ids, settings, **kwargs)

    monkeypatch.setattr(NativeCompactionView, "prepare", prepare)
    events = []
    agent.subscribe(events.append)
    result = await agent._invoke_provider(agent._flatten_messages_for_provider())
    assert seen == [(agent.session_manager.build_session_context()["messages"], agent.session_manager.get_message_entry_ids())]
    assert adapter.call["native_compaction_threshold"] > 0
    assert adapter.call["native_input_items"] is None
    assert agent._native_pending_output == adapter.output
    assert agent._native_pending_output is not adapter.output
    assert "secret-marker" not in repr(result) + repr(events)


@pytest.mark.asyncio
async def test_production_gate_and_error_disable(tmp_path):
    assert not CodexResponsesAdapter().supports_native_compaction_for("fake", f"{BASE_URL}/responses")
    adapter = FakeCodex()
    adapter.enabled = False
    agent = make_agent(tmp_path, adapter)
    await agent._invoke_provider(agent._flatten_messages_for_provider())
    assert adapter.call["native_compaction_threshold"] is None
    adapter.enabled = True
    adapter.failure = True
    with pytest.raises(NativeCompactionProtocolError, match="Native compaction request failed") as exc:
        await agent._invoke_provider(agent._flatten_messages_for_provider())
    assert "secret-marker" not in str(exc.value)
    assert agent._native_view.disabled


@pytest.mark.asyncio
async def test_prompt_persists_native_window_and_replays_next_turn(tmp_path):
    class SequenceCodex(FakeCodex):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def chat(self, api_key, model, messages, thinking_level, headers=None, max_tokens=None,
                       tools=None, native_compaction_threshold=None, native_input_items=None):
            self.calls.append(native_input_items)
            output = ([{"type": "compaction", "encrypted_content": "secret-marker"}]
                      if len(self.calls) == 1 else
                      [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]}])
            return ChatResult(text="A substantial answer. " * 20, raw={}, usage={}, native_output_items=output)

    adapter = SequenceCodex()
    agent = make_agent(tmp_path, adapter)
    events = []
    agent.subscribe(events.append)
    await agent.prompt("first")
    await agent.prompt("second")
    assert adapter.calls[0] is None
    assert adapter.calls[1][0]["type"] == "compaction"
    assert sum(e["type"] == "compaction_end" for e in events) == 1
    assert "secret-marker" not in repr(agent.messages) + repr(events)


@pytest.mark.asyncio
async def test_native_failure_retries_once_with_canonical_input(tmp_path):
    class RetryCodex(FakeCodex):
        def __init__(self):
            super().__init__()
            self.thresholds = []

        async def chat(self, api_key, model, messages, thinking_level, headers=None, max_tokens=None,
                       tools=None, native_compaction_threshold=None, native_input_items=None):
            self.thresholds.append(native_compaction_threshold)
            if len(self.thresholds) == 1:
                raise NativeCompactionProtocolError("secret-marker")
            return ChatResult(text="A substantial answer. " * 20, raw={}, usage={})

    adapter = RetryCodex()
    agent = make_agent(tmp_path, adapter)
    events = []
    agent.subscribe(events.append)
    await agent.prompt("test")
    assert len(adapter.thresholds) == 2
    assert adapter.thresholds[0] and adapter.thresholds[1] is None
    assert [e["outcome"] for e in events if e["type"] == "provider_request_end"] == ["error", "success"]
    assert "secret-marker" not in repr(events) + repr(agent.messages)


@pytest.mark.asyncio
async def test_native_storage_failure_does_not_repeat_response(tmp_path, monkeypatch):
    adapter = FakeCodex()
    agent = make_agent(tmp_path, adapter)
    events = []
    agent.subscribe(events.append)
    monkeypatch.setattr(agent, "_should_tool_nudge", lambda *args, **kwargs: False)

    def fail(*args, **kwargs):
        raise OSError("secret-marker")

    monkeypatch.setattr(agent.session_manager, "append_native_compaction", fail)
    await agent.prompt("test")
    assert agent._native_view.disabled
    assert agent._native_pending_output is None
    assert sum(e["type"] == "provider_request_start" for e in events) == 1
    assert not any(e["type"] == "compaction_end" for e in events)
    assert "secret-marker" not in repr(events) + repr(agent.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(400, 2), (422, 2), (401, 1), (403, 1), (429, 1), (500, 1)])
async def test_native_http_retry_is_bounded_and_safe(tmp_path, status, expected):
    from one.providers.native_compaction import NativeCompactionHTTPError

    class HTTPAdapter(FakeCodex):
        def __init__(self):
            self.calls = []

        async def chat(self, api_key, model, messages, thinking_level, headers=None, max_tokens=None,
                       tools=None, native_compaction_threshold=None, native_input_items=None, safe_errors=False):
            self.calls.append((native_compaction_threshold, safe_errors))
            if len(self.calls) == 1:
                raise NativeCompactionHTTPError(status)
            raise RuntimeError("secret-marker")

    adapter = HTTPAdapter()
    agent = make_agent(tmp_path, adapter)
    with pytest.raises(NativeCompactionProtocolError) as error:
        await agent._invoke_provider(agent._flatten_messages_for_provider())
    assert len(adapter.calls) == expected
    assert all(safe for _, safe in adapter.calls)
    assert "secret-marker" not in str(error.value)
    if expected == 2:
        assert adapter.calls[1][0] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(400, 2), (422, 2), (401, 1), (429, 1), (500, 1)])
async def test_prompt_native_http_failure_never_generic_retries(tmp_path, status, expected, monkeypatch):
    from one.providers.native_compaction import NativeCompactionHTTPError

    class FailingAdapter(FakeCodex):
        def __init__(self):
            self.calls = []

        async def chat(self, api_key, model, messages, thinking_level, headers=None, max_tokens=None,
                       tools=None, native_compaction_threshold=None, native_input_items=None, safe_errors=False):
            self.calls.append((native_compaction_threshold, messages, native_input_items, safe_errors))
            if len(self.calls) == 1:
                raise NativeCompactionHTTPError(status)
            raise RuntimeError("secret-marker 500")

    adapter = FailingAdapter()
    agent = make_agent(tmp_path, adapter)
    monkeypatch.setattr(agent.settings_manager, "get_retry_enabled", lambda: True)
    events = []
    agent.subscribe(events.append)
    await agent.prompt("prompt")
    assert len(adapter.calls) == expected
    assert all(call[3] for call in adapter.calls)
    if expected == 2:
        assert adapter.calls[1][0] is None
        assert adapter.calls[1][2] is None
        assert "prompt" in repr(adapter.calls[1][1])
    assert "secret-marker" not in repr(events) + repr(agent.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("signal", [_AbortSignal, asyncio.CancelledError])
async def test_native_cancellation_propagates_without_extra_call(tmp_path, fallback, signal):
    class CancelAdapter(FakeCodex):
        def __init__(self):
            self.calls = 0

        async def chat(self, api_key, model, messages, thinking_level, headers=None, max_tokens=None,
                       tools=None, native_compaction_threshold=None, native_input_items=None):
            self.calls += 1
            if fallback and self.calls == 1:
                raise NativeCompactionProtocolError("failed")
            raise signal()

    adapter = CancelAdapter()
    agent = make_agent(tmp_path, adapter)
    with pytest.raises(signal):
        await agent._invoke_provider(agent._flatten_messages_for_provider())
    assert adapter.calls == (2 if fallback else 1)
    assert agent._native_view.disabled is fallback


@pytest.mark.asyncio
async def test_native_stream_progress_is_buffered_until_success(tmp_path, monkeypatch):
    import asyncio

    class StreamingAdapter(FakeCodex):
        async def chat(self, api_key, model, messages, thinking_level, headers=None, max_tokens=None,
                       tools=None, native_compaction_threshold=None, native_input_items=None, on_delta=None):
            for _ in range(4):
                await asyncio.sleep(0.03)
                on_delta("streaming content ")
                assert not any(e["type"] == "message_update" for e in events)
            return ChatResult(text="streaming content " * 4, raw={}, usage={}, native_output_items=[])

    agent = make_agent(tmp_path, StreamingAdapter())
    monkeypatch.setattr(agent.settings_manager, "get_provider_timeout_sec", lambda: 0.06)
    events = []
    agent.subscribe(events.append)
    await agent._invoke_provider(agent._flatten_messages_for_provider())
    assert any(e["type"] == "message_update" for e in events)


@pytest.mark.asyncio
async def test_native_raw_branch_after_local_compaction(tmp_path):
    agent = make_agent(tmp_path, FakeCodex())
    storage = agent.session_manager
    storage.append_message({"role": "assistant", "content": "earlier"})
    kept = storage.append_message({"role": "user", "content": "kept"})
    storage.append_compaction("local summary", kept, tokens_before=10)
    agent.messages = storage.build_session_context()["messages"]
    assert len(agent.messages) != len(storage.get_message_entry_ids())
    await agent._invoke_provider(agent._flatten_messages_for_provider())
    assert not agent._native_view.disabled
    assert "hello" in repr(agent.providers["chatgpt"].call["messages"])


@pytest.mark.asyncio
async def test_private_view_preflight_uses_actual_payload(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, FakeCodex())
    agent.session_manager.append_message({"role": "user", "content": "x" * 400000})
    agent.messages = agent.session_manager.build_session_context()["messages"]
    monkeypatch.setattr(NativeCompactionView, "prepare", lambda *args, **kwargs: [
        {"type": "compaction", "encrypted_content": "small-private"}
    ])
    async def no_compaction(**kwargs):
        pytest.fail("private view fits; local compaction must not run")
    monkeypatch.setattr(agent, "compact", no_compaction)
    await agent._preflight_compact()
    await agent._invoke_provider(agent._flatten_messages_for_provider())
    assert agent.providers["chatgpt"].call["native_input_items"][0]["encrypted_content"] == "small-private"


@pytest.mark.asyncio
async def test_oversized_private_view_never_sent(tmp_path, monkeypatch):
    from one.core.agent_session import _CompactionInputError

    agent = make_agent(tmp_path, FakeCodex())
    monkeypatch.setattr(NativeCompactionView, "prepare", lambda *args, **kwargs: [
        {"type": "compaction", "encrypted_content": "x" * 200000}
    ])
    with pytest.raises(_CompactionInputError):
        await agent._invoke_provider(agent._flatten_messages_for_provider())
    assert not hasattr(agent.providers["chatgpt"], "call")


@pytest.mark.asyncio
async def test_native_compaction_batch_executes_tools_once_and_replays_results(tmp_path, monkeypatch):
    class BatchCodex(FakeCodex):
        def __init__(self):
            self.calls = []

        async def chat(self, api_key, model, messages, thinking_level, headers=None, max_tokens=None,
                       tools=None, native_compaction_threshold=None, native_input_items=None):
            self.calls.append(native_input_items)
            if len(self.calls) == 1:
                calls = [{"id": "a", "name": "read", "arguments": {"path": "a.txt"}},
                         {"id": "b", "name": "read", "arguments": {"path": "b.txt"}}]
                output = [{"type": "compaction", "encrypted_content": "secret-marker"}, *[
                    {"type": "function_call", "call_id": c["id"], "name": c["name"],
                     "arguments": '{"path":"' + c["arguments"]["path"] + '"}'} for c in calls]]
                return ChatResult(text="", raw={}, usage={}, native_tool_calls=calls, native_output_items=output)
            return ChatResult(text="Finished reading both files. " * 10, raw={}, usage={}, native_output_items=[
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}])

    adapter = BatchCodex()
    agent = make_agent(tmp_path, adapter)
    counts = []
    async def execute(name, args, timeout_sec=None):
        counts.append((name, args["path"]))
        return {"content": [{"type": "text", "text": "result-" + args["path"]}]}
    monkeypatch.setattr(agent, "_execute_tool_by_name", execute)
    monkeypatch.setattr(agent, "_should_tool_nudge", lambda *args, **kwargs: False)
    monkeypatch.setattr(agent, "_should_repair_tool_response", lambda *args, **kwargs: False)
    events = []
    agent.subscribe(events.append)
    await agent.prompt("read both")
    assert counts == [("read", "a.txt"), ("read", "b.txt")]
    assert len(adapter.calls) == 2 and adapter.calls[0] is None
    replay = adapter.calls[1]
    assert [(x["type"], x.get("call_id")) for x in replay] == [
        ("compaction", None), ("function_call", "a"), ("function_call", "b"),
        ("function_call_output", "a"), ("function_call_output", "b")]
    assert "result-a.txt" in repr(replay) and "result-b.txt" in repr(replay)
    assert [e["result"]["strategy"] for e in events if e["type"] == "compaction_end"] == ["native_openai"]
    assert not any(m.get("customType") == "compaction_summary" for m in agent.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("fork", [False, True])
async def test_disk_resume_or_fork_replays_native_window(tmp_path, fork, monkeypatch):
    adapter = FakeCodex()
    agent = make_agent(tmp_path, adapter)
    storage = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    agent.session_manager = storage
    agent.messages = []
    monkeypatch.setattr(agent, "_should_tool_nudge", lambda *args, **kwargs: False)
    await agent.prompt("first")
    path = storage.session_file
    assert path is not None
    reopened = (SessionManager.fork_from(path, str(tmp_path), str(tmp_path / "fork"))
                if fork else SessionManager.open(path))
    resumed = make_agent(tmp_path, FakeCodex())
    resumed.session_manager = reopened
    resumed.messages = reopened.build_session_context()["messages"]
    monkeypatch.setattr(resumed, "_should_tool_nudge", lambda *args, **kwargs: False)
    await resumed.prompt("second")
    assert resumed.providers["chatgpt"].call["native_input_items"][0]["encrypted_content"] == "secret-marker"
    assert "secret-marker" not in repr(resumed.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["model", "provider", "native_off", "v2_off"])
async def test_native_rollback_uses_canonical_history(tmp_path, monkeypatch, change):
    adapter = FakeCodex()
    agent = make_agent(tmp_path, adapter)
    monkeypatch.setattr(agent, "_should_tool_nudge", lambda *args, **kwargs: False)
    await agent.prompt("original prompt")
    assert agent._native_view is not None
    if change == "model":
        await agent.set_model(ModelInfo("chatgpt", "other", context_window=100000), enrich=False)
    elif change == "provider":
        agent.model_registry._auth.set_runtime_api_key("openai", "dummy")
        agent.providers["openai"] = adapter
        await agent.set_model(ModelInfo("openai", "other", context_window=100000), enrich=False)
    elif change == "native_off":
        agent.settings_manager._global["compaction"]["native"] = {"chatgpt": "off"}
    else:
        agent.settings_manager._global["compaction"]["experimentalV2"] = False
    await agent.prompt("next prompt")
    assert adapter.call["native_input_items"] is None
    assert adapter.call["native_compaction_threshold"] is None
    assert "original prompt" in repr(adapter.call["messages"])
    assert "secret-marker" not in repr(adapter.call["messages"])
