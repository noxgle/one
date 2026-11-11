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


class _CaptureProvider:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
        on_delta: Any = None,
        on_thinking_delta: Any = None,
        max_tokens: int | None = None,
        images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
        tools: list[dict[str, Any]] | None = None,
        stream_transport_timeout: float | None = None,
        codex_affinity_id: str | None = None,
    ) -> ChatResult:
        self.calls.append(codex_affinity_id)
        return ChatResult(text="done", usage={}, stop_reason="stop", raw={})


def _agent(
    tmp_path: Path,
    *,
    provider: str = "chatgpt",
    model_id: str = "gpt-5.6-sol",
    settings: dict[str, Any] | None = None,
) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key(provider, "test")
    registry = ModelRegistry.create(auth)
    model = registry.find(provider, model_id)
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


@pytest.mark.asyncio
async def test_chatgpt_requests_share_one_stable_affinity_by_default(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    provider = _CaptureProvider()
    agent.providers = {"chatgpt": provider}  # type: ignore[assignment]

    await agent.prompt("one")
    await agent.prompt("two")

    assert provider.calls == [agent.session_id, agent.session_id]
    assert agent.codex_affinity_id == agent.session_id


@pytest.mark.asyncio
async def test_profile_flag_restores_legacy_random_only_behavior(tmp_path: Path) -> None:
    agent = _agent(
        tmp_path,
        settings={
            "tools": {"maxSteps": 2},
            "providerProfiles": {"chatgpt": {"experimentalStableSessionId": False}},
        },
    )
    provider = _CaptureProvider()
    agent.providers = {"chatgpt": provider}  # type: ignore[assignment]

    await agent.prompt("one")
    await agent.prompt("two")

    assert provider.calls == [None, None]


@pytest.mark.asyncio
async def test_non_chatgpt_provider_never_receives_codex_affinity(tmp_path: Path) -> None:
    agent = _agent(tmp_path, provider="openai", model_id="gpt-4.1")
    provider = _CaptureProvider()
    agent.providers = {"openai": provider}  # type: ignore[assignment]

    await agent.prompt("one")

    assert provider.calls == [None]


def test_new_session_has_its_own_affinity(tmp_path: Path) -> None:
    first = SessionManager.in_memory(str(tmp_path))
    second = SessionManager.in_memory(str(tmp_path))
    assert first.get_effective_affinity_id() == first.session_id
    assert second.get_effective_affinity_id() == second.session_id
    assert first.session_id != second.session_id


def test_branch_and_fork_share_source_affinity_across_reloads(tmp_path: Path) -> None:
    directory = tmp_path / "sessions"
    source = SessionManager.create(str(tmp_path), str(directory))
    root = source.append_message({"role": "user", "content": "branch point"})
    source.append_message({"role": "assistant", "content": [{"type": "text", "text": "answer"}]})
    source_path = source.session_file
    source_affinity = source.session_id
    assert source.get_effective_affinity_id() == source_affinity

    branch_path = source.create_branched_session(root)
    assert branch_path
    branched = SessionManager.open(branch_path, str(directory))
    assert branched.session_id != source_affinity
    assert branched.get_effective_affinity_id() == source_affinity

    forked = SessionManager.fork_from(source_path, str(tmp_path / "forked-cwd"), str(directory))
    assert forked.session_id != source_affinity
    assert forked.get_effective_affinity_id() == source_affinity

    # A fork of a fork keeps the original lineage affinity.
    nested = SessionManager.fork_from(forked.session_file, str(tmp_path / "nested-cwd"), str(directory))
    assert nested.get_effective_affinity_id() == source_affinity

    reopened = SessionManager.open(branch_path, str(directory))
    assert reopened.get_effective_affinity_id() == source_affinity


@pytest.mark.asyncio
async def test_subagent_inherits_parent_affinity(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    provider = _CaptureProvider()
    agent.providers = {"chatgpt": provider}  # type: ignore[assignment]

    await agent._run_subagent("task", agent.model, [], 0)  # noqa: SLF001

    assert provider.calls == [agent.codex_affinity_id]
