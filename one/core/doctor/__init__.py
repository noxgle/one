"""Offline, read-only diagnostics for ONE."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import sys
from pathlib import Path

from one.config import VERSION, get_agent_dir_candidates
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.presets import PresetError, validate_preset_overrides
from one.core.run_context import RunLimits
from one.core.settings_manager import DEFAULT_SETTINGS, THINKING_LEVELS, SettingsManager
from one.core.temperature import normalize_temperature

from .migrations import migrate, version_error
from .readiness import assess_provider_model
from .reports import ISSUE_REGISTRY, DoctorIssue, DoctorReport
from .reports import RepairAction as RepairAction
from .reports import safe_diff as safe_diff

# Only maps with an inventoried set of fields belong here. Open-ended maps
# (such as thinkingBudgets and mcpServers) must not be inferred from defaults.
_NESTED_SETTINGS_TYPES = {
    "context": {"policy": str},
    "generation": {"maxOutputTokens": int},
    "compaction": {"enabled": bool, "thresholdPercent": (int, float), "recentTokens": int,
                   "minKeptMessages": int, "summarizeWithModel": bool, "maxSummaryInputTokens": int},
    "toolOutputPruning": {"enabled": bool, "recentTokens": int, "minResultTokens": int, "marker": str},
    "retry": {"enabled": bool, "mode": str, "maxRetries": int, "baseDelayMs": int, "maxDelayMs": int},
    "image": {"autoResize": bool, "blockImages": bool},
    "tui": {"infoPanel": str, "manualBashMode": str, "toolResultPreviewLines": int},
    "tools": {"maxSteps": int, "lastPositiveMaxSteps": int, "timeoutSec": int, "approval": bool,
              "approvalTools": list},
    "bash": {"showOutput": bool},
    "webConnect": {"timeoutSec": (int, float), "maxResponseBytes": int, "maxOutputChars": int, "maxRedirects": int,
                   "extractMainContent": bool, "preserveLinks": bool, "preserveCodeBlocks": bool},
    "subagents": {"enabled": bool, "maxConcurrent": int, "maxDepth": int, "timeoutSec": (int, float, str, bool, type(None)),
                  "idleTimeoutSec": (int, float, str, bool, type(None)),
                  "maxDurationSec": (int, float, str, bool, type(None))},
    "askUser": {"timeoutSec": int},
    "providers": {"timeoutSec": int},
    "budget": {"maxTokens": int, "maxTimeSec": int},
}

for _group, _fields in _NESTED_SETTINGS_TYPES.items():
    for _name in _fields:
        if (_group, _name) in {("tui", "manualBashMode"), ("subagents", "timeoutSec"),
                               ("subagents", "idleTimeoutSec")}:
            continue
        _key = f"{_group}.{_name}"
        ISSUE_REGISTRY["settings.selected." + _key] = (
            "info", f"{_key} is selected by the settings merge before getter normalization."
        )


class DoctorCheck:
    """A check produces fixed, non-sensitive diagnostic messages."""

    def run(self, cwd: Path) -> list[DoctorIssue]:
        raise NotImplementedError


class InstallationCheck(DoctorCheck):
    def run(self, cwd: Path) -> list[DoctorIssue]:
        issues = [DoctorIssue("installation.version", "info", "installation", f"ONE {VERSION}; Python version checked.")]
        if tuple(sys.version_info[:2]) < (3, 12):
            issues.append(DoctorIssue("installation.python", "error", "installation", "Python 3.12 or newer is required."))
        override, xdg, legacy = get_agent_dir_candidates()
        if override is None and legacy.exists():
            issues.append(DoctorIssue("paths.legacy", "warning", "global", (
                "Legacy state exists alongside the XDG location; verify manually."
                if xdg.exists() else "Legacy state exists; normal startup may migrate it. Verify manually."
            )))
        path = override or (legacy if legacy.exists() and not xdg.exists() else xdg)
        if path.exists() and not path.is_dir():
            issues.append(DoctorIssue("paths.agent_type", "error", "global", "The agent path is not a directory."))
        issues.append(DoctorIssue("paths.writability", "info", "global", "Writability is unknown without a write probe."))
        return issues


class SettingsCheck(DoctorCheck):
    def run(self, cwd: Path) -> list[DoctorIssue]:
        override, xdg, legacy = get_agent_dir_candidates()
        agent = override or (legacy if legacy.exists() and not xdg.exists() else xdg)
        manager = SettingsManager.read_only(str(cwd), str(agent))
        errors = {entry["scope"] for entry in manager.drain_errors()}
        layers = (("global", agent / "settings.json", manager._global),
                  ("project", cwd / ".one" / "settings.json", manager._project))
        issues = []
        for scope, path, data in layers:
            if scope in errors:
                issues.append(DoctorIssue("settings.unreadable", "error", scope,
                                          "Settings could not be read as a JSON object; file remains protected."))
                continue
            if not path.exists():
                issues.append(DoctorIssue("settings.missing", "info", scope, "Settings file is absent; defaults apply."))
                continue
            version_problem = version_error(data)
            if version_problem == "invalid_version":
                issues.append(DoctorIssue("settings.version_invalid", "error", scope, "Configuration version is invalid."))
            elif version_problem == "future_version":
                issues.append(DoctorIssue("settings.version_future", "warning", scope, "Configuration version is newer than supported."))
            other_scope, _, other = layers[1 if scope == "global" else 0]
            if not version_problem and other_scope not in errors and not version_error(other):
                if migrate(data, other, scope).conflict == "timeout_conflict":
                    issues.append(DoctorIssue("settings.timeout_migration_conflict", "warning", scope, ""))
            known = set(DEFAULT_SETTINGS) | {"configVersion", "runBudget", "mcpServers", "presets"}
            if set(data) - known:
                issues.append(DoctorIssue("settings.unknown", "warning", scope,
                                          "Unknown settings are present; review them manually."))
            for key, value in data.items():
                if key not in DEFAULT_SETTINGS or DEFAULT_SETTINGS[key] is None:
                    continue
                default = DEFAULT_SETTINGS[key]
                if type(value) is not type(default) and key != "defaultTemperature":
                    issues.append(DoctorIssue("settings.type." + key, "warning", scope,
                                              f"Invalid type for {key}; inspect the setting."))
            for key in ("runBudget", "mcpServers", "presets"):
                if key in data and not isinstance(data[key], dict):
                    issues.append(DoctorIssue("settings.type." + key, "warning", scope,
                                              f"Invalid type for {key}; an object is expected."))
            for key in ("defaultProvider", "defaultModel", "sessionDir", "shellCommandPrefix"):
                if key in data and data[key] is not None and not isinstance(data[key], str):
                    issues.append(DoctorIssue("settings.type." + key, "warning", scope, ""))
            for key, fields in _NESTED_SETTINGS_TYPES.items():
                value = data.get(key)
                if not isinstance(value, dict):
                    continue
                if set(value) - set(fields):
                    issues.append(DoctorIssue("settings.nested_unknown", "warning", scope, ""))
                if any(name in fields and type(item) not in (
                    fields[name] if isinstance(fields[name], tuple) else (fields[name],)
                ) for name, item in value.items()):
                    issues.append(DoctorIssue("settings.nested_type", "warning", scope, ""))
            invalid = False
            if "defaultThinkingLevel" in data and data["defaultThinkingLevel"] not in THINKING_LEVELS:
                invalid = True
            if isinstance(data.get("defaultMode"), str) and data["defaultMode"].lower() not in ("tui", "cli"):
                invalid = True
            context = data.get("context")
            if isinstance(context, dict) and isinstance(context.get("policy"), str) and context["policy"] not in ("endpoint-max", "provider-default"):
                invalid = True
            generation = data.get("generation")
            if isinstance(generation, dict) and type(generation.get("maxOutputTokens")) is int and generation["maxOutputTokens"] <= 0:
                invalid = True
            pruning = data.get("toolOutputPruning")
            if isinstance(pruning, dict):
                for name, minimum in (("recentTokens", 0), ("minResultTokens", 1)):
                    if type(pruning.get(name)) is int and pruning[name] < minimum:
                        invalid = True
                marker = pruning.get("marker")
                if isinstance(marker, str) and (not marker.strip() or len(marker.strip()) > 512):
                    invalid = True
            for group, name, minimum in (("askUser", "timeoutSec", 0), ("providers", "timeoutSec", 10)):
                settings = data.get(group)
                if isinstance(settings, dict) and type(settings.get(name)) is int and settings[name] < minimum:
                    invalid = True
            sub = data.get("subagents")
            if isinstance(sub, dict) and "maxDurationSec" in sub:
                try:
                    if int(sub["maxDurationSec"] or 0) < 0:
                        invalid = True
                except (TypeError, ValueError, OverflowError):
                    invalid = True
            if "defaultTemperature" in data:
                try:
                    normalize_temperature(data["defaultTemperature"])
                except ValueError:
                    invalid = True
            tui = data.get("tui")
            if isinstance(tui, dict):
                for name, allowed in (("infoPanel", ("top", "sidebar")), ("manualBashMode", ("strict", "dev"))):
                    if name in tui and tui[name] not in allowed:
                        invalid = True
                preview = tui.get("toolResultPreviewLines", 100)
                if type(preview) is not int or not 1 <= preview <= 1000:
                    invalid = True
                if scope == "project" and "manualBashMode" in tui:
                    issues.append(DoctorIssue("settings.manual_bash_ignored", "info", scope, ""))
            for name, validator, error in (("runBudget", RunLimits.from_config, ValueError),
                                           ("presets", validate_preset_overrides, PresetError)):
                if name in data:
                    try:
                        validator(data[name])
                    except error:
                        issue_id = "settings.run_budget_invalid" if name == "runBudget" else "settings.presets_invalid"
                        issues.append(DoctorIssue(issue_id, "warning", scope, ""))
            if invalid:
                issues.append(DoctorIssue("settings.invalid_value", "warning", scope, ""))
            if isinstance(data.get("subagents"), dict) and "timeoutSec" in data["subagents"]:
                issues.append(DoctorIssue("settings.legacy_timeout", "info", scope,
                                          "Legacy subagent timeout is present."))
        # Unlike ordinary merged settings, the getter looks for the new name
        # across both raw layers before considering the old name at all.
        subagents = {
            scope: data.get("subagents") if isinstance(data.get("subagents"), dict) else {}
            for scope, _, data in layers
        }
        global_tui = manager._global.get("tui")
        if isinstance(global_tui, dict) and "manualBashMode" in global_tui:
            issues.append(DoctorIssue("settings.manual_bash_source", "info", "global", ""))
            if global_tui["manualBashMode"] not in ("strict", "dev"):
                issues.append(DoctorIssue("settings.manual_bash_default", "info", "global", ""))
        else:
            issues.append(DoctorIssue("settings.manual_bash_default", "info", "global", ""))
        for key, default in DEFAULT_SETTINGS.items():
            if isinstance(default, dict):
                continue
            source = next((scope for scope, _, data in reversed(layers) if key in data), None)
            if source:
                issues.append(DoctorIssue("settings.selected." + key, "info", source, ""))
        for group, fields in _NESTED_SETTINGS_TYPES.items():
            for name in fields:
                issue_id = f"settings.selected.{group}.{name}"
                if issue_id not in ISSUE_REGISTRY:
                    continue
                selected = None
                for scope, _, data in layers:
                    if group in data:
                        value = data[group]
                        if not isinstance(value, dict):
                            selected = None
                        elif name in value:
                            selected = scope
                if selected:
                    issues.append(DoctorIssue(issue_id, "info", selected, ""))
        # MCP entries are deep-merged before McpManager.create sees them. A
        # project entry may legitimately supply only part of a global server.
        servers = manager.get_mcp_servers()
        if isinstance(servers, dict):
            project_servers = manager._project.get("mcpServers")
            for name, config in servers.items():
                project_entry = project_servers.get(name) if isinstance(project_servers, dict) else None
                scope = "project" if isinstance(project_servers, dict) and name in project_servers else "global"
                if not isinstance(config, dict) or not (config.get("command") or config.get("url")):
                    issues.append(DoctorIssue("settings.mcp_invalid", "warning", scope, ""))
                    continue
                args, env = config.get("args") or [], config.get("env") or {}
                for field, valid in (("args", isinstance(args, (list, tuple, str, dict))),
                                     ("env", isinstance(env, dict))):
                    if not valid:
                        source = "project" if isinstance(project_entry, dict) and field in project_entry else "global"
                        issues.append(DoctorIssue("settings.mcp_invalid", "warning", source, ""))
        for key, shadow_id in (("idleTimeoutSec", "settings.timeout_idle_shadowed"),
                               ("timeoutSec", "settings.timeout_legacy_shadowed")):
            if key in subagents["global"] and key in subagents["project"]:
                issues.append(DoctorIssue(shadow_id, "info", "global", ""))
        for key, issue_id in (("idleTimeoutSec", "settings.timeout_idle_effective"),
                              ("timeoutSec", "settings.timeout_legacy_effective")):
            source = next((scope for scope in ("project", "global") if key in subagents[scope]), None)
            if source is not None:
                issues.append(DoctorIssue(issue_id, "info", source, ""))
                try:
                    raw = subagents[source][key]
                    if raw is None or int(raw) < 0:
                        raise ValueError
                except (TypeError, ValueError, OverflowError):
                    issues.append(DoctorIssue("settings.timeout_fallback", "warning", source, ""))
                    issues.append(DoctorIssue("settings.timeout_default", "info", source, ""))
                if key == "idleTimeoutSec":
                    for scope in ("global", "project"):
                        if "timeoutSec" in subagents[scope]:
                            issues.append(DoctorIssue("settings.timeout_legacy_ignored", "info", scope, ""))
                break
        else:
            issues.append(DoctorIssue("settings.timeout_default", "info", "global", ""))
        def overlaps(global_data: dict, project_data: dict) -> bool:
            for key in global_data.keys() & project_data.keys():
                if key == "manualBashMode":
                    continue
                left, right = global_data[key], project_data[key]
                if not isinstance(left, dict) or not isinstance(right, dict) or overlaps(left, right):
                    return True
            return False

        known_global = {key: value for key, value in manager._global.items() if key in DEFAULT_SETTINGS}
        if overlaps(known_global, manager._project):
            issues.append(DoctorIssue("settings.override", "info", "project", ""))
        return issues


class ReadinessCheck(DoctorCheck):
    def run(self, cwd: Path) -> list[DoctorIssue]:
        override, xdg, legacy = get_agent_dir_candidates()
        agent = override or (legacy if legacy.exists() and not xdg.exists() else xdg)
        settings = SettingsManager.read_only(str(cwd), str(agent))
        registry = ModelRegistry(AuthStorage(str(agent / "auth.json"), read_only=True),
                                 str(agent / "models.json"), read_only=True)
        result = assess_provider_model(settings, registry)
        if result.selection == "unknown" or result.selected_auth == "unknown":
            issue = "readiness.unknown"
        elif result.selection != "selected" or result.selected_auth not in {"configured", "not_required"}:
            issue = "readiness.unavailable"
        elif result.status != "configured":
            issue = "readiness.fallback"
        else:
            issue = "readiness.unverified"
        return [DoctorIssue(issue, "", "global", "")]


class DoctorEngine:
    def __init__(self, checks: tuple[DoctorCheck, ...] | None = None) -> None:
        self.checks = checks if checks is not None else (InstallationCheck(), SettingsCheck(), ReadinessCheck())

    def check(self, cwd: Path) -> DoctorReport:
        issues = []
        for check in self.checks:
            try:
                issues.extend(check.run(cwd))
            except Exception:
                issues.append(DoctorIssue("check.failed", "error", "installation", "A diagnostic check failed safely."))
        return DoctorReport(tuple(sorted(issues, key=lambda i: tuple(i.projection().values()))))
