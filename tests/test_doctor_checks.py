# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
from pathlib import Path

import pytest

from one.core.doctor import DoctorEngine
from one.core.settings_manager import SettingsManager


@pytest.mark.parametrize(
    ("global_sub", "project_sub", "expected", "source", "issue"),
    [
        ({"timeoutSec": 12}, {}, 12, "global", "settings.timeout_legacy_effective"),
        ({"idleTimeoutSec": 23}, {"timeoutSec": 12}, 23, "global", "settings.timeout_idle_effective"),
        ({"timeoutSec": 12}, {"idleTimeoutSec": 0}, 0, "project", "settings.timeout_idle_effective"),
    ],
)
def test_timeout_source_matches_getter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, global_sub: dict, project_sub: dict,
    expected: int, source: str, issue: str,
) -> None:
    agent = tmp_path / "agent"
    project = tmp_path / "project"
    agent.mkdir()
    (project / ".one").mkdir(parents=True)
    (agent / "settings.json").write_text(json.dumps({"subagents": global_sub}))
    (project / ".one" / "settings.json").write_text(json.dumps({"subagents": project_sub}))
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(agent))
    manager = SettingsManager.read_only(str(project), str(agent))
    assert manager.get_subagents_idle_timeout_sec() == expected
    report = json.loads(DoctorEngine().check(project).to_json())
    assert any(item["id"] == issue and item["scope"] == source for item in report["issues"])
    if global_sub.get("idleTimeoutSec") is not None and "timeoutSec" in project_sub:
        assert any(item["id"] == "settings.timeout_legacy_ignored" and item["scope"] == "project"
                   for item in report["issues"])


@pytest.mark.parametrize(
    ("global_sub", "project_sub", "expected", "selected", "extra"),
    [
        ({"idleTimeoutSec": 0, "timeoutSec": 9}, {"timeoutSec": 0}, 0,
         ("settings.timeout_idle_effective", "global"), ("settings.timeout_legacy_ignored", "project")),
        ({"idleTimeoutSec": 9}, {"idleTimeoutSec": 0}, 0,
         ("settings.timeout_idle_effective", "project"), ("settings.timeout_idle_shadowed", "global")),
        ({"timeoutSec": 0}, {"timeoutSec": -1}, 1800,
         ("settings.timeout_legacy_effective", "project"), ("settings.timeout_fallback", "project")),
        ({}, {}, 1800, ("settings.timeout_default", "global"), ("settings.timeout_default", "global")),
    ],
)
def test_mixed_timeout_layers(tmp_path, monkeypatch, global_sub, project_sub, expected, selected, extra):
    agent, project = tmp_path / "agent", tmp_path / "project"
    agent.mkdir()
    (project / ".one").mkdir(parents=True)
    (agent / "settings.json").write_text(json.dumps({"subagents": global_sub, "tools": {"maxSteps": 4},
                                                     "private-canary": "secret-canary"}))
    (project / ".one" / "settings.json").write_text(json.dumps({"subagents": project_sub,
                                                                "tools": {"maxSteps": 0}}))
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(agent))
    assert SettingsManager.read_only(str(project), str(agent)).get_subagents_idle_timeout_sec() == expected
    report = DoctorEngine().check(project)
    text = report.to_json()
    assert text == DoctorEngine().check(project).to_json()
    items = {(item["id"], item["scope"]) for item in json.loads(text)["issues"]}
    assert selected in items and extra in items
    assert ("settings.selected.tools.maxSteps", "project") in items
    assert ("settings.override", "project") in items
    assert "private-canary" not in text and "secret-canary" not in text
    assert "private-canary" not in report.to_text() and "secret-canary" not in report.to_text()


def test_layer_validation_and_redaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = tmp_path / "agent"
    project = tmp_path / "project"
    agent.mkdir()
    (project / ".one").mkdir(parents=True)
    (agent / "settings.json").write_text(json.dumps({
        "defaultThinkingLevel": "secret-invalid", "runBudget": {"maxTokens": -1},
        "tui": {"manualBashMode": "dev"}, "subagents": {"idleTimeoutSec": -1},
    }))
    (project / ".one" / "settings.json").write_text(json.dumps({
        "tui": {"manualBashMode": "strict", "toolResultPreviewLines": 0},
        "mcpServers": {"private-server": {}}, "presets": {}, "runBudget": {},
    }))
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(agent))
    manager = SettingsManager.read_only(str(project), str(agent))
    assert manager.get_subagents_idle_timeout_sec() == 1800
    assert manager.get_tui_manual_bash_mode() == "dev"
    assert manager.get_tui_tool_result_preview_lines() == 100
    text = DoctorEngine().check(project).to_json()
    issues = json.loads(text)["issues"]
    ids = {(i["id"], i["scope"]) for i in issues}
    assert ("settings.invalid_value", "global") in ids
    assert ("settings.invalid_value", "project") in ids
    assert ("settings.manual_bash_ignored", "project") in ids
    assert ("settings.timeout_fallback", "global") in ids
    assert not any(i["id"] == "settings.unknown" for i in issues)
    assert "secret-invalid" not in text and "private-server" not in text


