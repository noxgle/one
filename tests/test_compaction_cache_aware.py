# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one

from __future__ import annotations

import copy
from unittest.mock import AsyncMock

import pytest

from one.providers.base import ChatResult
from tests.test_compaction import _mk_agent, _seed


class Provider:
    supports_cache_aware_compaction = True
    supports_native_tools = True
    summary_position = "suffix"

    def __init__(self):
        self.calls = []
        self.agent = None

    async def chat(self, api_key, model, messages, thinking_level, headers=None, tools=None, *,
                   disable_tool_calls=False):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(tools), disable_tool_calls,
                           copy.deepcopy(self.agent.messages)))
        return ChatResult(text=("# Objective\nBuild\n# Constraints\nNone\n# Work State\n"
                                "## Completed\nNone\n## Active\nWorking\n## Blocked\nNone\n"
                                "# Next Move\nContinue\n# Relevant Files\nNone\n"),
                          raw={}, usage={}, had_native_tool_call=True,
                          native_tool_calls=[{"id": "x", "name": "bash", "arguments": {}}])


@pytest.mark.asyncio
async def test_cache_prefix_and_no_tool_dispatch(tmp_path):
    provider = Provider()
    agent = _mk_agent(tmp_path, {"compaction": {"experimentalV2": True}}, provider)
    provider.agent = agent
    _seed(agent, 10)
    before = copy.deepcopy(agent.messages)
    prefix = agent._flatten_messages_for_provider()
    await agent.compact()
    assert len(provider.calls) == 2
    for request, tools, disabled, live in provider.calls:
        assert request[:len(prefix)] == prefix
        assert tools and disabled and live == before
    assert provider.calls[0][0][-1]["role"] == "developer"
    assert agent.session_manager.get_last_compaction()["strategy"] == "extractive"
    assert agent.messages[0]["customType"] == "compaction_summary"
    assert agent.session_manager.build_session_context()["messages"][0]["customType"] == "compaction_summary"
    projected = agent._flatten_messages_for_provider()
    assert projected[-1]["content"] == agent._flatten_conversation([agent.messages[0]])[0]["content"]


@pytest.mark.asyncio
async def test_unsafe_provider_uses_separate(tmp_path):
    provider = Provider()
    provider.supports_cache_aware_compaction = False
    agent = _mk_agent(tmp_path, {"compaction": {"experimentalV2": True}}, provider)
    provider.agent = agent
    _seed(agent, 2)
    await agent.compact()
    assert provider.calls[0][0][0]["content"].startswith("Create a checkpoint")
    assert not provider.calls[0][2]


def test_flag_off_preserves_summary_position(tmp_path):
    provider = Provider()
    agent = _mk_agent(tmp_path, {"compaction": {"experimentalV2": False}}, provider)
    agent.messages = [{"role": "custom", "customType": "compaction_summary", "content": "summary"},
                      {"role": "user", "content": "after"}]
    assert agent._flatten_messages_for_provider()[1:] == agent._flatten_conversation(agent.messages)


@pytest.mark.asyncio
async def test_images_force_separate(tmp_path):
    provider = Provider()
    agent = _mk_agent(tmp_path, {"compaction": {"experimentalV2": True}}, provider)
    provider.agent = agent
    _seed(agent, 2)
    agent._images = [{"blobHash": "image"}]
    # The stub cannot consume images; observe the shaped request at the boundary.
    agent._invoke_provider = invoke = AsyncMock(
        return_value={"content": [{"type": "text", "text": "invalid"}]})
    result = await agent.compact()
    assert invoke.call_args_list[0].args[0][0]["content"].startswith("Create a checkpoint")
    assert result["strategy"] != "local_cache_aware"


@pytest.mark.asyncio
async def test_mcp_schema_change_aborts_without_commit(tmp_path):
    from types import SimpleNamespace

    provider = Provider()
    agent = _mk_agent(tmp_path, {"compaction": {"experimentalV2": True}}, provider)
    provider.agent = agent
    _seed(agent, 2)
    tool = SimpleNamespace(name="external", description="before", input_schema={"type": "object"})
    agent._mcp_manager = SimpleNamespace(tools=lambda: [tool])
    agent._model_visible_tools = lambda: ["external"]
    before = copy.deepcopy(agent.messages)
    original = provider.chat

    async def change(*args, **kwargs):
        tool.input_schema["changed"] = True
        return await original(*args, **kwargs)

    provider.chat = change
    result = await agent.compact()
    assert result["aborted"]
    assert agent.messages == before
    assert agent.session_manager.get_last_compaction() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 422])
