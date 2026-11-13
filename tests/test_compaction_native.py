# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import httpx
import pytest

from one.core.compaction.native import NativeCompactionView, native_eligible, native_retryable
from one.core.session_manager import SessionManager
from one.providers.codex_responses import BASE_URL, CodexResponsesAdapter
from one.providers.native_compaction import NativeCompactionProtocolError

ENDPOINT = f"{BASE_URL}/responses"
SETTINGS = {"enabled": True, "experimentalV2": True, "strategy": "auto", "native": {"chatgpt": "auto"}}


class Enabled(CodexResponsesAdapter):
    def supports_native_compaction_for(self, model: str, endpoint: str) -> bool:
        return model == "fake" and endpoint == ENDPOINT


def prepare(view):
    sm = view.storage
    return view.prepare(sm.build_session_context()["messages"], sm.get_message_entry_ids(), SETTINGS)


def assistant(sm, text="answer"):
    return sm.append_message({"role": "assistant", "content": text})


def test_gate():
    adapter = Enabled()
    assert native_eligible(adapter, "fake", SETTINGS)
    assert not native_eligible(CodexResponsesAdapter(), "fake", SETTINGS)
    assert not native_eligible(adapter, "other", SETTINGS)
    assert not native_eligible(adapter, "fake", SETTINGS, endpoint=ENDPOINT + "/")
    for key, value in [("enabled", False), ("experimentalV2", False), ("strategy", "extractive"),
                       ("native", {"chatgpt": "off"})]:
        assert not native_eligible(adapter, "fake", {**SETTINGS, key: value})
    assert native_eligible(adapter, "fake", {**SETTINGS, "strategy": "native_openai"})


def test_replay_resume_second_compaction_and_binding(tmp_path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    first = sm.append_message({"role": "user", "content": "hello"})
    view = NativeCompactionView(sm, Enabled(), "fake", "settings-and-preset")
    assert prepare(view) is None
    output = [{"type": "message", "content": "discard"}, {"type": "compaction", "encrypted_content": "secret"},
              {"type": "reasoning", "encrypted_content": "other"}, {"type": "message", "content": "answer"}]
    aid = assistant(sm)
    assert view.commit(output, aid)
    assert "secret" not in repr(view)
    assert "secret" not in repr(sm.build_session_context())
    sm.append_message({"role": "user", "content": "later"})
    replay = prepare(view)
    assert replay[:3] == output[1:]
    assert replay[-1]["role"] == "user"
    aid2 = assistant(sm, "second")
    assert view.commit([{"type": "message", "content": "second"}], aid2)
    resumed = SessionManager.open(sm.session_file)
    again = NativeCompactionView(resumed, Enabled(), "fake", "settings-and-preset")
    replay2 = prepare(again)
    assert replay2 == replay + [{"type": "message", "content": "second"}]
    assert resumed.get_native_compaction(again._binding(), "fake", ENDPOINT)["coveredEntryIds"] == [
        first, aid, sm.get_message_entry_ids()[2], aid2]
    aid3 = assistant(resumed, "third")
    new = [{"type": "compaction", "encrypted_content": "old"}, {"type": "message"},
           {"type": "compaction", "encrypted_content": "new"}, {"type": "message", "content": "third"}]
    assert again.commit(new, aid3)
    assert prepare(again) == new[2:]
    changed = NativeCompactionView(resumed, Enabled(), "fake", "different-preset")
    assert prepare(changed) is None
    switched = NativeCompactionView(resumed, Enabled(), "other", "settings-and-preset")
    assert prepare(switched) is None and switched.disabled
    resumed.branch(aid)
    assert prepare(NativeCompactionView(resumed, Enabled(), "fake", "settings-and-preset")) is None


def test_tool_result_across_replay_boundary(tmp_path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    sm.append_message({"role": "user", "content": "go"})
    view = NativeCompactionView(sm, Enabled(), "fake", "context")
    assert prepare(view) is None
    call = {"type": "function_call", "call_id": "c1", "name": "read", "arguments": "{}"}
    aid = sm.append_message({"role": "assistant", "content": "", "_nativeToolCalls": [
        {"id": "c1", "name": "read", "arguments": {}}]})
    assert view.commit([{"type": "compaction", "encrypted_content": "secret"}, call], aid)
    sm.append_message({"role": "toolResult", "content": "result", "_nativeToolCallId": "c1"})
    replay = prepare(view)
    assert replay[1] == call
    assert replay[2] == {"type": "function_call_output", "call_id": "c1", "output": "result"}


def test_uncompacted_initial_and_binding_change(tmp_path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    sm.append_message({"role": "user", "content": "go"})
    view = NativeCompactionView(sm, Enabled(), "fake", "context")
    assert prepare(view) is None
    assert not view.commit([{"type": "message", "content": "answer"}], assistant(sm))
    assert prepare(view) is None
    view.fingerprint = "changed"
    with pytest.raises(NativeCompactionProtocolError):
        view.commit([{"type": "compaction", "encrypted_content": "private"}], assistant(sm))
    assert view.disabled


def test_failure_atomicity_and_retry(tmp_path, monkeypatch):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    sm.append_message({"role": "user", "content": "go"})
    view = NativeCompactionView(sm, Enabled(), "fake", "context")
    prepare(view)
    aid = assistant(sm)
    with pytest.raises(NativeCompactionProtocolError):
        view.commit([{"type": "compaction", "encrypted_content": 3}], aid)
    assert view.disabled and sm.get_native_compaction(view._binding(), "fake", ENDPOINT) is None
    view = NativeCompactionView(sm, Enabled(), "fake", "context")
    prepare(view)
    aid = assistant(sm)
    def fail(*args):
        raise OSError("disk")
    monkeypatch.setattr(sm, "append_native_compaction", fail)
    with pytest.raises(OSError):
        view.commit([{"type": "compaction", "encrypted_content": "x"}], aid)
    assert view.disabled
    request = httpx.Request("POST", ENDPOINT)
    for status in (400, 422, 429, 500):
        err = httpx.HTTPStatusError("redacted", request=request, response=httpx.Response(status, request=request))
        assert native_retryable(err) == (status in (400, 422))
    fresh = NativeCompactionView(sm, Enabled(), "fake", "context")
    assert fresh.disable(NativeCompactionProtocolError("bad"))
    assert not fresh.disable(NativeCompactionProtocolError("bad"))
    assert not native_retryable(httpx.ConnectError("failure"))


def test_branch_changed_or_unaligned_disables(tmp_path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    sm.append_message({"role": "user", "content": "go"})
    view = NativeCompactionView(sm, Enabled(), "fake", "context")
    assert view.prepare([], sm.get_message_entry_ids(), SETTINGS) is None and view.disabled
    view = NativeCompactionView(sm, Enabled(), "fake", "context")
    prepare(view)
    sm.append_message({"role": "user", "content": "intervening"})
    aid = assistant(sm)
    with pytest.raises(NativeCompactionProtocolError):
        view.commit([{"type": "compaction", "encrypted_content": "x"}], aid)
    assert view.disabled
