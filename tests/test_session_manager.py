from __future__ import annotations

import json
from pathlib import Path

import pytest

from one.core.session_manager import SESSION_NAME_MAX_CHARS, SessionManager, normalize_session_name


def test_session_append_and_context(tmp_path: Path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    sm.append_model_change("openai", "gpt-4.1")
    sm.append_thinking_level_change("medium")
    sm.append_message({"role": "user", "content": "hello"})
    sm.append_message({"role": "assistant", "content": [{"type": "text", "text": "hi"}], "provider": "openai", "model": "gpt-4.1"})

    ctx = sm.build_session_context()
    assert ctx["thinkingLevel"] == "medium"
    assert ctx["model"]["provider"] == "openai"
    assert len(ctx["messages"]) == 2


def test_migrate_v1_to_v4(tmp_path: Path):
    session_file = tmp_path / "legacy.jsonl"
    lines = [
        json.dumps({"type": "session", "id": "s1", "timestamp": "2020-01-01T00:00:00Z", "cwd": str(tmp_path)}),
        json.dumps({"type": "message", "timestamp": "2020-01-01T00:00:01Z", "message": {"role": "hookMessage", "content": "x"}}),
    ]
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    sm = SessionManager.open(str(session_file), str(tmp_path))
    entries = sm.get_entries()
    assert entries[0]["message"]["role"] == "custom"
    assert sm.get_header()["version"] == 4


def test_migrate_persists_upgrade_to_disk(tmp_path: Path):
    session_file = tmp_path / "legacy.jsonl"
    lines = [
        json.dumps({"type": "session", "id": "s1", "timestamp": "2020-01-01T00:00:00Z", "cwd": str(tmp_path)}),
        json.dumps({"type": "message", "timestamp": "2020-01-01T00:00:01Z", "message": {"role": "hookMessage", "content": "x"}}),
    ]
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    SessionManager.open(str(session_file), str(tmp_path))

    on_disk = [json.loads(x) for x in session_file.read_text(encoding="utf-8").splitlines()]
    assert on_disk[0]["version"] == 4
    assert on_disk[1]["message"]["role"] == "custom"
    # Re-opening an already-migrated file must stay at v4 and not re-migrate.
    sm = SessionManager.open(str(session_file), str(tmp_path))
    assert sm.get_header()["version"] == 4


def test_migrate_v3_backfills_ids_and_normalizes_timestamps(tmp_path: Path):
    session_file = tmp_path / "v3.jsonl"
    lines = [
        json.dumps({"type": "session", "version": 3, "id": "s1", "timestamp": "2020-01-01T00:00:00Z", "cwd": str(tmp_path)}),
        json.dumps({"type": "message", "id": "m1", "parentId": None, "timestamp": 1577836800000, "message": {"role": "user", "content": "x"}}),
        json.dumps({"type": "message", "parentId": "m1", "timestamp": "2020-01-02T00:00:00Z", "message": {"role": "assistant", "content": "y"}}),
    ]
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    sm = SessionManager.open(str(session_file), str(tmp_path))
    assert sm.get_header()["version"] == 4
    entries = sm.get_entries()
    assert entries[0]["timestamp"].endswith("Z")  # int ms timestamp normalized
    assert entries[1]["id"]  # missing id backfilled
    assert entries[1]["parentId"] == "m1"


def test_branch_and_get_branch(tmp_path: Path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    sm.append_message({"role": "user", "content": "a"})
    fork_point = sm.get_leaf_id()
    assert fork_point is not None
    sm.append_message({"role": "assistant", "content": "b1"})
    sm.branch(fork_point)
    assert sm.get_leaf_id() == fork_point
    sm.append_message({"role": "assistant", "content": "b2"})

    branch = sm.get_branch()
    contents = [e["message"]["content"] for e in branch if e["type"] == "message"]
    assert contents == ["a", "b2"]

    tree = sm.get_tree()
    assert len(tree) == 1
    root = tree[0]
    assert root["entry"]["type"] == "message"
    # b1 and b2 both fork from message "a"
    assert len(root["children"]) == 2


def test_branch_with_summary_inserts_summary_message(tmp_path: Path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    sm.append_message({"role": "user", "content": "a"})
    point = sm.get_leaf_id()
    sm.branch_with_summary(point, "Custom branch summary")

    ctx = sm.build_session_context()
    custom = [m for m in ctx["messages"] if m.get("role") == "custom"]
    assert custom and custom[0]["customType"] == "branch_summary"
    assert custom[0]["content"] == "Custom branch summary"
    # Summary message is persisted as an entry (fidelity for replay).
    assert any(e.get("type") == "branch_summary" for e in sm.get_entries())


def test_reset_leaf(tmp_path: Path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    sm.append_message({"role": "user", "content": "a"})
    sm.append_message({"role": "assistant", "content": "b"})
    sm.reset_leaf()
    assert sm.get_leaf_id() is None
    assert sm.get_branch() == []


def test_create_branched_session_copies_only_branch(tmp_path: Path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    sm.append_message({"role": "user", "content": "a"})
    sm.append_message({"role": "assistant", "content": "b"})
    target = sm.get_leaf_id()
    assert target is not None
    old_id = sm.session_id
    # Create an alternate branch, then branch off the original tip.
    first = sm.get_entries()[0]["id"]
    sm.branch(first)
    sm.append_message({"role": "assistant", "content": "b2"})

    path = sm.create_branched_session(target)
    assert path is not None
    # The manager itself is converted into the branched session.
    assert sm.session_id != old_id
    opened = SessionManager.open(path, str(tmp_path / "sessions"))
    ctx = opened.build_session_context()
    assert [m.get("content") for m in ctx["messages"]] == ["a", "b"]
    assert opened.get_header().get("parentSession")
    assert opened.session_id == sm.session_id


def test_fork_from_copies_all_entries(tmp_path: Path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    sm.append_message({"role": "user", "content": "a"})
    source_path = sm.session_file
    assert source_path is not None

    forked = SessionManager.fork_from(source_path, str(tmp_path / "other"))
    ctx = forked.build_session_context()
    assert ctx["messages"][0]["content"] == "a"
    assert forked.session_id != sm.session_id
    assert forked.get_header().get("parentSession") == str(Path(source_path).resolve())


def test_get_last_compaction(tmp_path: Path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    assert sm.get_last_compaction() is None
    first_kept = sm.get_entries()[0]["id"] if sm.get_entries() else "root"
    sm.append_compaction("S1", first_kept, tokens_before=10)
    sm.append_compaction("S2", first_kept, tokens_before=20)
    last = sm.get_last_compaction()
    assert last is not None
    assert last["summary"] == "S2"


def test_get_message_entry_ids_aligned_with_interleaved_entries(tmp_path: Path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    sm.append_model_change("openai", "gpt-4.1")
    sm.append_thinking_level_change("medium")
    for i in range(30):
        sm.append_message({"role": "user", "content": f"msg-{i}"})

    ids = sm.get_message_entry_ids()
    assert len(ids) == 30
    # The 3rd message maps to the 3rd message-producing entry, skipping
    # model_change/thinking_level_change entries that precede it.
    sm.append_compaction("S", ids[2], tokens_before=10)
    ctx = sm.build_session_context()
    contents = [m.get("content") for m in ctx["messages"] if m.get("role") != "custom"]
    assert contents == [f"msg-{i}" for i in range(2, 30)]


def test_compaction_context_reconstruction(tmp_path: Path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    for i in range(30):
        sm.append_message({"role": "user", "content": f"msg-{i}"})
    entries = sm.get_entries()
    first_kept = entries[10]["id"]
    sm.append_compaction("Summary of earlier messages", first_kept, tokens_before=100)
    sm.append_message({"role": "assistant", "content": "after"})

    ctx = sm.build_session_context()
    messages = ctx["messages"]
    summaries = [m for m in messages if m.get("customType") == "compaction_summary"]
    assert len(summaries) == 1
    assert summaries[0]["content"] == "Summary of earlier messages"
    contents = [m.get("content") for m in messages if m.get("role") != "custom"]
    assert "msg-0" not in contents and "msg-9" not in contents
    assert contents == [f"msg-{i}" for i in range(10, 30)] + ["after"]


def test_export_to_jsonl_round_trip(tmp_path: Path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    sm.append_message({"role": "user", "content": "u1"})
    sm.append_message({"role": "assistant", "content": "a1"})
    out = sm.export_to_jsonl(str(tmp_path / "export.jsonl"))
    assert Path(out).exists()

    reopened = SessionManager.open(out, str(tmp_path))
    ctx = reopened.build_session_context()
    assert [m["content"] for m in ctx["messages"]] == ["u1", "a1"]


def test_session_names_normalize_validate_and_auto_name_once(tmp_path: Path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    assert sm.set_automatic_name_from_prompt("  first\n\tprompt\x00  ") is True
    assert sm.get_session_name() == "first prompt"
    assert sm.set_automatic_name_from_prompt("later prompt") is False
    assert sm.get_session_name() == "first prompt"

    sm.set_session_name("  renamed\n title  ")
    assert sm.get_session_name() == "renamed title"
    with pytest.raises(ValueError, match="cannot be empty"):
        sm.set_session_name("\x00 \t")
    with pytest.raises(ValueError, match="at most"):
        sm.set_session_name("x" * (SESSION_NAME_MAX_CHARS + 1))
    assert normalize_session_name("é" * (SESSION_NAME_MAX_CHARS + 3), truncate=True) == "é" * SESSION_NAME_MAX_CHARS


def test_delete_managed_session_removes_only_matching_sidecar(tmp_path: Path):
    directory = tmp_path / "sessions"
    first = SessionManager.create(str(tmp_path), str(directory))
    first.append_message({"role": "user", "content": "one"})
    second = SessionManager.create(str(tmp_path), str(directory))
    second.append_message({"role": "user", "content": "two"})
    assert first.session_file and second.session_file
    sidecar = Path(first.session_file + ".evidence")
    sidecar.write_text("evidence\n", encoding="utf-8")

    SessionManager.delete(first.session_file, str(directory))

    assert not Path(first.session_file).exists()
    assert not sidecar.exists()
    assert Path(second.session_file).exists()
    with pytest.raises(ValueError, match="outside"):
        SessionManager.delete(str(tmp_path / "outside.jsonl"), str(directory))
