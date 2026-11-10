"""Pure, per-file settings migrations; this module never reads or writes files."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

CURRENT_CONFIG_VERSION = 1
Settings = dict[str, Any]
Step = Callable[[Settings, Settings, str], Settings]


@dataclass(frozen=True)
class MigrationResult:
    data: Settings
    conflict: str | None = None


def version_error(data: Settings, current: int = CURRENT_CONFIG_VERSION) -> str | None:
    version = data.get("configVersion", 0)
    if type(version) is not int or version < 0:
        return "invalid_version"
    if version > current:
        return "future_version"
    return None


def _selected(global_data: Settings, project_data: Settings) -> tuple[str, str, Any] | None:
    layers = [(scope, data.get("subagents")) for scope, data in
              (("global", global_data), ("project", project_data))]
    for key in ("idleTimeoutSec", "timeoutSec"):
        for scope, sub in reversed(layers):
            if isinstance(sub, dict) and key in sub:
                return scope, key, sub[key]
    return None


def _timeout_alias(data: Settings, other: Settings, scope: str) -> Settings:
    sub = data.get("subagents")
    if not isinstance(sub, dict) or "timeoutSec" not in sub or "idleTimeoutSec" in sub:
        return data
    # A malformed other layer makes the cross-layer interpretation uncertain.
    if "subagents" in other and not isinstance(other["subagents"], dict):
        raise ValueError("timeout_conflict")
    before = _selected(data, other) if scope == "global" else _selected(other, data)
    sub["idleTimeoutSec"] = deepcopy(sub["timeoutSec"])
    after = _selected(data, other) if scope == "global" else _selected(other, data)
    # Require the same source and raw value, not merely equal normalized fallbacks.
    if before is None or after is None or before[0] != after[0] or type(before[2]) is not type(after[2]) or before[2] != after[2]:
        raise ValueError("timeout_conflict")
    return data


MIGRATIONS: Mapping[int, Step] = {0: _timeout_alias}


def migrate(data: Settings, other: Settings, scope: str, *, current: int = CURRENT_CONFIG_VERSION,
            registry: Mapping[int, Step] = MIGRATIONS) -> MigrationResult:
    """Return a detached candidate or the untouched input on any conflict.

    Both raw layers must be supplied, including an empty mapping for a known
    absent file. Callers must not use this on unreadable or unknown layers.
    Steps are keyed by their source version and advance exactly one version.
    """
    original = deepcopy(data)
    if scope not in ("global", "project") or not isinstance(data, dict) or not isinstance(other, dict):
        return MigrationResult(original, "invalid_context")
    error = version_error(data, current)
    if error:
        return MigrationResult(original, error)
    if version_error(other, current):
        return MigrationResult(original, "invalid_context")
    candidate = deepcopy(data)
    for version in range(data.get("configVersion", 0), current):
        step = registry.get(version)
        if step is None:
            return MigrationResult(original, "missing_step")
        try:
            candidate = step(candidate, deepcopy(other), scope)
        except ValueError:
            return MigrationResult(original, "timeout_conflict")
        if not isinstance(candidate, dict):
            return MigrationResult(original, "invalid_step")
        candidate["configVersion"] = version + 1
    return MigrationResult(candidate)
