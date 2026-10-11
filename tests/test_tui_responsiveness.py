# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import importlib.util
import inspect
import time
from pathlib import Path

import pytest

from one.diagnostics.tui_responsiveness import BoundedQueueCounters, MetricSeries, redact_metadata, synthetic_workload


def test_metric_series_uses_nearest_rank_p95_p99_and_max() -> None:
    series = MetricSeries()
    for value in range(1, 101):
        series.add(value)

    assert series.summary() == {"count": 100, "p95Ms": 95.0, "p99Ms": 99.0, "maxMs": 100.0}


def test_metadata_redaction_never_retains_sensitive_payload_fields() -> None:
    report = redact_metadata(
        {
            "prompt": "private prompt",
            "args": {"command": "secret"},
            "apiKey": "key",
            "timing": {"p95Ms": 1.5},
            "nested": [{"evidence": "raw"}],
        }
    )

    assert report == {
        "prompt": "[redacted]",
        "args": "[redacted]",
        "apiKey": "[redacted]",
        "timing": {"p95Ms": 1.5},
        "nested": [{"evidence": "[redacted]"}],
    }
    assert "private prompt" not in repr(report)
    assert "secret" not in repr(report)


def test_queue_counters_track_bytes_high_water_and_age_without_payloads() -> None:
    queue = BoundedQueueCounters()
    queue.enqueue(byte_count=4, now_ns=1_000_000_000)
    queue.enqueue(byte_count=9, now_ns=1_001_000_000)

    assert queue.drain_one(now_ns=1_011_000_000) == 11.0
    snapshot = queue.snapshot()
    assert snapshot["pendingItems"] == 1
    assert snapshot["pendingBytes"] == 9
    assert snapshot["highWaterItems"] == 2
    assert snapshot["highWaterBytes"] == 13
    assert snapshot["maxDrainedAgeMs"] == 11.0


def test_synthetic_workload_is_deterministic_and_keeps_delta_rate_constant() -> None:
    first = synthetic_workload(50_000, seed=9)
    again = synthetic_workload(50_000, seed=9)
    small = synthetic_workload(50, seed=9)

    assert first == again
    assert first.visible_entries == small.visible_entries == 500
    assert first.delta_count == small.delta_count == 48
    assert first.child_count == small.child_count == 3


