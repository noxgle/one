"""Regression coverage for subagent model availability preflight."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from one.core.agent_session import AgentSession
from tests.test_subagents import _Loader, _mk_agent


def _assert_no_child_allocation(agent: AgentSession) -> None:
    assert agent.subagent_monitor.snapshot()["children"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "expected_error"),
    [
        ("missing-provider/model", "Provider 'missing-provider' is not available"),
        ("openai/", "Model is invalid: openai/"),
        ("/model", "Model is invalid: /model"),
    ],
)
async def test_spawn_subagent_model_preflight_returns_parent_tool_error_without_child(
    tmp_path: Path, model: str, expected_error: str
) -> None:
    agent = _mk_agent(tmp_path, tools=["spawn_subagent", "finish"])

    result = await agent._run_tool_call("spawn_subagent", {"task": "child", "model": model})

    assert result["ok"] is False
    assert expected_error in result["error"]
    tool_result = json.loads(agent.messages[-1]["content"])
    assert expected_error in tool_result["error"]
    _assert_no_child_allocation(agent)


@pytest.mark.asyncio
async def test_spawn_subagent_invalid_preset_provider_returns_parent_tool_error_without_child(tmp_path: Path) -> None:
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "unavailable.md").write_text(
        "---\nversion: 1\nid: unavailable\nname: Unavailable\nmodel: missing-provider/model\n---\nCHILD ROLE\n",
        encoding="utf-8",
    )
    agent = _mk_agent(tmp_path, tools=["spawn_subagent", "finish"])
    agent.resource_loader = _Loader()
    agent.resource_loader.agent_dir = str(tmp_path)  # type: ignore[attr-defined]

    result = await agent._run_tool_call("spawn_subagent", {"task": "child", "preset": "unavailable"})

    assert result["ok"] is False
    assert "Provider 'missing-provider' is not available" in result["error"]
    tool_result = json.loads(agent.messages[-1]["content"])
    assert "Provider 'missing-provider' is not available" in tool_result["error"]
    _assert_no_child_allocation(agent)


@pytest.mark.asyncio
async def test_spawn_subagent_dynamic_supported_model_reaches_run_subagent(tmp_path: Path) -> None:
    agent = _mk_agent(tmp_path, tools=["spawn_subagent", "finish"])
    captured: dict[str, Any] = {}

    async def capture(task: str, model: Any, tools: list[str], depth: int, **kwargs: Any) -> dict[str, Any]:
        captured.update(task=task, model=model, tools=tools, depth=depth, **kwargs)
        return {"ok": True, "summary": "done"}

    agent._run_subagent = capture  # type: ignore[method-assign]

    result = await agent._spawn_subagent({"task": "child", "model": "openai/dynamic-supported-model"})

    assert result["ok"] is True
    assert (captured["model"].provider, captured["model"].id) == ("openai", "dynamic-supported-model")
    assert captured["tools"] == ["spawn_subagent", "finish"]


class _NotFoundProvider:
    def __init__(self, status: int = 404) -> None:
        self.calls = 0
        self.status = status

    async def chat(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
        response = httpx.Response(self.status, request=request)
        raise httpx.HTTPStatusError("Client error '404 Not Found' for url", request=request, response=response)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_subagent_404_does_not_retry_forever_in_unlimited_mode(tmp_path: Path, status: int) -> None:
    agent = _mk_agent(
        tmp_path,
        tools=["finish"],
        settings_override={
            "retry": {"enabled": True, "mode": "unlimited", "baseDelayMs": 1, "maxDelayMs": 1},
            "subagents": {"idleTimeoutSec": 10, "maxDurationSec": 0},
        },
    )
    provider = _NotFoundProvider(status)
    agent.providers = {"openai": provider}

    result = await asyncio.wait_for(agent._run_subagent("child", agent.model, ["finish"], 0), timeout=3)

    assert result["ok"] is False
    assert provider.calls == 1

    assert result["errorType"] == "SubagentProviderUnavailable"
    assert "openai/gpt-4.1" in result["error"]
    children = agent.subagent_monitor.snapshot()["children"]
    assert children[0]["status"] == "failed"
    assert children[0]["presetName"] == "Default"


@pytest.mark.parametrize("message", ["chatgpt API error 429: rate limited", "chatgpt API error 503: down", "network disconnected"])
def test_transient_provider_failures_remain_retryable(message):
    from one.core.agent_session import AgentSession
    assert AgentSession._is_retryable_error(message)


@pytest.mark.parametrize("message", ["chatgpt API error 404: missing", "model_not_found", "model is not supported"])
def test_adapter_model_errors_are_permanent(message):
    from one.core.agent_session import AgentSession
    assert not AgentSession._is_retryable_error(message)
