# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import pytest

from one.core.agent_session import BusySessionError
from one.core.checkpoints import CheckpointError, CheckpointManager
from one.core.settings_manager import SettingsManager
from tests.test_checkpoint_prompt_lifecycle import make_agent


@pytest.mark.asyncio
async def test_patch_result_and_undo(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'file').write_text('before\n')
    manager = CheckpointManager(tmp_path / 'storage', workspace, 'session')
    try:
        agent, _ = make_agent(workspace, manager)
        patch = '*** Begin Patch\n*** Update File: file\n@@\n-before\n+after\n*** End Patch'
        with pytest.raises(CheckpointError, match='No active task'):
            await agent._execute_tool_by_name('apply_patch', {'patchText': patch})
        manager.begin_task('task')
        result = await agent._execute_tool_by_name('apply_patch', {'patchText': patch})
        assert result['content'][0]['text'] == 'Success. Updated the following files:\nM file'
        assert '-before\n+after\n' in result['details']['diff']
        manager.end_task()
        manager.confirm_undo(manager.prepare_undo('task', ['file'])['token'])
        assert (workspace / 'file').read_text() == 'before\n'
    finally:
        manager.close()


def test_lazy_enable_and_rebinding(tmp_path):
    agent, _ = make_agent(tmp_path)
    agent.settings_manager = SettingsManager.in_memory({'checkpoints': {'enabled': True}})
    class Session:
        def __init__(self):
            self.created = []
        def create_checkpoint_manager(self):
            manager = Manager()
            self.created.append(manager)
            return manager
    class Manager:
        def close(self):
            self.closed = True
    first, second = Session(), Session()
    agent.session_manager = first
    manager = agent._get_checkpoint_manager()
    assert agent._get_checkpoint_manager() is manager
    agent.session_manager = second
    assert agent._get_checkpoint_manager() is not manager
    assert manager.closed
    agent._is_streaming = True
    with pytest.raises(BusySessionError):
        agent.enable_checkpoints()
