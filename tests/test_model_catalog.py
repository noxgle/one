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
        budget = agent._context_budget()
        assert budget.output_reserve <= min(facts["outputLimit"], 16_384)
        assert budget.outgoing_max_tokens == min(facts["outputLimit"], facts.get("contextWindow", facts["outputLimit"]))


def test_codex_initial_status_uses_output_without_api_context(tmp_path):
    agent = _catalog_agent(tmp_path, ModelInfo("chatgpt", "gpt-6.1-sol", context_window=872_000))
    usage = agent.get_context_usage()
    assert usage["outputLimit"] == 128_000
    assert usage["outputReserve"] == 16_384
    assert usage["selectedContextWindow"] == 872_000
    assert usage["inputLimit"] is None
    agent.settings_manager.set_config_value("generation.maxOutputTokens", 20_000)
    assert agent.get_context_usage()["outputReserve"] == 16_384


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


@pytest.mark.asyncio
async def test_endpoint_provenance_survives_catalog_removal(tmp_path):
    agent = _catalog_agent(tmp_path, ModelInfo("chatgpt", "demo"))
    catalog = agent.model_registry.catalog
    catalog.facts = {("chatgpt", "demo"): {"outputLimit": 80_000}}
    agent.model = catalog.apply(agent.model)

    class Adapter:
        async def list_models_detailed(self, *args):
            return [{"id": "demo", "outputLimit": 42_000}]

    agent.providers = {"chatgpt": Adapter()}
    agent.model_registry.get_api_key_and_headers = lambda model: {"ok": True, "apiKey": "fake"}
    await agent.refresh_context_window_metadata()
    catalog.facts = {}
    restored = catalog.apply(agent.model)
    assert restored.output_limit == 42_000
    assert "output_limit" in restored.endpoint_fields
    from one.core.context_budget import resolve_context_budget
    assert resolve_context_budget(restored, max_output_tokens=8192).outgoing_max_tokens == 8192
    await agent.dispose()


@pytest.mark.parametrize("manual,catalog_value,expected,source", [
    (64_000, 128_000, 64_000, "models.json"),
    (None, 128_000, 128_000, "catalog"),
    (None, None, 8192, "settings"),
    (True, 128_000, 128_000, "catalog"),
    (0, None, 8192, "settings"),
])
@pytest.mark.asyncio
async def test_output_precedence_reaches_codex_payload(tmp_path, manual, catalog_value, expected, source):
    from one.core.agent_session import AgentSession
    from one.core.session_manager import SessionManager
    from one.core.settings_manager import SettingsManager
    from one.providers.base import ChatResult
    from one.providers.codex_responses import CodexResponsesAdapter
    from tests.support.agents import _Loader

    path = tmp_path / "models.json"
    path.write_text(json.dumps({"providers": {"chatgpt": [{
        "id": "demo", "contextWindow": 872_000, "outputLimit": manual,
    }]}}))
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("chatgpt", "fake")
    registry = ModelRegistry(auth, str(path))
    registry.catalog.facts = {("chatgpt", "demo"): {"outputLimit": catalog_value}} if catalog_value else {}
    agent = AgentSession(SessionManager.in_memory(str(tmp_path)),
                         SettingsManager.in_memory({"generation": {"maxOutputTokens": 8192}}),
                         registry, _Loader(), registry.find("chatgpt", "demo"), "off")
    payloads = []

    class Adapter(CodexResponsesAdapter):
        async def chat(self, model, messages, thinking_level, max_tokens=None, **kwargs):
            payloads.append(self._build_payload(model, messages, thinking_level, True, max_tokens=max_tokens))
            return ChatResult(text="A complete offline answer. " * 20, raw={}, usage={}, stop_reason="stop")

    agent.providers = {"chatgpt": Adapter()}
    try:
        await agent.prompt("hello")
        assert payloads and all(p["max_output_tokens"] == expected for p in payloads)
        usage = agent.get_session_stats()["contextUsage"]
        assert usage["selectedOutputLimit"] == expected
        assert usage["outputLimitSource"] == source
        assert usage["lastRequest"]["outgoingMaxTokens"] == expected
        usage["lastRequest"]["outgoingMaxTokens"] = 1
        assert agent.get_context_usage()["lastRequest"]["outgoingMaxTokens"] == expected
    finally:
        await agent.dispose()


def test_catalog_output_beats_endpoint_but_preserves_manual(tmp_path):
    catalog = ModelCatalog(tmp_path)
    catalog.facts = {("openai", "demo"): {"outputLimit": 128_000}}
    model = ModelInfo("openai", "demo", output_limit=8192, endpoint_fields=frozenset({"output_limit"}))
    assert catalog.apply(model).output_limit == 128_000
    manual = replace(model, output_limit=64_000, manual_fields=frozenset({"output_limit"}))
    assert catalog.apply(manual).output_limit == 64_000


def test_manual_output_limit_survives_persist_and_reload(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"providers": {"openai": [{"id": "gpt-6.1-sol", "outputLimit": 64_000}]}}))
    registry = ModelRegistry(AuthStorage.in_memory(), str(path))
    registry.persist_models("openai", ["gpt-6.1-sol"])
    restored = ModelRegistry(AuthStorage.in_memory(), str(path)).find("openai", "gpt-6.1-sol")
    assert restored.output_limit == 64_000
    assert "output_limit" in restored.manual_fields