@pytest.mark.parametrize("scope", ["global", "project"])
def test_nested_settings_inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str) -> None:
    agent = tmp_path / "agent"
    project = tmp_path / "project"
    agent.mkdir()
    (project / ".one").mkdir(parents=True)
    path = agent / "settings.json" if scope == "global" else project / ".one" / "settings.json"
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(agent))
    settings = {
        "tools": {"lastPositiveMaxSteps": 23},
        "subagents": {"timeoutSec": "42"},
        "compaction": {"thresholdPercent": 75.5},
        "webConnect": {"timeoutSec": 1.5},
        "thinkingBudgets": {"custom-model": {"arbitrary": 1}},
        "mcpServers": {"custom-server": {"command": "example", "env": {"CUSTOM": "value"}}},
    }
    path.write_text(json.dumps(settings))
    manager = SettingsManager.read_only(str(project), str(agent))
    assert manager.get_subagents_idle_timeout_sec() == 42
    assert manager.get_web_connect_settings()["timeoutSec"] == 1.5
    issues = json.loads(DoctorEngine().check(project).to_json())["issues"]
    assert not any(i["id"] in {"settings.nested_unknown", "settings.nested_type"} for i in issues)

    settings["tools"]["unrecognized"] = "private-value"
    settings["tui"] = {"toolResultPreviewLines": False}
    path.write_text(json.dumps(settings))
    report = DoctorEngine().check(project).to_json()
    ids = {(i["id"], i["scope"]) for i in json.loads(report)["issues"]}
    assert ("settings.nested_unknown", scope) in ids
    assert ("settings.nested_type", scope) in ids
    assert "private-value" not in report and "unrecognized" not in report


@pytest.mark.parametrize("scope", ["global", "project"])
@pytest.mark.parametrize(
    ("key", "value", "issue"),
    [
        ("runBudget", {"maxTokens": True}, "settings.run_budget_invalid"),
        ("runBudget", {"maxCostUsd": -1}, "settings.run_budget_invalid"),
        ("runBudget", {"canary-secret": 1}, "settings.run_budget_invalid"),
        ("presets", {"overrides": {"canary-secret": {"model": "invalid"}}}, "settings.presets_invalid"),
        ("presets", {"overrides": []}, "settings.presets_invalid"),
        ("mcpServers", {"canary-secret": {"command": "x", "env": [1]}}, "settings.mcp_invalid"),
        ("mcpServers", {"canary-secret": {}}, "settings.mcp_invalid"),
    ],
)
def test_extensible_maps_invalid(tmp_path, monkeypatch, scope, key, value, issue):
    agent, project = tmp_path / "agent", tmp_path / "project"
    agent.mkdir()
    (project / ".one").mkdir(parents=True)
    path = agent / "settings.json" if scope == "global" else project / ".one" / "settings.json"
    path.write_text(json.dumps({key: value}))
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(agent))
    report = DoctorEngine().check(project).to_json()
    assert (issue, scope) in {(i["id"], i["scope"]) for i in json.loads(report)["issues"]}
    assert "canary-secret" not in report
    assert "check.failed" not in report


def test_extensible_maps_merge_like_consumers(tmp_path, monkeypatch):
    from one.mcp.client import McpManager

    agent, project = tmp_path / "agent", tmp_path / "project"
    agent.mkdir()
    (project / ".one").mkdir(parents=True)
    (agent / "settings.json").write_text(json.dumps({
        "runBudget": {"maxTokens": 10}, "presets": {"overrides": {"custom": {"model": "local/model"}}},
        "mcpServers": {"custom": {"command": "example", "env": {"TOKEN": "secret"}}},
    }))
    (project / ".one" / "settings.json").write_text(json.dumps({
        "runBudget": {"maxTimeSec": 2}, "presets": {"overrides": {"other": {"model": None}}},
        "mcpServers": {"custom": {"args": ["--flag"]}, "http": {"url": "https://example.invalid"}},
    }))
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(agent))
    manager = SettingsManager.read_only(str(project), str(agent))
    assert manager.get_run_limits().max_tokens == 10
    assert set(manager.get_preset_overrides()) == {"custom", "other"}
    assert len(McpManager.create(manager)._servers) == 2
    report = DoctorEngine().check(project).to_json()
    assert not any(i["id"] in {"settings.run_budget_invalid", "settings.presets_invalid", "settings.mcp_invalid",
                                "settings.unknown", "check.failed"} for i in json.loads(report)["issues"])
    assert "secret" not in report


