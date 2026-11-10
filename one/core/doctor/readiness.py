"""Offline assessment of explicitly configured provider and model.

A registered model, credential or catalogue entry is not evidence that an
endpoint currently serves it or that credentials will be accepted.
"""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from one.core.model_registry import ModelRegistry
from one.core.settings_manager import SettingsManager
from one.providers.registry import supported_provider_names


@dataclass(frozen=True)
class ProviderModelReadiness:
    provider: str | None
    model: str | None
    status: Literal["missing", "invalid", "unsupported_provider", "configured"]
    model_source: Literal["registry", "catalog", "dynamic"] | None = None
    default_selectable: bool = False
    availability: Literal["unverified"] = "unverified"
    auth: Literal["not_checked", "not_required", "configured", "missing", "unknown"] = "not_checked"
    selected_provider: str | None = None
    selected_model: str | None = None
    selected_auth: Literal["not_checked", "not_required", "configured", "missing", "unknown"] = "not_checked"
    selection: Literal["selected", "none", "unknown"] = "unknown"


def assess_provider_model(settings: SettingsManager, registry: ModelRegistry) -> ProviderModelReadiness:
    """Assess effective defaults and runtime fallback without contacting providers.

    Pass a read-only registry and SettingsManager when loading from disk. Dynamic
    IDs for local providers are also accepted by runtime default selection;
    cloud selection is registry and auth dependent.
    """
    provider = settings.get_default_provider()
    model = settings.get_default_model()

    def auth_status(name: str) -> Literal["not_required", "configured", "missing", "unknown"]:
        try:
            status = registry.get_provider_auth_status(name)
            if not status["requiresApiKey"]:
                return "not_required"
            return "configured" if status["configured"] else "missing"
        except (AttributeError, TypeError, ValueError):
            return "unknown"

    # Invalid settings are not passed into runtime selection; their diagnostics
    # remain separate from any inferred fallback.
    if any(value is not None and (not isinstance(value, str) or not value.strip()) for value in (provider, model)):
        return ProviderModelReadiness(None, None, "invalid")
    auth = auth_status(provider) if provider in supported_provider_names() else "not_checked"
    try:
        selected = registry.select_default(provider, model)
    except (AttributeError, TypeError, ValueError):
        selected = None
        selection = "unknown"
    else:
        selection = "selected" if selected is not None else "none"
    selected_auth = auth_status(selected.provider) if selected else "not_checked"
    extra = dict(auth=auth, selected_provider=selected.provider if selected else None,
                 selected_model=selected.id if selected else None, selected_auth=selected_auth, selection=selection)
    if provider is not None and provider not in supported_provider_names():
        return ProviderModelReadiness(provider, model, "unsupported_provider", **extra)
    if provider is None or model is None:
        usable = selected is not None and selected_auth in {"configured", "not_required"}
        return ProviderModelReadiness(provider, model, "configured" if usable else "missing", **extra)
    if registry.find(provider, model) is not None:
        source = "registry"
    elif (provider, model) in registry.catalog.facts:
        source = "catalog"
    else:
        source = "dynamic"
    exact = selected is not None and (selected.provider, selected.id) == (provider, model)
    return ProviderModelReadiness(provider, model, "configured" if exact else "missing", source, exact, **extra)