@pytest.mark.parametrize("existing", [False, True])
def test_registered_endpoint_provenance_refresh_removal_and_resume(tmp_path, existing):
    from one.core.context_budget import resolve_context_budget

    path = tmp_path / "models.json"
    if existing:
        path.write_text(json.dumps({"providers": {"openai": [{"id": "demo", "inputLimit": 9000}]}}))
    registry = ModelRegistry(AuthStorage.in_memory(), str(path))
    registry.catalog.facts = {}
    entry = {"id": "demo", "contextWindow": 10000, "inputLimit": 8000, "outputLimit": 1000}
    assert registry.register_models("openai", [entry]) == (0 if existing else 1)
    model = registry.find("openai", "demo")
    assert model.output_limit == 1000
    assert "output_limit" in model.endpoint_fields
    assert resolve_context_budget(model, max_output_tokens=3000).selected_output_limit == 3000
    assert model.input_limit == (9000 if existing else 8000)

    registry.catalog.facts = {("openai", "demo"): {"outputLimit": 5000, "contextWindow": 20000}}
    overlaid = registry.find("openai", "demo")
    assert (overlaid.output_limit, overlaid.context_window) == (5000, 10000)
    registry.register_models("openai", [{"id": "demo", "outputLimit": 1200}])
    refreshed = registry.find("openai", "demo")
    registry.catalog.facts = {}
    removed = registry.catalog.apply(refreshed)
    assert removed.output_limit == 1200
    assert "output_limit" not in removed.catalog_fields
    assert resolve_context_budget(removed, max_output_tokens=3000).selected_output_limit == 3000

    registry.persist_models("openai", [entry])
    saved = json.loads(path.read_text())["providers"]["openai"][0]
    assert "outputLimit" not in saved and "contextWindow" not in saved
    assert saved.get("inputLimit") == (9000 if existing else None)
    resumed = ModelRegistry(AuthStorage.in_memory(), str(path))
    resumed.catalog.facts = {}
    resumed.register_models("openai", [{"id": "demo", "outputLimit": 1400}])
    model = resumed.find("openai", "demo")
    assert "output_limit" in model.endpoint_fields and "output_limit" not in model.manual_fields
    assert resolve_context_budget(model, max_output_tokens=3000).selected_output_limit == 3000


@pytest.mark.asyncio
async def test_selected_limit_reaches_openai_http_body(tmp_path, monkeypatch):
    import httpx

    from one.core.context_budget import resolve_context_budget
    from one.providers import openai_compatible as module

    bodies = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json, **kwargs):
            bodies.append(json)
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(module.httpx, "AsyncClient", Client)
    adapter = module.OpenAICompatibleAdapter("openai", "https://offline.invalid")
    catalog = ModelCatalog(tmp_path)
    model = catalog.apply(ModelInfo("openai", "gpt-6.1-sol"))
    selected = resolve_context_budget(model, max_output_tokens=8192)
    await adapter.chat("fake", model.id, [{"role": "user", "content": "hi"}], "off",
                       max_tokens=selected.outgoing_max_tokens)
    assert bodies[0]["max_tokens"] == 128_000


@pytest.mark.asyncio
async def test_context_clipping_is_reported_with_actual_payload_limit(tmp_path):
    from one.providers.base import ChatResult
    from one.providers.codex_responses import CodexResponsesAdapter

    agent = _catalog_agent(tmp_path, ModelInfo("chatgpt", "gpt-6.1-sol", context_window=200_000))
    agent.model_registry._auth.set_runtime_api_key("chatgpt", "fake")
    agent._current_request_tokens = lambda request: 199_000
    payloads = []

    class Adapter(CodexResponsesAdapter):
        async def chat(self, model, messages, thinking_level, max_tokens=None, **kwargs):
            payloads.append(self._build_payload(model, messages, thinking_level, True, max_tokens=max_tokens))
            return ChatResult(text="partial", raw={}, usage={}, stop_reason="max_output_tokens", output_truncated=True)

    agent.providers = {"chatgpt": Adapter()}
    try:
        result = await agent._invoke_provider([{"role": "user", "content": "hi"}])
        assert payloads[0]["max_output_tokens"] == 1000
        assert result["outputTruncation"]["selectedOutputLimit"] == 128_000
        assert result["outputTruncation"]["outputLimitReason"] == "available context"
        assert "after context clipping" in agent._assistant_text(result)
        assert "Increase generation.maxOutputTokens" not in agent._assistant_text(result)
        assert agent.get_context_usage()["lastRequest"]["outgoingMaxTokens"] == 1000
    finally:
        await agent.dispose()


@pytest.mark.asyncio
async def test_server_cap_is_reported_in_stats_and_truncation(tmp_path):
    from one.providers.base import ChatResult

    agent = _catalog_agent(tmp_path, ModelInfo("chatgpt", "demo", context_window=100_000,
                                              server_output_limit=4000))
    agent.model_registry._auth.set_runtime_api_key("chatgpt", "fake")
    agent._current_request_tokens = lambda request: 1000

    class Adapter:
        async def chat(self, api_key, model, messages, thinking_level, headers=None, max_tokens=None):
            assert max_tokens == 4000
            return ChatResult(text="partial", raw={}, usage={}, stop_reason="max_output_tokens", output_truncated=True)

    agent.providers = {"chatgpt": Adapter()}
    try:
        result = await agent._invoke_provider([{"role": "user", "content": "hi"}])
        assert result["outputTruncation"]["outputLimitReason"] == "server output hard cap"
        assert "after context clipping" not in agent._assistant_text(result)
        assert "Increase generation.maxOutputTokens" not in agent._assistant_text(result)
        assert agent.get_context_usage()["lastRequest"]["outputLimitReason"] == "server output hard cap"
    finally:
        await agent.dispose()
