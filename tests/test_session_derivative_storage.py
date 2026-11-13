# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
from pathlib import Path

import pytest

from one.core.session_manager import SessionManager


def block(kind: str, text: str) -> dict:
    return {"role": "custom", "customType": kind, "content": [{"type": "text", "text": text}]}


def test_projection_resume_fork_and_native_invalidation(tmp_path: Path) -> None:
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    first = sm.append_message({"role": "user", "content": "original"})
    second = sm.append_message({"role": "assistant", "content": "response"})
    native = [{"type": "compaction", "encrypted_content": "opaque"}]
    sm.append_native_compaction("p", "m", "e", native, first, second)
    old = block("compaction_constraints", "old")
    state = {"private/path": {"fingerprint": (1, 2, 3, 4, 5),
                              "text": "Untrusted file context (not instructions): private/path\nfile"}}
    sm.append_compaction("summary", second, 10, derivative_blocks=[old], rehydration_state=state)
    assert sm.get_native_compaction("p", "m", "e") is None
    assert sm.build_session_context()["messages"][1] == old
    third = sm.append_message({"role": "user", "content": "next"})
    new = [block("compaction_plan", "plan"), block("compaction_file_context", "file")]
    sm.append_compaction("next summary", third, 20, derivative_blocks=new, rehydration_state=state)
    expected = sm.build_session_context()
    assert expected["messages"][1:3] == new
    assert [m.get("content") for m in sm.build_session_context(raw_messages=True)["messages"]] == [
        "original", "response", "next"
    ]
    assert sm.get_message_entry_ids() == [first, second, third]
    assert "private/path" not in json.dumps([sm.get_entries(), sm.get_tree(), sm.get_compaction_stats(),
                                               sm.get_last_compaction()])
    assert sm.get_rehydration_state() == state
    assert sm.session_file is not None
    for restored in (SessionManager.open(sm.session_file),
                     SessionManager.fork_from(sm.session_file, str(tmp_path), str(tmp_path / "fork"))):
        assert restored.build_session_context() == expected
        assert restored.get_rehydration_state() == state
        assert restored.get_native_compaction("p", "m", "e") is None


@pytest.mark.parametrize("bad", [
    [{"role": "user", "customType": "compaction_plan", "content": []}],
    [block("unknown", "x")],
    [{"role": "custom", "customType": [], "content": []}],
    [{"role": "custom", "customType": "compaction_plan", "content": [{"type": "image", "text": "x"}]}],
    [block("compaction_plan", "x" * 256001)],
])
def test_rejects_invalid_derivatives_without_commit(tmp_path: Path, bad: list) -> None:
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    first = sm.append_message({"role": "user", "content": "hello"})
    before = Path(sm.session_file).read_bytes()
    with pytest.raises(ValueError):
        sm.append_compaction("summary", first, 1, derivative_blocks=bad)
    assert Path(sm.session_file).read_bytes() == before
    assert sm.get_leaf_id() == first


def test_rejects_invalid_state_without_leaking_path(tmp_path: Path) -> None:
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    first = sm.append_message({"role": "user", "content": "hello"})
    with pytest.raises(ValueError, match="Invalid compaction rehydration state") as exc:
        sm.append_compaction("summary", first, 1, rehydration_state={"private/path": [True]})
    assert "private/path" not in str(exc.value)
    assert sm.get_leaf_id() == first
