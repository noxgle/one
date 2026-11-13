# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import os

import pytest

from tests.test_checkpoint_prompt_lifecycle import make_agent


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_xattr", [False, True], ids=["normal", "missing-xattr"])
async def test_checkpoint_patch_requires_active_task(tmp_path, monkeypatch, missing_xattr):
    from one.core.checkpoints import CheckpointError, CheckpointManager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "file"
    path.write_text("before")
    new_path = workspace / "new"
    if missing_xattr:
        monkeypatch.delattr(os, "listxattr", raising=False)
    manager = CheckpointManager(tmp_path / "storage", workspace, "session")
    try:
        agent, _ = make_agent(workspace, manager)
        with pytest.raises(CheckpointError, match="No active task"):
            await agent._execute_tool_by_name("apply_patch", {
                "patchText": "*** Begin Patch\n*** Add File: new\n+content\n*** End Patch"})
        assert path.read_text() == "before"
        assert not new_path.exists()
    finally:
        manager.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
async def test_checkpoint_write_records_and_undoes(tmp_path):
    from one.core.checkpoints import CheckpointManager

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    path = workspace / 'file'
    path.write_text('before')
    manager = CheckpointManager(tmp_path / 'storage', workspace, 'session')
    try:
        agent, _ = make_agent(workspace, manager)
        manager.begin_task('task')
        await agent._execute_tool_by_name('write', {'path': 'file', 'content': 'changed'})
        manager.end_task()
        assert path.read_text() == 'changed'
        manager.confirm_undo(manager.prepare_undo('task', ['file'])['token'])
        assert path.read_text() == 'before'
    finally:
        manager.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
async def test_checkpoint_edit_preserves_original_for_undo(tmp_path):
    from one.core.checkpoints import CheckpointManager

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    path = workspace / 'file'
    path.write_text('original text')
    manager = CheckpointManager(tmp_path / 'storage', workspace, 'session')
    try:
        agent, _ = make_agent(workspace, manager)
        manager.begin_task('task')
        result = await agent._execute_tool_by_name('edit', {'path': 'file', 'edits': [{'oldString': 'original', 'newString': 'changed'}]})
        assert '-original text' in result['details']['diff']
        manager.end_task()
        manager.confirm_undo(manager.prepare_undo('task', ['file'])['token'])
        assert path.read_text() == 'original text'
    finally:
        manager.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
async def test_checkpoint_edit_rejects_intervening_external_write(tmp_path, monkeypatch):
    from one.core.checkpoints import CheckpointError, CheckpointManager

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    path = workspace / 'file'
    path.write_text('before')
    manager = CheckpointManager(tmp_path / 'storage', workspace, 'session')
    try:
        agent, _ = make_agent(workspace, manager)
        manager.begin_task('task')
        original = manager.capture_preimage

        def capture(paths, moves=None):
            path.write_text('external')
            original(paths, moves)

        monkeypatch.setattr(manager, 'capture_preimage', capture)
        with pytest.raises(CheckpointError, match='Intervening change'):
            await agent._execute_tool_by_name('edit', {
                'path': 'file', 'edits': [{'oldString': 'before', 'newString': 'changed'}],
            })
        assert path.read_text() == 'external'
        manager.end_task()
        assert manager.list_checkpoints()[0]['status'] == 'incomplete'
    finally:
        manager.close()
