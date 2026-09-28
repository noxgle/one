from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from one.core import activity_watchdog
from one.core.activity_watchdog import ActivityWatchdog


class _TouchBeforeClearEvent:
    """Event that models activity arriving immediately before clear()."""

    def __init__(self, watchdog: ActivityWatchdog) -> None:
        self._event = asyncio.Event()
        self._watchdog = watchdog

    def clear(self) -> None:
        self._watchdog.touch("provider_delta")
        self._event.clear()

    def set(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


@pytest.mark.asyncio
async def test_wait_for_expiry_recalculates_deadline_after_event_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    """Activity immediately before clear must not leave a stale idle deadline."""
    clock = iter((0.0, 5.0, 5.0))
    monkeypatch.setattr(activity_watchdog, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    watchdog = ActivityWatchdog(idle_timeout=10, max_duration=0)
    watchdog._changed = _TouchBeforeClearEvent(watchdog)  # type: ignore[assignment]
    timeouts: list[float | None] = []

    async def stop_after_recording(waiter: object, *, timeout: float | None = None) -> None:
        timeouts.append(timeout)
        if hasattr(waiter, "close"):
            waiter.close()  # type: ignore[union-attr]
        raise asyncio.CancelledError

    monkeypatch.setattr(activity_watchdog, "asyncio", SimpleNamespace(wait_for=stop_after_recording))

    with pytest.raises(asyncio.CancelledError):
        await watchdog.wait_for_expiry()

    assert timeouts == [10.0]
