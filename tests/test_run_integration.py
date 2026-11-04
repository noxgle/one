"""Offline integration contracts for the live AgentSession execution tree."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.run_context import NodeState
from one.core.run_pricing import PriceSnapshot
from one.core.settings_manager import SettingsManager
from one.providers.base import ChatResult
from tests.test_run_budget_runtime import make_agent


def tool(name: str, **args: Any) -> str:
    return json.dumps({"tool": name, "args": args})


FINISH = tool("finish", summary="done", goal_success=True)


class ScriptedProvider:
    # Exercise the real OpenAI usage normalizer, not an unknown fake adapter.
    __module__ = "one.providers.openai_compatible"

    def __init__(self, agent: AgentSession, responses: list[str | Exception]) -> None:
        self.agent = agent
        self.responses = iter(responses)
        self.contexts: list[Any] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.cancelled = False

    async def chat(self, max_tokens: int | None = None, **kwargs: Any) -> ChatResult:
        self.contexts.append(self.agent._run_context)
        self.entered.set()
        if self.block:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return ChatResult(text=response, raw={}, usage={"prompt_tokens": 2, "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 0}}, stop_reason="stop")


def agent_with_script(tmp_path: Path, responses: list[str | Exception], **settings: Any):
    config = {"compaction": {"enabled": False}, "retry": {"enabled": False},
              "tools": {"maxSteps": 8, "timeoutSec": 5}, "generation": {"maxOutputTokens": 16}}
    config.update(settings)
    agent, _ = make_agent(tmp_path, SettingsManager.in_memory(config))
    agent.model.pricing = PriceSnapshot("openai", agent.model.id, "offline-test", Decimal("1"), Decimal("2"),
                                        cache_mode="none")
    for model in agent.model_registry._models:
        if (model.provider, model.id) == (agent.model.provider, agent.model.id):
            model.pricing = agent.model.pricing
    provider = ScriptedProvider(agent, responses)
    agent.providers = {"openai": provider}
    return agent, provider


def assert_joined(context: Any) -> None:
    assert all(node.state in {NodeState.COMPLETED, NodeState.CANCELLED, NodeState.FAILED}
               for node in context._nodes.values())
    assert not context._tasks
    assert context._deadline_timer is None
    assert all(attempt.state != "reserved" for attempt in context._attempts.values())


@pytest.mark.asyncio
async def test_root_child_grandchild_share_pool_and_identity(tmp_path: Path) -> None:
    agent, provider = agent_with_script(tmp_path, [tool("spawn_subagent", task="child"),
        tool("spawn_subagent", task="grandchild"), FINISH, FINISH, FINISH],
        runBudget={"maxTokens": 10_000_000, "maxActiveSubagents": 2}, subagents={"maxDepth": 3})
    await agent.prompt("delegate twice")
    context = agent._last_run_context
    assert len(provider.contexts) == 5
    assert all(item is context for item in provider.contexts)
    nodes = sorted(context._nodes.values(), key=lambda n: n.depth)
    assert [n.depth for n in nodes] == [0, 1, 2]
    assert nodes[1].parent_node_id == nodes[0].node_id
    assert nodes[2].parent_node_id == nodes[1].node_id
    assert {a.node_id for a in context._attempts.values()} == {n.node_id for n in nodes}
    assert sum(a.tokens for a in context._attempts.values()) == 15
    assert_joined(context)


@pytest.mark.asyncio
async def test_parallel_three_level_tree_accounting(tmp_path: Path) -> None:
    agent, provider = agent_with_script(tmp_path, [], subagents={"maxDepth": 3},
                                        runBudget={"maxActiveSubagents": 4})
    ready, release = asyncio.Event(), asyncio.Event()
    seen: set[str] = set()
    leaves: set[str] = set()

    async def chat(max_tokens: int | None = None, **kwargs: Any) -> ChatResult:
        user = next(m["content"] for m in kwargs["messages"] if m["role"] == "user")
        if user not in seen:
            seen.add(user)
            if user == "root":
                text = tool("spawn_subagent", tasks=["left", "right"])
            elif user in {"left", "right"}:
                text = tool("spawn_subagent", task=f"{user}-leaf")
            else:
                leaves.add(user)
                if len(leaves) == 2:
                    ready.set()
                await release.wait()
                text = FINISH
        else:
            text = FINISH
        return ChatResult(text=text, raw={}, usage={"prompt_tokens": 2, "completion_tokens": 1,
                          "prompt_tokens_details": {"cached_tokens": 0}}, stop_reason="stop")

    provider.chat = chat
    task = asyncio.create_task(agent.prompt("root"))
    try:
        await asyncio.wait_for(ready.wait(), 3)
        context = agent._run_context
        assert {n.depth for n in context._nodes.values()} == {0, 1, 2}
        assert len(context._nodes) == 5
        release.set()
        await asyncio.wait_for(task, 3)
        tree = context.tree_snapshot()
        root = next(n for n in tree["nodes"] if n["depth"] == 0)
        total = sum(a.tokens for a in context._attempts.values())
        assert len(context._attempts) == 8
        assert total == 24
        assert sum(n["ownUsage"]["tokens"] for n in tree["nodes"]) == total
        assert root["subtreeUsage"]["tokens"] == total
        assert context.history_snapshot()["aggregates"]["tokens"] == total
        assert_joined(context)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_followup_drain_same_run_next_prompt_and_manual_compaction_new_runs(tmp_path: Path) -> None:
    agent, provider = agent_with_script(tmp_path, ["first" * 100, "queued" * 100, "second" * 100, "summary"])
    provider.block = True
    task = asyncio.create_task(agent.prompt("start"))
    try:
        await asyncio.wait_for(provider.entered.wait(), 2)
        await agent.prompt("queued", {"streamingBehavior": "followUp"})
        provider.release.set()
        await asyncio.wait_for(task, 3)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    first = agent._last_run_context
    assert provider.contexts == [first, first]
    assert agent.get_pending_queues() == {"steering": [], "followUp": []}
    await agent.prompt("independent")
    second = agent._last_run_context
    result = await agent.compact()
    third = agent._last_run_context
    assert result["summary"] == "summary"
    assert len({c.run_id for c in (first, second, third)}) == 3
    assert provider.contexts == [first, first, second, third]
    for context in (first, second, third):
        assert_joined(context)


@pytest.mark.asyncio
async def test_retry_and_in_prompt_compaction_keep_run(tmp_path: Path) -> None:
    agent, provider = agent_with_script(tmp_path, [RuntimeError("503 service unavailable"), "ok" * 200, "summary"],
        retry={"enabled": True, "maxRetries": 1, "baseDelayMs": 1, "maxDelayMs": 1})
    await agent.prompt("retry")
    context = agent._last_run_context
    assert provider.contexts == [context, context]
    assert len(context._attempts) == 2
    assert any(a.state == "uncertain" for a in context._attempts.values())
    async def operation():
        await agent.compact()
        assert agent._run_context is not context
        current = agent._run_context
        await agent._invoke_provider([])
        return current
    provider.responses = iter(["summary", "answer"])
    current = await agent._run_operation(operation)
    assert provider.contexts[-2:] == [current, current]
    assert_joined(current)


@pytest.mark.asyncio
async def test_batch_admission_is_atomic_and_capacity_recovers(tmp_path: Path) -> None:
    agent, provider = agent_with_script(tmp_path, [FINISH], runBudget={"maxActiveSubagents": 1})
    async def operation():
        context = agent._run_context
        with pytest.raises(RuntimeError, match="active subagent limit"):
            await agent._spawn_subagent({"tasks": ["one", "two"]})
        assert len(context._nodes) == 1
        result = await agent._spawn_subagent({"task": "allowed"})
        assert result["ok"]
        return context
    context = await agent._run_operation(operation)
    assert len(provider.contexts) == 1
    assert_joined(context)


@pytest.mark.asyncio
async def test_startup_failure_rolls_back_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, provider = agent_with_script(tmp_path, [], runBudget={"maxActiveSubagents": 2})
    original = AgentSession.__init__
    def fail_child(self, *args, **kwargs):
        raise RuntimeError("injected child startup failure")
    async def operation():
        monkeypatch.setattr(AgentSession, "__init__", fail_child)
        with pytest.raises(RuntimeError, match="injected child startup failure"):
            await agent._spawn_subagent({"tasks": ["one", "two"]})
        monkeypatch.setattr(AgentSession, "__init__", original)
        context = agent._run_context
        assert len(context._nodes) == 3
        assert all(n.state == NodeState.CANCELLED for n in context._nodes.values() if n.depth)
        assert not context._tasks.keys() - {context.root_id}
        return context
    context = await agent._run_operation(operation)
    assert not provider.contexts
    assert_joined(context)


@pytest.mark.asyncio
async def test_limits_are_frozen_for_children_and_next_run_adopts_edits(tmp_path: Path) -> None:
    agent, provider = agent_with_script(tmp_path, [FINISH, FINISH], runBudget={"maxActiveSubagents": 1})
    async def operation():
        context = agent._run_context
        agent.settings_manager._global["runBudget"] = {"maxActiveSubagents": 2}
        with pytest.raises(RuntimeError, match="active subagent limit"):
            await agent._spawn_subagent({"tasks": ["a", "b"]})
        await agent._spawn_subagent({"task": "child"})
        assert context.limits.max_active_subagents == 1
        return context
    first = await agent._run_operation(operation)
    await agent.prompt("new")
    assert agent._last_run_context.limits.max_active_subagents == 2
    assert provider.contexts[0] is first
    assert_joined(first)


@pytest.mark.asyncio
async def test_nested_active_limit_is_global_not_per_parent(tmp_path: Path) -> None:
    agent, provider = agent_with_script(tmp_path, [tool("spawn_subagent", task="child"),
        tool("spawn_subagent", task="denied grandchild"), FINISH, FINISH],
        runBudget={"maxActiveSubagents": 1}, subagents={"maxDepth": 3})
    await agent.prompt("delegate")
    context = agent._last_run_context
    assert len(provider.contexts) == 4
    assert sorted(n.depth for n in context._nodes.values()) == [0, 1]
    assert len({a.node_id for a in context._attempts.values()}) == 2
    assert_joined(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,limit", [("maxTokens", 1), ("maxCostUsd", 0.000001)])
async def test_insufficient_shared_pool_rejects_before_paid_request(tmp_path: Path, field: str, limit: int | float) -> None:
    agent, provider = agent_with_script(tmp_path, [FINISH], runBudget={field: limit})
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    with pytest.raises(asyncio.CancelledError):
        await agent.prompt("too small")
    context = agent._last_run_context
    assert not provider.contexts
    assert not context._attempts
    assert not any(e["type"] == "provider_request_start" for e in events)
    assert any(e["type"] == "turn_end" and e.get("willRetry") is False and not e.get("ok") for e in events)
    assert_joined(context)


@pytest.mark.asyncio
async def test_auto_compaction_and_queue_share_original_run(tmp_path: Path) -> None:
    agent, provider = agent_with_script(tmp_path, ["answer" * 100, "summary", FINISH],
        compaction={"enabled": True, "thresholdPercent": 0, "recentTokens": 1, "minKeptMessages": 1})
    # Preflight sees only the new user message, so there is nothing to drop.
    provider.block = True
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    task = asyncio.create_task(agent.prompt("original"))
    try:
        await asyncio.wait_for(provider.entered.wait(), 2)
        await agent.prompt("queued", {"streamingBehavior": "followUp"})
        provider.release.set()
        await asyncio.wait_for(task, 3)
        context = agent._last_run_context
        assert len(provider.contexts) >= 3
        assert all(c is context for c in provider.contexts)
        assert any(e["type"] == "provider_request_start" and e.get("purpose") == "compaction" for e in events)
        assert agent.get_pending_queues() == {"steering": [], "followUp": []}
        assert_joined(context)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_child_admission_uses_root_spend_not_a_fresh_token_budget(tmp_path: Path) -> None:
    agent, provider = agent_with_script(tmp_path, ["root response", FINISH], runBudget={"maxTokens": 10018})
    agent.model.input_limit = 10000
    async def operation():
        context = agent._run_context
        await agent._invoke_provider([])
        assert sum(a.tokens for a in context._attempts.values()) == 3
        # A 10016-token child reservation fits a fresh pool but not the 10015
        # remaining after the root's settled request.
        result = await agent._spawn_subagent({"task": "cannot afford another request"})
        assert not result["ok"]
        assert "budget" in str(result).lower()
        return context
    with pytest.raises(asyncio.CancelledError):
        await agent._run_operation(operation)
    context = agent._last_run_context
    assert context.node(context.root_id).reason == "budget_exceeded"
    assert len(provider.contexts) == 1
    assert len(context._attempts) == 1
    assert len(context._nodes) == 2
    assert_joined(context)
