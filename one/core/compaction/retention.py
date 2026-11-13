"""Plan a contiguous, replay-safe tail without modifying session messages.

The caller must reserve space for the prompt, output and summary before passing
available_tokens. AgentSession restores active plans from durable plan messages
and keeps the active plan separately; do not duplicate it in the retained tail.
"""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from one.providers.base import native_tool_replay_pairs


@dataclass(frozen=True)
class RetentionPlan:
    """A suffix boundary and its estimated cost, or an explicit failure reason."""

    keep_start: int
    tokens_kept: int
    reason: Literal["ok", "no_safe_reduction", "oversized_exchange"]


def native_replay_keep_start(messages: list[dict[str, Any]], keep_start: int) -> int:
    """Do not compact away one half of a persisted Responses call pair."""
    pairs, _ = native_tool_replay_pairs(messages)
    for (call_index, _), output_index in pairs.items():
        if call_index < keep_start <= output_index:
            keep_start = call_index
    return keep_start


def plan_retention(
    messages: list[dict[str, Any]],
    estimate_tokens: Callable[[dict[str, Any]], int],
    recent_tokens: int,
    min_kept_messages: int,
    available_tokens: int,
) -> RetentionPlan:
    """Choose a safe suffix, prioritizing the latest exchange and message floor.

    A user boundary may expand the target by up to 15%, provided the resulting
    suffix fits available_tokens. Failures are non-actionable: callers must not
    compact with their boundary. Plan markers are not user exchange boundaries.
    """
    if min(recent_tokens, min_kept_messages, available_tokens) < 0:
        raise ValueError("retention limits must be nonnegative")
    costs = [estimate_tokens(message) for message in messages]
    if any(cost < 0 for cost in costs):
        raise ValueError("message token estimates must be nonnegative")
    total = len(messages)
    suffix = [0] * (total + 1)
    for i in range(total - 1, -1, -1):
        suffix[i] = suffix[i + 1] + costs[i]

    def safe(start: int) -> int:
        # The legacy helper is intentionally unchanged; repeated calls close
        # overlapping pairs even when their discovery order crosses the boundary.
        while True:
            adjusted = native_replay_keep_start(messages, start)
            if adjusted == start:
                return start
            start = adjusted

    user_boundaries = [i for i, m in enumerate(messages) if m.get("role") == "user" and not m.get("customType")]
    latest = safe(user_boundaries[-1] if user_boundaries else max(0, total - 1))
    if suffix[latest] > available_tokens:
        return RetentionPlan(latest, suffix[latest], "oversized_exchange")
    floor = safe(min(latest, max(0, total - min_kept_messages)))
    if suffix[floor] > available_tokens:
        return RetentionPlan(floor, suffix[floor], "no_safe_reduction")

    start = floor
    target = min(recent_tokens, available_tokens)
    while start > 0:
        candidate = safe(start - 1)
        if suffix[candidate] > target:
            break
        start = candidate
    # Prefer the nearest preceding user boundary, but do not expand an already
    # mandatory tail beyond the soft target's tolerance.
    preceding = next((i for i in reversed(user_boundaries) if i <= start), None)
    if preceding is not None:
        candidate = safe(preceding)
        if candidate > 0 and suffix[candidate] * 100 <= recent_tokens * 115 and suffix[candidate] <= available_tokens:
            start = candidate
    return RetentionPlan(start, suffix[start], "ok" if start else "no_safe_reduction")
