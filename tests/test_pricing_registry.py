# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.run_pricing import PriceSnapshot, TokenUsage
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.core.types import ModelInfo
from one.providers.base import ChatResult


def schedule(**overrides):
    # Deliberately synthetic offline prices, not a production price catalogue.
    return {"schemaVersion": 1, "version": "fixture-v1", "source": "offline-test",
            "currency": "USD", "unit": "perMillionTokens", "cacheMode": "read_write",
            "input": "2", "output": "4", "cacheRead": "1", "cacheWrite": "3", **overrides}


def registry_at(tmp_path, pricing):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"providers": {"openai": [{"id": "gpt-4.1", "pricing": pricing}]}}))
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "offline-dummy")
    return ModelRegistry(auth, str(path))


def test_identity_preservation_and_no_builtin_fallback(tmp_path):
    registry = registry_at(tmp_path, schedule())
    model = registry.find("openai", "gpt-4.1")
    price = registry.get_price_snapshot(model)
    assert price.version == "fixture-v1" and price.source == "offline-test"
    assert model.input_image  # builtin capability merge must preserve pricing
    assert registry.get_price_snapshot(ModelInfo("other", "gpt-4.1")) is None
    assert registry.get_price_snapshot(ModelInfo("openai", "alias")) is None
    assert registry.get_price_snapshot(replace(model, base_url="https://different.invalid")) is None
    assert registry.get_price_snapshot(registry.find("openai", "gpt-4o")) is None
    registry.register_models("openai", ["gpt-4.1"])
    registry.persist_models("openai", ["gpt-4.1"])
    reloaded = ModelRegistry(AuthStorage.in_memory(), str(tmp_path / "models.json"))
    assert reloaded.get_price_snapshot(model) == price
    assert next(m for m in registry.all() if m.id == model.id).pricing == price


@pytest.mark.parametrize("changes", [
    {"schemaVersion": True}, {"schemaVersion": 2}, {"version": ""}, {"source": " "},
    {"currency": "EUR"}, {"unit": "tokens"}, {"input": 0.1}, {"input": True},
    {"output": "NaN"}, {"output": "Infinity"}, {"input": "-1"}, {"input": "invalid"},
    {"cacheMode": []}, {"cacheMode": "unknown"}, {"extra": 1},
    {"cacheMode": "none"}, {"cacheMode": "read"},
])
def test_invalid_price_is_reported_and_unknown(tmp_path, changes):
    registry = registry_at(tmp_path, schedule(**changes))
    assert registry.get_price_snapshot(registry.find("openai", "gpt-4.1")) is None
    assert "Invalid pricing" in str(registry.drain_errors()[0]["error"])


@pytest.mark.parametrize("mode,read,write,bound", [
    ("none", None, None, "6"), ("read", "1", None, "6"),
    ("read_write", "1", "3", "7"), ("read_write", "1", None, None),
])
def test_cache_modes(mode, read, write, bound):
    price = PriceSnapshot.from_config("p", "m", schedule(cacheMode=mode, cacheRead=read, cacheWrite=write))
    assert price.bound(1_000_000, 1_000_000) == (Decimal(bound) if bound else None)
    if mode == "none":
        assert TokenUsage(10, 1, 1, 0).cost(price) is None
    if mode == "read":
        assert TokenUsage(10, 1, 0, 1).cost(price) is None


class Loader:
    def get_system_prompt(self, selected_tools=None):
        return "offline test"


class Provider:
    # Match the adapter wire-format discriminator without constructing a client.
    __module__ = "one.providers.openai_compatible"

    def __init__(self, agent):
        self.agent = agent
        self.calls = 0
        self.reserved = None

    async def chat(self, *, max_tokens, **kwargs):
        self.calls += 1
        context = self.agent._run_context
        self.reserved = next(iter(context._attempts.values()))
        assert self.reserved.state == "reserved"
        # Simulate a concurrent registry change: settlement must use v1.
        self.agent.model_registry.find("openai", "gpt-4.1").pricing = None
        return ChatResult(text="done", raw={}, usage={"prompt_tokens": 10, "completion_tokens": 2,
                                                     "prompt_tokens_details": {"cached_tokens": 0}},
                          stop_reason="stop")


@pytest.mark.asyncio
@pytest.mark.parametrize("pricing,limit,allowed", [
    (schedule(), 10, True), (schedule(), 0.000001, False),
    (None, 10, False), (schedule(cacheWrite=None), 10, False),
    (schedule(cacheMode="none", cacheRead=None, cacheWrite=None), 10, True),
    (None, 0, True),
])
async def test_provider_reservation_offline(tmp_path, pricing, limit, allowed):
    registry = registry_at(tmp_path, pricing)
    model = registry.find("openai", "gpt-4.1")
    agent = AgentSession(SessionManager.in_memory(str(tmp_path)),
                         SettingsManager.in_memory({"runBudget": {"maxCostUsd": limit}}),
                         registry, Loader(), model, "off")
    provider = Provider(agent)
    agent.providers = {"openai": provider}
    if not allowed:
        with pytest.raises(RuntimeError, match="cost"):
            await agent._invoke_provider([])
        assert provider.calls == 0
        return
    await agent._invoke_provider([])
    assert provider.calls == 1
    settled = next(iter(agent._last_run_context._attempts.values()))
    if pricing is None:
        assert settled.cost_usd is None
    else:
        assert provider.reserved.price.version == "fixture-v1"
        assert settled.price is provider.reserved.price
        assert settled.cost_usd == Decimal("0.000028")
