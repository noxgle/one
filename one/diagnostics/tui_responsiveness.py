"""Metadata-only, deterministic building blocks for the offline TUI profiler.

This module deliberately has no dependency on a session, provider transport, MCP
client, filesystem state, or environment configuration.  The executable harness
owns a temporary directory and uses these helpers to make its report testable.
"""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one

from __future__ import annotations

import math
import random
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field


def percentile(values: Iterable[float], percent: int) -> float | None:
    """Return the nearest-rank percentile, or ``None`` for an empty sample."""
    samples = sorted(values)
    if not samples:
        return None
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return samples[max(0, math.ceil(len(samples) * percent / 100) - 1)]


@dataclass
class MetricSeries:
    """A numeric metric with summaries suitable for a machine-readable report."""

    values: list[float] = field(default_factory=list)

    def add(self, value_ms: float) -> None:
        self.values.append(float(value_ms))

    def summary(self) -> dict[str, float | int | None]:
        return {
            "count": len(self.values),
            "p95Ms": percentile(self.values, 95),
            "p99Ms": percentile(self.values, 99),
            "maxMs": max(self.values, default=None),
        }


@dataclass(frozen=True)
class QueueItem:
    """Queue accounting data only; payloads are intentionally never retained."""

    created_ns: int
    byte_count: int


class BoundedQueueCounters:
    """Queue age/byte/high-water counters without inspecting or logging payloads."""

    def __init__(self) -> None:
        self._items: deque[QueueItem] = deque()
        self.pending_bytes = 0
        self.high_water_items = 0
        self.high_water_bytes = 0
        self.max_age_ms = 0.0

    def enqueue(self, *, byte_count: int, now_ns: int | None = None) -> None:
        if byte_count < 0:
            raise ValueError("byte_count cannot be negative")
        self._items.append(QueueItem(now_ns if now_ns is not None else time.monotonic_ns(), byte_count))
        self.pending_bytes += byte_count
        self.high_water_items = max(self.high_water_items, len(self._items))
        self.high_water_bytes = max(self.high_water_bytes, self.pending_bytes)

    def drain_one(self, *, now_ns: int | None = None) -> float | None:
        if not self._items:
            return None
        item = self._items.popleft()
        self.pending_bytes -= item.byte_count
        age_ms = ((now_ns if now_ns is not None else time.monotonic_ns()) - item.created_ns) / 1_000_000
        self.max_age_ms = max(self.max_age_ms, age_ms)
        return age_ms

    def snapshot(self) -> dict[str, float | int]:
        oldest_age_ms = 0.0
        if self._items:
            oldest_age_ms = (time.monotonic_ns() - self._items[0].created_ns) / 1_000_000
        return {
            "pendingItems": len(self._items),
            "pendingBytes": self.pending_bytes,
            "highWaterItems": self.high_water_items,
            "highWaterBytes": self.high_water_bytes,
            "oldestAgeMs": oldest_age_ms,
            "maxDrainedAgeMs": self.max_age_ms,
        }


@dataclass(frozen=True)
class SyntheticWorkload:
    """Sizes and rates only; it deliberately has no prompt or tool-argument text."""

    durable_entries: int
    visible_entries: int
    delta_count: int
    delta_bytes: int
    child_count: int
    output_bytes: int
    argument_bytes: int
    thinking_bytes: int


def synthetic_workload(durable_entries: int, *, seed: int = 1) -> SyntheticWorkload:
    """Produce repeatable metadata-only workload dimensions for one history size."""
    if durable_entries < 1:
        raise ValueError("durable_entries must be positive")
    rng = random.Random(f"tui-responsiveness:{seed}:{durable_entries}")
    return SyntheticWorkload(
        durable_entries=durable_entries,
        visible_entries=500,
        delta_count=48,
        delta_bytes=512 + rng.randrange(128),
        child_count=3,
        output_bytes=64 * 1024,
        argument_bytes=32 * 1024,
        thinking_bytes=48 * 1024,
    )


def redact_metadata(value: object) -> object:
    """Reject accidental content-bearing report fields before JSON serialization."""
    sensitive = {
        "prompt",
        "prompts",
        "argument",
        "arguments",
        "args",
        "evidence",
        "credential",
        "credentials",
        "token",
        "tokens",
        "apikey",
        "authorization",
        "reason",
    }
    if isinstance(value, dict):
        return {
            key: "[redacted]"
            if key.lower().replace("-", "").replace("_", "") in sensitive
            else redact_metadata(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_metadata(item) for item in value]
    return value


def measure(series: MetricSeries, operation: Callable[[], object]) -> object:
    """Run the supplied operation and record elapsed monotonic milliseconds."""
    started = time.monotonic_ns()
    result = operation()
    series.add((time.monotonic_ns() - started) / 1_000_000)
    return result
