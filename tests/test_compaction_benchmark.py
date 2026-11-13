# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

from scripts import bench_compaction as bench

ROOT = Path(__file__).resolve().parents[1]


def test_offline_deterministic_and_failure_safety(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    first = asyncio.run(bench.run())
    assert first == asyncio.run(bench.run())
    rows = first["results"]
    assert len(rows) == 12
    for row in rows:
        assert row["requests"] == 1
        assert row["constraint_total"] == 1
        assert row["latency_ms"] is None
        if row["provider"] == "failure":
            assert row["failures"] == 1 and row["synthetic_usage"] is None
            assert row["usage_covered_requests"] == 0
            assert row["failure_history_changed"] == (row["mode"] == "legacy")
        else:
            assert row["failures"] == 0 and row["constraint_exact_retained"] == 1
            assert row["task_success_proxy"] == 1 and row["usage_covered_requests"] == 1
            assert set(row["synthetic_usage"]) == {"input", "cached", "output", "reasoning"}
    output = json.dumps(first)
    assert bench.CONSTRAINT not in output and bench.MARKER not in output
    assert "synthetic provider failure" not in output


def test_cli_json_schema(tmp_path):
    env = {**os.environ, "ONE_CODING_AGENT_DIR": str(tmp_path)}
    result = subprocess.run([sys.executable, "scripts/bench_compaction.py"], cwd=ROOT,
                            env=env, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    assert not result.stderr
    assert (data["schema_version"], data["fixture_schema"], data["seed"]) == (1, 1, 0)
    assert len(data["results"]) == 12
    assert str(tmp_path) not in result.stdout and str(ROOT) not in result.stdout
    assert bench.CONSTRAINT not in result.stdout and bench.MARKER not in result.stdout


def test_provenance_and_measured_latency(monkeypatch):
    first = bench.provenance()
    assert len(first["content_sha256"]) == 64
    original = Path.read_bytes

    def changed(path):
        data = original(path)
        return data + b"changed" if path.name == "small.json" else data

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert bench.provenance()["content_sha256"] != first["content_sha256"]
    measured = asyncio.run(bench.run(measure_latency=True))
    assert all(isinstance(row["latency_ms"], float) and row["latency_ms"] >= 0 for row in measured["results"])
    assert "not provider latency" in measured["latency_note"]
