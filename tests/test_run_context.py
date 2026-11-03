# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from one.core.run_context import NodeState, RunContext, RunLimits


@pytest.mark.asyncio
async def test_identity_limits_and_clock() -> None:
    now = [10.0]
    limits = RunLimits(100, 5, Decimal("1.25"), 2)
    first = RunContext("session", limits, clock=lambda: now[0])
    second = RunContext("session", limits)
    root = first.node(first.root_id)
    child = first.register(root.node_id, "session")
    grandchild = first.register(child.node_id, "other")
    assert first.run_id != second.run_id
    assert root.node_id != second.root_id
    assert (child.run_id, child.parent_node_id, child.depth) == (first.run_id, root.node_id, 1)
    assert (grandchild.parent_node_id, grandchild.depth) == (child.node_id, 2)
    assert first.started_at == 10.0 and first.limits == limits
    with pytest.raises(FrozenInstanceError):
        setattr(limits, "max_tokens", 1)
    with pytest.raises(FrozenInstanceError):
        setattr(child, "parent_node_id", grandchild.node_id)
    first.start(grandchild.node_id)
    now[0] = 12.0
    assert first.finalize(grandchild.node_id, NodeState.COMPLETED).finished_at == 12.0


@pytest.mark.asyncio
async def test_registration_rejects_duplicates_cycles_and_unknown_parents() -> None:
    ctx = RunContext("root", RunLimits())
    child = ctx.register(ctx.root_id, "child", node_id="child")
    with pytest.raises(ValueError, match="duplicate"):
        ctx.register(child.node_id, "again", node_id=ctx.root_id)
    with pytest.raises(ValueError, match="duplicate"):
        ctx.register(child.node_id, "again", node_id="child")
    with pytest.raises(KeyError):
        ctx.register("missing", "child")
    with pytest.raises(ValueError, match="empty"):
        ctx.register(ctx.root_id, "")


@pytest.mark.asyncio
async def test_cancellation_closes_subtree_but_not_sibling() -> None:
    ctx = RunContext("root", RunLimits())
    child = ctx.register(ctx.root_id, "child")
    grandchild = ctx.register(child.node_id, "grandchild")
    sibling = ctx.register(ctx.root_id, "sibling")
    ctx.start(ctx.root_id)
    ctx.start(child.node_id)
    ctx.cancel_subtree(child.node_id, "user_cancel")
    ctx.cancel_subtree(child.node_id, "parent_cancel")
    assert ctx.node(child.node_id).reason == "user_cancel"
    assert ctx.node(grandchild.node_id).state == NodeState.CANCELLING
    with pytest.raises(ValueError, match="admit"):
        ctx.register(grandchild.node_id, "late")
    with pytest.raises(ValueError, match="active descendants"):
        ctx.finalize(child.node_id, NodeState.CANCELLED)
    ctx.finalize(grandchild.node_id, NodeState.CANCELLED)
    ctx.finalize(child.node_id, NodeState.CANCELLED)
    ctx.register(sibling.node_id, "allowed")
    with pytest.raises(ValueError, match="active descendants"):
        ctx.finalize(ctx.root_id, NodeState.COMPLETED)
    ctx.cancel_subtree(ctx.root_id, "runtime_shutdown")
    with pytest.raises(ValueError, match="admit"):
        ctx.register(ctx.root_id, "late")


@pytest.mark.asyncio
async def test_transitions_and_exactly_once_finalization() -> None:
    ctx = RunContext("root", RunLimits())
    with pytest.raises(ValueError, match="invalid final"):
        ctx.finalize(ctx.root_id, NodeState.COMPLETED)
    ctx.start(ctx.root_id)
    with pytest.raises(ValueError, match="pending"):
        ctx.start(ctx.root_id)
    with pytest.raises(ValueError, match="terminal"):
        ctx.finalize(ctx.root_id, NodeState.RUNNING)
    with pytest.raises(ValueError, match="cancel node"):
        ctx.finalize(ctx.root_id, NodeState.CANCELLED)
    ctx.cancel_subtree(ctx.root_id, "timeout")
    with pytest.raises(ValueError, match="invalid final"):
        ctx.finalize(ctx.root_id, NodeState.COMPLETED)
    with pytest.raises(ValueError, match="reason"):
        ctx.finalize(ctx.root_id, NodeState.CANCELLED, "other")
    assert ctx.finalize(ctx.root_id, NodeState.CANCELLED).reason == "timeout"
    with pytest.raises(ValueError, match="already finalized"):
        ctx.finalize(ctx.root_id, NodeState.CANCELLED)


