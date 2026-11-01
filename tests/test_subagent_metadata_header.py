"""Verify metadata reaches real child provider requests, not just loader output."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from one.providers.base import ChatResult
from one.resources import resource_loader
from one.resources.resource_loader import DefaultResourceLoader
from tests.test_subagents import _mk_agent


class _RecordingProvider:
    def __init__(self, nested: bool, preset: str | None) -> None:
        self.nested = nested
        self.preset = preset
        self.requests: list[tuple[str, str]] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ChatResult:
        task = next(message["content"] for message in messages if message["role"] == "user")
        system = messages[0]["content"]
        first = not any(previous_task == task for previous_task, _ in self.requests)
        self.requests.append((task, system))
        if self.nested and task == "child task" and first:
            args = {"task": "grandchild task"}
            if self.preset is not None:
                args["preset"] = self.preset
            response = {"tool": "spawn_subagent", "args": args}
        else:
            response = {"tool": "finish", "args": {"summary": "done", "goal_success": True}}
        return ChatResult(text=json.dumps(response), raw={}, usage={}, stop_reason="stop")


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", [None, "default", "focused"])
@pytest.mark.parametrize("nested", [False, True])
async def test_child_provider_receives_metadata_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preset: str | None, nested: bool,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent_dir = tmp_path / "agent"
    presets = agent_dir / "presets"
    presets.mkdir(parents=True)
    (presets / "focused.md").write_text(
        "---\nversion: 1\nid: focused\nname: Focused\ntools: [read, spawn_subagent, finish]\n---\n"
        "FOCUSED CHILD ROLE\n", encoding="utf-8",
    )
    monkeypatch.setattr(resource_loader, "_current_date", lambda: "2030-02-03")
    monkeypatch.setattr(resource_loader, "_system_env", lambda: "Test OS 42")
    monkeypatch.setattr(resource_loader, "_user_privileges", lambda: "user(test)")
    agent = _mk_agent(workspace, tools=["read", "bash", "spawn_subagent", "finish"])
    loader = DefaultResourceLoader(
        str(workspace), str(agent_dir), agent.settings_manager,
        no_extensions=True, no_skills=True, no_prompt_templates=True, no_themes=True,
    )
    # No reload/discovery: avoid ambient project files, skills, extensions or host probes.
    agent.resource_loader = loader
    provider = _RecordingProvider(nested, preset)
    agent.providers = {"openai": provider}
    args = {"task": "child task"}
    if preset is not None:
        args["preset"] = preset
    try:
        result = await agent._spawn_subagent(args)
        assert result["ok"] is True, result
        assert {task for task, _ in provider.requests} == (
            {"child task", "grandchild task"} if nested else {"child task"}
        )
        expected_header = (
            "Current date: 2030-02-03\n"
            f"workspace={workspace.as_posix()}\n"
            "env=Test OS 42\n"
            "user_privileges=user(test)\n"
        )
        for task, system in provider.requests:
            assert system.startswith(expected_header)
            for line in expected_header.splitlines():
                assert system.splitlines().count(line) == 1
            # Metadata stays in the system prompt, not the delegated task text.
            assert task in {"child task", "grandchild task"}
            if preset == "focused":
                assert "FOCUSED CHILD ROLE" in system
                assert "- read {path" in system
                assert "- bash {command" not in system
            else:
                assert "FOCUSED CHILD ROLE" not in system
                assert "- bash {command" in system
    finally:
        await agent.dispose()