@pytest.mark.parametrize("scope", ["global", "project"])
@pytest.mark.parametrize(("setting", "invalid"), [
    ({"context": {"policy": "canary-secret"}}, True),
    ({"context": {"policy": "provider-default"}}, False),
    ({"generation": {"maxOutputTokens": 0}}, True),
    ({"generation": {"maxOutputTokens": -1}}, True),
    ({"generation": {"maxOutputTokens": 1}}, False),
    ({"defaultMode": "canary-secret"}, True),
    ({"defaultMode": "CLI"}, False),
    ({"toolOutputPruning": {"recentTokens": -1}}, True),
    ({"toolOutputPruning": {"recentTokens": 0}}, False),
    ({"toolOutputPruning": {"minResultTokens": 0}}, True),
    ({"toolOutputPruning": {"minResultTokens": 1}}, False),
    ({"toolOutputPruning": {"marker": " "}}, True),
    ({"toolOutputPruning": {"marker": "x" * 513}}, True),
    ({"toolOutputPruning": {"marker": "x" * 512}}, False),
    ({"providers": {"timeoutSec": 0}}, True),
    ({"providers": {"timeoutSec": 10}}, False),
    ({"askUser": {"timeoutSec": -1}}, True),
    ({"askUser": {"timeoutSec": 0}}, False),
    ({"subagents": {"maxDurationSec": "canary-secret"}}, True),
    ({"subagents": {"maxDurationSec": "-1"}}, True),
    ({"subagents": {"maxDurationSec": None}}, False),
    ({"subagents": {"maxDurationSec": "0"}}, False),
])
def test_getter_normalization_diagnostics(tmp_path, monkeypatch, scope, setting, invalid):
    agent, project = tmp_path / "agent", tmp_path / "project"
    agent.mkdir()
    (project / ".one").mkdir(parents=True)
    path = agent / "settings.json" if scope == "global" else project / ".one" / "settings.json"
    path.write_text(json.dumps(setting))
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(agent))
    report = DoctorEngine().check(project).to_json()
    issues = json.loads(report)["issues"]
    assert (("settings.invalid_value", scope) in {(i["id"], i["scope"]) for i in issues}) is invalid
    assert "canary-secret" not in report
    assert "check.failed" not in report


def test_invalid_shadowed_values_and_effective_getters(tmp_path, monkeypatch):
    agent, project = tmp_path / "agent", tmp_path / "project"
    agent.mkdir()
    (project / ".one").mkdir(parents=True)
    (agent / "settings.json").write_text(json.dumps({
        "context": {"policy": "canary-secret"}, "generation": {"maxOutputTokens": 0},
    }))
    project_settings = project / ".one" / "settings.json"
    project_settings.write_text(json.dumps({
        "context": {"policy": "provider-default"}, "generation": {"maxOutputTokens": 1},
    }))
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(agent))
    manager = SettingsManager.read_only(str(project), str(agent))
    assert manager.get_context_policy() == "provider-default"
    assert manager.get_explicit_generation_max_output_tokens() == 1
    issues = json.loads(DoctorEngine().check(project).to_json())["issues"]
    assert ("settings.invalid_value", "global") in {(i["id"], i["scope"]) for i in issues}
    assert ("settings.invalid_value", "project") not in {(i["id"], i["scope"]) for i in issues}
    project_settings.write_text('{}')
    manager = SettingsManager.read_only(str(project), str(agent))
    assert manager.get_context_policy() == "endpoint-max"
    assert manager.get_generation_max_output_tokens() == 16384
    assert manager.get_explicit_generation_max_output_tokens() is None


@pytest.mark.parametrize("scope", ["global", "project"])
def test_provider_profiles_are_known_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str,
) -> None:
    agent = tmp_path / "agent"
    project = tmp_path / "project"
    agent.mkdir()
    (project / ".one").mkdir(parents=True)
    path = agent / "settings.json" if scope == "global" else project / ".one" / "settings.json"
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(agent))
    path.write_text(json.dumps({
        "providerProfiles": {"chatgpt": {"experimentalStableSessionId": False}},
    }))
    manager = SettingsManager.read_only(str(project), str(agent))
    assert manager.get_chatgpt_stable_session_id() is False
    issues = json.loads(DoctorEngine().check(project).to_json())["issues"]
    ids = {issue["id"] for issue in issues}
    assert "settings.unknown" not in ids
    assert "settings.nested_unknown" not in ids
    assert "settings.type.providerProfiles" not in ids
