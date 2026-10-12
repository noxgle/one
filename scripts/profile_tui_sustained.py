#!/usr/bin/env python3
"""Offline, metadata-only 80-tool detached TUI workload.

This is a virtual-driver baseline, not a physical-terminal measurement.  It
uses the production session, host, facade and TUI command path; only the read
tool body and provider are offline fixtures.  No prompt, result, or evidence
content is reported.
"""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from one.core.agent_runtime_facade import AgentRuntimeFacade
from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.core.threaded_agent_runtime import RuntimeBootstrapSpec
from one.core.tui_runtime_host import TUI_COMMANDS, TuiRuntimeHost
from one.modes.tui_mode import TuiSessionView, _OneTextualApp
from one.providers.base import ChatResult
from one.tools.index import all_tools

TOOLS = 80
CHECKPOINTS = (10, 20, 40, 80)
HEARTBEAT_SECONDS = 0.01


class _Loader:
    def __init__(self, cwd: str) -> None:
        self.cwd = cwd

    def get_system_prompt(self, selected_tools=None):  # noqa: ANN001
        return "offline sustained test"


class _Provider:
    def __init__(self) -> None:
        self.calls = 0
        self.at_40 = threading.Event()
        self.release = threading.Event()

    async def chat(self, *args, on_delta=None, on_thinking_delta=None, **kwargs):  # noqa: ANN001
        self.calls += 1
        if self.calls <= TOOLS:
            if on_thinking_delta:
                on_thinking_delta("waiting")
            await asyncio.sleep(0.055)
            if self.calls == 41:
                self.at_40.set()
                await asyncio.to_thread(self.release.wait)
            return ChatResult(
                text="",
                raw={},
                usage={},
                stop_reason="tool_calls",
                had_native_tool_call=True,
                native_tool_calls=[
                    {"id": f"read-{self.calls}", "name": "read", "arguments": {"path": "offline.txt"}}
                ],
            )
        if on_delta:
            on_delta("completed sustained offline workload")
        return ChatResult(
            text='{"tool":"finish","summary":"completed","goal_success":true}',
            raw={},
            usage={},
            stop_reason="stop",
        )


def _percentiles(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p95Ms": 0.0, "p99Ms": 0.0, "maxMs": 0.0}

    def percentile(percent: float) -> float:
        return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * percent))]

    return {
        "count": len(ordered),
        "p95Ms": round(percentile(0.95), 3),
        "p99Ms": round(percentile(0.99), 3),
        "maxMs": round(ordered[-1], 3),
    }


