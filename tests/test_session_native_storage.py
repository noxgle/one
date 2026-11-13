# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from one.core.session_manager import SessionManager


def test_native_private_resume_forks_and_binding(tmp_path: Path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    first = sm.append_message({"role": "user", "content": "hello"})
    second = sm.append_message({"role": "assistant", "content": "answer"})
    items = [{"type": "message", "content": "private-marker"},
             {"type": "compaction", "encrypted_content": "opaque-marker"}]
    native = sm.append_native_compaction("p", "m", "e", items, first, second)
    items[0]["content"] = "changed"
    view = sm.get_native_compaction("p", "m", "e")
    assert view is not None and view["nativeInputItems"][0]["content"] == "private-marker"
    view["nativeInputItems"].clear()
    assert sm.get_native_compaction("p", "m", "e")["coveredEntryIds"] == [first, second]
    assert sm.get_native_compaction("p", "other", "e") is None
    assert sm.get_native_compaction("other", "m", "e") is None
    assert sm.get_native_compaction("p", "m", "other") is None
    assert sm.get_last_compaction() is None
    assert sm.session_file is not None
    path = Path(sm.session_file)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "opaque-marker" in path.read_text()
    public = [sm.get_entries(), sm.get_entry(native), sm.get_branch(), sm.get_tree(),
              sm.build_session_context(), sm.get_compaction_stats()]
    assert "marker" not in json.dumps(public)
    assert len(sm.build_session_context()["messages"]) == 2
    exported = Path(sm.export_to_jsonl(str(tmp_path / "export.jsonl")))
    assert "marker" not in exported.read_text()
    resumed = SessionManager.open(str(path))
    assert resumed.get_native_compaction("p", "m", "e")["entryId"] == native
    fork = SessionManager.fork_from(str(path), str(tmp_path), str(tmp_path / "fork"))
    assert fork.get_native_compaction("p", "m", "e")["entryId"] == native
    branch_path = resumed.create_branched_session(native)
    assert branch_path is not None
    assert SessionManager.open(branch_path).get_native_compaction("p", "m", "e")["entryId"] == native
    third = fork.append_message({"role": "user", "content": "later"})
    latest = fork.append_native_compaction("p", "m", "e", [{"type": "compaction", "encrypted_content": "new"}], first, third)
    assert fork.get_native_compaction("p", "m", "e")["entryId"] == latest
    fork.branch(second)
    assert fork.get_native_compaction("p", "m", "e") is None
    fork.branch(latest)
    fork._by_id[latest]["coveredEntryIds"] = [first, third]
    assert fork.get_native_compaction("p", "m", "e") is None


def test_native_transaction_failure_and_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    first = sm.append_message({"role": "user", "content": "hello"})
    before = Path(sm.session_file).read_bytes()
    def fail(*args, **kwargs):
        raise OSError("write failed")
    monkeypatch.setattr("one.core.session_manager.atomic_write_text", fail)
    with pytest.raises(OSError, match="write failed"):
        sm.append_native_compaction("p", "m", "e", [{"type": "compaction", "encrypted_content": "secret"}], first, first)
    assert sm.get_leaf_id() == first
    assert sm.get_native_compaction("p", "m", "e") is None
    assert Path(sm.session_file).read_bytes() == before
    with pytest.raises(ValueError):
        sm.append_native_compaction("p", "m", "e", [], first, first)
