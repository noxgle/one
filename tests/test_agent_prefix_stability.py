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


class _Loader:
    def get_system_prompt(self, **_: object) -> str:
        return "test"


def _agent(tmp_path: Path, settings: dict[str, Any] | None = None) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("chatgpt", "test")
    registry = ModelRegistry.create(auth)
    model = registry.find("chatgpt", "gpt-6.2-sol")
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
    def __init__(self) -> None:
        self.calls = 0
        self.last_payload: dict[str, Any] | None = None

    async def chat(self, prompt_cache: dict[str, Any] | None = None, **kwargs: Any) -> ChatResult:
        self.calls += 1
        self.last_payload = kwargs
        return ChatResult(text="done", usage={}, stop_reason="stop", raw={})


@pytest.mark.asyncio
async def test_stable_prefix_hash_identical_across_requests(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    provider = _Provider()
    agent.providers = {"chatgpt": provider}  # type: ignore[assignment]
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("hello")
    await agent.prompt("world")

    starts = [e for e in events if e["type"] == "provider_request_start"]
    assert len(starts) == 2
    assert starts[0]["prefixHash"] == starts[1]["prefixHash"]


@pytest.mark.asyncio
async def test_system_prompt_is_first_developer_message(tmp_path: Path) -> None:
    from one.providers.codex_responses import CodexResponsesAdapter

    adapter = CodexResponsesAdapter()
    payload = adapter._build_payload(
        "gpt-6.2-sol",
        [{"role": "system", "content": "be terse"}, {"role": "user", "content": "hi"}],
        "medium", stream=False,
    )
    first = payload["input"][0]
    assert first["role"] == "developer"
    assert first["content"][0]["type"] == "input_text"
    assert first["content"][0]["text"] == "be terse"
    # instructions stays a short constant, not the full system prompt.
    assert payload["instructions"] != "be terse"


@pytest.mark.asyncio
async def test_native_tool_definitions_sorted_stable(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    # Force a known tool set and confirm ordering is deterministic.
    agent._active_tools = ["bash", "read", "write", "edit", "grep", "finish"]
    defs = agent._native_tool_definitions()
    names = [d["name"] for d in defs]
    assert names == sorted(names)


@pytest.mark.asyncio
async def test_prewarm_fires_once_when_enabled(tmp_path: Path) -> None:
    agent = _agent(tmp_path, {
        "tools": {"maxSteps": 2},
        "providerProfiles": {"chatgpt": {"promptCache": {"enabled": True, "prewarm": True}}},
    })
    provider = _Provider()
    agent.providers = {"chatgpt": provider}  # type: ignore[assignment]
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("hello")
    await agent.prompt("world")

    purposes = [e.get("purpose") for e in events if e["type"] == "provider_request_start"]
    assert purposes.count("prewarm") == 1
    assert purposes.count("response") == 2


@pytest.mark.asyncio
async def test_prewarm_skipped_when_disabled(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    provider = _Provider()
    agent.providers = {"chatgpt": provider}  # type: ignore[assignment]
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("hello")

    purposes = [e.get("purpose") for e in events if e["type"] == "provider_request_start"]
    assert "prewarm" not in purposes