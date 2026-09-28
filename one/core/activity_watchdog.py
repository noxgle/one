from __future__ import annotations

import asyncio
import time


class ActivityWatchdog:
    """Cancellation-safe lifecycle watchdog; activity values never contain output."""

    def __init__(self, idle_timeout: float, max_duration: float) -> None:
        self.idle_timeout = max(0.0, idle_timeout)
        self.max_duration = max(0.0, max_duration)
        self.started_at = time.monotonic()
        self.last_activity_at = self.started_at
        self.last_activity_kind = "started"
        self._active_operations = 0
        self._changed = asyncio.Event()

    def touch(self, kind: str) -> None:
        self.last_activity_at = time.monotonic()
        self.last_activity_kind = kind
        self._changed.set()

    def begin_operation(self, kind: str) -> None:
        self._active_operations += 1
        self.touch(kind)

    def end_operation(self, kind: str) -> None:
        self._active_operations = max(0, self._active_operations - 1)
        self.touch(kind)

    async def wait_for_expiry(self) -> str:
        """Wait for ``idle`` or ``max_duration`` without creating child tasks."""
        while True:
            # Clear before reading the timestamps.  An activity notification after
            # this point either updates the deadline we calculate below or leaves
            # the event set, so it cannot be lost by a later clear().
            self._changed.clear()
            now = time.monotonic()
            max_remaining = self.max_duration - (now - self.started_at) if self.max_duration else None
            if max_remaining is not None and max_remaining <= 0:
                return "max_duration"
            # Bounded operations are governed by their own timeouts.  While one
            # is active it is not an idle subagent, but max duration still wins.
            idle_remaining = None
            if self.idle_timeout and not self._active_operations:
                idle_remaining = self.idle_timeout - (now - self.last_activity_at)
                if idle_remaining <= 0:
                    return "idle"
            waits = [value for value in (max_remaining, idle_remaining) if value is not None]
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=min(waits) if waits else None)
            except TimeoutError:
                pass
