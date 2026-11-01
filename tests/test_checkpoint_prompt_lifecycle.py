# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.providers.base import ChatResult
from tests.support.agents import _Loader


class Checkpoints:
    def __init__(self):
        self.calls = []
        self.active = None

    def begin_task(self, task_id):
        assert self.active is None
        self.active = task_id
        self.calls.append(('begin', task_id))

    def end_task(self):
        assert self.active is not None
        self.calls.append(('end', self.active))
        self.active = None


class Provider:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def chat(self, **kwargs):
        self.started.set()
        await self.release.wait()
        return ChatResult(text='OK', raw={}, usage={}, stop_reason='stop')


def make_agent(path: Path, manager=None):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key('openai', 'dummy')
    registry = ModelRegistry.create(auth)
    agent = AgentSession(SessionManager.in_memory(str(path)), SettingsManager.in_memory(), registry,
                         _Loader(), registry.find('openai', 'gpt-4.1'), 'medium',
                         checkpoint_manager=manager)
    provider = Provider()
    agent.providers = {'openai': provider}
    return agent, provider


@pytest.mark.asyncio
async def test_follow_up_gets_separate_checkpoint_after_first_end(tmp_path):
    manager = Checkpoints()
    agent, provider = make_agent(tmp_path, manager)
    task = asyncio.create_task(agent.prompt('first'))
    await provider.started.wait()
    await agent.prompt('second', {'streamingBehavior': 'followUp'})
    assert [call[0] for call in manager.calls] == ['begin']
    provider.release.set()
    await task
    assert [call[0] for call in manager.calls] == ['begin', 'end', 'begin', 'end']
    first, second = manager.calls[0][1], manager.calls[2][1]
    assert uuid.UUID(first).version == uuid.UUID(second).version == 4
    assert first != second
    assert manager.calls[1][1] == first and manager.calls[3][1] == second


@pytest.mark.asyncio
async def test_cancel_closes_checkpoint(tmp_path):
    manager = Checkpoints()
    agent, provider = make_agent(tmp_path, manager)
    task = asyncio.create_task(agent.prompt('first'))
    await provider.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [call[0] for call in manager.calls] == ['begin', 'end']


@pytest.mark.asyncio
async def test_setup_failure_closes_checkpoint(tmp_path, monkeypatch):
    manager = Checkpoints()
    agent, _ = make_agent(tmp_path, manager)
    def fail(_text):
        raise RuntimeError('setup failure')
    monkeypatch.setattr(agent.session_manager, 'set_automatic_name_from_prompt', fail)
    with pytest.raises(RuntimeError, match='setup failure'):
        await agent.prompt('first')
    assert [call[0] for call in manager.calls] == ['begin', 'end']


@pytest.mark.asyncio
async def test_checkpoint_does_not_change_event_sequence(tmp_path):
    sequences = []
    for manager in (None, Checkpoints()):
        agent, provider = make_agent(tmp_path, manager)
        events = []
        agent.subscribe(lambda event: events.append(event['type']))
        provider.release.set()
        await agent.prompt('hello')
        sequences.append(events)
    assert sequences[0] == sequences[1]