@pytest.mark.asyncio
async def test_task_handles_and_owner_loop() -> None:
    ctx = RunContext("root", RunLimits())
    task = asyncio.create_task(asyncio.sleep(0))
    ctx.start(ctx.root_id, task)
    assert ctx.task(ctx.root_id) is task
    with pytest.raises(ValueError, match="active task"):
        ctx.finalize(ctx.root_id, NodeState.COMPLETED)
    await task
    ctx.finalize(ctx.root_id, NodeState.COMPLETED)
    assert ctx.task(ctx.root_id) is None
    with pytest.raises(RuntimeError, match="owner event loop"):
        await asyncio.to_thread(lambda: asyncio.run(_read_on_other_loop(ctx)))


async def _read_on_other_loop(ctx: RunContext) -> None:
    ctx.node(ctx.root_id)


@pytest.mark.asyncio
async def test_signal_subtree_closes_admission_and_preserves_sibling_cleanup() -> None:
    ctx = RunContext("root", RunLimits())
    child = ctx.register(ctx.root_id, "child")
    grandchild = ctx.register(child.node_id, "grandchild")
    sibling = ctx.register(ctx.root_id, "sibling")
    pending = ctx.register(child.node_id, "pending")
    ready = [asyncio.Event() for _ in range(3)]
    cleaning = asyncio.Event()
    release = asyncio.Event()

    async def work(index: int) -> None:
        ready[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            if index == 0:
                cleaning.set()
                await release.wait()

    tasks = [asyncio.create_task(work(i)) for i in range(3)]
    ctx.start(ctx.root_id)
    for node, task in zip((child, grandchild, sibling), tasks, strict=True):
        ctx.start(node.node_id, task)
    await asyncio.gather(*(event.wait() for event in ready))
    ctx.cancel_tasks_subtree(child.node_id, "abort")
    with pytest.raises(ValueError, match="admit"):
        ctx.register(child.node_id, "late")
    await cleaning.wait()
    ctx.cancel_tasks_subtree(child.node_id, "again")
    assert not tasks[0].done() and not tasks[2].done()
    assert ctx.node(child.node_id).reason == "abort"
    release.set()
    results = await asyncio.gather(*tasks[:2], return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    ctx.cancel_pending_batch([pending.node_id], "rollback")
    ctx.finalize(grandchild.node_id, NodeState.CANCELLED)
    ctx.finalize(child.node_id, NodeState.CANCELLED)
    ctx.cancel_tasks_subtree(ctx.root_id, "shutdown")
    assert isinstance((await asyncio.gather(tasks[2], return_exceptions=True))[0], asyncio.CancelledError)
    ctx.finalize(sibling.node_id, NodeState.CANCELLED)
    ctx.finalize(ctx.root_id, NodeState.CANCELLED)


@pytest.mark.asyncio
async def test_cancel_and_wait_reports_unfinished_cleanup_without_affecting_sibling() -> None:
    ctx = RunContext("root", RunLimits())
    child = ctx.register(ctx.root_id, "child")
    sibling = ctx.register(ctx.root_id, "sibling")
    ready = asyncio.Event()
    cleanup = asyncio.Event()
    release = asyncio.Event()

    async def work() -> None:
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()

    task = asyncio.create_task(work())
    other = asyncio.create_task(asyncio.Event().wait())
    ctx.start(child.node_id, task)
    ctx.start(sibling.node_id, other)
    await ready.wait()
    assert await ctx.cancel_and_wait_subtree(child.node_id, "abort", timeout=0) == [child.node_id]
    await cleanup.wait()
    assert not other.done()
    with pytest.raises(ValueError, match="admit"):
        ctx.register(child.node_id, "late")
    release.set()
    assert await ctx.cancel_and_wait_subtree(child.node_id, "again", timeout=1) == []
    assert ctx.node(child.node_id).reason == "abort"
    assert isinstance((await asyncio.gather(task, return_exceptions=True))[0], asyncio.CancelledError)
    ctx.finalize(child.node_id, NodeState.CANCELLED)
    ctx.cancel_tasks_subtree(sibling.node_id, "cleanup")
    await asyncio.gather(other, return_exceptions=True)
    ctx.finalize(sibling.node_id, NodeState.CANCELLED)


@pytest.mark.asyncio
async def test_cancel_and_wait_rejects_invalid_timeout_and_self_join() -> None:
    ctx = RunContext("root", RunLimits())
    ctx.start(ctx.root_id, asyncio.current_task())
    for timeout in (-1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError, match="timeout"):
            await ctx.cancel_and_wait_subtree(ctx.root_id, "abort", timeout=timeout)
    with pytest.raises(ValueError, match="current task"):
        await ctx.cancel_and_wait_subtree(ctx.root_id, "abort", timeout=0)
    assert ctx.node(ctx.root_id).state == NodeState.RUNNING


@pytest.mark.asyncio
async def test_deadline_cancels_tree_and_closes_admission() -> None:
    loop = asyncio.get_running_loop()
    callbacks = []

    def schedule(delay, callback):
        assert delay == 5
        callbacks.append(callback)
        return loop.call_later(3600, callback)

    now = [0.0]
    ctx = RunContext("root", RunLimits(max_time_sec=5), clock=lambda: now[0], schedule=schedule)
    child = ctx.register(ctx.root_id, "child")
    grandchild = ctx.register(child.node_id, "grandchild")
    ready = asyncio.Event()

    async def work():
        ready.set()
        await asyncio.Event().wait()

    tasks = [asyncio.create_task(work()) for _ in range(3)]
    for node_id, task in zip((ctx.root_id, child.node_id, grandchild.node_id), tasks, strict=True):
        ctx.start(node_id, task)
    await ready.wait()
    now[0] = 5
    ctx._deadline_timer.cancel()  # Simulate the scheduler consuming the timer.
    callbacks[0]()
    callbacks[0]()
    with pytest.raises(ValueError, match="deadline"):
        ctx.register(child.node_id, "late")
    assert all(ctx.node(node_id).reason == "deadline" for node_id in (ctx.root_id, child.node_id, grandchild.node_id))
    assert all(isinstance(result, asyncio.CancelledError) for result in await asyncio.gather(*tasks, return_exceptions=True))
    for node_id in (grandchild.node_id, child.node_id, ctx.root_id):
        ctx.finalize(node_id, NodeState.CANCELLED)


@pytest.mark.asyncio
async def test_deadline_timer_lifecycle_and_admission_boundary() -> None:
    loop = asyncio.get_running_loop()
    handles = []
    callbacks = []

    def schedule(delay, callback):
        callbacks.append(callback)
        handle = loop.call_later(3600, callback)
        handles.append(handle)
        return handle

    now = [0.0]
    ctx = RunContext("root", RunLimits(max_time_sec=5), clock=lambda: now[0], schedule=schedule)
    ctx.start(ctx.root_id)
    ctx.finalize(ctx.root_id, NodeState.COMPLETED)
    assert handles[0].cancelled()
    callbacks[0]()  # A callback already queued at finalization is harmless.
    assert ctx.node(ctx.root_id).state == NodeState.COMPLETED

    ctx = RunContext("root", RunLimits(max_time_sec=5), clock=lambda: now[0], schedule=schedule)
    now[0] = 5
    with pytest.raises(ValueError, match="deadline"):
        ctx.start(ctx.root_id)
    assert handles[1].cancelled()
    assert ctx.node(ctx.root_id).state == NodeState.CANCELLING
    ctx.finalize(ctx.root_id, NodeState.CANCELLED)

    RunContext("unlimited", RunLimits(), schedule=schedule)
    assert len(handles) == 2


@pytest.mark.asyncio
async def test_deadline_callback_runs_on_owner_loop() -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()

    async def work():
        started.set()
        await asyncio.Event().wait()

    ctx = RunContext("root", RunLimits(max_time_sec=1), schedule=lambda delay, cb: loop.call_later(0, cb))
    task = asyncio.create_task(work())
    ctx.start(ctx.root_id, task)
    await started.wait()
    result, = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result, asyncio.CancelledError)
    assert ctx.node(ctx.root_id).reason == "deadline"
    ctx.finalize(ctx.root_id, NodeState.CANCELLED)
