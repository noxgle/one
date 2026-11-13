#!/usr/bin/env python3
"""Deterministic, offline compaction experiment; output only aggregate metrics."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Support direct invocation from the repository root without installing ONE.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from one.core.agent_session import AgentSession  # noqa: E402
from one.core.auth_storage import AuthStorage  # noqa: E402
from one.core.model_registry import ModelRegistry  # noqa: E402
from one.core.session_manager import SessionManager  # noqa: E402
from one.core.settings_manager import SettingsManager  # noqa: E402

CONSTRAINT = "Never delete synthetic data."
MARKER = "synthetic objective marker"
SETTINGS = {"recentTokens": 128, "minKeptMessages": 2, "summarizeWithModel": True,
            "strategy": "local_separate", "rehydrate": {"enabled": False}}


class Loader:
    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:
        return "Synthetic benchmark agent."


class FakeProvider:
    """Intercept the provider boundary; counters are invented, not provider telemetry."""

    def __init__(self, fail: bool):
        self.fail = fail
        self.requests = 0
        self.usage = dict.fromkeys(("input", "cached", "output", "reasoning"), 0)
        self.covered = 0

    async def invoke(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        self.requests += 1
        if self.fail:
            raise RuntimeError("synthetic provider failure")
        prompt = messages[-1]["content"]
        # The fake copies the supplied ledger, as a compliant synthetic model would.
        ledger = prompt.split("user constraint ledger exactly (including provenance):\n", 1)
        ledger = ledger[1].split("\nAdditional summary instruction", 1)[0] if len(ledger) == 2 else CONSTRAINT
        text = (f"# Objective\n{MARKER}\n# Constraints\n{ledger or 'None'}\n"
                "# Work State\n## Completed\nNone\n## Active\nSynthetic work\n## Blocked\nNone\n"
                "# Next Move\nContinue\n# Relevant Files\nNone")
        usage = {"input": (sum(len(str(m.get("content", ""))) for m in messages) + 3) // 4,
                 "cached": 16, "output": (len(text) + 3) // 4, "reasoning": 8}
        for key, value in usage.items():
            self.usage[key] += value
        self.covered += 1
        return {"content": text, "_compactionUsage": {"inputTokens": usage["input"],
                "cachedTokens": usage["cached"], "outputTokens": usage["output"],
                "reasoningTokens": usage["reasoning"]}}


def fixture(label: str) -> list[dict[str, Any]]:
    """Schema 1, seed 0: fixed alternating turns; tool-heavy adds paired calls/results."""
    turns = {"small": 6, "medium": 30, "tool-heavy": 30}[label]
    messages: list[dict[str, Any]] = [{"role": "user", "content": f"{MARKER}\n{CONSTRAINT}"}]
    for i in range(turns):
        messages.append({"role": "assistant", "content": f"synthetic response {i} " + "x" * 100})
        if label == "tool-heavy":
            messages[-1]["_nativeToolCalls"] = [{"id": f"c{i}", "name": "read", "arguments": {}}]
            messages.append({"role": "toolResult", "content": "synthetic observation " + "y" * 150,
                             "_nativeToolCallId": f"c{i}"})
        messages.append({"role": "user", "content": f"synthetic turn {i}"})
    return messages


async def scenario(label: str, v2: bool, fail: bool, cwd: str) -> dict[str, Any]:
    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"compaction": {**SETTINGS, "experimentalV2": v2}})
    agent = AgentSession(SessionManager.in_memory(cwd), settings, registry, Loader(), model, "medium")
    for message in fixture(label):
        agent.session_manager.append_message(message)
    agent.messages = agent.session_manager.build_session_context()["messages"]
    before = copy.deepcopy(agent.messages)
    before_tokens = sum(agent._approx_message_tokens(m) for m in before)
    fake = FakeProvider(fail)
    agent._invoke_provider = fake.invoke
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    result = await agent.compact()
    after_tokens = sum(agent._approx_message_tokens(m) for m in agent.messages)
    # Inspect only in memory; neither summaries nor exception messages leave this process.
    content = str(agent.messages)
    failure = any(event.get("type") == "compaction_failed" for event in events)
    return {"scenario": label, "mode": "v2" if v2 else "legacy", "provider": "failure" if fail else "success",
            "requests": fake.requests, "synthetic_usage": fake.usage if fake.covered == fake.requests else None,
            "usage_covered_requests": fake.covered, "failures": int(failure),
            "skipped": bool(result.get("skipped")), "constraint_exact_retained": int(CONSTRAINT in content),
            "constraint_total": 1, "task_success_proxy": int(MARKER in content),
            "failure_history_changed": int(fail and agent.messages != before),
            "failure_original_messages_missing": sum(m not in agent.messages for m in before) if fail else 0,
            "context_tokens_before_estimate": before_tokens, "context_tokens_after_estimate": after_tokens,
            "latency_ms": None}


def provenance() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    paths = sorted([*root.joinpath("one").rglob("*.py"),
                    *root.joinpath("tests/fixtures/compaction").rglob("*.json"), Path(__file__).resolve()])
    digest = hashlib.sha256()
    for path in paths:
        name = path.relative_to(root).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big") + name + len(content).to_bytes(8, "big") + content)
    try:
        result = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=root,
                                capture_output=True, text=True, check=True, timeout=5)
        head = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        head = None
    return {"base_head": head, "content_sha256": digest.hexdigest(),
            "scope": "one/**/*.py, tests/fixtures/compaction/**/*.json, scripts/bench_compaction.py"}


async def run(measure_latency: bool = False) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as cwd:
        rows = []
        for label in ("small", "medium", "tool-heavy"):
            for fail in (False, True):
                for v2 in (False, True):
                    start = time.perf_counter() if measure_latency else 0.0
                    row = await scenario(label, v2, fail, cwd)
                    if measure_latency:
                        row["latency_ms"] = round((time.perf_counter() - start) * 1000, 3)
                    rows.append(row)
    return {"schema_version": 1, "fixture_schema": 1, "seed": 0,
            "provenance": provenance(), "model": "synthetic-fake",
            "settings": {**SETTINGS, "thinking": "medium", "other_settings": "application defaults"},
            "usage_note": "Synthetic counters, including cached tokens; not cache evidence or pricing.",
            "task_success_note": "Marker survival proxy only, not real task accuracy.",
            "latency_note": ("Offline scenario wall time; not provider latency." if measure_latency
                             else "Not measured for determinism."), "results": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--measure-latency", action="store_true", help="Measure offline scenario wall time")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.measure_latency)), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
