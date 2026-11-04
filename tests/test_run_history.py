# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
import os

import pytest

from one.core.run_history import RUN_HISTORY_VERSION, RunHistory


def snapshot(run_id: str, *states: str) -> dict:
    return {"runId": run_id, "nodes": [{"state": state} for state in states],
            "aggregates": {"tokens": 42}}


def test_absent_sidecar_does_not_create_files(tmp_path):
    history = RunHistory(tmp_path / "session.jsonl", "session")
    assert history.read() == []
    assert not history.path.exists()


def test_versioned_multi_run_snapshots_and_latest_update(tmp_path):
    history = RunHistory(tmp_path / "session.jsonl", "session")
    history.append(snapshot("first", "running"))
    history.append(snapshot("second", "completed"))
    history.append(snapshot("first", "failed"))
    records = [json.loads(line) for line in history.path.read_text().splitlines()]
    assert all(record["version"] == RUN_HISTORY_VERSION and record["sessionId"] == "session" for record in records)
    assert history.read() == [snapshot("first", "failed"), snapshot("second", "completed")]
    if os.name == "posix":
        assert history.path.stat().st_mode & 0o777 == 0o600


def test_historical_nonterminal_nodes_are_interrupted_without_mutation(tmp_path):
    history = RunHistory(tmp_path / "session.jsonl", "session")
    original = snapshot("run", "pending", "running", "cancelling", "completed", "failed", "cancelled", "timed_out")
    history.append(original)
    expected = snapshot("run", "interrupted", "interrupted", "interrupted", "completed", "failed", "cancelled", "timed_out")
    for node in expected["nodes"][:3]:
        node["reason"] = "runtime_shutdown"
    expected.update(historical=True, externalState="unknown")
    assert history.read() == [expected]
    assert original["nodes"][0]["state"] == "pending"
    assert json.loads(history.path.read_text())["snapshot"] == original


def test_truncated_tail_is_ignored_and_following_append_survives(tmp_path):
    history = RunHistory(tmp_path / "session.jsonl", "session")
    history.append(snapshot("first", "completed"))
    with history.path.open("ab") as stream:
        stream.write(b'{"type":"run_snapshot","snapshot":')
    assert history.read() == [snapshot("first", "completed")]
    history.append(snapshot("second", "running"))
    interrupted = snapshot("second", "interrupted")
    interrupted["nodes"][0]["reason"] = "runtime_shutdown"
    interrupted.update(historical=True, externalState="unknown")
    assert history.read() == [snapshot("first", "completed"), interrupted]


@pytest.mark.parametrize("state", ["running", "interrupted"])
def test_interrupted_reservation_bounds_remain_historical(tmp_path, state):
    history = RunHistory(tmp_path / "session.jsonl", "session")
    record = snapshot("run", state)
    record["nodes"][0]["ownUsage"] = {"reservedTokens": 30, "reservedCostUsd": "2"}
    history.append(record)
    result = history.read()[0]
    assert result["historical"] is True and result["externalState"] == "unknown"
    assert result["nodes"][0]["ownUsage"] == record["nodes"][0]["ownUsage"]


def test_skips_unknown_version_and_rejects_invalid_snapshot(tmp_path):
    history = RunHistory(tmp_path / "session.jsonl", "session")
    with pytest.raises(ValueError, match="Invalid run snapshot"):
        history.append(snapshot("run", "unknown"))
    history.append(snapshot("first", "completed"))
    with history.path.open("a") as stream:
        stream.write(json.dumps({"type": "run_snapshot", "sessionId": "session", "version": 99}) + "\n")
    history.append(snapshot("second", "completed"))
    assert history.read() == [snapshot("first", "completed"), snapshot("second", "completed")]


def test_unknown_historical_reasons_are_normalized(tmp_path):
    history = RunHistory(tmp_path / "session.jsonl", "session")
    record = snapshot("run", "failed", "cancelled")
    record["nodes"][0]["reason"] = {"unexpected": "secret"}
    record["nodes"][1]["reason"] = "user_cancelled"
    history.append(record)
    result = history.read()[0]
    assert result["nodes"][0]["reason"] is None
    assert result["nodes"][1]["reason"] == "user_cancelled"


def test_symlink_sidecar_rejected(tmp_path):
    target = tmp_path / "target"
    target.write_text("unchanged")
    history = RunHistory(tmp_path / "session.jsonl", "session")
    history.path.symlink_to(target)
    with pytest.raises(ValueError, match="sidecar"):
        history.read()
    with pytest.raises(ValueError, match="sidecar"):
        history.append(snapshot("run", "running"))
    assert target.read_text() == "unchanged"


