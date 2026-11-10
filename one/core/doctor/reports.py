"""Safe, deterministic Doctor report contract.

Only registry-owned strings cross the output boundary. Check data, paths,
exception messages and proposed values must never be interpolated into reports.
"""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from one.config import VERSION
from one.core.settings_manager import DEFAULT_SETTINGS

# IDs and their levels/messages are owned by Doctor, not by input files.
ISSUE_REGISTRY: dict[str, tuple[str, str]] = {
    "installation.version": ("info", f"ONE {VERSION}; Python version checked."),
    "installation.python": ("error", "Python 3.12 or newer is required."),
    "paths.legacy": ("warning", "Legacy state exists; verify locations manually."),
    "paths.agent_type": ("error", "The agent path is not a directory."),
    "paths.writability": ("info", "Writability is unknown without a write probe."),
    "readiness.unverified": ("info", "A model is locally selectable; endpoint availability and credentials are unverified offline."),
    "readiness.unavailable": ("warning", "No locally usable model and authentication could be selected; review setup and login."),
    "readiness.unknown": ("warning", "Local model selection or authentication could not be determined; review setup and login."),
    "readiness.fallback": ("warning", "Configured defaults are not locally selectable; runtime may use a fallback. Review setup and login."),
    "settings.unreadable": ("error", "Settings could not be read as a JSON object; file remains protected."),
    "settings.missing": ("info", "Settings file is absent; defaults apply."),
    "settings.version_invalid": ("error", "Configuration version is invalid."),
    "settings.version_future": ("warning", "Configuration version is newer than supported."),
    "settings.timeout_migration_conflict": ("warning", "Subagent timeout migration requires manual review across settings layers."),
    "settings.unknown": ("warning", "Unknown settings are present; review them manually."),
    "settings.legacy_timeout": ("info", "Legacy subagent timeout is present."),
    "settings.timeout_legacy_effective": ("info", "The legacy subagent timeout is selected for normalization."),
    "settings.timeout_legacy_ignored": ("info", "The legacy subagent timeout is ignored because idleTimeoutSec is configured."),
    "settings.timeout_idle_effective": ("info", "idleTimeoutSec is selected for normalization."),
    "settings.nested_type": ("warning", "A known nested setting has an invalid type."),
    "settings.nested_unknown": ("warning", "Unknown nested settings are present; review them manually."),
    "settings.invalid_value": ("warning", "A known setting has an invalid value or falls back to a default."),
    "settings.run_budget_invalid": ("warning", "runBudget is rejected by the run limits validator."),
    "settings.presets_invalid": ("warning", "presets is rejected by the preset overrides validator."),
    "settings.mcp_invalid": ("warning", "An effective MCP server configuration is skipped or cannot be loaded; inspect mcpServers."),
    "settings.override": ("info", "Project settings override a global setting."),
    "settings.manual_bash_ignored": ("info", "Project tui.manualBashMode is ignored; only the global setting applies."),
    "settings.timeout_fallback": ("warning", "The selected subagent timeout falls back to its default."),
    "settings.timeout_default": ("info", "The effective subagent idle timeout comes from the default."),
    "settings.timeout_idle_shadowed": ("info", "The global idleTimeoutSec is overridden by the project setting."),
    "settings.timeout_legacy_shadowed": ("info", "The global timeoutSec is overridden by the project setting."),
    "settings.manual_bash_source": ("info", "The global manual bash setting is selected for normalization."),
    "settings.manual_bash_default": ("info", "The effective manual bash mode comes from the default."),
    "check.failed": ("error", "A diagnostic check failed safely."),
}
ISSUE_REGISTRY.update({
    "settings.type." + key: ("warning", f"Invalid type for {key}; inspect the setting.")
    for key in (*DEFAULT_SETTINGS, "runBudget", "mcpServers", "presets")
})
ISSUE_REGISTRY.update({
    "settings.selected." + key: ("info", f"{key} is selected by the settings merge before getter normalization.")
    for key in DEFAULT_SETTINGS if not isinstance(DEFAULT_SETTINGS[key], dict)
})
SCOPES = frozenset({"installation", "global", "project"})
ISSUE_STATUSES = frozenset({"open", "resolved"})
ACTION_STATUSES = frozenset({"not_available", "planned", "applied", "failed", "declined", "conflict", "rolled_back", "unsafe_rollback", "interrupted"})
# Only registry-owned descriptions may cross the output boundary.
ACTION_REGISTRY: dict[str, str] = {
    "settings.migrate": "Migrate settings schema with a private backup.",
    "settings.review": "Review settings manually; no automatic change is proposed.",
    "diagnostic.review": "Review the diagnostic issue manually; no automatic change is proposed.",
}


