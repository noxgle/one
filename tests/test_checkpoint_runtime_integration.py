# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from one.core.checkpoints import CheckpointError
from one.core.session_manager import SessionManager
from one.core.tool_policy import ToolPolicy
from one.core.tui_runtime_host import TUI_COMMANDS, TuiRuntimeHost
from tests.test_checkpoint_prompt_lifecycle import make_agent


@pytest.fixture
def runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = tmp_path / "sessions"
    session = SessionManager.create(str(workspace), str(sessions))
    session.append_message({"role": "user", "content": "hello"})
    agent, _ = make_agent(workspace)
    agent.session_manager = session
    host = TuiRuntimeHost(SimpleNamespace(session=agent), None)
    yield host, agent, workspace, session
    manager = getattr(agent, "_checkpoint_manager", None)
    if manager is not None:
        manager.close()


def call(host, action, **kwargs):
    return host.checkpoints({"action": action, "epoch": host._epoch, **kwargs})


def make_checkpoint(host, workspace):
    assert call(host, "enable")["enabled"]
    manager = host._session()._get_checkpoint_manager()
    (workspace / "old").write_text("before")
    manager.begin_task("task")
    manager.capture_preimage(["old", "new"])
    (workspace / "old").write_text("after")
    (workspace / "new").write_text("created")
    manager.record_postimage()
    manager.end_task()
    return manager


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_selection_confirm_and_one_use(runtime):
    host, _, workspace, _ = runtime
    assert call(host, "list") == {"epoch": 0, "enabled": False, "checkpoints": []}
    make_checkpoint(host, workspace)
    rows = call(host, "list")["checkpoints"]
    assert rows == [{"id": "task", "status": "complete", "paths": ["old", "new"]}]
    prepared = call(host, "prepare", id="task", paths=["new"])
    assert prepared["paths"] == [{"path": "new", "action": "delete"}]
    assert call(host, "confirm", token=prepared["token"])["restored"] == ["new"]
    assert not (workspace / "new").exists()
    assert (workspace / "old").read_text() == "after"
    with pytest.raises((ValueError, CheckpointError)):
        call(host, "confirm", token=prepared["token"])
    prepared = call(host, "prepare", id="task")
    assert prepared["paths"] == [{"path": "old", "action": "restore"}]
    call(host, "confirm", token=prepared["token"])
    assert (workspace / "old").read_text() == "before"


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_cancel_conflict_expiry_and_new_task(runtime):
    host, _, workspace, _ = runtime
    manager = make_checkpoint(host, workspace)
    token = call(host, "prepare", id="task")["token"]
    call(host, "cancel")
    with pytest.raises((ValueError, CheckpointError)):
        call(host, "confirm", token=token)
    token = call(host, "prepare", id="task")["token"]
    (workspace / "old").write_text("external")
    with pytest.raises(CheckpointError, match="Conflict"):
        call(host, "confirm", token=token)
    assert (workspace / "old").read_text() == "external"
    (workspace / "old").write_text("after")
    token = call(host, "prepare", id="task")["token"]
    task, paths, _ = manager.tokens[token]
    manager.tokens[token] = (task, paths, 0.0)
    with pytest.raises(CheckpointError, match="expired"):
        call(host, "confirm", token=token)
    token = call(host, "prepare", id="task")["token"]
    manager.begin_task("next")
    manager.end_task()
    with pytest.raises((ValueError, CheckpointError)):
        call(host, "confirm", token=token)
    assert (workspace / "old").read_text() == "after"


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_epoch_idle_readonly_and_validation(runtime):
    host, agent, workspace, _ = runtime
    manager = make_checkpoint(host, workspace)
    token = call(host, "prepare", id="task")["token"]
    with pytest.raises(ValueError, match="epoch"):
        host.checkpoints({"action": "confirm", "epoch": 99, "token": token})
    host._epoch += 1
    with pytest.raises(ValueError, match="stale"):
        call(host, "confirm", token=token)
    agent._is_streaming = True
    with pytest.raises(ValueError, match="idle"):
        call(host, "list")
    agent._is_streaming = False
    token = call(host, "prepare", id="task")["token"]
    agent._tool_policy = ToolPolicy.readonly()
    with pytest.raises(ValueError, match="file-write"):
        call(host, "confirm", token=token)
    assert not manager.tokens
    assert (workspace / "old").read_text() == "after"
    with pytest.raises(ValueError, match="request"):
        call(host, "list", unexpected=True)


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_session_sidecar_resume_fork_and_delete(runtime):
    host, agent, workspace, session = runtime
    manager = make_checkpoint(host, workspace)
    sidecar = session.checkpoint_path()
    assert sidecar is not None and sidecar.is_dir()
    session_file = session.session_file
    assert session_file is not None
    manager.close()
    agent._checkpoint_manager = None
    resumed = SessionManager.open(session_file)
    agent.session_manager = resumed
    assert call(host, "list")["checkpoints"][0]["id"] == "task"
    agent._checkpoint_manager.close()
    agent._checkpoint_manager = None
    fork = SessionManager.fork_from(session_file, str(workspace), str(sidecar.parent))
    assert fork.checkpoint_path() != sidecar
    assert not fork.checkpoint_path().exists()
    agent.session_manager = fork
    assert call(host, "list")["enabled"] is False
    fork_manager = agent.enable_checkpoints()
    fork_manager.begin_task("fork-task")
    fork_manager.end_task()
    fork_manager.close()
    agent._checkpoint_manager = None
    SessionManager.delete(session_file, str(sidecar.parent))
    assert not sidecar.exists()
    assert fork.checkpoint_path().is_dir()
    reopened = fork.create_checkpoint_manager()
    try:
        assert reopened.list_checkpoints()[0]["id"] == "fork-task"
    finally:
        reopened.close()
    SessionManager.delete(fork.session_file, str(sidecar.parent))
    assert not fork.checkpoint_path().exists()