@pytest.mark.asyncio
async def test_session_run_lifecycle_resume_and_fork(tmp_path):
    from decimal import Decimal
    from types import SimpleNamespace

    from one.core.agent_session import AgentSession
    from one.core.run_context import RunLimits
    from one.core.session_manager import SessionManager

    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    session = SimpleNamespace(
        session_manager=manager, session_id=manager.session_id,
        settings_manager=SimpleNamespace(get_run_limits=lambda: RunLimits(max_tokens=100)),
        _run_context=None, _run_node_id=None, _last_run_context=None,
        _active_tool_tasks=set(), _active_chat_tasks=set(), _pending_questions={},
    )
    async def operation():
        context = session._run_context
        context.reserve("request", context.root_id, 10, Decimal("1"))
        context.settle("request", 5, Decimal("0.5"))
        assert manager.run_history().read()[-1]["nodes"][0]["state"] == "interrupted"
        assert context.node(context.root_id).state == "running"
        assert context._attempts["request"].tokens == 5
        return context.run_id

    first = await AgentSession._run_operation(session, operation)
    second = await AgentSession._run_operation(session, operation)
    runs = manager.run_history().read()
    assert [run["runId"] for run in runs] == [first, second]
    assert runs[1]["previousRunId"] == first
    assert runs[1]["aggregates"]["tokens"] == 5
    assert runs[1]["nodes"][0]["state"] == "completed"
    assert "runId" not in manager.get_header()
    reopened = SessionManager.open(manager.session_file, manager.session_dir)
    assert reopened.run_history().read() == runs
    fork = SessionManager.fork_from(manager.session_file, str(tmp_path), manager.session_dir)
    assert fork.get_header()["parentRunId"] == second
    assert fork.run_history().read() == []
    path = manager.session_file
    SessionManager.delete(path, manager.session_dir)
    assert not os.path.exists(path + ".runs")


@pytest.mark.asyncio
async def test_intermediate_snapshot_and_resume_after_crash(tmp_path):
    from decimal import Decimal
    from types import SimpleNamespace

    from one.core.agent_session import AgentSession
    from one.core.run_context import RunLimits
    from one.core.session_manager import SessionManager

    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    def make_session(manager):
        return SimpleNamespace(
            session_manager=manager, session_id=manager.session_id,
            settings_manager=SimpleNamespace(get_run_limits=lambda: RunLimits()),
            _run_context=None, _run_node_id=None, _last_run_context=None,
            _active_tool_tasks=set(), _active_chat_tasks=set(), _pending_questions={},
        )
    session = make_session(manager)
    async def operation():
        context = session._run_context
        child = context.register(context.root_id, "child")
        context.start(child.node_id)
        context.reserve("request", child.node_id, 10, Decimal("1"))
        context.settle("request", 7, Decimal("0.25"))
        context.cancel_subtree(child.node_id, "secret-password-value")
        # Read an independent copy of the journal as it would exist after a crash.
        crash = tmp_path / "crash.jsonl.runs"
        crash.write_bytes(manager.run_history().path.read_bytes())
        recovered = RunHistory(tmp_path / "crash.jsonl", manager.session_id).read()[0]
        assert [n["state"] for n in recovered["nodes"]] == ["interrupted", "interrupted"]
        assert all(n["reason"] == "runtime_shutdown" for n in recovered["nodes"])
        assert recovered["aggregates"]["tokens"] == 7
        assert recovered["aggregates"]["costUsd"] == "0.25"
        assert "secret-password-value" not in crash.read_text()
        assert context.node(child.node_id).reason == "secret-password-value"
        from one.core.run_context import NodeState
        context.finalize(child.node_id, NodeState.CANCELLED)
        return context.run_id

    first = await AgentSession._run_operation(session, operation)
    # Replace the completed journal with the crash image and reopen the session.
    manager.run_history().path.write_bytes((tmp_path / "crash.jsonl.runs").read_bytes())
    reopened = SessionManager.open(manager.session_file, manager.session_dir)
    resumed = make_session(reopened)
    async def next_operation():
        return resumed._run_context.run_id
    second = await AgentSession._run_operation(resumed, next_operation)
    records = reopened.run_history().read()
    assert second != first
    assert records[1]["previousRunId"] == first
    assert records[0]["aggregates"]["tokens"] == 7
    assert records[0]["aggregates"]["costUsd"] == "0.25"
    assert records[1]["aggregates"]["tokens"] == 0
    from one.core.sdk import run_history_page, run_tree_page
    archived = run_history_page(resumed, first)
    current = run_tree_page(resumed)
    assert archived["aggregates"] == records[0]["aggregates"]
    assert archived["nodes"][0]["subtreeUsage"]["tokens"] == 7
    assert current["runId"] == second
    assert current["nodes"][0]["subtreeUsage"]["tokens"] == 0


