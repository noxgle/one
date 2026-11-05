# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from one.core.agent_session import _effective_context_window
from one.core.auth_storage import AuthStorage
from one.core.model_catalog import ModelCatalog, _normalise
from one.core.model_registry import ModelRegistry
from one.core.types import ModelInfo


def test_catalog_uses_exact_provider_and_model_without_chatgpt_alias(tmp_path):
    catalog = ModelCatalog(tmp_path)
    model = catalog.apply(ModelInfo("chatgpt", "gpt-6-luna"))
    assert model.context_window is None
    assert model.max_context_window is None
    # The pinned public source may legitimately contain an OpenAI model with
    # this id; it must never be aliased onto the ChatGPT/Codex backend.
    assert catalog.apply(ModelInfo("openai", "gpt-6-luna")).provider == "openai"


def test_catalog_manual_limits_are_authoritative(tmp_path):
    catalog = ModelCatalog(tmp_path)
    model = catalog.apply(ModelInfo("chatgpt", "gpt-6-luna", context_window=100_000, metadata_source="models.json"))
    assert model.context_window == 100_000
    assert model.max_context_window is None


def test_catalog_removes_fields_and_entire_models_reversibly(tmp_path):
    catalog = ModelCatalog(tmp_path)
    base = ModelInfo("openai", "demo", context_window=10_000)
    catalog.facts = {("openai", "demo"): {"contextWindow": 1_000_000, "inputLimit": 272_000}}
    overlaid = catalog.apply(base)
    assert (overlaid.context_window, overlaid.input_limit) == (1_000_000, 272_000)

    catalog.facts = {("openai", "demo"): {"contextWindow": 1_000_000}}
    without_input = catalog.apply(overlaid)
    assert (without_input.context_window, without_input.input_limit) == (1_000_000, None)

    catalog.facts = {}
    restored = catalog.apply(without_input)
    assert (restored.context_window, restored.input_limit) == (10_000, None)


def test_catalog_removal_never_clobbers_equal_endpoint_value(tmp_path):
    catalog = ModelCatalog(tmp_path)
    catalog.facts = {("openai", "demo"): {"contextWindow": 100_000}}
    overlaid = catalog.apply(ModelInfo("openai", "demo", context_window=None))
    endpoint = replace(overlaid, endpoint_fields=frozenset({"context_window"}), metadata_source="provider endpoint")

    catalog.facts = {}
    retained = catalog.apply(endpoint)
    assert retained.context_window == 100_000
    assert retained.metadata_source == "provider endpoint"


def test_registry_catalog_refresh_removes_old_overlay_and_keeps_manual_field(tmp_path):
    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth, str(tmp_path / "models.json"))
    registry._models = [ModelInfo("openai", "demo", context_window=50_000)]
    registry.catalog.facts = {("openai", "demo"): {"contextWindow": 1_000_000, "inputLimit": 272_000}}
    first = registry.find("openai", "demo")
    assert first is not None and first.input_limit == 272_000

    registry.catalog.facts = {("openai", "demo"): {"contextWindow": 500_000}}
    second = registry.catalog.apply(first)
    assert (second.context_window, second.input_limit) == (500_000, None)
    registry.catalog.facts = {}
    assert registry.catalog.apply(second).context_window == 50_000

    manual = ModelInfo("openai", "manual", context_window=100_000, input_limit=50_000,
                       metadata_source="models.json", manual_fields=frozenset({"context_window", "input_limit"}))
    registry.catalog.facts = {("openai", "manual"): {"contextWindow": 1_000_000, "inputLimit": 272_000}}
    assert registry.catalog.apply(manual).input_limit == 50_000


def test_codex_default_budget_is_distinct_from_advertised_maximum():
    model = ModelInfo("chatgpt", "mock", context_window=1_000_000, max_context_window=1_000_000,
                      default_context_window=272_000, default_input_budget=272_000)
    assert _effective_context_window(model) == 983_616


def test_catalog_accepts_explicit_models_dev_provider_alias_only():
    facts = _normalise({"google": {"models": {"gemini-test": {"limit": {"context": 10, "output": 2}}}}, "openai": {"models": {"gemini-test": {"limit": {"context": 99}}}}})
    assert facts[("gemini", "gemini-test")] == {"contextWindow": 10, "outputLimit": 2}
    assert facts[("openai", "gemini-test")]["contextWindow"] == 99


@pytest.mark.asyncio
async def test_catalog_refresh_is_opt_in_validated_and_cached(tmp_path):
    calls = 0

    async def fetch(_url: str):
        nonlocal calls
        calls += 1
        return {"providers": {"openai": {"gpt-test": {"contextWindow": 64_000, "outputLimit": 4_000}}}}

    disabled = ModelCatalog(tmp_path, fetcher=fetch)
    assert not await disabled.refresh()
    enabled = ModelCatalog(tmp_path, fetcher=fetch, enabled=True)
    assert await enabled.refresh()
    assert calls == 1
    cache = json.loads((tmp_path / "model-catalog.json").read_text())
    assert cache["providers"]["openai"]["gpt-test"]["contextWindow"] == 64_000
    assert enabled.apply(ModelInfo("openai", "gpt-test")).output_limit == 4_000


