# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import copy

import pytest

from one.core.agent_session import BusySessionError, _AbortSignal, _RunAdmissionError
from one.core.compaction.checkpoint import CheckpointFailure
from one.core.session_manager import SessionManager
from tests.test_compaction_v2 import agent_with_history


@pytest.mark.asyncio
@pytest.mark.parametrize(("exception", "reason"), [(RuntimeError("secret"), "provider_error"), (TimeoutError(), "timeout")])
async def test_provider_failure(tmp_path, monkeypatch, exception, reason):
    agent = agent_with_history(tmp_path)
    before = copy.deepcopy(agent.messages)
    events = []
    agent.subscribe(events.append)

    async def invoke(*args, **kwargs):
        raise exception

    monkeypatch.setattr(agent, "_invoke_provider", invoke)
    result = await agent.compact()
    assert result["failureReason"] == reason
    assert agent.messages == before
    assert agent.session_manager.get_last_compaction() is None
    assert sum(e["type"] == "compaction_end" for e in events) == 1
    assert "secret" not in repr(events)


@pytest.mark.asyncio
async def test_storage_failure_preserves_leaf(tmp_path, monkeypatch):
    agent = agent_with_history(tmp_path, model=False)
    before = copy.deepcopy(agent.messages)
    leaf = agent.session_manager.get_branch()[-1]["id"]

    def fail(*args, **kwargs):
        raise OSError("secret")

    monkeypatch.setattr(agent.session_manager, "_append_transactional", fail)
    events = []
    agent.subscribe(events.append)
    result = await agent.compact()
    assert result["failureReason"] == "error"
    assert agent.messages == before
    assert agent.session_manager.get_branch()[-1]["id"] == leaf
    assert sum(e["type"] == "compaction_failed" for e in events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["abort", "messages"])
