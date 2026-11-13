# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from one.core.session_manager import SESSION_NAME_MAX_CHARS, SessionManager, normalize_session_name


@pytest.mark.parametrize("existing", [False, True])
def test_v2_transaction_write_failure_preserves_session(tmp_path: Path, monkeypatch, existing: bool):
    from one.core import persistence

    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    if existing:
        sm.append_message({"role": "user", "content": "kept"})
    path = Path(sm.session_file)
    original = path.read_bytes() if existing else None
    entries = list(sm.get_entries())
    leaf = sm.get_leaf_id()
    context = sm.build_session_context()

    def fail_write(fd, data):
        os.write(fd, data[:10])
        raise OSError("injected write failure")

    monkeypatch.setattr(persistence, "_write_all", fail_write)
    with pytest.raises(OSError, match="injected write failure"):
        sm.append_compaction("summary", leaf or "root", 10, summary_schema="v2", transactional_v2=True)
    assert sm.get_entries() == entries
    assert sm.get_leaf_id() == leaf
    if leaf:
        assert sm.get_entry(leaf) == entries[-1]
    assert sm.build_session_context() == context
    if existing:
        assert path.read_bytes() == original
        reopened = SessionManager.open(str(path))
        assert reopened.get_entries() == entries
        assert reopened.get_leaf_id() == leaf
        assert reopened.build_session_context() == context
    else:
        assert not path.exists()


@pytest.mark.parametrize("persistent", [False, True])
def test_v2_transaction_commit_is_detached_and_reloadable(tmp_path: Path, persistent: bool):
    sm = (SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
          if persistent else SessionManager.in_memory(str(tmp_path)))
    kept = sm.append_message({"role": "user", "content": "kept"})
    details = {"nested": ["original"]}
    entry = sm.append_compaction("summary", kept, 10, details=details, summary_schema="v2",
                                 transactional_v2=True)
    assert isinstance(entry, dict)
    assert entry["id"] == sm.get_leaf_id()
    assert entry["timestamp"]
    assert entry["parentId"] == kept
    details["nested"].append("input mutation")
    entry["details"]["nested"].append("returned mutation")
    assert sm.get_entry(entry["id"])["details"] == {"nested": ["original"]}
    assert [m["content"] for m in sm.build_session_context()["messages"]] == ["summary", "kept"]
    if persistent:
        path = Path(sm.session_file)
        assert path.stat().st_mode & 0o777 == 0o600
        reopened = SessionManager.open(str(path))
        assert reopened.get_leaf_id() == entry["id"]
        assert reopened.get_entry(entry["id"]) == sm.get_entry(entry["id"])
        assert reopened.build_session_context() == sm.build_session_context()


def test_v2_transaction_requires_v2_schema(tmp_path: Path):
    sm = SessionManager.in_memory(str(tmp_path))
    with pytest.raises(ValueError, match="requires summary_schema"):
        sm.append_compaction("summary", "root", 10, transactional_v2=True)
    assert sm.get_entries() == []