@pytest.mark.asyncio
async def test_catalog_outage_and_malformed_payload_keep_snapshot(tmp_path):
    async def malformed(_url: str):
        return {"providers": {"openai": {"x": {"contextWindow": True}}}}

    catalog = ModelCatalog(tmp_path, fetcher=malformed, enabled=True)
    assert not await catalog.refresh()
    assert catalog.apply(ModelInfo("chatgpt", "gpt-6-luna")).context_window is None


@pytest.mark.asyncio
async def test_successful_refresh_is_authoritative_but_outage_keeps_last_cache(tmp_path):
    responses = [
        {"providers": {"openai": {"demo": {"contextWindow": 100_000}}}},
        {"providers": {"openai": {"other": {"contextWindow": 200_000}}}},
    ]

    async def fetch(_url: str):
        return responses.pop(0)

    catalog = ModelCatalog(tmp_path, fetcher=fetch, enabled=True)
    assert await catalog.refresh()
    assert catalog.facts_for("openai", "demo")
    assert await catalog.refresh()
    assert catalog.facts_for("openai", "demo") is None
    assert catalog.facts_for("openai", "other")

    async def outage(_url: str):
        raise OSError("offline")

    catalog.fetcher = outage
    assert not await catalog.refresh()
    assert catalog.facts_for("openai", "other")


def test_codex_output_only_exact_id_fallback_and_precedence(tmp_path):
    catalog = ModelCatalog(tmp_path)
    catalog.facts = {("openai", "demo"): {
        "contextWindow": 1_000_000, "inputLimit": 900_000, "outputLimit": 100_000,
    }}
    model = catalog.apply(ModelInfo("chatgpt", "demo"))
    assert model.output_limit == 100_000
    assert model.context_window is None and model.input_limit is None
    assert catalog.apply(ModelInfo("chatgpt", "demo-other")).output_limit is None
    assert catalog.apply(ModelInfo("ollama", "demo")).output_limit is None
    manual = catalog.apply(ModelInfo("chatgpt", "demo", output_limit=5000, metadata_source="models.json"))
    assert manual.output_limit == 5000
    catalog.facts[("chatgpt", "demo")] = {"outputLimit": 50_000}
    assert catalog.apply(model).output_limit == 50_000
    catalog.facts = {}
    assert catalog.apply(model).output_limit is None


def _catalog_agent(tmp_path, model, settings=None):
    from one.core.agent_session import AgentSession
    from one.core.session_manager import SessionManager
    from one.core.settings_manager import SettingsManager
    from tests.support.agents import _Loader

    registry = ModelRegistry(AuthStorage.in_memory(), str(tmp_path / "models.json"))
    return AgentSession(SessionManager.in_memory(str(tmp_path)),
                        SettingsManager.in_memory(settings), registry, _Loader(), model, "off")


def test_all_bundled_output_limits_reach_initial_status(tmp_path):
    catalog = ModelCatalog(tmp_path)
    # Exercise every supported provider/model in the pinned package, not just Sol.
    for (provider, model_id), facts in catalog.facts.items():
        if "outputLimit" not in facts:
            continue
        agent = _catalog_agent(tmp_path, ModelInfo(provider, model_id))
        usage = agent.get_context_usage()
        assert usage["outputLimit"] == facts["outputLimit"], (provider, model_id)
        assert agent._context_budget().output_reserve == min(
            facts["outputLimit"], facts.get("contextWindow", facts["outputLimit"])
        )


def test_codex_initial_status_uses_output_without_api_context(tmp_path):
    agent = _catalog_agent(tmp_path, ModelInfo("chatgpt", "gpt-6.1-sol", context_window=872_000))
    usage = agent.get_context_usage()
    assert usage["outputLimit"] == usage["outputReserve"] == 128_000
    assert usage["selectedContextWindow"] == 872_000
    assert usage["inputLimit"] is None
    agent.settings_manager.set_config_value("generation.maxOutputTokens", 20_000)
    assert agent.get_context_usage()["outputReserve"] == 20_000


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [None, 800_000])
@pytest.mark.parametrize("manual", [False, True])
async def test_endpoint_output_updates_selected_model_even_without_context(tmp_path, window, manual):
    model = ModelInfo("chatgpt", "demo", output_limit=5000 if manual else None,
                      manual_fields=frozenset({"output_limit"}) if manual else frozenset())
    agent = _catalog_agent(tmp_path, model)

    class Adapter:
        async def list_models_detailed(self, *args):
            return [{"id": "demo", "contextWindow": window, "outputLimit": 42_000}]

    agent.providers = {"chatgpt": Adapter()}
    agent.model_registry.get_api_key_and_headers = lambda model: {"ok": True, "apiKey": "fake"}
    await agent.refresh_context_window_metadata()
    assert agent.get_context_usage()["outputLimit"] == (5000 if manual else 42_000)
