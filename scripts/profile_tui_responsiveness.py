#!/usr/bin/env python3
"""Offline R6 TUI responsiveness validation (metadata only; no real state)."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from one.diagnostics.tui_responsiveness import MetricSeries, measure, redact_metadata

HISTORY_SIZES = (50, 500, 5_000, 50_000)
VIEWPORT_SIZES = ((80, 24), (160, 48))
MEASUREMENT_SAMPLES = 8
DELTA_FLOOD = 96


def _metadata(viewport: tuple[int, int]) -> dict[str, Any]:
    import textual
    return {"python": sys.version.split()[0], "textual": getattr(textual, "__version__", "unknown"),
            "terminalSize": {"columns": viewport[0], "rows": viewport[1]},
            "hardware": {"cpuCount": os.cpu_count(), "machine": platform.machine()},
            "environment": {"platform": platform.system(), "ci": bool(os.environ.get("CI")), "offline": True}}


def _summary_gate(metrics: dict[str, Any], queue: dict[str, Any], slices: MetricSeries, paints: MetricSeries) -> dict[str, Any]:
    """Evaluate only measured production UI-loop values; never infer success."""
    keyboard = metrics["keypressHandlerCompletion"]
    wheel = metrics["wheelHandlerCompletion"]
    input_refresh = metrics["inputToAfterRefresh"]
    paint = metrics["paint"]
    render = metrics["render"]
    drain = metrics["drainSlice"]
    sidebar = metrics["sidebarRefresh"]

    def at_most(summary: dict[str, Any], field: str, limit: float) -> bool:
        return isinstance(summary.get(field), (int, float)) and summary[field] <= limit

    blocking_sources = {
        "keypressHandlerCompletion": keyboard,
        "wheelHandlerCompletion": wheel,
        "inputToAfterRefresh": input_refresh,
        "paint": paint,
        "drainSlice": drain,
        "sidebarRefresh": sidebar,
    }
    measured_maxima = {
        name: summary["maxMs"]
        for name, summary in blocking_sources.items()
        if isinstance(summary.get("maxMs"), (int, float))
    }
    max_stall_ms = max(measured_maxima.values(), default=None)
    checks = {
        "keypressP95Le50Ms": at_most(keyboard, "p95Ms", 50), "keypressP99Le100Ms": at_most(keyboard, "p99Ms", 100),
        "wheelP95Le50Ms": at_most(wheel, "p95Ms", 50), "wheelP99Le100Ms": at_most(wheel, "p99Ms", 100),
        "inputRefreshP99Le50Ms": at_most(input_refresh, "p99Ms", 50),
        "drainP99Le8Ms": at_most(drain, "p99Ms", 8),
        "paintP99Le100Ms": at_most(paint, "p99Ms", 100),
        "renderP99Le100Ms": at_most(render, "p99Ms", 100),
        "maxEventLoopBlockingLe250Ms": max_stall_ms is not None and max_stall_ms <= 250,
        "paintCapLe30Hz": metrics["paintCap"]["configuredHz"] <= 30,
        "queueBounded": queue["highWaterItems"] <= 256 and queue["highWaterBytes"] <= 512 * 1024,
    }
    return {"status": "met" if all(checks.values()) else "not_met", "checks": checks,
            "paintRender": {"p99Ms": paint["p99Ms"], "maxMs": paint["maxMs"], "measuredPaintCount": paint["count"]},
            "maxEventLoopBlocking": {"maxMs": max_stall_ms, "sources": measured_maxima,
                                      "target": "no ordinary stalls >250ms"},
            "ordinaryStallOver250Ms": max_stall_ms is None or max_stall_ms > 250,
            "sliceTarget": {"p99Ms": drain["p99Ms"], "target": "p99 <=8ms"},
            "basis": "actual detached TuiSessionView/Textual virtual-driver measurements"}


class _OfflineRuntime:
    """Worker-owned stub: commands are DTO-only and no provider/MCP/state exists."""
    def __init__(self, history_size: int) -> None:
        self.history_size = history_size
        # This is deliberately a command-only control stub.  It models the
        # correlation checks made by the runtime host without starting a
        # provider, MCP client, or a persisted session.
        self.epoch = 1
        self.approval_id = "offline-approval-1"
        self.approval_resolved = False
        self.compactions = 0

    async def dispatch(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        if command == "approval_resolve":
            accepted = (
                payload.get("epoch") == self.epoch
                and payload.get("approval_id") == self.approval_id
                and not self.approval_resolved
            )
            if accepted:
                self.approval_resolved = True
            return {"accepted": accepted, "epoch": self.epoch}
        if command == "ui" and payload == {"operation": "compact", "payload": {"instructions": "offline"}}:
            self.compactions += 1
            return {"accepted": True, "epoch": self.epoch, "compactions": self.compactions}
        if command != "snapshot" or payload:
            raise ValueError("invalid offline profiler command")
        # A bounded production snapshot deliberately represents history as the
        # latest viewport, rather than creating 50k durable fsync operations.
        count = min(self.history_size, 500)
        return {"epoch": 1, "sidebar": {"model": "offline/stub", "cwd": ".", "sessionId": "offline",
                "contextKnown": True, "contextPercent": 42.0, "mcpEnabled": False, "streaming": True,
                "subagents": True, "bashOutput": False},
                "messages": [{"role": "assistant", "content": "x" * 32} for _ in range(count)],
                "monitor": {"revision": 1, "children": [{"sessionId": f"child-{i}", "status": "running"} for i in range(3)]}}


def _facade(history_size: int):
    from one.core.agent_runtime_facade import AgentRuntimeFacade
    from one.core.threaded_agent_runtime import RuntimeBootstrapSpec
    return AgentRuntimeFacade(RuntimeBootstrapSpec(cwd=".", agent_dir="/offline"), lambda _spec: _OfflineRuntime(history_size),
                              event_capacity=256, event_queue_max_bytes=512 * 1024,
                               command_allowlist={"snapshot", "approval_resolve", "ui"})


def _durable_persistence(root: Path) -> dict[str, Any]:
    """Measure a small, explicit durable sample; fixture construction is not timed."""
    from one.core.session_manager import SessionManager
    manager = SessionManager.create(str(root), str(root / "sessions"))
    series = MetricSeries()
    for index in range(MEASUREMENT_SAMPLES):
        measure(series, lambda index=index: manager.append_message({"role": "user", "content": f"sample-{index}"}))
    return {"appendMessageFsync": series.summary(), "samples": MEASUREMENT_SAMPLES,
            "note": "separate durable sample; large-history fixture does not issue one fsync per entry"}


async def _detached_production_case(history_size: int, viewport: tuple[int, int]) -> dict[str, Any]:
    from textual import events

    import one.modes.tui_mode as tui_mode
    from one.modes.tui_mode import _UI_FRAME_SECONDS, TuiSessionView, _OneTextualApp

    facade = _facade(history_size)
    await facade.start()
    view = TuiSessionView(facade)
    await view.refresh()
    app = _OneTextualApp(view)
    keypress, wheel, input_lag, drain, paint, lifecycle, sidebar, delta_reduction = (MetricSeries() for _ in range(8))
    queue_high_items = queue_high_bytes = 0
    queue_high_age_ms = 0.0
    queue_first_seen_ns: int | None = None
    control_latency, context_projection, stats_projection = MetricSeries(), MetricSeries(), MetricSeries()
    control_accepted: list[bool] = []
    stale_decisions_rejected: list[bool] = []
    critical_seen: list[str] = []
    original_drain, original_paint = app._flush_pending_ui_events, app._render_stream
    original_lifecycle, original_sidebar = app._handle_session_event, app._refresh_sidebar
    original_delta_reduction = app._apply_assistant_deltas

    def timed_drain() -> None:
        measure(drain, original_drain)

    def timed_paint() -> None:
        measure(paint, original_paint)

    def timed_lifecycle(event: dict[str, Any]) -> None:
        if event.get("type") in {"tui_approval_request", "compaction_start", "compaction_end", "message_end"}:
            critical_seen.append(str(event["type"]))
        measure(lifecycle, lambda: original_lifecycle(event))

    def timed_sidebar() -> None:
        measure(sidebar, original_sidebar)

    def timed_delta_reduction(delta: str) -> bool:
        result = False

        def apply() -> None:
            nonlocal result
            result = original_delta_reduction(delta)

        measure(delta_reduction, apply)
        return result

    app._flush_pending_ui_events, app._render_stream = timed_drain, timed_paint
    app._handle_session_event, app._refresh_sidebar = timed_lifecycle, timed_sidebar
    app._apply_assistant_deltas = timed_delta_reduction
    try:
        async with app.run_test(size=viewport) as pilot:
            await pilot.pause()
            command_input = app.query_one("#input")
            input_type = type(command_input)
            original_key_handler = input_type._on_key
            original_wheel_handler = type(app).on_mouse_scroll_up

            async def timed_key(_self: Any, event: Any) -> None:
                started = time.monotonic_ns()
                await original_key_handler(_self, event)
                keypress.add((time.monotonic_ns() - started) / 1_000_000)

            def timed_wheel(_self: Any, event: Any) -> None:
                started = time.monotonic_ns()
                original_wheel_handler(_self, event)
                wheel.add((time.monotonic_ns() - started) / 1_000_000)

            input_type._on_key = timed_key
            setattr(type(app), "on_mouse_scroll_up", timed_wheel)
            original_git = tui_mode.get_git_branch

            def slow_git_probe(_cwd: str) -> str:
                time.sleep(0.006)
                return "offline"

            tui_mode.get_git_branch = slow_git_probe
            # Exercise the production cache/to_thread Git path while key and
            # wheel events are delivered; the stub never invokes host Git.
            app._refresh_sidebar()
            # The producer runs outside the Textual loop, enters the real facade,
            # then the real detached-view subscription and UI accumulator.
            worker_done = threading.Event()

            def flood() -> None:
                nonlocal queue_first_seen_ns, queue_high_age_ms, queue_high_items, queue_high_bytes
                try:
                    # The approval and compaction barriers are intentionally
                    # interleaved with output, I/O, and CPU work.  They are
                    # offline DTO events, not simulated provider activity.
                    facade._on_worker_event({"type": "message_start", "message": {"role": "assistant", "content": ""}})  # noqa: SLF001
                    for i in range(DELTA_FLOOD):
                        # Newlines make this a real multi-line transcript workload,
                        # rather than one invisible unpainted logical line.
                        facade._on_worker_event({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "x" * 511 + "\n"}})  # noqa: SLF001
                        if i % 16 == 0:
                            facade._on_worker_event({"type": "subagent_monitor_invalidated", "revision": i})  # noqa: SLF001
                        if i == 24:
                            facade._on_worker_event({"type": "tui_approval_request", "approvalId": "offline-approval-1", "tool": "bash"})  # noqa: SLF001
                        if i == 48:
                            facade._on_worker_event({"type": "compaction_start"})  # noqa: SLF001
                        if i == 64:
                            facade._on_worker_event({"type": "compaction_end", "ok": True})  # noqa: SLF001
                        with facade._space:  # noqa: SLF001 - counters only, never event content
                            queue_high_items = max(queue_high_items, len(facade._events))  # noqa: SLF001
                            queue_high_bytes = max(queue_high_bytes, facade._event_bytes)  # noqa: SLF001
                            if facade._events:  # noqa: SLF001
                                now_ns = time.monotonic_ns()
                                queue_first_seen_ns = queue_first_seen_ns or now_ns
                                queue_high_age_ms = max(queue_high_age_ms, (now_ns - queue_first_seen_ns) / 1_000_000)
                            else:
                                queue_first_seen_ns = None
                        # Span several UI frames so paint measurements describe
                        # real transcript updates rather than only initial layout.
                        if i and i % (DELTA_FLOOD // 3) == 0:
                            time.sleep(_UI_FRAME_SECONDS * 1.25)
                    # Offline slow I/O/fsync and Git probes occur on this worker, not UI.
                    with tempfile.TemporaryFile() as file:
                        file.write(b"x" * 4096)
                        file.flush()
                        os.fsync(file.fileno())
                    end = time.monotonic() + 0.015  # CPU/GIL contention probe
                    while time.monotonic() < end:
                        sum(range(400))
                    facade._on_worker_event({"type": "message_end", "message": {"role": "assistant", "content": ""}})  # noqa: SLF001
                finally:
                    worker_done.set()
            worker = threading.Thread(target=flood, name="offline-r6-producer")
            worker.start()
            for _ in range(MEASUREMENT_SAMPLES):
                await pilot.press("x")
                lag_start = time.monotonic_ns()
                app.call_after_refresh(lambda start=lag_start: input_lag.add((time.monotonic_ns() - start) / 1_000_000))
                container = app.query_one("#stream_container")
                wheel_event = events.MouseScrollUp(container, 1, 1, 0, -1, 0, False, False, False)
                # Measure the production handler directly: posting to the
                # child scroll widget may stop propagation before the app
                # handler and would otherwise create a false zero sample.
                timed_wheel(app, wheel_event)
                if not control_accepted:
                    started = time.monotonic_ns()
                    stale_id = await facade.command("approval_resolve", {"approval_id": "wrong", "approved": True, "reason": "", "epoch": 1})
                    stale_epoch = await facade.command("approval_resolve", {"approval_id": "offline-approval-1", "approved": True, "reason": "", "epoch": 0})
                    approval = await facade.command("approval_resolve", {"approval_id": "offline-approval-1", "approved": True, "reason": "", "epoch": 1})
                    compact = await facade.command("ui", {"operation": "compact", "payload": {"instructions": "offline"}})
                    control_latency.add((time.monotonic_ns() - started) / 1_000_000)
                    stale_decisions_rejected.extend([not bool(stale_id["result"]["accepted"]), not bool(stale_epoch["result"]["accepted"])])
                    control_accepted.extend([bool(approval["result"]["accepted"]), bool(compact["result"]["accepted"])])
                started = time.monotonic_ns()
                await facade.command("snapshot")
                context_projection.add((time.monotonic_ns() - started) / 1_000_000)
                started = time.monotonic_ns()
                await facade.command("snapshot")
                stats_projection.add((time.monotonic_ns() - started) / 1_000_000)
                await pilot.pause()
                with facade._space:  # noqa: SLF001 - observability only; no payload read
                    queue_high_items = max(queue_high_items, len(facade._events))  # noqa: SLF001
                    queue_high_bytes = max(queue_high_bytes, facade._event_bytes)  # noqa: SLF001
            # Never join a worker on Textual's/UI loop. Poll its completion while
            # continuing to service the virtual driver and drain/paint work.
            completion_deadline = time.monotonic() + 2
            while not worker_done.is_set():
                if time.monotonic() >= completion_deadline:
                    raise RuntimeError("offline producer did not complete")
                await pilot.pause()
                await asyncio.sleep(0.001)
            # Wait for the actual transcript lifecycle barrier and at least two
            # frame-capped paints. A profiler result without content/paints is a
            # failed measurement, not a successful latency sample.
            while True:
                with facade._space:  # noqa: SLF001
                    facade_empty = not facade._events  # noqa: SLF001
                with app._pending_ui_events_lock:  # noqa: SLF001
                    app_empty = not app._pending_ui_events and not app._legacy_deferred_ui_events  # noqa: SLF001
                if facade_empty and app_empty and not app._assistant_stream_open and len(app._stream_lines) > 1 and int(paint.summary()["count"] or 0) >= 2:
                    break
                if time.monotonic() >= completion_deadline:
                    raise RuntimeError(
                        "offline producer completed without draining and painting transcript content "
                        f"(facade_empty={facade_empty}, app_empty={app_empty}, stream_open={app._assistant_stream_open}, "
                        f"lines={len(app._stream_lines)}, paints={paint.summary()['count']})"
                    )
                await pilot.pause()
                await asyncio.sleep(0.005)
            with facade._space:  # noqa: SLF001
                queue = {"highWaterItems": queue_high_items, "highWaterBytes": queue_high_bytes,
                         "pendingItems": len(facade._events), "pendingBytes": facade._event_bytes,
                         "highWaterAgeMs": queue_high_age_ms}
            metrics = {"keypressHandlerCompletion": keypress.summary(), "wheelHandlerCompletion": wheel.summary(),
                       "inputToAfterRefresh": input_lag.summary(), "drainSlice": drain.summary(),
                       "paintCap": {"configuredHz": 1 / _UI_FRAME_SECONDS, "frameMs": _UI_FRAME_SECONDS * 1_000},
                        "paint": paint.summary(), "render": paint.summary(),
                        "lifecycleReduction": lifecycle.summary(), "sidebarRefresh": sidebar.summary(),
                         "deltaReduction": delta_reduction.summary(),
                        "approvalCompactionControl": control_latency.summary(),
                        "contextProjection": context_projection.summary(), "statsProjection": stats_projection.summary(),
                        "cache": {"completedBlockEntries": len(app._stream_content_cache), "visibleLines": len(app._stream_lines)}}
            result = {"metadata": _metadata(viewport), "workload": {"durableEntries": history_size, "visibleStreamLines": 500,
                    "deltaFlood": DELTA_FLOOD, "deltaBytes": 512, "children": 3, "slowGitStubMs": 6, "slowIoFsync": True, "cpuContentionMs": 15},
                    "route": "detached_production_route",
                    "productionRoute": "TuiSessionView -> AgentRuntimeFacade -> bounded detached event delivery -> Textual",
                    "metrics": metrics, "queue": queue, "gates": _summary_gate(metrics, queue, drain, paint),
                    "controls": {"approvalIdEpochBound": True, "staleDecisionsRejected": stale_decisions_rejected, "commandAccepted": control_accepted,
                                 "commandLatency": control_latency.summary(), "criticalEventsExpected": ["tui_approval_request", "compaction_start", "compaction_end", "message_end"],
                                 "criticalEventsObserved": critical_seen, "noLostCriticalEvents": len(critical_seen) == 4},
                    "limitations": ["keypress/wheel completion and after_refresh use Textual's virtual driver, not a physical terminal flush",
                                    "offline worker stubs model slow Git/I/O/CPU; no provider, MCP, host Git, or real state is used"]}
            input_type._on_key = original_key_handler
            setattr(type(app), "on_mouse_scroll_up", original_wheel_handler)
            tui_mode.get_git_branch = original_git
            return result
    finally:
        app._flush_pending_ui_events, app._render_stream = original_drain, original_paint
        app._handle_session_event, app._refresh_sidebar = original_lifecycle, original_sidebar
        app._apply_assistant_deltas = original_delta_reduction
        await facade.close()
        facade.finalize()


def _legacy_session(history_size: int, root: Path) -> Any:
    """Build the synthetic in-process control with scratch-only state."""
    from one.core.agent_session import AgentSession
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.settings_manager import SettingsManager

    class Loader:
        cwd = "."

        def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
            return "offline control"

    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth, str(root / "models.json"))
    session = AgentSession(
        SessionManager.in_memory(str(root)),
        SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}, "bash": {"showOutput": False}}),
        registry, Loader(), registry.find("openai", "gpt-4.1"), "medium",
    )
    # This synthetic in-memory session is passed directly to the legacy
    # compatibility path. It is not a historical binary or full runtime bootstrap.
    session.messages = [{"role": "assistant", "content": "h" * 32}] * history_size
    return session


async def _legacy_in_process_control_reference_case(history_size: int, viewport: tuple[int, int], root: Path) -> dict[str, Any]:
    """Measure the synthetic direct-session compatibility control."""
    from textual import events

    from one.modes.tui_mode import _UI_FRAME_SECONDS, _OneTextualApp

    session = _legacy_session(history_size, root)
    app = _OneTextualApp(session)
    keypress, wheel, input_lag, drain, paint, sidebar, context, stats = (MetricSeries() for _ in range(8))
    original_drain, original_paint, original_sidebar = app._flush_pending_ui_events, app._render_stream, app._refresh_sidebar

    def timed_drain() -> None:
        measure(drain, original_drain)

    def timed_paint() -> None:
        measure(paint, original_paint)

    def timed_sidebar() -> None:
        measure(sidebar, original_sidebar)

    app._flush_pending_ui_events, app._render_stream, app._refresh_sidebar = timed_drain, timed_paint, timed_sidebar
    try:
        async with app.run_test(size=viewport) as pilot:
            await pilot.pause()
            input_type = type(app.query_one("#input"))
            old_key, old_wheel = input_type._on_key, type(app).on_mouse_scroll_up

            async def timed_key(instance: Any, event: Any) -> None:
                started = time.monotonic_ns()
                await old_key(instance, event)
                keypress.add((time.monotonic_ns() - started) / 1_000_000)

            def timed_wheel(self: Any, event: Any) -> None:  # noqa: ARG001
                started = time.monotonic_ns()
                old_wheel(self, event)
                wheel.add((time.monotonic_ns() - started) / 1_000_000)

            input_type._on_key, type(app).on_mouse_scroll_up = timed_key, timed_wheel
            try:
                flood_complete = threading.Event()
                stop_flood = threading.Event()

                def flood() -> None:
                    try:
                        session._emit({"type": "message_start", "message": {"role": "assistant", "content": ""}})
                        for index in range(DELTA_FLOOD):
                            if stop_flood.is_set():
                                break
                            session._emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "x" * 511 + "\n"}})
                        session._emit({"type": "message_end", "message": {"role": "assistant", "content": ""}})
                    finally:
                        flood_complete.set()

                async def wait_for_flood_completion() -> None:
                    """Keep the virtual driver alive while the producer finishes.

                    The timeout is cooperative: request the bounded producer to
                    stop, then continue yielding until its completion signal
                    confirms it has cleaned up.  Never join a thread on
                    Textual's UI loop.
                    """
                    deadline = time.monotonic() + 2
                    timed_out = False
                    while not flood_complete.is_set():
                        if not timed_out and time.monotonic() >= deadline:
                            stop_flood.set()
                            timed_out = True
                        await pilot.pause()
                    if timed_out:
                        raise TimeoutError("legacy in-process control flood did not finish within 2 seconds")

                worker = threading.Thread(target=flood, name="offline-r6-legacy-in-process-control")
                worker.start()
                try:
                    for _ in range(MEASUREMENT_SAMPLES):
                        await pilot.press("x")
                        started = time.monotonic_ns()
                        app.call_after_refresh(lambda start=started: input_lag.add((time.monotonic_ns() - start) / 1_000_000))
                        container = app.query_one("#stream_container")
                        timed_wheel(app, events.MouseScrollUp(container, 1, 1, 0, -1, 0, False, False, False))
                        measure(context, session.get_context_usage)
                        measure(stats, session.get_session_stats)
                        await pilot.pause()
                    await wait_for_flood_completion()
                    while app._pending_ui_events or app._legacy_deferred_ui_events:
                        app._flush_pending_ui_events()
                        await pilot.pause()
                finally:
                    stop_flood.set()
                    await wait_for_flood_completion()
            finally:
                input_type._on_key, type(app).on_mouse_scroll_up = old_key, old_wheel
        metrics = {"keypressHandlerCompletion": keypress.summary(), "wheelHandlerCompletion": wheel.summary(),
                   "inputToAfterRefresh": input_lag.summary(), "drainSlice": drain.summary(),
                   "paint": paint.summary(), "render": paint.summary(), "sidebarRefresh": sidebar.summary(),
                   "contextProjection": context.summary(), "statsProjection": stats.summary(),
                   "paintCap": {"configuredHz": 1 / _UI_FRAME_SECONDS, "frameMs": _UI_FRAME_SECONDS * 1_000}}
        return {"metadata": _metadata(viewport), "route": "legacy_in_process_control_reference",
                "productionRoute": "synthetic in-memory AgentSession -> _OneTextualApp legacy compatibility path",
                "workload": {"durableEntries": history_size, "visibleStreamLines": 500, "deltaFlood": DELTA_FLOOD},
                "metrics": metrics,
                "limitations": [
                    "control/reference only: provider, MCP, extensions, and runtime construction are not included",
                    "no historical binary was run; a synthetic in-memory AgentSession is passed directly to the legacy compatibility path",
                    "virtual-driver timings are not physical terminal flush timings",
                ]}
    finally:
        app._flush_pending_ui_events, app._render_stream, app._refresh_sidebar = original_drain, original_paint, original_sidebar


async def run_profile(*, history_sizes: tuple[int, ...] = HISTORY_SIZES, viewports: tuple[tuple[int, int], ...] = VIEWPORT_SIZES) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="one-tui-r6-") as temporary:
        root = Path(temporary)
        persistence = _durable_persistence(root)
        rows: list[dict[str, Any]] = []
        comparisons: list[dict[str, Any]] = []
        for size in history_sizes:
            for viewport in viewports:
                legacy = await _legacy_in_process_control_reference_case(size, viewport, root)
                detached = await _detached_production_case(size, viewport)
                rows.extend((legacy, detached))
                comparable = ("sidebarRefresh", "paint", "render", "keypressHandlerCompletion", "wheelHandlerCompletion", "inputToAfterRefresh")
                comparisons.append({
                    "label": "legacy_in_process_control_reference vs detached_production_route",
                    "workload": {"durableEntries": size, "viewport": {"columns": viewport[0], "rows": viewport[1]}},
                    "comparableMetrics": {
                        name: {"legacy_in_process_control_reference": legacy["metrics"][name], "detached_production_route": detached["metrics"][name]}
                        for name in comparable
                    },
                    "routeSpecificMetrics": {
                        name: {"legacy_in_process_control_reference": legacy["metrics"][name], "detached_production_route": detached["metrics"][name]}
                        for name in ("contextProjection", "statsProjection")
                    },
                    "notComparable": ["legacy contextProjection/statsProjection are direct in-process calls; detached route publishes bounded runtime projections", "legacy has no detached command-control lane"],
                })
        detached_rows = [row for row in rows if row["route"] == "detached_production_route"]
        return redact_metadata({"schema": "one.tui-responsiveness/v5", "metadataOnly": True,
                                "durablePersistence": persistence, "rows": rows, "comparisons": comparisons,
                                "gateSummary": {"status": "met" if all(c["gates"]["status"] == "met" and c["controls"]["noLostCriticalEvents"] and all(c["controls"]["commandAccepted"]) for c in detached_rows) else "not_met",
                                                "rule": "not_met if any detached target, control acceptance, or critical-event survival check misses"}})  # type: ignore[return-value]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.parse_args()
    print(json.dumps(asyncio.run(run_profile()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
