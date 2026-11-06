"""Single, conservative context-budget policy resolver."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from one.core.types import ModelInfo

FALLBACK_CONTEXT_WINDOW = 128_000
DEFAULT_OUTPUT_RESERVE = 16_384
POLICIES = frozenset({"endpoint-max", "provider-default"})


def valid_limit(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def legacy_models_json_manual_fields(model: ModelInfo, fields: Iterable[str]) -> frozenset[str]:
    """Recover per-field provenance for old models.json entries.

    Older entries have only a source label. A missing or invalid capacity value
    was never an explicit manual constraint and must remain enrichable.
    """
    if model.metadata_source != "models.json":
        return frozenset()
    return frozenset(field for field in fields if field not in model.endpoint_fields
                     and field not in model.catalog_fields and valid_limit(getattr(model, field, None)))


@dataclass(frozen=True)
class ContextBudget:
    policy: str
    selected_context_window: int | None
    input_budget: int
    output_reserve: int
    outgoing_max_tokens: int | None
    reason: str
    constraints: tuple[str, ...]
    estimated: bool
    selected_output_limit: int | None = None
    output_limit_source: str = "settings"
    output_limit_reason: str = "generation.maxOutputTokens fallback"
    outgoing_limit_reason: str = "generation.maxOutputTokens fallback"


def resolve_context_budget(model: ModelInfo | None, policy: object = "endpoint-max", *, request_tokens: int | None = None, max_output_tokens: int | None = None) -> ContextBudget:
    """Resolve total/input/output limits without treating defaults as endpoint facts."""
    selected_policy = policy if isinstance(policy, str) and policy in POLICIES else "endpoint-max"
    manual = set(model.manual_fields) if model else set()
    if model and not manual:
        manual.update(legacy_models_json_manual_fields(model, ("context_window", "output_limit")))
    output_limit = valid_limit(model.output_limit) if model else None
    if output_limit and "output_limit" in manual:
        requested, source, output_reason = output_limit, "models.json", "manual outputLimit"
    elif output_limit and model and ("output_limit" in model.catalog_fields or "output_limit" not in model.endpoint_fields):
        requested, source, output_reason = output_limit, "catalog", "catalog outputLimit"
    else:
        requested, source, output_reason = valid_limit(max_output_tokens) or DEFAULT_OUTPUT_RESERVE, "settings", "generation.maxOutputTokens fallback"
    if model is None:
        return ContextBudget(selected_policy, None, FALLBACK_CONTEXT_WINDOW, requested,
                             requested, "unknown estimate", (), True, requested, source, output_reason, output_reason)
    # Older persisted models predate per-field provenance. Their models.json
    # capacity is still an explicit user configuration, never a catalogue hint.
    if not manual:
        manual.update(legacy_models_json_manual_fields(model, ("context_window",)))
    constraints: list[str] = []
    # ``server`` is an observed serving cap.  ``max`` is an advertised endpoint
    # capacity; contextWindow is used only when that stronger fact is absent.
    endpoint = valid_limit(model.max_context_window) or valid_limit(model.context_window)
    server = valid_limit(model.server_context_window)
    if server:
        endpoint = min(endpoint, server) if endpoint else server
        constraints.append("serverContextWindow")
    manual_total = valid_limit(model.context_window) if "context_window" in manual else None
    total = min(endpoint, manual_total) if endpoint and manual_total else endpoint or manual_total
    if manual_total:
        constraints.append("manualContextWindow")
    server_output = valid_limit(model.server_output_limit)
    effective = min(requested, server_output) if server_output else requested
    effective_reason = "server output hard cap" if server_output and server_output < requested else output_reason
    if server_output:
        constraints.append("serverOutputLimit")
    # Compaction needs a usable input budget even for models advertising an
    # output limit close to their entire context. This is not the request cap.
    reserve = min(effective, DEFAULT_OUTPUT_RESERVE, max(1, total // 8)) if total else min(effective, DEFAULT_OUTPUT_RESERVE)
    if source != "settings":
        constraints.append("outputLimit")

    hard_input = valid_limit(model.input_limit)
    manual_effective = valid_limit(model.effective_input_budget) if "effective_input_budget" in manual else None
    provider_default = valid_limit(model.default_input_budget) or valid_limit(model.default_context_window)
    total_input = max(0, total - reserve) if total else None
    if selected_policy == "provider-default" and provider_default:
        budget = provider_default
        reason = "provider default input budget"
    elif total_input:
        budget = total_input
        reason = "endpoint maximum"
    else:
        budget = hard_input or valid_limit(model.effective_input_budget) or FALLBACK_CONTEXT_WINDOW
        reason = "input-only limit" if budget != FALLBACK_CONTEXT_WINDOW else "unknown estimate"
    if total_input is not None:
        budget = min(budget, total_input)
        constraints.append("selectedContextWindow")
    for value, name in ((hard_input, "inputLimit"), (manual_effective, "manualEffectiveInputBudget")):
        if value:
            budget = min(budget, value)
            constraints.append(name)
    # An explicitly manual input-only budget is always a user cap.
    if "input_limit" in manual and hard_input and "inputLimit" not in constraints:
        constraints.append("manualInputLimit")
    request = valid_limit(request_tokens)
    outgoing = effective
    if total:
        remaining = max(0, total - (request or 0)) if request or total_input else 0
        if remaining < outgoing:
            outgoing = remaining
            effective_reason = "available context"
    return ContextBudget(selected_policy, total, budget, reserve, outgoing, reason,
                          tuple(dict.fromkeys(constraints)),
                          total is None and not (hard_input or valid_limit(model.effective_input_budget)),
                          requested, source, output_reason, effective_reason)