async def test_openai_rejection_falls_back_without_leaking_body(tmp_path, monkeypatch, caplog, status):
    import httpx

    from one.providers.openai_compatible import OpenAICompatibleAdapter

    requests = []
    secret = "private rejection content"
    valid = ("# Objective\nBuild\n# Constraints\nNone\n# Work State\n"
             "## Completed\nNone\n## Active\nWorking\n## Blocked\nNone\n"
             "# Next Move\nContinue\n# Relevant Files\nNone\n")

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            requests.append(kwargs["json"])
            if len(requests) == 1:
                return httpx.Response(status, json={"error": secret})
            return httpx.Response(200, json={"choices": [{"message": {"content": valid}}],
                                             "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    provider = OpenAICompatibleAdapter("openai", "https://api.openai.com", supports_native_tools=True)
    agent = _mk_agent(tmp_path, {"compaction": {"experimentalV2": True}}, provider)
    _seed(agent, 2, prefix="OPAQUE_TRANSCRIPT_MARKER {}")
    events = []
    agent.subscribe(events.append)
    result = await agent.compact(custom_instructions="PRIVATE_PROMPT_MARKER")
    assert len(requests) == 2
    assert result["strategy"] == "local_separate" and result["fallback"]
    assert result["attempts"] == 2 and all(value is None for value in result["usage"].values())
    ends = [event for event in events if event["type"] == "provider_request_end"]
    assert [event["purpose"] for event in ends] == ["compaction", "compaction"]
    assert [event["outcome"] for event in ends] == ["error", "success"]
    assert secret not in repr(events) + repr(result) + repr(agent.session_manager.get_entries())
    stats = agent.get_session_stats()["providerCache"]["byPurpose"]["compaction"]
    assert stats["requests"] == 2 and stats["requestsMissingUsage"] == 1
    diagnostics = repr([event for event in events if event["type"].startswith("provider_request_")]) + caplog.text
    for marker in (secret, "OPAQUE_TRANSCRIPT_MARKER", "PRIVATE_PROMPT_MARKER", valid):
        assert marker not in diagnostics


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["openai", "anthropic", "gemini", "codex"])
async def test_normal_wire_projection_and_legacy_bytes(tmp_path, monkeypatch, provider_name):
    import json

    import httpx

    from one.providers.anthropic import AnthropicAdapter
    from one.providers.codex_responses import CodexResponsesAdapter
    from one.providers.gemini import GeminiAdapter
    from one.providers.openai_compatible import OpenAICompatibleAdapter

    captured = []

    class Captured(Exception):
        pass

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            captured.append(json.dumps(kwargs["json"], separators=(",", ":")).encode())
            raise Captured

        def stream(self, *args, **kwargs):
            captured.append(json.dumps(kwargs["json"], separators=(",", ":")).encode())
            raise Captured

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    adapter = {"openai": OpenAICompatibleAdapter("openai", "https://api.openai.com"),
               "anthropic": AnthropicAdapter(), "gemini": GeminiAdapter(),
               "codex": CodexResponsesAdapter()}[provider_name]
    agent = _mk_agent(tmp_path, {"compaction": {"experimentalV2": False}}, adapter)
    agent.messages = [{"role": "custom", "customType": "compaction_summary", "content": "SUMMARY_MARKER"},
                      {"role": "user", "content": "HISTORY_MARKER"}]
    before = copy.deepcopy(agent.messages)
    legacy = [{"role": "system", "content": agent._provider_system_prompt()},
              *agent._flatten_conversation(agent.messages)]
    for messages in (legacy, agent._flatten_messages_for_provider()):
        with pytest.raises(Captured):
            await adapter.chat("key", "model", messages, "off")
    assert captured[0] == captured[1]
    agent.settings_manager = _mk_agent(tmp_path, {"compaction": {"experimentalV2": True}}).settings_manager
    projected = agent._flatten_messages_for_provider()
    with pytest.raises(Captured):
        await adapter.chat("key", "model", projected, "off")
    wire = captured[-1]
    assert (wire.index(b"SUMMARY_MARKER") > wire.index(b"HISTORY_MARKER")) == (provider_name == "codex")
    assert agent.messages == before
    if provider_name == "codex":
        payload = adapter._build_payload("model", projected, "off", False)
        assert "SUMMARY_MARKER" in repr(payload["input"][-1])