@pytest.mark.asyncio
async def test_persistence_failure_waits_for_operation_and_preserves_error(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from one.core.agent_session import AgentSession
    from one.core.run_context import RunLimits
    from one.core.session_manager import SessionManager

    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    session = SimpleNamespace(
        session_manager=manager, session_id=manager.session_id,
        settings_manager=SimpleNamespace(get_run_limits=lambda: RunLimits()),
        _run_context=None, _run_node_id=None, _last_run_context=None,
        _active_tool_tasks=set(), _active_chat_tasks=set(), _pending_questions={},
    )
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(RunHistory, "append", fail)
    finished = False
    async def operation():
        nonlocal finished
        finished = True
    with pytest.raises(OSError, match="disk full"):
        await AgentSession._run_operation(session, operation)
    assert finished and session._run_context is None
    async def failed_operation():
        raise RuntimeError("operation failed")
    with pytest.raises(RuntimeError, match="operation failed") as exc:
        await AgentSession._run_operation(session, failed_operation)
    assert "Run history persistence failed" in exc.value.__notes__[0]
    assert session._run_context is None


def test_malformed_records_are_skipped_without_losing_valid_snapshots(tmp_path):
    history = RunHistory(tmp_path / "session.jsonl", "session")
    history.append(snapshot("first", "completed"))
    with history.path.open("ab") as stream:
        stream.write(b'not json\n[]\n{"type":"run_snapshot","sessionId":"session","version":1,"snapshot":{}}\n')
    history.append(snapshot("second", "completed"))
    assert history.read() == [snapshot("first", "completed"), snapshot("second", "completed")]


@pytest.mark.asyncio
async def test_restored_accounting_pages_through_sdk_host_and_facade(tmp_path):
    from decimal import Decimal
    from types import SimpleNamespace

    from one.core.agent_runtime_facade import AgentRuntimeFacade
    from one.core.run_context import RunContext, RunLimits
    from one.core.sdk import run_history_page
    from one.core.session_manager import SessionManager
    from one.core.threaded_agent_runtime import RuntimeBootstrapSpec
    from one.core.tui_runtime_host import TUI_COMMANDS, TuiRuntimeHost

    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    manager.ensure_run_session()
    context = RunContext(manager.session_id, RunLimits(max_tokens=100))
    child = context.register(context.root_id, "child")
    context.reserve("request", child.node_id, 20, Decimal("2"))
    before = context.tree_snapshot()
    manager.run_history().append(context.history_snapshot())
    reopened = SessionManager.open(manager.session_file, manager.session_dir)
    session = SimpleNamespace(session_manager=reopened, _run_context=None, _last_run_context=None)
    first = run_history_page(session, context.run_id, limit=1)
    assert first["limits"] == before["limits"]
    assert first["nodes"][0]["subtreeUsage"] == before["nodes"][0]["subtreeUsage"]
    assert first["nodes"][0]["subtreeUsage"]["reservedTokens"] == 20
    assert first["nextOffset"] == 1
    assert run_history_page(session, context.run_id, 1, 1)["nodes"][0]["ownUsage"] == before["nodes"][1]["ownUsage"]
    assert run_history_page(session, "missing")["available"] is False
    with pytest.raises(ValueError):
        run_history_page(session, context.run_id, limit=True)
    assert context.tree_snapshot() == before

    def factory(_spec):
        host = object.__new__(TuiRuntimeHost)
        host._host = SimpleNamespace(session=session)
        host._epoch = 3
        return SimpleNamespace(dispatch=host.dispatch)

    facade = AgentRuntimeFacade(RuntimeBootstrapSpec(cwd=str(tmp_path), agent_dir=str(tmp_path)), factory,
                                command_allowlist=TUI_COMMANDS)
    await facade.start()
    try:
        reply = await facade.run_history_page(3, context.run_id, 1, 1)
        assert reply["result"]["nodes"][0]["ownUsage"]["reservedTokens"] == 20
        with pytest.raises(ValueError, match="Session changed"):
            await facade.run_history_page(2, context.run_id)
    finally:
        await facade.close()
        assert facade.finalize()
    assert context.tree_snapshot() == before
