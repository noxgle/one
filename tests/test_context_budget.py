# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from one.core.context_budget import resolve_context_budget
from one.core.settings_manager import SettingsManager
from one.core.types import ModelInfo


def test_endpoint_max_codex_default_and_reserve_are_separate():
    model = ModelInfo("chatgpt", "codex", context_window=872_000,
                      max_context_window=872_000, default_input_budget=272_000)
    budget = resolve_context_budget(model)
    assert (budget.selected_context_window, budget.output_reserve, budget.input_budget) == (872_000, 16_384, 855_616)
    assert round(11_169 / budget.input_budget * 100, 3) == 1.305
    assert resolve_context_budget(model, "provider-default").input_budget == 272_000


def test_provider_default_is_still_clamped_by_the_selected_server_total():
    model = ModelInfo("chatgpt", "codex", context_window=872_000,
                      server_context_window=32_768, default_input_budget=272_000)
    budget = resolve_context_budget(model, "provider-default")
    assert (budget.selected_context_window, budget.output_reserve, budget.input_budget) == (32_768, 4_096, 28_672)


def test_tiny_total_has_no_dishonest_input_or_output_room():
    budget = resolve_context_budget(ModelInfo("openai", "tiny", context_window=1))
    assert (budget.input_budget, budget.output_reserve, budget.outgoing_max_tokens) == (0, 1, 0)


def test_manual_and_server_constraints_win_and_small_output_is_safe():
    model = ModelInfo("llama.cpp", "local", context_window=100_000,
                      max_context_window=1_000_000, server_context_window=32_768,
                      input_limit=20_000, output_limit=100,
                      manual_fields=frozenset({"context_window"}))
    budget = resolve_context_budget(model)
    assert budget.selected_context_window == 32_768
    assert budget.input_budget == 20_000
    assert budget.output_reserve == 100
    assert budget.outgoing_max_tokens == 100


def test_unknown_and_input_only_models_remain_usable_without_a_total():
    unknown = resolve_context_budget(ModelInfo("openai", "unknown"))
    assert unknown.selected_context_window is None and unknown.input_budget == 128_000 and unknown.estimated
    input_only = resolve_context_budget(ModelInfo("openai", "input-only", input_limit=42_000))
    assert input_only.selected_context_window is None and input_only.input_budget == 42_000 and not input_only.estimated


def test_effective_input_only_budget_is_known_but_fallback_is_not():
    input_only = resolve_context_budget(ModelInfo("openai", "input-only", effective_input_budget=42_000))
    unknown = resolve_context_budget(ModelInfo("openai", "unknown"))
    assert not input_only.estimated
    assert unknown.estimated


def test_context_policy_invalid_config_falls_back_and_setter_rejects_it():
    for value in ("unsafe", [], {}, None, True):
        settings = SettingsManager.in_memory({"context": {"policy": value}})
        assert settings.get_context_policy() == "endpoint-max"
        try:
            settings.set_config_value("context.policy", value)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid context policy was accepted")


def test_legacy_models_json_context_is_a_manual_smaller_cap():
    model = ModelInfo("openai", "legacy", context_window=32_768, max_context_window=872_000,
                      metadata_source="models.json")
    assert resolve_context_budget(model).selected_context_window == 32_768


def test_generation_limit_is_separate_from_reserve_default_and_clamped():
    model = ModelInfo("chatgpt", "large", context_window=200_000, output_limit=80_000)
    assert resolve_context_budget(model).outgoing_max_tokens == 16_384
    budget = resolve_context_budget(model, max_output_tokens=65_536, request_tokens=10_000)
    assert budget.outgoing_max_tokens == budget.output_reserve == 65_536
    assert budget.input_budget == 200_000 - 65_536
    assert resolve_context_budget(model, max_output_tokens=100_000).outgoing_max_tokens == 80_000
    assert resolve_context_budget(model, max_output_tokens=65_536, request_tokens=199_000).outgoing_max_tokens == 1000
    assert resolve_context_budget(model, max_output_tokens=65_536, request_tokens=200_001).outgoing_max_tokens == 0


def test_generation_setting_validation_and_fallback():
    import pytest

    for value in (0, -1, True, "65536", 3.5, None, [], {}):
        settings = SettingsManager.in_memory({"generation": {"maxOutputTokens": value}})
        assert settings.get_generation_max_output_tokens() == 16_384
        with pytest.raises(ValueError, match="positive integer"):
            settings.set_config_value("generation.maxOutputTokens", value)
    settings = SettingsManager.in_memory({"generation": {"maxOutputTokens": 65_536}})
    assert settings.get_generation_max_output_tokens() == 65_536
