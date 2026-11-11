# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.providers.base import ChatResult

_CODEX_USAGE = {
    "input_tokens": 1000,
    "output_tokens": 50,
    "input_tokens_details": {"cached_tokens": 800},
    "output_tokens_details": {"reasoning_tokens": 20},
}


class _Loader:
    def get_system_prompt(self, **_: object) -> str:
        return "test"


def _agent(tmp_path: Path, settings: dict[str, Any] | None = None) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "test")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    return AgentSession(
        SessionManager.in_memory(str(tmp_path)),
        SettingsManager.in_memory(settings or {"tools": {"maxSteps": 2}}),
        registry,
        _Loader(),
        model,
        "off",
        tools=[],
    )


class _Provider:
    def __init__(self, responses: list[dict[str, Any] | BaseException]) -> None:
        self.responses = responses
        self.calls = 0

    async def chat(self, **_: object) -> ChatResult:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(response, BaseException):
            raise response
        return ChatResult(text="done", usage=response, stop_reason="stop", raw={})


def _provider_entries(agent: AgentSession) -> list[dict[str, Any]]:
    return [
        entry
        for entry in agent.session_manager.get_entries()
        if entry.get("type") == "timing" and entry.get("scope") == "provider_request"
    ]


@pytest.mark.asyncio
async def test_success_usage_reaches_event_timing_and_session_stats(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    agent.providers = {"openai": _Provider([_CODEX_USAGE])}  # type: ignore[assignment]
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("hello")

    end = next(event for event in events if event["type"] == "provider_request_end")
    assert (end["inputTokens"], end["cachedTokens"], end["outputTokens"], end["reasoningTokens"]) == (
        1000, 800, 50, 20,
    )
    timing = _provider_entries(agent)
    assert len(timing) == 1
    assert (timing[0]["inputTokens"], timing[0]["cachedTokens"], timing[0]["outputTokens"],
            timing[0]["reasoningTokens"]) == (1000, 800, 50, 20)
    assert timing[0]["provider"] == "openai"
    assert timing[0]["purpose"] == "response"

    cache = agent.get_session_stats()["providerCache"]
    assert cache["requests"] == 1
    assert cache["requestsWithUsage"] == 1
    assert cache["requestsMissingUsage"] == 0
    assert cache["ratioRequests"] == 1
    assert cache["usageComplete"] is True
    assert (cache["input"], cache["cached"], cache["output"], cache["reasoning"]) == (1000, 800, 50, 20)
    assert cache["cacheRatio"] == 0.8
    assert cache["byProvider"]["openai"]["cacheRatio"] == 0.8
    assert cache["byPurpose"]["response"]["cacheRatio"] == 0.8


@pytest.mark.asyncio
async def test_missing_usage_remains_unknown_and_emits_no_counter_keys(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    agent.providers = {"openai": _Provider([{}])}  # type: ignore[assignment]
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("hello")

    end = next(event for event in events if event["type"] == "provider_request_end")
    assert "inputTokens" not in end
    assert "cachedTokens" not in end
    assert "outputTokens" not in end
    assert "reasoningTokens" not in end
    timing = _provider_entries(agent)
    assert len(timing) == 1
    assert not {"inputTokens", "cachedTokens", "outputTokens", "reasoningTokens"} & set(timing[0])

    cache = agent.get_session_stats()["providerCache"]
    assert cache["requests"] == 1
    assert cache["requestsWithUsage"] == 0
    assert cache["requestsMissingUsage"] == 1
    assert cache["ratioRequests"] == 0
    assert cache["usageComplete"] is False
    assert cache["input"] is None
    assert cache["cached"] is None
    assert cache["output"] is None
    assert cache["reasoning"] is None
    assert cache["cacheRatio"] is None


@pytest.mark.asyncio
async def test_each_retry_attempt_is_counted_once_without_hiding_unknown_usage(tmp_path: Path) -> None:
    agent = _agent(
        tmp_path,
        {"tools": {"maxSteps": 2}, "retry": {
            "enabled": True, "mode": "on", "maxRetries": 1, "baseDelayMs": 0, "maxDelayMs": 0,
        }},
    )
    agent.providers = {"openai": _Provider([RuntimeError("temporary network failure"), _CODEX_USAGE])}  # type: ignore[assignment]

    await agent.prompt("hello")

    timing = _provider_entries(agent)
    assert [entry["outcome"] for entry in timing] == ["error", "success"]
    assert "inputTokens" not in timing[0]
    assert timing[1]["inputTokens"] == 1000
    cache = agent.get_session_stats()["providerCache"]
    assert cache["requests"] == 2
    assert cache["requestsWithUsage"] == 1
    assert cache["requestsMissingUsage"] == 1
    assert cache["ratioRequests"] == 1
    assert cache["usageComplete"] is False
    assert cache["input"] == 1000
    assert cache["cached"] == 800
    assert cache["cacheRatio"] == 0.8