async def test_invalidate_during_provider(tmp_path, monkeypatch, change):
    agent = agent_with_history(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def invoke(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"content": "invalid"}

    monkeypatch.setattr(agent, "_invoke_provider", invoke)
    task = asyncio.create_task(agent.compact())
    await entered.wait()
    if change == "abort":
        agent.abort_compaction()
        assert agent.is_compacting
        with pytest.raises(BusySessionError):
            await agent.compact()
    else:
        agent.messages.append({"role": "user", "content": "new"})
    release.set()
    result = await task
    assert result["aborted"] and calls == 1
    assert not agent.is_compacting
    assert agent.session_manager.get_last_compaction() is None


@pytest.mark.asyncio
async def test_admission_does_not_retry(tmp_path, monkeypatch):
    agent = agent_with_history(tmp_path)
    calls = 0
    events = []
    agent.subscribe(events.append)

    async def invoke(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise _RunAdmissionError("denied")

    monkeypatch.setattr(agent, "_invoke_provider", invoke)
    with pytest.raises(_RunAdmissionError):
        await agent.compact()
    assert calls == 1
    assert sum(e["type"] == "compaction_end" for e in events) == 1


@pytest.mark.asyncio
async def test_concurrent_compaction_admission_cannot_overwrite_busy_flag(tmp_path, monkeypatch):
    agent = agent_with_history(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def invoke(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"content": "invalid"}

    monkeypatch.setattr(agent, "_invoke_provider", invoke)
    first = asyncio.create_task(agent.compact())
    second = asyncio.create_task(agent.compact())
    await asyncio.wait_for(entered.wait(), timeout=2)
    done, pending = await asyncio.wait({first, second}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
    assert len(done) == len(pending) == 1
    with pytest.raises(BusySessionError):
        done.pop().result()
    assert agent.is_compacting
    release.set()
    await asyncio.wait_for(pending.pop(), timeout=2)
    assert not agent.is_compacting


@pytest.mark.asyncio
async def test_latch_manual_bypass_and_success_reset(tmp_path, monkeypatch):
    agent = agent_with_history(tmp_path)
    calls = 0

    async def invoke(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("failure")

    monkeypatch.setattr(agent, "_invoke_provider", invoke)
    await agent.compact()
    await agent.compact()
    assert (await agent.compact(reason="auto"))["skipReason"] == "failure_latch"
    assert calls == 2
    await agent.compact()
    assert calls == 3
    agent.settings_manager._global.setdefault("compaction", {})["summarizeWithModel"] = False
    assert not (await agent.compact())["skipped"]
    assert agent._v2_compaction_failures == 0


@pytest.mark.asyncio
async def test_fallback_failure_and_new_user_reset(tmp_path, monkeypatch):
    agent = agent_with_history(tmp_path)
    before = copy.deepcopy(agent.messages)

    async def fail(*args, **kwargs):
        raise CheckpointFailure("invalid_fallback", 2)

    monkeypatch.setattr("one.core.agent_session.generate_checkpoint", fail)
    assert (await agent.compact())["failureReason"] == "invalid_fallback"
    assert agent.messages == before and agent.session_manager.get_last_compaction() is None
    agent._v2_compaction_failures = 2
    agent._steering = ["new real message"]
    await agent._inject_pending_steering()
    assert agent._v2_compaction_failures == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["abort", "run_cancel"])
async def test_stop_after_provider_response_prevents_commit(tmp_path, monkeypatch, stop):
    agent = agent_with_history(tmp_path)
    before = copy.deepcopy(agent.messages)
    events = []
    agent.subscribe(events.append)

    async def invoke(*args, **kwargs):
        if stop == "abort":
            agent._abort_requested = True
        else:
            agent._run_context.cancel_subtree(agent._run_node_id, "user_cancel")
        return {"content": "invalid"}

    monkeypatch.setattr(agent, "_invoke_provider", invoke)
    with pytest.raises(_AbortSignal if stop == "abort" else asyncio.CancelledError):
        await agent.compact()
    assert agent.messages == before
    assert agent.session_manager.get_last_compaction() is None
    assert [e["type"] for e in events if e["type"].startswith("compaction_")] == [
        "compaction_start", "compaction_end"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["skip", "commit"])
async def test_disk_fault_preserves_existing_compaction_on_resume(tmp_path, monkeypatch, stage):
    agent = agent_with_history(tmp_path, model=False)
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    manager.append_message({"role": "user", "content": "Build it. Never delete files."})
    agent.session_manager = manager
    agent.messages = manager.build_session_context()["messages"]
    assert not (await agent.compact())["skipped"]
    before = copy.deepcopy(agent.messages)
    branch = copy.deepcopy(manager.get_branch())
    path = manager.session_file
    from pathlib import Path
    disk = Path(path).read_bytes()
    events = []
    agent.subscribe(events.append)

    def fail(*args, **kwargs):
        raise OSError("disk fault")

    monkeypatch.setattr("one.core.session_manager.atomic_write_text", fail)
    if stage == "skip":
        result = await agent.compact(reason="auto")
        assert result["skipped"]
        expected = ["compaction_start", "compaction_end"]
    else:
        result = await agent.compact()
        assert result["failureReason"] == "error" and result["summary"] == ""
        expected = ["compaction_start", "compaction_failed", "compaction_end"]
    assert [e["type"] for e in events if e["type"].startswith("compaction_")] == expected
    assert agent.messages == before and manager.get_branch() == branch
    assert Path(path).read_bytes() == disk
    assert SessionManager.open(path).build_session_context()["messages"] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["extension", "count", "ledger"])
async def test_error_before_outcome_has_one_end_and_no_placeholder(tmp_path, monkeypatch, stage):
    agent = agent_with_history(tmp_path)
    before = copy.deepcopy(agent.messages)
    events = []
    agent.subscribe(events.append)

    async def fail_async(*args, **kwargs):
        raise RuntimeError("private")

    if stage == "extension":
        class Hooks:
            def has_hooks(self, name):
                return True
        agent._extension_runtime = Hooks()
        monkeypatch.setattr(agent, "_invoke_extension_compacting", fail_async)
    elif stage == "count":
        monkeypatch.setattr(agent, "_exact_request_tokens", fail_async)
    else:
        def overflow(*args, **kwargs):
            raise ValueError("ledger overflow")
        monkeypatch.setattr("one.core.agent_session.extract_constraints", overflow)

    result = await agent.compact()
    assert result["failureReason"] == {"extension": "error", "count": "provider_error",
                                        "ledger": "invalid_constraints"}[stage]
    assert result["summary"] == "" and agent.messages == before
    assert agent.session_manager.get_last_compaction() is None
    assert [e["type"] for e in events if e["type"].startswith("compaction_")] == [
        "compaction_start", "compaction_failed", "compaction_end"
    ]