def test_v2_transaction_preserves_unparsed_disk_content(tmp_path: Path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    kept = sm.append_message({"role": "user", "content": "kept"})
    path = Path(sm.session_file)
    original = path.read_bytes() + b"unknown data without newline"
    path.write_bytes(original)
    entry = sm.append_compaction("summary", kept, 10, summary_schema="v2", transactional_v2=True)
    assert isinstance(entry, dict)
    assert path.read_bytes().startswith(original + b"\n")
    assert SessionManager.open(str(path)).get_leaf_id() == entry["id"]


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


def test_compaction_stats_persist_reasons_and_skips(tmp_path: Path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    first_kept = sm.get_entries()[0]["id"] if sm.get_entries() else "root"
    sm.append_compaction("manual", first_kept, tokens_before=10, reason="manual")
    sm.append_compaction("automatic", first_kept, tokens_before=20, reason="auto_preflight")
    sm.append_compaction("recovery", first_kept, tokens_before=30, reason="context_limit_retry")
    sm.append_compaction_skipped("auto")
    assert sm.session_file is not None

    reopened = SessionManager.open(sm.session_file, str(tmp_path / "sessions"))
    stats = reopened.get_compaction_stats()
    assert stats["completed"] == 3
    assert stats["skipped"] == 1
    assert stats["tokensBefore"] == 60
    assert stats["byReason"] == {"manual": 1, "automatic": 1, "recovery": 1}
    assert stats["last"]["reason"] == "context_limit_retry"
    assert stats["last"]["timestamp"]


def test_compaction_telemetry_sanitizes_and_aggregates_old_and_new_entries(tmp_path: Path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    sm.append_compaction("old summary", "root", 9)
    sm.append_compaction("new summary", "root", 10, tokens_after=8, tokens_kept=4,
                         strategy="local_separate", summary_schema="legacy", attempts=1, fallback=False,
                         duration_ms=20, usage={"inputTokens": 100, "cachedTokens": 25, "secret": "private"},
                         constraints={"extracted": 2, "prompt": "private"}, rehydrated={"files": 1, "path": "private"})
    sm.append_compaction("bad", "root", 3, tokens_after=True, duration_ms=float("nan"),
                         strategy="private", usage={"inputTokens": True, "cachedTokens": -1},
                         constraints={"extracted": float("inf")})
    last = sm.get_last_compaction()
    assert "tokensAfter" not in last and "durationMs" not in last and last["usage"] is None
    assert "private" not in str(last)
    # Historical extras may not have passed the writer's sanitizer.
    last.update({"strategy": {"private": 1}, "usage": {"inputTokens": True, "cachedTokens": float("inf")},
                 "constraints": {"extracted": -1, "path": "private"}, "rehydrated": {"files": True}})
    stats = sm.get_compaction_stats()
    assert stats["tokensBefore"] == 22 and stats["last"]["summary"] == "bad"
    assert stats["byStrategy"] == {"legacy": 2, "local_separate": 1}
    assert stats["cache"]["input"] == 100 and stats["cache"]["cacheRatio"] == .25
    assert stats["cache"]["requestsMissingUsage"] == 2
    assert stats["avgDurationMs"] == 20
    assert stats["constraints"] == {"extracted": 2} and stats["rehydrated"] == {"files": 1}
    assert "private" not in str(stats)


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


def test_provider_cache_stats_aggregates_reported_counters(tmp_path: Path):
    sm = SessionManager.in_memory(str(tmp_path))
    sm.append_timing(
        "provider_request", 10, "success", provider="chatgpt", purpose="response",
        inputTokens=1000, cachedTokens=800, outputTokens=50, reasoningTokens=20,
    )
    sm.append_timing(
        "provider_request", 12, "success", provider="chatgpt", purpose="response",
        inputTokens=500, cachedTokens=100, outputTokens=30, reasoningTokens=10,
    )
    stats = sm.get_provider_cache_stats()
    assert stats["requests"] == 2
    assert stats["requestsWithUsage"] == 2
    assert stats["requestsMissingUsage"] == 0
    assert stats["usageComplete"] is True
    assert (stats["input"], stats["cached"], stats["output"], stats["reasoning"]) == (1500, 900, 80, 30)
    assert stats["cacheRatio"] == 0.6
    assert stats["byProvider"]["chatgpt"]["requests"] == 2
    assert stats["byProvider"]["chatgpt"]["cacheRatio"] == 0.6
    assert stats["byPurpose"]["response"]["input"] == 1500


def test_provider_cache_stats_missing_usage_is_unknown_not_zero(tmp_path: Path):
    sm = SessionManager.in_memory(str(tmp_path))
    sm.append_timing("provider_request", 5, "error", provider="chatgpt", purpose="response")
    sm.append_timing(
        "provider_request", 8, "success", provider="chatgpt", purpose="response",
        inputTokens=100, cachedTokens=40, outputTokens=5,
    )
    stats = sm.get_provider_cache_stats()
    assert stats["requests"] == 2
    assert stats["requestsWithUsage"] == 1
    assert stats["requestsMissingUsage"] == 1
    assert stats["ratioRequests"] == 1
    assert stats["usageComplete"] is False
    assert stats["input"] == 100
    assert stats["cached"] == 40
    assert stats["output"] == 5
    assert stats["reasoning"] is None
    assert stats["cacheRatio"] == 0.4
    assert stats["byProvider"]["chatgpt"]["requests"] == 2


def test_provider_cache_stats_partial_counters_and_zero_values(tmp_path: Path):
    sm = SessionManager.in_memory(str(tmp_path))
    sm.append_timing(
        "provider_request", 3, "success", provider="openai", purpose="response",
        inputTokens=100, outputTokens=1,
    )
    partial = sm.get_provider_cache_stats()
    assert partial["requestsWithUsage"] == 1
    assert partial["usageComplete"] is False
    assert partial["input"] == 100
    assert partial["cached"] is None
    assert partial["cacheRatio"] is None

    sm2 = SessionManager.in_memory(str(tmp_path / "zero"))
    sm2.append_timing(
        "provider_request", 3, "success", provider="chatgpt", purpose="response",
        inputTokens=0, cachedTokens=0, outputTokens=0, reasoningTokens=0,
    )
    zero = sm2.get_provider_cache_stats()
    assert (zero["input"], zero["cached"], zero["output"], zero["reasoning"]) == (0, 0, 0, 0)
    assert zero["usageComplete"] is True
    assert zero["cacheRatio"] is None


def test_provider_cache_stats_groups_and_branch_reload(tmp_path: Path):
    directory = tmp_path / "sessions"
    sm = SessionManager(str(tmp_path), str(directory), None, True)
    root = sm.append_message({"role": "user", "content": "branch point"})
    sm.append_timing(
        "provider_request", 4, "success", provider="chatgpt", purpose="compaction",
        inputTokens=200, cachedTokens=100, outputTokens=10,
    )
    sm.append_timing(
        "provider_request", 6, "success", provider="anthropic", purpose="response",
        inputTokens=50, outputTokens=5,
    )
    path = sm.session_file
    assert path
    reloaded = SessionManager(str(tmp_path), str(directory), path, True)
    stats = reloaded.get_provider_cache_stats()
    assert stats["requests"] == 2
    assert stats["ratioRequests"] == 1
    assert stats["usageComplete"] is False
    assert stats["input"] == 250
    assert stats["cached"] == 100
    assert stats["cacheRatio"] == 0.5
    assert stats["byProvider"]["anthropic"]["requests"] == 1
    assert stats["byProvider"]["chatgpt"]["cached"] == 100
    assert stats["byPurpose"]["compaction"]["cacheRatio"] == 0.5

    # A sibling branch must not contribute to the active branch totals.
    reloaded.branch(root)
    reloaded.append_timing(
        "provider_request", 7, "success", provider="chatgpt", purpose="response",
        inputTokens=10, cachedTokens=10, outputTokens=1,
    )
    branched = reloaded.get_provider_cache_stats()
    assert branched["requests"] == 1
    assert branched["input"] == 10
    assert branched["cacheRatio"] == 1.0


def test_append_timing_accepts_only_non_negative_integer_counters(tmp_path: Path):
    sm = SessionManager.in_memory(str(tmp_path))
    sm.append_timing(
        "provider_request", 1, "success", provider="chatgpt",
        inputTokens=10, cachedTokens="20", outputTokens=True, reasoningTokens=-1,
    )
    entry = next(e for e in sm.get_entries() if e.get("scope") == "provider_request")
    assert entry["inputTokens"] == 10
    assert "cachedTokens" not in entry
    assert "outputTokens" not in entry
    assert "reasoningTokens" not in entry


def test_compaction_telemetry_disk_reload_and_fork(tmp_path: Path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    sm.append_message({"role": "user", "content": "old"})
    kept = sm.append_message({"role": "user", "content": "kept"})
    sm.append_compaction("legacy", kept, 12)
    sm.append_compaction("checkpoint", kept, 10, summary_schema="v2", strategy="extractive",
                         fallback=True, constraints={"extracted": 3, "preserved": 2},
                         usage={"inputTokens": 8, "cachedTokens": 2}, tokens_after=4, duration_ms=5)
    sm.append_message({"role": "assistant", "content": "after"})
    path = sm.session_file
    assert path is not None
    reopened = SessionManager.open(path)
    forked = SessionManager.fork_from(path, str(tmp_path), str(tmp_path / "fork"))
    assert forked.session_file is not None
    forked = SessionManager.open(forked.session_file)
    for session in (reopened, forked):
        entries = [e for e in session.get_entries() if e.get("type") == "compaction"]
        assert len(entries) == 2 and "summarySchema" not in entries[0]
        assert entries[1]["summarySchema"] == "v2" and entries[1]["fallback"] is True
        stats = session.get_compaction_stats()
        assert stats["constraints"] == {"extracted": 3, "preserved": 2}
        assert stats["byStrategy"] == {"legacy": 1, "extractive": 1}
        assert stats["cache"]["requestsMissingUsage"] == 1
        assert [m["content"] for m in session.build_session_context()["messages"]] == [
            "checkpoint", "kept", "after"
        ]
