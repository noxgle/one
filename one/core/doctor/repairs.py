"""Conservative repair planning and execution from approved snapshots."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from one.config import get_agent_dir_candidates

from . import DoctorEngine, settings_write
from .migrations import migrate, version_error
from .reports import DoctorReport, RepairAction, safe_diff


@dataclass(frozen=True)
class RepairPlan:
    report: DoctorReport
    paths: dict[str, Path]
    snapshots: dict[str, settings_write.Snapshot | None]
    candidates: dict[str, bytes]


def _inspect(path: Path) -> settings_write.Snapshot | None:
    try:
        return settings_write.inspect(path)
    except FileNotFoundError:
        # Missing parents are tracked as absence, and must remain absent.
        if not path.parent.exists() and not path.parent.is_symlink():
            return None
        raise


def make_plan(cwd: Path, report: DoctorReport) -> RepairPlan:
    override, xdg, legacy = get_agent_dir_candidates()
    agent = override or (legacy if legacy.exists() and not xdg.exists() else xdg)
    paths = {"global": agent / "settings.json", "project": cwd / ".one" / "settings.json"}
    snapshots: dict[str, settings_write.Snapshot | None] = {}
    layers = {}
    blocked = False
    for scope, path in paths.items():
        try:
            snap = _inspect(path)
            snapshots[scope] = snap
            data = json.loads(snap.data) if snap and snap.exists else {}
            if not isinstance(data, dict) or version_error(data):
                raise ValueError("invalid settings")
            layers[scope] = data
        except (OSError, ValueError, UnicodeError, settings_write.WriteError):
            blocked = True
            layers[scope] = {}
    actions = []
    candidates = {}
    for scope in ("global", "project"):
        snap = snapshots.get(scope)
        if not snap or not snap.exists:
            continue
        if blocked:
            actions.append(RepairAction("settings.review", scope, "", "not_available", "manual"))
            continue
        other = "project" if scope == "global" else "global"
        result = migrate(layers[scope], layers[other], scope)
        if result.conflict:
            actions.append(RepairAction("settings.review", scope, "", "not_available", "decision"))
        elif result.data != layers[scope]:
            actions.append(RepairAction("settings.migrate", scope, "", "planned", "safe", safe_diff(layers[scope], result.data)))
            candidates[scope] = (json.dumps(result.data, indent=2, ensure_ascii=True) + "\n").encode()
            layers[scope] = result.data
    # An ambiguous layer invalidates the combined migration plan.
    if any(a.classification == "decision" for a in actions):
        actions = [a for a in actions if a.id != "settings.migrate"]
        candidates.clear()
    if blocked and not actions:
        actions.append(RepairAction("settings.review", "global", "", "not_available", "manual"))
    reviewed = {action.scope for action in actions if action.id == "settings.review"}
    for issue in report.issues:
        projected = issue.projection()
        scope = projected["scope"]
        if projected["level"] != "info" and scope not in reviewed:
            action_id = "settings.review" if scope in paths else "diagnostic.review"
            actions.append(RepairAction(action_id, scope, "", "not_available", "manual"))
            reviewed.add(scope)
    return RepairPlan(DoctorReport(report.issues, tuple(actions), preview=True), paths, snapshots, candidates)


def plan_repairs(cwd: Path, report: DoctorReport) -> DoctorReport:
    return make_plan(cwd, report).report


def execute_plan(cwd: Path, plan: RepairPlan, approved: bool) -> DoctorReport:
    snapshots = dict(plan.snapshots)
    actions = []
    stopped = False
    for action in plan.report.actions:
        if action.id != "settings.migrate":
            actions.append(action)
            continue
        if not approved:
            actions.append(replace(action, status="declined"))
            continue
        status = "not_available"
        if not stopped:
            applying = False
            try:
                if any(_inspect(path) != snapshots[scope] for scope, path in plan.paths.items()):
                    raise settings_write.WriteError("conflict")
                data = plan.candidates[action.scope]
                def validate(raw: bytes) -> None:
                    if raw != data or not isinstance(json.loads(raw), dict):
                        raise ValueError("invalid replacement")
                expected = snapshots[action.scope]
                assert expected is not None
                applying = True
                settings_write.apply(plan.paths[action.scope], expected, data, validate)
                written = _inspect(plan.paths[action.scope])
                if written is None or not written.exists or written.data != data:
                    raise settings_write.WriteError("conflict")
                snapshots[action.scope] = written
                status = "applied"
            except settings_write.WriteError as exc:
                status = exc.status if exc.status in {"conflict", "rolled_back", "unsafe_rollback", "interrupted"} else "failed"
                stopped = True
            except KeyboardInterrupt:
                status = "unsafe_rollback" if applying else "interrupted"
                stopped = True
            except (OSError, ValueError, UnicodeError):
                status = "failed"
                stopped = True
        actions.append(replace(action, status=status))
    try:
        issues = DoctorEngine().check(cwd).issues
    except KeyboardInterrupt:
        issues = plan.report.issues
        actions.append(RepairAction("diagnostic.review", "installation", "", "interrupted", "manual"))
    return DoctorReport(issues, tuple(actions))