def _early_late(values: list[float]) -> dict[str, dict[str, float | int]]:
    """Return metadata-only summaries of the first and last workload halves."""
    midpoint = max(1, len(values) // 2)
    return {"early": _percentiles(values[:midpoint]), "late": _percentiles(values[midpoint:])}


async def _wait_for(predicate, *, timeout: float, pilot) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("sustained TUI workload did not reach its bounded checkpoint")
        await pilot.pause(0.01)


async def run_sustained() -> dict[str, Any]:
    """Run the fixed workload and return metadata only (no prompt/result text)."""
    from textual import events
    from textual.containers import VerticalScroll
    from textual.widgets import Static, TextArea

    with tempfile.TemporaryDirectory(prefix="one-sustained-") as raw:
        root = Path(raw)
        (root / "sessions").mkdir()
        provider = _Provider()
        original_read_body = all_tools["read"].fn

        # Keep AgentSession's lifecycle, evidence, and finish implementation
        # real.  The only substituted body is the harmless read operation.
        def read_body(cwd: str, path: str, offset=None, limit=None):  # noqa: ANN001
            return {"ok": True, "output": "\n".join(f"line {index}" for index in range(100))}

        all_tools["read"].fn = read_body

        async def factory(_spec):  # noqa: ANN001
            auth = AuthStorage.in_memory()
            auth.set_runtime_api_key("openai", "offline")
            registry = ModelRegistry.create(auth, str(root / "models.json"))
            model = registry.find("openai", "gpt-4.1")
            assert model is not None
            session = AgentSession(
                SessionManager.create(str(root), str(root / "sessions")),
                SettingsManager.in_memory(
                    {"tools": {"maxSteps": 100}, "bash": {"showOutput": True}, "subagents": {"enabled": True}}
                ),
                registry,
                _Loader(str(root)),
                model,
                "medium",
                tools=["read", "finish"],
            )
            session.providers = {"openai": provider}
            return TuiRuntimeHost(SimpleNamespace(session=session, dispose=session.dispose), None)

        facade = AgentRuntimeFacade(
            RuntimeBootstrapSpec(cwd=str(root), agent_dir=str(root / "agent")), factory, command_allowlist=TUI_COMMANDS
        )
        heartbeat_stop = asyncio.Event()
        heartbeat_drifts: list[float] = []
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            await facade.start()
            view = TuiSessionView(facade)
            await view.refresh()
            app = _OneTextualApp(view)
            app.options["details"] = True
            checkpoints: dict[int, dict[str, int]] = {}
            render_times: list[float] = []
            input_handler_lags: list[float] = []
            input_refresh_lags: list[float] = []
            spinner_frames: list[float] = []
            input_posts: deque[float] = deque()
            wheel_posts: deque[float] = deque()
            wheel_handler_lags: list[float] = []
            applied_reads = 0
            rendered_reads = 0
            finish_ends = 0
            idle_render_count = 0

            async def heartbeat() -> None:
                target = time.monotonic() + HEARTBEAT_SECONDS
                while not heartbeat_stop.is_set():
                    await asyncio.sleep(max(0.0, target - time.monotonic()))
                    now = time.monotonic()
                    heartbeat_drifts.append(max(0.0, (now - target) * 1000))
                    # Measure each scheduled 10ms tick once.  Do not replay a
                    # backlog of missed ticks: that would create a tight loop
                    # and turn one stall into an artificial cumulative lag.
                    target = now + HEARTBEAT_SECONDS

            heartbeat_task = asyncio.create_task(heartbeat())
            original_render = app._render_stream
            original_handle = app._handle_session_event

            def capture_checkpoint() -> None:
                for checkpoint in CHECKPOINTS:
                    if checkpoint <= rendered_reads and checkpoint not in checkpoints:
                        checkpoints[checkpoint] = {
                            "completedToolResults": rendered_reads,
                            "cacheEntries": len(app._stream_content_cache),
                            "cacheBytes": sum(len(str(item).encode("utf-8")) for item in app._stream_content_cache.values()),
                            "lineIds": len(app._stream_line_ids),
                            "pendingEvents": len(app._pending_ui_events),
                            "pendingBytes": app._pending_ui_event_bytes,
                        }

            def rendered() -> None:
                nonlocal rendered_reads
                started = time.monotonic()
                original_render()
                render_times.append((time.monotonic() - started) * 1000)
                rendered_reads = applied_reads
                app.call_after_refresh(capture_checkpoint)

            def handled(event: dict[str, Any]) -> None:
                nonlocal applied_reads, finish_ends
                original_handle(event)
                if event.get("type") == "tool_call_end":
                    if event.get("tool") == "read":
                        applied_reads += 1
                    elif event.get("tool") == "finish":
                        finish_ends += 1

            app._render_stream = rendered  # type: ignore[method-assign]
            app._handle_session_event = handled  # type: ignore[method-assign]

            async with app.run_test(size=(100, 30)) as pilot:
                input_widget = app.query_one("#input", TextArea)
                input_widget.focus()
                input_class = type(input_widget)
                original_input_key = input_class._on_key

                async def input_key(widget, event):  # noqa: ANN001
                    if widget is input_widget and event.key == "x" and input_posts:
                        posted = input_posts.popleft()
                        input_handler_lags.append((time.monotonic() - posted) * 1000)
                        app.call_after_refresh(lambda posted=posted: input_refresh_lags.append((time.monotonic() - posted) * 1000))
                    await original_input_key(widget, event)

                input_class._on_key = input_key

                waiting = app.query_one("#waiting_status", Static)
                original_waiting_update = waiting.update

                def waiting_update(*args, **kwargs):  # noqa: ANN001
                    if args and str(args[0]):
                        spinner_frames.append(time.monotonic())
                    return original_waiting_update(*args, **kwargs)

                waiting.update = waiting_update  # type: ignore[method-assign]
                try:
                    await pilot.press(*"sustained", "enter")
                    await _wait_for(lambda: provider.at_40.is_set() and rendered_reads >= 40, timeout=20, pilot=pilot)
                    assert finish_ends == 0 and app._turn_active

                    container = app.query_one("#stream_container", VerticalScroll)
                    container.scroll_end(animate=False, immediate=True)
                    await pilot.pause(0.05)
                    before_wheel = container.scroll_y
                    container_class = type(container)
                    original_container_wheel_up = container_class._on_mouse_scroll_up

                    def container_wheel_up(self, event):  # noqa: ANN001
                        if self is container and wheel_posts:
                            wheel_handler_lags.append((time.monotonic() - wheel_posts.popleft()) * 1000)
                        return original_container_wheel_up(self, event)

                    container_class._on_mouse_scroll_up = container_wheel_up
                    try:
                        wheel_posts.append(time.monotonic())
                        container.post_message(events.MouseScrollUp(container, 1, 1, 0, -1, 0, False, False, False))
                        await _wait_for(lambda: container.scroll_y < before_wheel, timeout=2, pilot=pilot)
                    finally:
                        container_class._on_mouse_scroll_up = original_container_wheel_up

                    heartbeat_before_keys = len(heartbeat_drifts)
                    for _ in range(20):
                        input_posts.append(time.monotonic())
                        await pilot.press("x")
                    await _wait_for(lambda: len(input_refresh_lags) == 20, timeout=2, pilot=pilot)
                    heartbeat_after_keys = len(heartbeat_drifts)
                    assert input_widget.text.endswith("x" * 20)
                    assert heartbeat_after_keys > heartbeat_before_keys

                    renders_before_idle = len(render_times)
                    await _wait_for(lambda: len(spinner_frames) >= 3, timeout=2, pilot=pilot)
                    idle_render_count = len(render_times) - renders_before_idle
                    assert idle_render_count == 0
                    assert not provider.release.is_set() and finish_ends == 0 and app._turn_active
                    provider.release.set()
                    await _wait_for(lambda: finish_ends == 1 and not app._turn_active, timeout=25, pilot=pilot)
                    await _wait_for(lambda: set(checkpoints) == set(CHECKPOINTS), timeout=2, pilot=pilot)
                finally:
                    input_class._on_key = original_input_key

            assert provider.calls == TOOLS + 1
            assert all(row["completedToolResults"] >= checkpoint for checkpoint, row in checkpoints.items())
            assert all(row["cacheEntries"] <= max(1, row["lineIds"]) for row in checkpoints.values())
            spinner_gaps = [(later - earlier) * 1000 for earlier, later in zip(spinner_frames, spinner_frames[1:])]
            metric_values = {
                "eventLoopDrift": heartbeat_drifts,
                "inputToHandler": input_handler_lags,
                "inputToAfterRefresh": input_refresh_lags,
                "render": render_times,
                "wheel": wheel_handler_lags,
                "spinnerGap": spinner_gaps,
            }
            return {
                "metadataOnly": True,
                "baselineCurrentWorkingCode": True,
                "tools": TOOLS,
                "resultLinesPerTool": 100,
                "checkpoints": checkpoints,
                "metrics": {
                    "eventLoopDrift": _percentiles(heartbeat_drifts),
                    "inputToHandler": _percentiles(input_handler_lags),
                    "inputToAfterRefresh": _percentiles(input_refresh_lags),
                    "render": _percentiles(render_times),
                    "wheel": {**_percentiles(wheel_handler_lags), "movedAwayFromTail": True},
                    "spinnerGap": _percentiles(spinner_gaps),
                },
                "earlyLate": {name: _early_late(values) for name, values in metric_values.items()},
                "definitions": {
                    "heartbeat": "independent 10ms asyncio scheduled-target drift task",
                    "input": "post timestamp to TextArea key handler and after-refresh callback",
                    "wheel": "real MouseScrollUp posted to the stream container from tail",
                    "checkpoint": "read tool_call_end applied, transcript rendered, then after-refresh settled",
                    "idle": "spinner frames update waiting status without transcript renders",
                },
                "idleSpinnerTranscriptRenders": idle_render_count,
                "limitations": ["Textual virtual driver; no physical terminal flush measured"],
            }
        finally:
            provider.release.set()
            heartbeat_stop.set()
            if heartbeat_task is not None:
                await heartbeat_task
            try:
                await facade.close()
            finally:
                facade.finalize()
                all_tools["read"].fn = original_read_body


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sustained", action="store_true", required=True)
    parser.parse_args()
    print(json.dumps(asyncio.run(run_sustained()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
