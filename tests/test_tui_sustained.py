# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_sustained_80_tool_detached_workload_is_bounded() -> None:
    path = Path(__file__).parents[1] / "scripts" / "profile_tui_sustained.py"
    spec = importlib.util.spec_from_file_location("tui_sustained", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = await module.run_sustained()
    assert report["tools"] == 80 and set(report["checkpoints"]) == {10, 20, 40, 80}
    assert report["metadataOnly"] and report["baselineCurrentWorkingCode"]
    assert report["resultLinesPerTool"] == 100
    # The heartbeat is a separately scheduled task, so it must advance while
    # twenty keypresses are handled rather than timing that keypress loop.
    assert report["metrics"]["eventLoopDrift"]["count"] > 20
    assert report["metrics"]["inputToHandler"]["count"] == 20
    assert report["metrics"]["inputToAfterRefresh"]["count"] == 20
    assert report["metrics"]["wheel"]["count"] == 1
    assert report["metrics"]["wheel"]["movedAwayFromTail"]
    assert report["idleSpinnerTranscriptRenders"] == 0
    assert report["metrics"]["spinnerGap"]["count"] >= 2
    for checkpoint, row in report["checkpoints"].items():
        assert row["completedToolResults"] >= int(checkpoint)
        assert row["pendingBytes"] >= 0
