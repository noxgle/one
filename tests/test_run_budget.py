# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from one.core.run_context import NodeState, RunContext, RunLimits
from one.core.settings_manager import SettingsManager


@pytest.mark.parametrize("config", [None, [], {"maxTokens": True}, {"maxTimeSec": 1.5},
                                    {"maxActiveSubagents": -1}, {"maxCostUsd": float("nan")},
                                    {"maxCostUsd": "1"}, {"other": 1}])
def test_invalid_config(config: object) -> None:
    with pytest.raises(ValueError, match="runBudget"):
        RunLimits.from_config(config)


@pytest.mark.parametrize("kwargs", [
    {"max_tokens": -1}, {"max_tokens": True}, {"max_active_subagents": 1.5},
    {"max_time_sec": float("nan")}, {"max_time_sec": float("inf")}, {"max_time_sec": True},
    {"max_cost_usd": Decimal("NaN")}, {"max_cost_usd": Decimal("-1")}, {"max_cost_usd": 1.0},
])
def test_direct_limits_reject_invalid_values(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        RunLimits(**kwargs)


def test_settings_snapshot() -> None:
    settings = SettingsManager(".", "/tmp", in_memory=True, initial={"runBudget": {"maxCostUsd": 0.1}})
    assert settings.get_run_limits().max_cost_usd == Decimal("0.1")
    assert RunLimits.from_config({}) == RunLimits()


@pytest.mark.asyncio
async def test_competing_reservations_and_uncertain_settlement() -> None:
    ctx = RunContext("root", RunLimits(max_tokens=10, max_cost_usd=Decimal("1")))
    child = ctx.register(ctx.root_id, "child")

    async def reserve(request: str, node: str) -> bool:
        try:
            ctx.reserve(request, node, 6, Decimal("0.6"))
            return True
        except ValueError:
            return False

    assert sum(await asyncio.gather(reserve("a", ctx.root_id), reserve("b", child.node_id))) == 1
    result = ctx.settle("a", None, None)
    assert result.state == "uncertain" and result.tokens == 6
    assert ctx.settle("a", 1, Decimal("0.1")) == result
    with pytest.raises(ValueError):
        ctx.reserve("c", child.node_id, 5, Decimal("0.5"))
    with pytest.raises(ValueError):
        ctx.release("a")


@pytest.mark.asyncio
async def test_child_and_grandchild_share_root_pool() -> None:
    ctx = RunContext("root", RunLimits(max_tokens=10))
    child = ctx.register(ctx.root_id, "child")
    grandchild = ctx.register(child.node_id, "grandchild")
    ctx.reserve("root", ctx.root_id, 4, None)
    ctx.reserve("child", child.node_id, 3, None)
    ctx.reserve("grandchild", grandchild.node_id, 3, None)
    with pytest.raises(ValueError, match="token budget"):
        ctx.reserve("extra", grandchild.node_id, 1, None)
    ctx.release("child")
    with pytest.raises(ValueError, match="does not admit"):
        ctx.reserve("after-release", grandchild.node_id, 3, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("limits,tokens,cost", [
    (RunLimits(max_tokens=10), 10, Decimal(0)),
    (RunLimits(max_cost_usd=Decimal("1")), 1, Decimal("1")),
])
async def test_confirmed_exhaustion_signals_all_registered_tasks(limits, tokens, cost) -> None:
    ctx = RunContext("root", limits)
    child = ctx.register(ctx.root_id, "child")
    grandchild = ctx.register(child.node_id, "grandchild")
    entered = [asyncio.Event() for _ in range(3)]

    async def work(ready: asyncio.Event) -> None:
        ready.set()
        await asyncio.Event().wait()

    tasks = [asyncio.create_task(work(ready)) for ready in entered]
    for node, task in zip((ctx.root_id, child.node_id, grandchild.node_id), tasks, strict=True):
        ctx.start(node, task)
    await asyncio.gather(*(ready.wait() for ready in entered))
    ctx.reserve("request", ctx.root_id, tokens, cost)
    ctx.settle("request", tokens, cost)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert all(ctx.node(node).reason == "budget_exceeded"
               for node in (ctx.root_id, child.node_id, grandchild.node_id))
    with pytest.raises(ValueError, match="does not admit"):
        ctx.reserve("later", ctx.root_id, 0, Decimal(0))


@pytest.mark.asyncio
async def test_boundary_release_and_deadline() -> None:
    now = [0.0]
    ctx = RunContext("root", RunLimits(10, 5, Decimal("1")), clock=lambda: now[0])
    with pytest.raises(ValueError, match="known cost"):
        ctx.reserve("unknown", ctx.root_id, 1, None)
    ctx.reserve("a", ctx.root_id, 10, Decimal("1"))
    ctx.release("a")
    ctx.release("a")
    ctx.reserve("b", ctx.root_id, 10, Decimal("1"))
    assert ctx.settle("b", 11, Decimal("1.1")).tokens == 11
    now[0] = 5
    with pytest.raises(ValueError, match="deadline"):
        ctx.reserve("c", ctx.root_id, 0, Decimal(0))


@pytest.mark.asyncio
@pytest.mark.parametrize("limits,tokens,cost,kind", [
    (RunLimits(max_tokens=10), 10, Decimal(0), "token"),
    (RunLimits(max_cost_usd=Decimal("1")), 0, Decimal("1"), "cost"),
])
async def test_exhausted_budget_rejects_zero_reservations(limits, tokens, cost, kind) -> None:
    ctx = RunContext("root", limits)
    ctx.reserve("first", ctx.root_id, tokens, cost)
    with pytest.raises(ValueError, match=kind):
        ctx.reserve("second", ctx.root_id, 0, Decimal(0))
    ctx.release("first")
    with pytest.raises(ValueError, match="does not admit"):
        ctx.reserve("third", ctx.root_id, 0, Decimal(0))


@pytest.mark.asyncio
async def test_batch_slots_are_atomic_across_tree_and_released_on_finalization() -> None:
    ctx = RunContext("root", RunLimits(max_active_subagents=2))
    child = ctx.register(ctx.root_id, "child")

    async def compete(parent: str, name: str) -> bool:
        await asyncio.sleep(0)
        try:
            ctx.register(parent, name, node_id=name)
            return True
        except ValueError:
            return False

    assert sum(await asyncio.gather(compete(child.node_id, "grandchild"), compete(ctx.root_id, "sibling"))) == 1
    with pytest.raises(ValueError, match="limit"):
        ctx.register_batch(ctx.root_id, [("a", "a"), ("b", "b")])
    with pytest.raises(KeyError):
        ctx.node("a")
    ctx.cancel_subtree(child.node_id, "cleanup")
    if ctx.node("grandchild").state == NodeState.CANCELLING:
        ctx.finalize("grandchild", NodeState.CANCELLED)
    ctx.finalize(child.node_id, NodeState.CANCELLED)
    ctx.register(ctx.root_id, "replacement")


@pytest.mark.asyncio
async def test_batch_validation_and_unknown_cost() -> None:
    ctx = RunContext("root", RunLimits(max_active_subagents=2))
    with pytest.raises(ValueError, match="duplicate"):
        ctx.register_batch(ctx.root_id, [("a", "same"), ("b", "same")])
    with pytest.raises(KeyError):
        ctx.node("same")
    ctx.register_batch(ctx.root_id, [("a", "a"), ("b", "b")])
    ctx.reserve("unknown", ctx.root_id, 1, None)
    attempt = ctx.settle("unknown", 1, None)
    assert attempt.state == "uncertain" and attempt.cost_usd is None


@pytest.mark.asyncio
async def test_pending_batch_rollback_is_atomic_and_releases_slots() -> None:
    ctx = RunContext("root", RunLimits(max_active_subagents=2))
    first, second = ctx.register_batch(ctx.root_id, [("a", "a"), ("b", "b")])
    ctx.start(second.node_id)
    with pytest.raises(ValueError, match="pending"):
        ctx.cancel_pending_batch([first.node_id, second.node_id], "startup failed")
    assert ctx.node(first.node_id).state == NodeState.PENDING
    with pytest.raises(ValueError, match="duplicate"):
        ctx.cancel_pending_batch([first.node_id, first.node_id], "startup failed")
    with pytest.raises(ValueError, match="non-root"):
        ctx.cancel_pending_batch([ctx.root_id], "startup failed")
    ctx.cancel_pending_batch([first.node_id], "startup failed")
    assert ctx.node(first.node_id).state == NodeState.CANCELLED
    assert ctx.node(first.node_id).reason == "startup failed"
    ctx.register(ctx.root_id, "replacement")


@pytest.mark.asyncio
async def test_pending_batch_rollback_rejects_descendants() -> None:
    ctx = RunContext("root", RunLimits())
    child = ctx.register(ctx.root_id, "child")
    grandchild = ctx.register(child.node_id, "grandchild")
    with pytest.raises(ValueError, match="descendants"):
        ctx.cancel_pending_batch([child.node_id, grandchild.node_id], "startup failed")
    assert ctx.node(grandchild.node_id).state == NodeState.PENDING


@pytest.mark.asyncio
async def test_pending_batch_rollback_after_deadline() -> None:
    now = [0.0]
    ctx = RunContext("root", RunLimits(max_time_sec=10, max_active_subagents=2), clock=lambda: now[0])
    first, second = ctx.register_batch(ctx.root_id, [("a", "a"), ("b", "b")])
    now[0] = 10
    with pytest.raises(ValueError, match="deadline"):
        ctx.register(ctx.root_id, "too late")
    assert ctx.node(first.node_id).state == NodeState.CANCELLING
    ctx.cancel_pending_batch([first.node_id, second.node_id], "startup failed")
    for node in (first, second):
        assert ctx.node(node.node_id).state == NodeState.CANCELLED
        assert ctx.node(node.node_id).reason == "deadline"


@pytest.mark.asyncio
async def test_pending_batch_rollback_mixed_cancellation_reasons() -> None:
    ctx = RunContext("root", RunLimits(max_active_subagents=2))
    first, second = ctx.register_batch(ctx.root_id, [("a", "a"), ("b", "b")])
    ctx.cancel_subtree(first.node_id, "earlier cancellation")
    ctx.cancel_pending_batch([first.node_id, second.node_id], "startup failed")
    assert ctx.node(first.node_id).reason == "earlier cancellation"
    assert ctx.node(second.node_id).reason == "startup failed"
    assert all(ctx.node(node.node_id).state == NodeState.CANCELLED for node in (first, second))
    assert len(ctx.register_batch(ctx.root_id, [("c", "c"), ("d", "d")])) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("task_state", ["none", "live", "done"])
async def test_pending_batch_rollback_rejects_started_cancelling_atomically(task_state: str) -> None:
    ctx = RunContext("root", RunLimits(max_active_subagents=2))
    first, second = ctx.register_batch(ctx.root_id, [("a", "a"), ("b", "b")])
    task = asyncio.create_task(asyncio.sleep(0)) if task_state != "none" else None
    ctx.start(second.node_id, task)
    if task_state == "done":
        await task
    ctx.cancel_subtree(second.node_id, "earlier cancellation")
    try:
        with pytest.raises(ValueError, match="unstarted"):
            ctx.cancel_pending_batch([first.node_id, second.node_id], "startup failed")
        assert ctx.node(first.node_id).state == NodeState.PENDING
        assert ctx.node(second.node_id).state == NodeState.CANCELLING
        assert ctx.node(second.node_id).reason == "earlier cancellation"
        with pytest.raises(ValueError, match="limit"):
            ctx.register(ctx.root_id, "replacement")
    finally:
        if task is not None:
            await task
