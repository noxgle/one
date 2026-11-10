"""Small offline, read-only hints for interactive TUI startup."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from pathlib import Path

from one.core.auth_storage import AuthStorage
from one.core.doctor.migrations import CURRENT_CONFIG_VERSION, migrate, version_error
from one.core.doctor.readiness import assess_provider_model
from one.core.model_registry import ModelRegistry
from one.core.settings_manager import SettingsManager


def startup_hints(cwd: str, agent_dir: str, *, explicit_selection: bool = False) -> tuple[str, ...]:
    """Inspect settings and local model metadata without creating runtime resources."""
    settings = SettingsManager.read_only(cwd, agent_dir)
    errors = {error["scope"] for error in settings.drain_errors()}
    layers = (settings._global, settings._project)
    paths = (Path(agent_dir) / "settings.json", Path(cwd) / ".one" / "settings.json")
    hints = []
    if errors or any(version_error(layer) for layer in layers):
        hints.append("Settings need review; run `one doctor --check`.")
    elif any(path.exists() and layer.get("configVersion", 0) < CURRENT_CONFIG_VERSION
             for path, layer in zip(paths, layers, strict=True)):
        results = [migrate(layers[i], layers[1 - i], scope) for i, scope in enumerate(("global", "project"))]
        if any(result.conflict for result in results):
            hints.append("Settings migration needs review; run `one doctor --check`.")
        else:
            hints.append("Settings migration is available; review `one doctor --fix --dry-run`.")
    if not errors and not explicit_selection:
        auth = AuthStorage(str(Path(agent_dir) / "auth.json"), read_only=True)
        registry = ModelRegistry(auth, str(Path(agent_dir) / "models.json"), read_only=True)
        readiness = assess_provider_model(settings, registry)
        if (readiness.status != "configured" or readiness.selection != "selected"
                or readiness.selected_auth not in {"configured", "not_required"}):
            hints.append("Provider/model setup may be needed; run `one doctor --setup`.")
    return tuple(hints)
