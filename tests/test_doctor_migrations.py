# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
from copy import deepcopy
from itertools import product

import pytest

from one.core.doctor import DoctorEngine
from one.core.doctor.migrations import CURRENT_CONFIG_VERSION, migrate
from one.core.settings_manager import SettingsManager


def timeout(global_data, project_data):
    manager = SettingsManager("", "", in_memory=True)
    manager._initial = None
    manager._global = global_data
    manager._project = project_data
    return manager.get_subagents_idle_timeout_sec()


@pytest.mark.parametrize("version", [True, False, None, -1, 1.0, "1", [], {}])
def test_invalid_version_is_untouched(version):
    data = {"configVersion": version, "subagents": {"timeoutSec": 1}}
    assert migrate(data, {}, "global").conflict == "invalid_version"
    assert data == {"configVersion": version, "subagents": {"timeoutSec": 1}}


def test_future_and_per_file_versions():
    future = {"configVersion": CURRENT_CONFIG_VERSION + 1}
    assert migrate(future, {}, "global").conflict == "future_version"
    assert migrate({}, future, "project").conflict == "invalid_context"
    assert migrate({"configVersion": 1}, {}, "global").data == {"configVersion": 1}
    assert migrate({}, {"configVersion": 1}, "project").data == {"configVersion": 1}


def test_synthetic_sequential_registry_and_missing_step():
    def first(data, other, scope):
        data["first"] = 1
        return data

    def second(data, other, scope):
        assert data["configVersion"] == 1
        data["second"] = 2
        return data

    data = {"unknown": {"items": [1]}}
    result = migrate(data, {}, "global", current=2, registry={0: first, 1: second})
    assert result.data == {"unknown": {"items": [1]}, "first": 1, "second": 2, "configVersion": 2}
    assert migrate(result.data, {}, "global", current=2, registry={0: first, 1: second}).data == result.data
    assert migrate(data, {}, "global", current=2, registry={0: first}).data == data
    assert migrate(data, {}, "global", current=2, registry={0: first}).conflict == "missing_step"
    assert data == {"unknown": {"items": [1]}}


# Exhaust all combinations of absent/legacy/new/both in each layer, including
# invalid values and zero. Compare each independent migration to the real getter.
@pytest.mark.parametrize("global_kind,project_kind,value,invalid_scope", product(
    range(4), range(4), [0, 12, -1, "bad", None], ["global", "project"]
))
def test_timeout_layers(global_kind, project_kind, value, invalid_scope):
    def layer(kind, v):
        sub = {"unknown": {"keep": [1, 2]}}
        if kind & 1:
            sub["timeoutSec"] = v
        if kind & 2:
            sub["idleTimeoutSec"] = v
        return {"subagents": sub, "custom": ["keep"]}

    global_data = layer(global_kind, value if invalid_scope == "global" else 7)
    project_data = layer(project_kind, value if invalid_scope == "project" else 7)
    originals = deepcopy((global_data, project_data))
    for scope, data, other in (("global", global_data, project_data), ("project", project_data, global_data)):
        result = migrate(data, other, scope)
        assert (global_data, project_data) == originals
        if result.conflict:
            assert result.conflict == "timeout_conflict"
            assert result.data == data
        else:
            migrated_global, migrated_project = ((result.data, other) if scope == "global" else (other, result.data))
            assert timeout(global_data, project_data) == timeout(migrated_global, migrated_project)
            assert result.data["custom"] == data["custom"]
            assert result.data["subagents"]["unknown"] == data["subagents"]["unknown"]
            assert result.data["subagents"].get("timeoutSec") == data["subagents"].get("timeoutSec")
            assert migrate(result.data, other, scope).data == result.data


def test_conflict_and_malformed_other_are_conservative():
    data = {"subagents": {"timeoutSec": 5}}
    assert migrate(data, {"subagents": {"timeoutSec": 7}}, "global").conflict == "timeout_conflict"
    assert migrate(data, {"subagents": None}, "global").conflict == "timeout_conflict"


def test_doctor_uses_registry_versions_and_reports_conflict(tmp_path, monkeypatch):
    agent = tmp_path / "agent"
    project = tmp_path / "project"
    agent.mkdir()
    (project / ".one").mkdir(parents=True)
    (agent / "settings.json").write_text(json.dumps({"subagents": {"timeoutSec": 5}}))
    (project / ".one" / "settings.json").write_text(json.dumps({
        "configVersion": 1, "subagents": {"timeoutSec": 7}, "unknown": "secret"
    }))
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(agent))
    report = DoctorEngine().check(project).to_json()
    issues = {(item["scope"], item["id"]) for item in json.loads(report)["issues"]}
    assert ("global", "settings.timeout_migration_conflict") in issues
    assert ("project", "settings.unknown") in issues
    assert ("project", "settings.version_future") not in issues
    assert "secret" not in report
