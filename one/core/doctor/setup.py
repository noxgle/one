"""Interactive, offline settings setup; no credentials are collected."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
import sys
from pathlib import Path

from one.config import get_agent_dir_candidates
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.settings_manager import SettingsManager
from one.providers.registry import supported_provider_names

from . import DoctorEngine, settings_write
from .migrations import version_error
from .readiness import assess_provider_model
from .repairs import _inspect
from .reports import safe_diff


def run_setup(cwd: Path, *, dry_run: bool = False) -> int:
    if dry_run:
        print("Setup preview: choose scope, provider and model, review a redacted diff, then confirm.")
        print("Credentials remain unchanged; use /login separately. Network availability is unverified.")
        report = DoctorEngine().check(cwd)
        print(report.to_text())
        return 0 if report.result == "ok" else 1
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Setup requires an interactive terminal.", file=sys.stderr)
        return 2
    override, xdg, legacy = get_agent_dir_candidates()
    agent = override or (legacy if legacy.exists() and not xdg.exists() else xdg)
    paths = {"global": agent / "settings.json", "project": cwd / ".one" / "settings.json"}
    approved = False
    try:
        snapshots = {scope: _inspect(path) for scope, path in paths.items()}
        layers = {}
        for scope, snap in snapshots.items():
            data = json.loads(snap.data) if snap and snap.exists else {}
            if not isinstance(data, dict) or version_error(data):
                raise ValueError("invalid settings")
            layers[scope] = data
        registry = ModelRegistry(AuthStorage(str(agent / "auth.json"), read_only=True),
                                 str(agent / "models.json"), read_only=True)
        settings = SettingsManager.read_only(str(cwd), str(agent))
        current = assess_provider_model(settings, registry)
        print(f"Current local configuration: {current.status}; selected authentication: {current.selected_auth}.")
        print("Credentials are retained unchanged. For login or OAuth, use /login separately.")
        scope = input("Scope [global/project]: ").strip().lower()
        if scope not in paths:
            raise ValueError("invalid scope")
        print("Available providers: " + ", ".join(sorted(supported_provider_names())))
        provider = input("Provider: ").strip()
        if provider not in supported_provider_names():
            raise ValueError("invalid provider")
        model = input("Model ID: ").strip()
        if not model or any(ord(c) < 32 for c in model):
            raise ValueError("invalid model")
        if scope == "global" and any(
            key in layers["project"] and layers["project"][key] != value
            for key, value in (("defaultProvider", provider), ("defaultModel", model))
        ):
            print("Project settings override this selection; choose project scope or update the project settings.",
                  file=sys.stderr)
            return 1
        candidate = dict(layers[scope], defaultProvider=provider, defaultModel=model)
        print(f"Settings diff ({scope}): {safe_diff(layers[scope], candidate)}")
        print("Endpoint, model availability and credentials have not been verified over the network.")
        if input("Apply settings? [y/N]: ").strip().lower() not in {"y", "yes"}:
            print("Setup cancelled; no settings written.")
            return 1
        approved = True
        if any(_inspect(path) != snapshots[name] for name, path in paths.items()):
            raise settings_write.WriteError("conflict")
        path = paths[scope]
        if snapshots[scope] is None:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            expected = settings_write.inspect(path)
            if expected.exists:
                raise settings_write.WriteError("conflict")
        else:
            expected = snapshots[scope]
        data = (json.dumps(candidate, indent=2, ensure_ascii=True) + "\n").encode()
        def validate(raw: bytes) -> None:
            if raw != data or json.loads(raw) != candidate:
                raise ValueError("invalid replacement")
        if candidate != layers[scope] or not expected.exists:
            settings_write.apply(path, expected, data, validate)
        settings = SettingsManager.read_only(str(cwd), str(agent))
        result = assess_provider_model(settings, registry)
        print(f"Effective local configuration: {result.status}; selected authentication: {result.selected_auth}.")
        if result.selected_model != result.model or result.selected_provider != result.provider:
            print("Runtime will select a different model than the configured default.")
        print("Endpoint, model availability and credentials remain unverified; use /login if needed.")
        report = DoctorEngine().check(cwd)
        print(report.to_text())
        return 0 if (report.result == "ok" and result.status == "configured" and result.selection == "selected"
                     and (result.provider, result.model) == (provider, model)
                     and (result.selected_provider, result.selected_model) == (provider, model)
                     and result.selected_auth in {"configured", "not_required"}) else 1
    except (EOFError, KeyboardInterrupt):
        print("\nSetup interrupted; check settings and backups." if approved else
              "\nSetup cancelled; no settings written.")
        return 1
    except (OSError, ValueError, UnicodeError, settings_write.WriteError):
        print("Setup could not safely apply settings; review configuration and retry.", file=sys.stderr)
        return 1
