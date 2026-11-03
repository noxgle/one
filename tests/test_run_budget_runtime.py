"""Test runtime budget enforcement."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.providers.base import ChatResult


class Loader:
    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:
        return "test"


class Provider:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, **kwargs: Any) -> ChatResult:
        self.calls += 1
        return ChatResult(text="A substantial offline answer. " * 20, raw={}, usage={}, stop_reason="stop")


def make_agent(tmp_path: Path, settings: SettingsManager) -> tuple[AgentSession, Provider]:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    agent = AgentSession(SessionManager.in_memory(str(tmp_path)), settings, registry, Loader(), model, "medium")
    provider = Provider()
    agent.providers = {"openai": provider}
    return agent, provider


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("maxTokens", 10_000_000), ("maxTimeSec", 60),
                                         ("maxActiveSubagents", 2)])
async def test_nonzero_limits_are_enforced_not_rejected(tmp_path: Path, field: str, value: int) -> None:
    agent, provider = make_agent(tmp_path, SettingsManager.in_memory({"runBudget": {field: value}}))
    # An explicit signature is required for the adapter's enforceable output cap.
    async def chat(max_tokens: int | None = None, **kwargs: Any) -> ChatResult:
        return await Provider.chat(provider, **kwargs)
    provider.chat = chat
    await agent.prompt("hello")
    assert provider.calls == 1
    assert agent._last_run_context.limits == agent.settings_manager.get_run_limits()


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [None, [], {"maxTokens": True}, {"maxTimeSec": -1},
                                    {"maxCostUsd": float("nan")}, {"extra": 0}])
async def test_invalid_limits_fail_before_prompt(tmp_path: Path, budget: object) -> None:
    agent, provider = make_agent(tmp_path, SettingsManager.in_memory({"runBudget": budget}))
    with pytest.raises(ValueError, match="runBudget"):
        await agent.prompt("hello")
    assert provider.calls == 0
    assert not agent.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("settings", [{}, {"runBudget": {"maxTokens": 0, "maxTimeSec": 0,
                                                            "maxCostUsd": 0, "maxActiveSubagents": 0}}])
async def test_unlimited_prompt_and_compaction_still_work(tmp_path: Path, settings: dict[str, Any]) -> None:
    agent, provider = make_agent(tmp_path, SettingsManager.in_memory(settings))
    await agent.prompt("hello")
    assert provider.calls > 0
    result = await agent.compact()
    assert "aborted" in result


@pytest.mark.asyncio
async def test_file_settings_validate_shadowed_invalid_scope(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    project = tmp_path / "project"
    global_dir.mkdir()
    (project / ".one").mkdir(parents=True)
    (global_dir / "settings.json").write_text(json.dumps({"runBudget": {"maxTokens": -1}}))
    (project / ".one" / "settings.json").write_text(json.dumps({"runBudget": {"maxTokens": 0}}))
    agent, provider = make_agent(tmp_path, SettingsManager(str(project), str(global_dir)))
    with pytest.raises(ValueError, match="global settings: runBudget.maxTokens"):
        await agent.prompt("hello")
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_unreadable_settings_cannot_hide_budget(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "settings.json").write_text('{"runBudget":')
    agent, provider = make_agent(tmp_path, SettingsManager(str(tmp_path), str(global_dir)))
    with pytest.raises(ValueError, match="Cannot validate runBudget: global settings"):
        await agent.prompt("hello")
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_changed_settings_apply_to_next_run(tmp_path: Path) -> None:
    settings = SettingsManager.in_memory()
    agent, provider = make_agent(tmp_path, settings)
    await agent.prompt("first")
    first = agent._last_run_context
    settings._global["runBudget"] = {"maxActiveSubagents": 1}
    await agent.prompt("continue")
    second = agent._last_run_context
    assert second.run_id != first.run_id
    assert first.limits.max_active_subagents == 0
    assert second.limits.max_active_subagents == 1
    assert provider.calls == 2
