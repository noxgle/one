# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from uuid import uuid4

from one.core.run_pricing import PriceSnapshot, TokenUsage


class NodeState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"


_TERMINAL = frozenset({NodeState.CANCELLED, NodeState.COMPLETED, NodeState.FAILED,
                       NodeState.TIMED_OUT, NodeState.INTERRUPTED})


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Immutable limit snapshot; configuration is validated by from_config."""

    max_tokens: int = 0
    max_time_sec: float = 0
    max_cost_usd: Decimal = Decimal(0)
    max_active_subagents: int = 0

    def __post_init__(self) -> None:
        for name in ("max_tokens", "max_active_subagents"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if (isinstance(self.max_time_sec, bool) or not isinstance(self.max_time_sec, (int, float))
                or not math.isfinite(self.max_time_sec) or self.max_time_sec < 0):
            raise ValueError("max_time_sec must be a non-negative finite number")
        if (not isinstance(self.max_cost_usd, Decimal) or not self.max_cost_usd.is_finite()
                or self.max_cost_usd < 0):
            raise ValueError("max_cost_usd must be a non-negative finite Decimal")

    @classmethod
    def from_config(cls, config: object) -> RunLimits:
        """Validate a runBudget object and return an immutable limit snapshot."""
        if not isinstance(config, dict):
            raise ValueError("runBudget must be an object")
        fields = {"maxTokens", "maxTimeSec", "maxCostUsd", "maxActiveSubagents"}
        unknown = set(config) - fields
        if unknown:
            raise ValueError(f"runBudget contains unknown field: {next(iter(unknown))}")
        values = {}
        for name in ("maxTokens", "maxTimeSec", "maxActiveSubagents"):
            value = config.get(name, 0)
            if type(value) is not int or value < 0:
                raise ValueError(f"runBudget.{name} must be a non-negative integer")
            values[name] = value
        cost = config.get("maxCostUsd", 0)
        if isinstance(cost, bool) or not isinstance(cost, (int, float, Decimal)):
            raise ValueError("runBudget.maxCostUsd must be a non-negative finite number")
        cost_decimal = Decimal(str(cost))
        if not cost_decimal.is_finite() or cost_decimal < 0:
            raise ValueError("runBudget.maxCostUsd must be a non-negative finite number")
        return cls(values["maxTokens"], values["maxTimeSec"], cost_decimal, values["maxActiveSubagents"])


@dataclass(frozen=True, slots=True)
class RunNode:
    run_id: str
    node_id: str
    parent_node_id: str | None
    session_id: str
    depth: int
    state: NodeState
    reason: str | None = None
    finished_at: float | None = None
    cleanup_timed_out: bool = False


@dataclass(frozen=True, slots=True)
class BudgetAttempt:
    node_id: str
    tokens: int
    cost_usd: Decimal | None
    state: str = "reserved"
    price: PriceSnapshot | None = None
    tokens_known: bool = False
    cost_known: bool = False


class RunContext:
    """In-memory execution tree, owned exclusively by its creating event loop.

    A deadline closes admission and cancels registered tasks. Callers remain
    responsible for joining tasks and finalizing nodes after cancellation.
    """

    def __init__(self, session_id: str, limits: RunLimits, *, clock: Callable[[], float] = time.monotonic,
                 schedule: Callable[[float, Callable[[], None]], asyncio.TimerHandle] | None = None) -> None:
        if not session_id:
            raise ValueError("session_id must not be empty")
        self._loop = asyncio.get_running_loop()
        self._clock = clock
        self._limits = limits
        self._run_id = uuid4().hex
        self._root_id = uuid4().hex
        self._started_at = clock()
        root = RunNode(self._run_id, self._root_id, None, session_id, 0, NodeState.PENDING)
        self._nodes = {root.node_id: root}
        self._tasks: dict[str, asyncio.Task[object]] = {}
        self._owners: dict[str, asyncio.Task[object]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._signalled_nodes: set[str] = set()
        self._started_nodes: set[str] = set()
        self._attempts: dict[str, BudgetAttempt] = {}
        self._deadline_fired = False
        self._deadline_timer: asyncio.TimerHandle | None = None
        if limits.max_time_sec:
            self._deadline_timer = (schedule or self._loop.call_later)(limits.max_time_sec, self._deadline_expired)

    def _deadline_expired(self) -> None:
        self._check_loop()
        if self._deadline_fired or self._nodes[self._root_id].state in _TERMINAL:
            return
        self._deadline_fired = True
        self._deadline_timer = None
        self.cancel_tasks_subtree(self._root_id, "deadline")

    def _check_deadline(self) -> None:
        if self._limits.max_time_sec and self._clock() - self._started_at >= self._limits.max_time_sec:
            if self._deadline_timer is not None:
                self._deadline_timer.cancel()
            self._deadline_expired()
            raise ValueError("run deadline exceeded")

    def reserve(self, request_id: str, node_id: str, tokens: int, cost_usd: Decimal | None) -> None:
        """Atomically reserve a caller-supplied bound on the owner loop."""
        self._check_loop()
        self._check_deadline()
        node = self._nodes[node_id]
        if node.state not in (NodeState.PENDING, NodeState.RUNNING):
            raise ValueError("node does not admit requests")
        if not request_id or request_id in self._attempts:
            raise ValueError("empty or duplicate request_id")
        if type(tokens) is not int or tokens < 0:
            raise ValueError("tokens must be a non-negative integer")
        if cost_usd is None:
            if self._limits.max_cost_usd:
                raise ValueError("known cost bound required")
        if cost_usd is not None and (not isinstance(cost_usd, Decimal) or not cost_usd.is_finite() or cost_usd < 0):
            raise ValueError("cost bound must be a non-negative finite Decimal")
        used_tokens = sum(a.tokens for a in self._attempts.values())
        used_cost = sum((Fraction(a.cost_usd) for a in self._attempts.values() if a.cost_usd is not None), Fraction(0))
        if self._limits.max_tokens and (used_tokens >= self._limits.max_tokens
                                       or used_tokens + tokens > self._limits.max_tokens):
            self.cancel_tasks_subtree(self._root_id, "budget_exceeded")
            raise ValueError("run token budget exceeded")
        if self._limits.max_cost_usd and cost_usd is not None and (
            used_cost >= Fraction(self._limits.max_cost_usd)
            or used_cost + Fraction(cost_usd) > Fraction(self._limits.max_cost_usd)
        ):
            self.cancel_tasks_subtree(self._root_id, "budget_exceeded")
            raise ValueError("run cost budget exceeded")
        self._attempts[request_id] = BudgetAttempt(node_id, tokens, cost_usd)

    def reserve_priced(
        self, request_id: str, node_id: str, input_bound: int, output_bound: int, price: PriceSnapshot | None,
        *, price_is_applicable: bool,
    ) -> None:
        """Reserve with a snapshot explicitly confirmed applicable to all request pricing modes.

        The caller must also ensure the token bounds are enforceable. In particular,
        a cache-write rate for one duration cannot bound a more expensive duration.
        """
        if price is not None and not isinstance(price, PriceSnapshot):
            raise ValueError("price must be a PriceSnapshot or None")
        if type(price_is_applicable) is not bool or (price is not None and not price_is_applicable):
            raise ValueError("price applicability must be explicitly confirmed")
        if type(input_bound) is not int or input_bound < 0 or type(output_bound) is not int or output_bound < 0:
            raise ValueError("token bounds must be non-negative integers")
        cost = price.bound(input_bound, output_bound) if price is not None else None
        self.reserve(request_id, node_id, input_bound + output_bound, cost)
        self._attempts[request_id] = replace(self._attempts[request_id], price=price)

    def settle_usage(self, request_id: str, usage: TokenUsage | None) -> BudgetAttempt:
        """Settle normalized usage with the price captured at reservation."""
        self._check_loop()
        attempt = self._attempts[request_id]
        if usage is not None and not isinstance(usage, TokenUsage):
            raise ValueError("usage must be TokenUsage or None")
        return self.settle(request_id, usage.tokens if usage is not None else None,
                           usage.cost(attempt.price) if usage is not None else None)

    def settle(self, request_id: str, tokens: int | None, cost_usd: Decimal | None) -> BudgetAttempt:
        """Settle once; retain bounds only for the dimensions still unknown."""
        self._check_loop()
        attempt = self._attempts[request_id]
        if attempt.state != "reserved":
            return attempt
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise ValueError("tokens must be a non-negative integer")
        if cost_usd is not None and (
            not isinstance(cost_usd, Decimal) or not cost_usd.is_finite() or cost_usd < 0
        ):
            raise ValueError("cost must be a non-negative finite Decimal")
        attempt = replace(attempt, tokens=attempt.tokens if tokens is None else tokens,
                          cost_usd=attempt.cost_usd if cost_usd is None else cost_usd,
                          tokens_known=tokens is not None, cost_known=cost_usd is not None,
                          state="uncertain" if tokens is None or cost_usd is None else "settled")
        self._attempts[request_id] = attempt
        # Reservations held by in-flight requests do not by themselves exhaust
        # the budget: they can still be released or settle below their bound.
        # Confirmed usage at the limit, however, must stop the whole tree.
        settled_tokens = sum(entry.tokens for entry in self._attempts.values() if entry.tokens_known)
        settled_cost = sum((Fraction(entry.cost_usd) for entry in self._attempts.values()
                            if entry.cost_known and entry.cost_usd is not None), Fraction(0))
        if ((self._limits.max_tokens and settled_tokens >= self._limits.max_tokens)
                or (self._limits.max_cost_usd and settled_cost >= Fraction(self._limits.max_cost_usd))):
            self.cancel_tasks_subtree(self._root_id, "budget_exceeded")
        return attempt

    def release(self, request_id: str) -> None:
        """Release only an attempt known not to have reached the provider."""
        self._check_loop()
        attempt = self._attempts[request_id]
        if attempt.state == "reserved":
            self._attempts[request_id] = replace(attempt, tokens=0, cost_usd=Decimal(0), state="released")
        elif attempt.state != "released":
            raise ValueError("cannot release a settled attempt")

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def root_id(self) -> str:
        return self._root_id

    @property
    def limits(self) -> RunLimits:
        return self._limits

    @property
    def started_at(self) -> float:
        return self._started_at

    def _check_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError("RunContext requires its owner event loop") from None
        if loop is not self._loop:
            raise RuntimeError("RunContext requires its owner event loop")

    def node(self, node_id: str) -> RunNode:
        self._check_loop()
        return self._nodes[node_id]

    def register(self, parent_node_id: str, session_id: str, *, node_id: str | None = None) -> RunNode:
        return self.register_batch(parent_node_id, [(session_id, node_id)])[0]

    def register_batch(self, parent_node_id: str, children: list[tuple[str, str | None]]) -> list[RunNode]:
        """Admit the entire batch without waiting; pending nodes occupy slots until finalized."""
        self._check_loop()
        self._check_deadline()
        parent = self._nodes[parent_node_id]
        if parent.state not in (NodeState.PENDING, NodeState.RUNNING):
            raise ValueError("parent does not admit children")
        active = sum(node.node_id != self._root_id and node.state not in _TERMINAL for node in self._nodes.values())
        if self._limits.max_active_subagents and active + len(children) > self._limits.max_active_subagents:
            raise ValueError("run active subagent limit exceeded")
        nodes = []
        ids = set(self._nodes)
        for session_id, node_id in children:
            if not session_id or node_id == "":
                raise ValueError("session_id and node_id must not be empty")
            node_id = node_id if node_id is not None else uuid4().hex
            if node_id in ids:
                raise ValueError("duplicate node_id")
            ids.add(node_id)
            nodes.append(RunNode(self._run_id, node_id, parent_node_id, session_id, parent.depth + 1, NodeState.PENDING))
        self._nodes.update((node.node_id, node) for node in nodes)
        return nodes

    def start(self, node_id: str, task: asyncio.Task[object] | None = None) -> RunNode:
        self._check_loop()
        self._check_deadline()
        node = self._nodes[node_id]
        if node.state != NodeState.PENDING:
            raise ValueError("only pending nodes can start")
        if task is not None and task.get_loop() is not self._loop:
            raise ValueError("task belongs to a different event loop")
        if task is not None and task.done():
            raise ValueError("task is already done")
        if task is not None:
            self._tasks[node_id] = task
        self._started_nodes.add(node_id)
        node = replace(node, state=NodeState.RUNNING)
        self._nodes[node_id] = node
        return node

    def cancel_pending_batch(self, node_ids: list[str], reason: str) -> None:
        """Roll back an unstarted admission without partially releasing a batch."""
        self._check_loop()
        if not reason:
            raise ValueError("cancellation reason must not be empty")
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("duplicate node_id")
        for node_id in node_ids:
            node = self._nodes[node_id]
            if (node_id == self._root_id or node.state not in (NodeState.PENDING, NodeState.CANCELLING)
                    or node_id in self._started_nodes or node_id in self._tasks):
                raise ValueError("only unstarted pending or cancelling non-root nodes can be rolled back")
            if len(self._subtree(node_id)) != 1:
                raise ValueError("cannot roll back a node with descendants")
        for node_id in node_ids:
            self.cancel_subtree(node_id, reason)
            self.finalize(node_id, NodeState.CANCELLED)

    def task(self, node_id: str) -> asyncio.Task[object] | None:
        self._check_loop()
        self._nodes[node_id]
        return self._tasks.get(node_id)

    def _subtree(self, node_id: str) -> list[str]:
        self._nodes[node_id]
        result = [node_id]
        for current in result:
            result.extend(node.node_id for node in self._nodes.values() if node.parent_node_id == current)
        return result

    def cancel_subtree(self, node_id: str, reason: str) -> None:
        self._check_loop()
        if not reason:
            raise ValueError("cancellation reason must not be empty")
        for current in self._subtree(node_id):
            node = self._nodes[current]
            if node.state in (NodeState.PENDING, NodeState.RUNNING):
                self._nodes[current] = replace(node, state=NodeState.CANCELLING, reason=reason)
                self.cancellation_event(current).set()

    def cancellation_event(self, node_id: str) -> asyncio.Event:
        self._check_loop()
        self._nodes[node_id]
        return self._cancel_events.setdefault(node_id, asyncio.Event())

    def mark_cleanup_timeout(self, node_id: str) -> None:
        """Keep admission closed and report unknown effects, never successful cleanup."""
        self._check_loop()
        for current in self._subtree(node_id):
            node = self._nodes[current]
            if node.state not in _TERMINAL:
                self._nodes[current] = replace(node, cleanup_timed_out=True)
        if node_id == self._root_id and self._deadline_timer is not None:
            self._deadline_timer.cancel()
            self._deadline_timer = None

    def cancel_tasks_subtree(self, node_id: str, reason: str) -> None:
        """Close admission before signalling registered tasks in this subtree.

        Joining tasks and finalizing nodes remain the caller's responsibility.
        Unstarted nodes are left cancelling so they can be rolled back.
        """
        self.cancel_subtree(node_id, reason)
        for current in self._subtree(node_id):
            task = self._tasks.get(current)
            if task is not None and not task.done() and current not in self._signalled_nodes:
                self._signalled_nodes.add(current)
                task.cancel()

    async def cancel_and_wait_subtree(self, node_id: str, reason: str, *, timeout: float) -> list[str]:
        """Signal a subtree and wait at most timeout seconds; return unfinished node IDs.

        The caller must not be one of the tasks being joined. Unfinished tasks are
        not cancelled again, so their cleanup can continue after this method returns.
        Finalizing nodes and reporting external effects remain the caller's responsibility.
        """
        self._check_loop()
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout < float("inf"):
            raise ValueError("timeout must be a non-negative finite number")
        nodes = self._subtree(node_id)
        current = asyncio.current_task()
        if current is not None and any(self._tasks.get(node) is current for node in nodes):
            raise ValueError("cannot wait for the current task")
        self.cancel_tasks_subtree(node_id, reason)
        tasks = {self._tasks[node] for node in nodes if node in self._tasks and not self._tasks[node].done()}
        tasks.update(self._owners[node] for node in nodes
                     if node in self._owners and self._owners[node] is not current and not self._owners[node].done())
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        return [node for node in nodes
                if (node in self._tasks and not self._tasks[node].done())
                or (node in self._owners and self._owners[node] is not current and not self._owners[node].done())]

    def finalize(self, node_id: str, state: NodeState, reason: str | None = None) -> RunNode:
        self._check_loop()
        node = self._nodes[node_id]
        if node.state in _TERMINAL:
            raise ValueError("node already finalized")
        if state not in _TERMINAL:
            raise ValueError("final state must be terminal")
        if node.state == NodeState.PENDING or (node.state == NodeState.CANCELLING and state != NodeState.CANCELLED):
            raise ValueError("invalid final transition")
        if node.state == NodeState.RUNNING and state == NodeState.CANCELLED:
            raise ValueError("cancel node before finalizing it as cancelled")
        if any(self._nodes[child].state not in _TERMINAL for child in self._subtree(node_id)[1:]):
            raise ValueError("cannot finalize with active descendants")
        task = self._tasks.get(node_id)
        if task is not None and not task.done():
            raise ValueError("cannot finalize with an active task")
        if node.state == NodeState.CANCELLING and reason is not None and reason != node.reason:
            raise ValueError("cancellation reason cannot be changed")
        node = replace(node, state=state, reason=node.reason if node.state == NodeState.CANCELLING else reason,
                       finished_at=self._clock())
        self._nodes[node_id] = node
        self._tasks.pop(node_id, None)
        if node_id == self._root_id and self._deadline_timer is not None:
            self._deadline_timer.cancel()
            self._deadline_timer = None
        return node
