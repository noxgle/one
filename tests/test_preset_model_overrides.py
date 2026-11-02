"""Offline validation and precedence tests for per-preset model settings."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json

import pytest

from one.core.presets import PresetDefinition, PresetError, apply_preset_overrides, preset_model_details
from one.core.settings_manager import SettingsManager


@pytest.mark.parametrize("value", [None, [], {"other": {}}, {"overrides": []},
    {"overrides": {"BAD": {}}}, {"overrides": {"foo": None}},
    {"overrides": {"foo": {"tools": []}}}, {"overrides": {"foo": {"model": "bad"}}},
    {"overrides": {"foo": {"model": "openai/ bad"}}}])
def test_invalid_overrides(value: object) -> None:
    settings = SettingsManager.in_memory({"presets": value})
    with pytest.raises(PresetError, match="settings:"):
        settings.get_preset_overrides()


def test_project_precedence_and_null(tmp_path) -> None:
    agent = tmp_path / "agent"
    project = tmp_path / "project"
    agent.mkdir()
    (project / ".one").mkdir(parents=True)
    (agent / "settings.json").write_text(json.dumps({"presets": {"overrides": {"foo": {"model": "openai/gpt-4o"}}}}))
    path = project / ".one" / "settings.json"
    preset = PresetDefinition("foo", "Foo", "", "test", model="openai/gpt-4.1")
    path.write_text(json.dumps({"presets": {"overrides": {"foo": {"model": "anthropic/claude"}}}}))
    assert apply_preset_overrides(preset, SettingsManager.create(str(project), str(agent))).model == "anthropic/claude"
    assert preset_model_details(preset, SettingsManager.create(str(project), str(agent)))["modelSource"] == "project settings"
    path.write_text(json.dumps({"presets": {"overrides": {"foo": {"model": None}}}}))
    assert apply_preset_overrides(preset, SettingsManager.create(str(project), str(agent))).model == preset.model
    assert preset_model_details(preset, SettingsManager.create(str(project), str(agent)))["modelSource"] == "preset"
    path.write_text("{}")
    assert apply_preset_overrides(preset, SettingsManager.create(str(project), str(agent))).model == "openai/gpt-4o"


def test_invalid_global_override_is_not_hidden_by_project(tmp_path) -> None:
    agent = tmp_path / "agent"
    project = tmp_path / "project"
    agent.mkdir()
    (project / ".one").mkdir(parents=True)
    (agent / "settings.json").write_text(json.dumps({"presets": {"overrides": {"explore": {"model": False}}}}))
    (project / ".one" / "settings.json").write_text(
        json.dumps({"presets": {"overrides": {"explore": {"model": "openai/gpt-4o"}}}})
    )
    with pytest.raises(PresetError, match="global settings"):
        SettingsManager.create(str(project), str(agent)).get_preset_overrides()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_child_override_precedence(tmp_path, explicit) -> None:
    from tests.test_subagents import _mk_agent

    agent = _mk_agent(tmp_path, tools=["read", "finish", "spawn_subagent"], settings_override={
        "presets": {"overrides": {"explore": {"model": "openai/gpt-4o"}}}
    })
    agent.resource_loader.agent_dir = str(tmp_path)
    original = agent.model
    captured = {}

    async def capture(task, model, tools, depth, **kwargs):
        captured["model"] = model
        return {"ok": True}

    agent._run_subagent = capture
    args = {"task": "find facts", "preset": "explore"}
    if explicit:
        args["model"] = "openai/gpt-4.1"
    assert (await agent._spawn_subagent(args))["ok"]
    assert captured["model"].id == ("gpt-4.1" if explicit else "gpt-4o")
    assert agent.model is original


@pytest.mark.asyncio
async def test_child_unavailable_override_does_not_fall_back(tmp_path) -> None:
    from tests.test_subagents import _mk_agent

    agent = _mk_agent(tmp_path, settings_override={
        "presets": {"overrides": {"explore": {"model": "missing-provider/model"}}}
    })
    agent.resource_loader.agent_dir = str(tmp_path)
    result = await agent._spawn_subagent({"task": "find facts", "preset": "explore"})
    assert result["ok"] is False
