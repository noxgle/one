"""Event-gated, offline cancellation through the real AgentSession runtime."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.run_context import NodeState
from one.providers.base import ChatResult
from tests.test_run_integration import FINISH, agent_with_script, assert_joined, tool


async def reap(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_abort_cleanup_timeout_blocks_readmission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from one.core.agent_session import RunCleanupTimeoutError

    monkeypatch.setattr("one.core.agent_session._RUN_CLEANUP_TIMEOUT_SEC", 0.02)
    agent, _ = agent_with_script(tmp_path, [])
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stubborn():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    task = asyncio.create_task(agent._run_operation(stubborn))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        context = agent._run_context
        await agent.abort()
        with pytest.raises(RunCleanupTimeoutError, match="external state is unknown"):
            await asyncio.wait_for(task, 1)
        assert cleaning.is_set()
        node = context.node(context.root_id)
        assert node.state == NodeState.CANCELLING and node.cleanup_timed_out
        with pytest.raises(RunCleanupTimeoutError, match="cannot safely admit"):
            await agent.prompt("must not start")
    finally:
        release.set()
        await reap(task)
        if agent._run_context is not None:
            operation = agent._run_context.task(agent._run_node_id)
            if operation is not None:
                await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.asyncio
async def test_direct_subagent_timeouts_release_owners_between_runs(tmp_path: Path) -> None:
    agent, provider = agent_with_script(tmp_path, [])

    async def blocked(max_tokens: int | None = None, **kwargs: Any):
        await asyncio.Event().wait()

    provider.chat = blocked
    for _ in range(2):
        result = await asyncio.wait_for(agent._spawn_subagent({"task": "blocked"}, timeout_sec=1), 4)
        assert result["timedOut"] is True
        context = agent._last_run_context
        assert not context._owners
        assert_joined(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["deadline", "abort", "cancel"])
async def test_global_stop_joins_tree_and_does_not_drain_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    agent, provider = agent_with_script(tmp_path, [], runBudget={"maxTimeSec": 1 if mode == "deadline" else 0})
    children: list[AgentSession] = []
    original = AgentSession.__init__
    def capture(self, *args, **kwargs):
        original(self, *args, **kwargs)
        children.append(self)
    monkeypatch.setattr(AgentSession, "__init__", capture)
    entered = asyncio.Event()
    stopped = asyncio.Event()
    calls = 0
    async def chat(max_tokens: int | None = None, **kwargs: Any):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ChatResult(text=tool("spawn_subagent", task="blocked child"), raw={}, usage={}, stop_reason="stop")
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    provider.chat = chat
    task = asyncio.create_task(agent.prompt("delegate"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        context = agent._run_context
        await agent.prompt("must not execute", {"streamingBehavior": "followUp"})
        if mode == "abort":
            await agent.abort()
        elif mode == "cancel":
            task.cancel()
        results = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        if mode == "abort":
            assert results == [None]
        elif mode == "cancel":
            assert isinstance(results[0], asyncio.CancelledError)
        assert stopped.is_set()
        assert calls == 2
        assert context.node(context.root_id).state == NodeState.CANCELLED
        assert context.node(context.root_id).reason == {"deadline": "deadline", "abort": "abort",
                                                       "cancel": "cancelled"}[mode]
        assert agent.get_pending_queues()["followUp"] == ["must not execute"]
        assert not agent._active_chat_tasks and not agent._active_tool_tasks
        assert all(not child._active_chat_tasks and not child._active_tool_tasks for child in children)
        assert_joined(context)
    finally:
        await reap(task)


@pytest.mark.asyncio
async def test_cancel_subtree_stops_grandchild_but_sibling_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, provider = agent_with_script(tmp_path, [], subagents={"maxDepth": 3},
                                        runBudget={"maxActiveSubagents": 3})
    children: list[AgentSession] = []
    original = AgentSession.__init__
    def capture(self, *args, **kwargs):
        original(self, *args, **kwargs)
        children.append(self)
    monkeypatch.setattr(AgentSession, "__init__", capture)
    grandchild_ready, sibling_ready = asyncio.Event(), asyncio.Event()
    grandchild_stopped, sibling_release = asyncio.Event(), asyncio.Event()
    sibling_cancelled = False
    seen: set[str] = set()
    async def chat(max_tokens: int | None = None, **kwargs: Any):
        nonlocal sibling_cancelled
        # First user message uniquely identifies each node, even after tools.
        user = next(m["content"] for m in kwargs["messages"] if m["role"] == "user")
        if user not in seen:
            seen.add(user)
            if user == "root":
                text = tool("spawn_subagent", tasks=["branch", "sibling"])
            elif user == "branch":
                text = tool("spawn_subagent", task="grandchild")
            elif user == "grandchild":
                grandchild_ready.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    grandchild_stopped.set()
            else:
                assert user == "sibling"
                sibling_ready.set()
                try:
                    await sibling_release.wait()
                except asyncio.CancelledError:
                    sibling_cancelled = True
                    raise
                text = FINISH
        else:
            text = FINISH
        return ChatResult(text=text, raw={}, usage={}, stop_reason="stop")
    provider.chat = chat
    task = asyncio.create_task(agent.prompt("root"))
    try:
        await asyncio.wait_for(asyncio.gather(grandchild_ready.wait(), sibling_ready.wait()), 2)
        context = agent._run_context
        branch = next(child for child in children if child._subagent_depth() == 1
                      and any(m.get("content") == "branch" for m in child.messages if m.get("role") == "user"))
        branch_id = branch._run_node_id
        await branch.abort()
        await asyncio.wait_for(grandchild_stopped.wait(), 2)
        assert not sibling_cancelled
        sibling_release.set()
        await asyncio.wait_for(task, 3)
        assert context.node(branch_id).state == NodeState.CANCELLED
        descendants = [n for n in context._nodes.values() if n.parent_node_id == branch_id]
        assert len(descendants) == 1 and descendants[0].state == NodeState.CANCELLED
        sibling = next(n for n in context._nodes.values() if n.depth == 1 and n.node_id != branch_id)
        assert sibling.state == NodeState.COMPLETED
        assert not sibling_cancelled
        assert_joined(context)
    finally:
        sibling_release.set()
        await reap(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["answer", "abort", "cancel", "deadline"])
async def test_pending_question_cleanup(tmp_path: Path, mode: str) -> None:
    agent, provider = agent_with_script(tmp_path, [tool("ask_user", question="Which option?"), FINISH],
        cooperation={"enabled": True}, runBudget={"maxTimeSec": 1 if mode == "deadline" else 0})
    async def approve(name, args):
        return True, ""
    agent.approval_callback = approve
    ready = asyncio.Event()
    questions: list[str] = []
    def event_listener(event):
        if event["type"] == "ask_user":
            questions.append(event["id"])
            ready.set()
    agent.subscribe(event_listener)
    task = asyncio.create_task(agent.prompt("ask"))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        context = agent._run_context
        assert len(agent.get_pending_questions()) == 1
        if mode == "answer":
            agent.answer_question(questions[0], "option A")
        elif mode == "abort":
            await agent.abort()
        elif mode == "cancel":
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        assert not agent.get_pending_questions()
        with pytest.raises(ValueError, match="No pending question"):
            agent.answer_question(questions[0], "late answer")
        assert context.node(context.root_id).state == (NodeState.COMPLETED if mode == "answer" else NodeState.CANCELLED)
        assert not agent._active_tool_tasks and not agent._active_chat_tasks
        assert_joined(context)
    finally:
        await reap(task)