def _profiler_module():
    path = Path(__file__).parents[1] / "scripts" / "profile_tui_responsiveness.py"
    spec = importlib.util.spec_from_file_location("tui_responsiveness_profile", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_in_process_control_profiler_never_joins_a_worker_on_the_textual_ui_path() -> None:
    profiler = _profiler_module()

    assert ".join(" not in inspect.getsource(profiler._legacy_in_process_control_reference_case)


@pytest.mark.parametrize(
    ("ok", "key"),
    [(True, "outputText"), (False, "error")],
)
def test_oversized_legacy_tool_end_keeps_top_level_display_value(ok: bool, key: str) -> None:
    from one.modes.tui_mode import _OneTextualApp

    reduced = _OneTextualApp._reduce_oversized_ui_event(
        {
            "type": "tool_call_end",
            "ok": ok,
            key: "top-level display value",
            "result": {key: "nested display value"},
        }
    )

    assert reduced[key] == "top-level display value"
    assert "result" not in reduced


@pytest.mark.asyncio
async def test_profile_reports_paired_routes_controls_and_gate_shape() -> None:
    profiler = _profiler_module()
    report = await profiler.run_profile(history_sizes=(50,), viewports=((80, 24),))

    assert report["schema"] == "one.tui-responsiveness/v5"
    assert report["metadataOnly"] is True
    assert [row["route"] for row in report["rows"]] == ["legacy_in_process_control_reference", "detached_production_route"]
    comparison = report["comparisons"][0]
    assert comparison["label"] == "legacy_in_process_control_reference vs detached_production_route"
    assert "notComparable" in comparison
    assert set(comparison["comparableMetrics"]["paint"]) == {"legacy_in_process_control_reference", "detached_production_route"}
    assert set(comparison["routeSpecificMetrics"]) == {"contextProjection", "statsProjection"}
    case = report["rows"][1]
    assert case["productionRoute"].startswith("TuiSessionView -> AgentRuntimeFacade")
    assert case["workload"]["visibleStreamLines"] == 500
    assert case["workload"]["deltaFlood"] == profiler.DELTA_FLOOD
    assert case["workload"]["slowIoFsync"] is True
    assert "virtual driver" in case["limitations"][0]
    assert set(case["metrics"]) >= {"keypressHandlerCompletion", "wheelHandlerCompletion", "inputToAfterRefresh", "drainSlice", "paint", "render", "sidebarRefresh", "contextProjection", "statsProjection", "cache"}
    for name in ("keypressHandlerCompletion", "wheelHandlerCompletion", "inputToAfterRefresh", "drainSlice", "paint", "render"):
        assert case["metrics"][name]["count"] > 0, name
        assert set(case["metrics"][name]) == {"count", "p95Ms", "p99Ms", "maxMs"}
    assert set(case["queue"]) == {"highWaterItems", "highWaterBytes", "pendingItems", "pendingBytes", "highWaterAgeMs"}
    assert case["gates"]["status"] in {"met", "not_met"}
    assert set(case["gates"]["checks"]) >= {"keypressP95Le50Ms", "wheelP95Le50Ms", "paintP99Le100Ms", "renderP99Le100Ms", "maxEventLoopBlockingLe250Ms", "queueBounded"}
    assert case["gates"]["paintRender"]["measuredPaintCount"] == case["metrics"]["paint"]["count"]
    assert case["gates"]["paintRender"]["p99Ms"] == case["metrics"]["render"]["p99Ms"]
    assert case["gates"]["maxEventLoopBlocking"]["target"] == "no ordinary stalls >250ms"
    assert case["gates"]["sliceTarget"]["target"] == "p99 <=8ms"
    assert report["durablePersistence"]["appendMessageFsync"]["count"] == profiler.MEASUREMENT_SAMPLES
    assert report["gateSummary"]["status"] in {"met", "not_met"}
    assert case["metrics"]["cache"]["visibleLines"] > 1
    assert case["metrics"]["paint"]["count"] >= 2
    assert case["controls"]["approvalIdEpochBound"] is True
    assert case["controls"]["staleDecisionsRejected"] == [True, True]
    assert case["controls"]["commandAccepted"] == [True, True]
    assert case["controls"]["noLostCriticalEvents"] is True
    assert case["controls"]["criticalEventsObserved"] == case["controls"]["criticalEventsExpected"]
    legacy_case = report["rows"][0]
    assert legacy_case["productionRoute"] == "synthetic in-memory AgentSession -> _OneTextualApp legacy compatibility path"
    assert "provider, MCP, extensions, and runtime construction are not included" in legacy_case["limitations"][0]
    assert "no historical binary was run" in legacy_case["limitations"][1]


@pytest.mark.asyncio
async def test_ui_drain_yields_after_expensive_event_and_preserves_critical_order() -> None:
    """Reduction slices yield without reordering lifecycle/approval/error barriers."""
    from one.modes.tui_mode import TuiSessionView, _OneTextualApp

    class Facade:
        def subscribe(self, _listener, **_kwargs):
            return lambda: None

        async def command(self, _name, _payload=None):
            return {"result": {"epoch": 0, "sidebar": {}, "messages": []}}

    app = _OneTextualApp(TuiSessionView(Facade()))
    seen: list[str] = []
    original = app._handle_session_event

    def expensive(event):
        seen.append(event["type"])
        end = time.monotonic() + 0.003
        while time.monotonic() < end:
            pass
        original(event)

    app._handle_session_event = expensive  # type: ignore[method-assign]
    events = [
        {"type": "warning", "warning": "first"},
        {"type": "tui_approval_request", "approvalId": "a", "tool": "bash"},
        {"type": "tool_approval_rejected", "tool": "bash", "reason": "no"},
        {"type": "error", "error": "last"},
    ]
    async with app.run_test() as pilot:
        with app._pending_ui_events_lock:  # noqa: SLF001
            app._pending_ui_events = list(events)  # noqa: SLF001
            app._pending_ui_event_bytes = sum(app._ui_event_size(event) for event in events)  # noqa: SLF001
        app._flush_pending_ui_events()  # noqa: SLF001
        assert 0 < len(seen) < len(events)
        while app._pending_ui_events:  # noqa: SLF001
            await pilot.pause()
            app._flush_pending_ui_events()  # noqa: SLF001

    assert seen == [event["type"] for event in events]


@pytest.mark.asyncio
async def test_lifecycle_batch_coalesces_sidebar_refresh_per_frame_and_keeps_order() -> None:
    """Lifecycle barriers reduce in order without rebuilding the sidebar per event."""
    from one.modes.tui_mode import TuiSessionView, _OneTextualApp

    class Facade:
        def subscribe(self, _listener, **_kwargs):
            return lambda: None

        async def command(self, _name, _payload=None):
            return {"result": {"epoch": 0, "sidebar": {}, "messages": []}}

    app = _OneTextualApp(TuiSessionView(Facade()))
    seen: list[str] = []
    sidebar_refreshes: list[float] = []
    original = app._handle_session_event

    def record(event):
        seen.append(event["type"])
        original(event)

    app._handle_session_event = record  # type: ignore[method-assign]
    app._refresh_sidebar = lambda: sidebar_refreshes.append(time.monotonic())  # type: ignore[method-assign]
    events = [
        {"type": "turn_start", "turnId": "one"},
        {"type": "agent_start", "turnId": "one"},
        {"type": "provider_request_end", "turnId": "one", "requestId": "one", "elapsedMs": 1},
        {"type": "agent_end", "turnId": "one", "elapsedMs": 2},
    ]
    async with app.run_test() as pilot:
        # Ignore the mount-time initial sidebar paint; this assertion concerns
        # only the lifecycle batch below.
        await pilot.pause()
        sidebar_refreshes.clear()
        with app._pending_ui_events_lock:  # noqa: SLF001
            app._pending_ui_events = list(events)  # noqa: SLF001
            app._pending_ui_event_bytes = sum(app._ui_event_size(event) for event in events)  # noqa: SLF001
        app._flush_pending_ui_events()  # noqa: SLF001
        assert seen == [event["type"] for event in events]
        assert sidebar_refreshes == []
        app._paint_scheduled_sidebar()  # noqa: SLF001
        assert len(sidebar_refreshes) == 1

    assert seen == [event["type"] for event in events]


@pytest.mark.asyncio
async def test_legacy_direct_ingress_is_bounded_and_preserves_accepted_barrier_order(tmp_path: Path) -> None:
    """The compatibility listener fails explicitly instead of growing without bound."""
    from one.modes.tui_mode import (
        _UI_EVENT_CAPACITY,
        _UI_LEGACY_DEFERRED_CAPACITY,
        _UI_LEGACY_DEFERRED_MAX_BYTES,
        _OneTextualApp,
    )
    from tests.support.tui import _mk_app_session

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    accepted = [
        {"type": "tool_call_end", "tool": f"tool-{index}", "ok": bool(index % 2)}
        for index in range(_UI_LEGACY_DEFERRED_CAPACITY)
    ]
    seen: list[str] = []
    original = app._handle_session_event

    def record(event):
        seen.append(str(event.get("tool", event["type"])))
        original(event)

    app._handle_session_event = record  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        with app._pending_ui_events_lock:  # noqa: SLF001
            app._pending_ui_events = [{"type": "warning", "warning": "full"}] * _UI_EVENT_CAPACITY  # noqa: SLF001
            app._pending_ui_event_bytes = sum(app._ui_event_size(event) for event in app._pending_ui_events)  # noqa: SLF001
        for event in accepted:
            session._emit(event)
        with app._pending_ui_events_lock:  # noqa: SLF001
            assert len(app._legacy_deferred_ui_events) == _UI_LEGACY_DEFERRED_CAPACITY  # noqa: SLF001
            assert app._legacy_deferred_ui_event_bytes <= _UI_LEGACY_DEFERRED_MAX_BYTES  # noqa: SLF001
        with pytest.raises(BufferError, match="legacy TUI deferred ingress is full"):
            session._emit({"type": "error", "error": "must not disappear"})

        while app._pending_ui_events or app._legacy_deferred_ui_events:  # noqa: SLF001
            app._flush_pending_ui_events()  # noqa: SLF001
            await pilot.pause()

    assert seen[-len(accepted) :] == [event["tool"] for event in accepted]


@pytest.mark.asyncio
async def test_burst_tool_completions_share_the_30hz_stream_scheduler(tmp_path: Path) -> None:
    """Tool lifecycle completion must not bypass the frame-capped paint path."""
    from one.modes.tui_mode import _UI_FRAME_SECONDS, _OneTextualApp
    from tests.support.tui import _mk_app_session

    app = _OneTextualApp(_mk_app_session(tmp_path))
    paints: list[float] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app._render_stream = lambda: paints.append(time.monotonic())  # type: ignore[method-assign]
        for index in range(32):
            tool = f"burst-{index}"
            app._handle_session_event({"type": "tool_call_start", "tool": tool, "args": {}})
            app._handle_session_event({"type": "tool_call_end", "tool": tool, "ok": True})
        await pilot.pause(_UI_FRAME_SECONDS * 3)

    assert 1 <= len(paints) <= 3
    assert all(later - earlier >= _UI_FRAME_SECONDS * 0.75 for earlier, later in zip(paints, paints[1:]))