def _scope(scope: object) -> str:
    return scope if isinstance(scope, str) and scope in SCOPES else "unknown"


def safe_diff(before: Any, after: Any) -> str:
    """Render no unapproved keys or values, even for nested/unknown data."""
    if before == after:
        return "[no changes]"
    if isinstance(before, dict) and isinstance(after, dict):
        old = before.get("configVersion", 0)
        new = after.get("configVersion", 0)
        if type(old) is int and type(new) is int and 0 <= old < new <= 1:
            return f"configVersion: {old} -> {new}; [other changes redacted]"
    return "[changes redacted]"


@dataclass(frozen=True)
class DoctorIssue:
    id: str
    level: str
    scope: str
    description: str
    status: str = "open"

    def projection(self) -> dict[str, str]:
        known = ISSUE_REGISTRY.get(self.id) if isinstance(self.id, str) else None
        level, description = known or ("error", "An unregistered diagnostic issue was reported.")
        return {"id": self.id if known else "issue.unregistered", "level": level,
                "scope": _scope(self.scope), "description": description,
                "status": self.status if known and self.status in ISSUE_STATUSES else "open"}


@dataclass(frozen=True)
class RepairAction:
    id: str
    scope: str
    description: str
    status: str = "not_available"
    classification: str = "manual"
    diff: str = "[no changes]"

    def projection(self) -> dict[str, str]:
        description = ACTION_REGISTRY.get(self.id) if isinstance(self.id, str) else None
        if self.id == "settings.migrate" and self.status == "declined":
            description = "Repair not approved; rerun with --yes or confirm interactively."
        return {"id": self.id if description else "action.unregistered", "level": "info" if description else "error",
                "scope": _scope(self.scope),
                "description": description or "An unregistered repair action was reported.",
                "status": self.status if self.status in ACTION_STATUSES else "not_available",
                "classification": self.classification if self.classification in {"safe", "decision", "manual"} else "manual",
                "file": {"global": "global settings.json", "project": "project .one/settings.json"}.get(self.scope, "unknown"),
                "diff": self.diff if self.diff in {"[changes redacted]", "[no changes]",
                                                  "configVersion: 0 -> 1; [other changes redacted]"}
                else "[changes redacted]",
                "preconditions": "Recheck both settings files and metadata before any write; reject unsafe targets."}


@dataclass(frozen=True)
class DoctorReport:
    issues: tuple[DoctorIssue, ...]
    actions: tuple[RepairAction, ...] = ()
    preview: bool = False

    def _items(self) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        def ordered(items: list[dict[str, str]]) -> list[dict[str, str]]:
            return sorted(items, key=lambda item: tuple(item[k] for k in ("scope", "id", "level", "status", "description")))
        return ordered([i.projection() for i in self.issues]), ordered([a.projection() for a in self.actions])

    @property
    def result(self) -> str:
        issues, actions = self._items()
        return "action_required" if (any(i["level"] != "info" and i["status"] != "resolved" for i in issues)
                                     or any(a["level"] == "error" or (a["status"] != "applied" and
                                            (not self.preview or a["classification"] != "safe")) for a in actions)) else "ok"

    def to_json(self) -> str:
        issues, actions = self._items()
        return json.dumps({"formatVersion": 1, "issues": issues, "actions": actions, "result": self.result}, indent=2)

    def to_text(self) -> str:
        issues, actions = self._items()
        lines = [f"[{i['level'].upper()}] {i['scope']}: {i['description']}" for i in issues]
        lines += [f"[ACTION] {a['id']} {a['scope']} {a['file']}: {a['description']} ({a['classification']}, {a['status']}); {a['diff']}; {a['preconditions']}" for a in actions]
        return "\n".join([*lines, f"Result: {self.result}"])