def test_checkpoint_command_is_allowed():
    assert "checkpoints" in TUI_COMMANDS


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
async def test_checkpoint_facade_roundtrip_runs_on_owner_loop(tmp_path):
    import threading

    from one.core.agent_runtime_facade import AgentRuntimeFacade
    from one.core.threaded_agent_runtime import RuntimeBootstrapSpec

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    owner_threads = []

    async def factory(_spec):
        session = SessionManager.create(str(workspace), str(tmp_path / "sessions"))
        session.append_message({"role": "user", "content": "hello"})
        agent, _ = make_agent(workspace)
        agent.session_manager = session
        host = TuiRuntimeHost(SimpleNamespace(session=agent), None)
        original = host.checkpoints

        def checked(payload):
            owner_threads.append(threading.get_ident())
            return original(payload)

        host.checkpoints = checked
        make_checkpoint(host, workspace)
        # The transport closes its runtime through dispose().
        async def dispose():
            agent._checkpoint_manager.close()
        host.dispose = dispose
        return host

    facade = AgentRuntimeFacade(
        RuntimeBootstrapSpec(cwd=str(workspace), agent_dir=str(tmp_path)), factory,
        command_allowlist=TUI_COMMANDS,
    )
    try:
        await facade.start()
        listed = (await facade.list_checkpoints(0))["result"]
        assert listed["checkpoints"][0]["id"] == "task"
        assert "before" not in str(listed)
        prepared = (await facade.prepare_checkpoint_undo("task", 0, ["old"]))["result"]
        assert (workspace / "old").read_text() == "after"
        confirmed = (await facade.confirm_checkpoint_undo(prepared["token"], 0))["result"]
        assert confirmed["restored"] == ["old"]
        assert (workspace / "old").read_text() == "before"
        assert owner_threads and all(t != threading.get_ident() for t in owner_threads)
    finally:
        await facade.close()
