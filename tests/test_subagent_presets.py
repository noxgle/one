"""Offline regression coverage for explicit subagent preset selection."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.test_subagents import _Loader, _mk_agent


@pytest.mark.asyncio
async def test_child_preset_resolves_configuration_without_mutating_parent(tmp_path: Path) -> None:
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "focused.md").write_text(
        "---\nversion: 1\nid: focused\nname: Focused\nmodel: openai/gpt-4o\n"
        "temperature: 0.2\nthinking: high\ntools: [read, finish]\n---\nCHILD ROLE\n",
        encoding="utf-8",
    )
    agent = _mk_agent(tmp_path, tools=["read", "bash", "finish", "spawn_subagent"])
    agent.resource_loader = _Loader()
    agent.resource_loader.agent_dir = str(tmp_path)  # type: ignore[attr-defined]
    original = (agent.model, agent.temperature, agent.thinking_level, list(agent.active_tools))
    captured: dict[str, Any] = {}

    async def capture(task: str, model: Any, tools: list[str], depth: int, **kwargs: Any) -> dict[str, Any]:
        captured.update(task=task, model=model, tools=tools, depth=depth, **kwargs)
        return {"ok": True, "summary": "done"}

    agent._run_subagent = capture  # type: ignore[method-assign]
    result = await agent._spawn_subagent({"task": "child", "preset": "focused"})

    assert result["ok"] is True
    assert (captured["model"].provider, captured["model"].id) == ("openai", "gpt-4o")
    assert captured["temperature"] == 0.2
    assert captured["thinking_level"] == "high"
    assert captured["tools"] == ["read", "finish"]
    assert captured["preset"].body == "CHILD ROLE\n"
    assert (agent.model, agent.temperature, agent.thinking_level, agent.active_tools) == original


def test_flat_subagent_call_accepts_explicit_preset(tmp_path: Path) -> None:
    agent = _mk_agent(tmp_path, tools=["spawn_subagent", "finish"])
    assert agent._try_parse_tool_call('{"tool":"spawn_subagent","task":"child","preset":"focused"}') == {
        "tool": "spawn_subagent", "args": {"task": "child", "preset": "focused"}, "toolCallId": None,
    }


def test_child_preset_metadata_is_bounded_literal_and_only_when_spawn_visible(tmp_path: Path) -> None:
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "review.md").write_text(
        "---\nversion: 1\nid: review\nname: Review <literal>\n"
        "description: Review changed files concisely\n---\nSECRET BODY MUST NOT APPEAR\n",
        encoding="utf-8",
    )
    agent = _mk_agent(tmp_path, tools=["spawn_subagent", "finish"])
    agent.resource_loader.agent_dir = str(tmp_path)  # type: ignore[attr-defined]
    prompt = agent._build_runtime_system_prompt()
    assert '"review": "Review <literal>"' in prompt
    assert "Review changed files concisely" in prompt
    assert "SECRET BODY MUST NOT APPEAR" not in prompt
    assert str(tmp_path) not in prompt

    agent._subagents_hard_disabled = True
    assert "# Child Presets" not in agent._build_runtime_system_prompt()
