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
    return frozenset(field for field in fields if valid_limit(getattr(model, field, None)))


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


def resolve_context_budget(model: ModelInfo | None, policy: object = "endpoint-max", *, request_tokens: int | None = None) -> ContextBudget:
    """Resolve total/input/output limits without treating defaults as endpoint facts."""
    selected_policy = policy if isinstance(policy, str) and policy in POLICIES else "endpoint-max"
    if model is None:
        return ContextBudget(selected_policy, None, FALLBACK_CONTEXT_WINDOW, DEFAULT_OUTPUT_RESERVE,
                             DEFAULT_OUTPUT_RESERVE, "unknown estimate", (), True)
    manual = set(model.manual_fields)
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
    output_limit = valid_limit(model.output_limit)
    reserve = min(DEFAULT_OUTPUT_RESERVE, output_limit) if output_limit else DEFAULT_OUTPUT_RESERVE
    if total:
        reserve = min(reserve, max(1, total // 8))
    if output_limit:
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
    outgoing = reserve
    if total:
        remaining = total - request if request else reserve if total_input else 0
        outgoing = min(reserve, max(0, remaining))
    if output_limit:
        outgoing = min(outgoing, output_limit)
    return ContextBudget(selected_policy, total, budget, reserve, outgoing, reason,
                          tuple(dict.fromkeys(constraints)),
                          total is None and not (hard_input or valid_limit(model.effective_input_budget)))
