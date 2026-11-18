# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import copy

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
                   disable_tool_calls=False, prompt_cache=None):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(tools), disable_tool_calls,
                           copy.deepcopy(prompt_cache)))
        return ChatResult(text=("# Objective\nBuild\n# Constraints\nNone\n# Work State\n"
                                "## Completed\nNone\n## Active\nWorking\n## Blocked\nNone\n"
                                "# Next Move\nContinue\n# Relevant Files\nNone\n"),
                          raw={}, usage={}, had_native_tool_call=True,
                          native_tool_calls=[{"id": "x", "name": "bash", "arguments": {}}])


@pytest.mark.asyncio
async def test_compaction_summary_prefix_matches_normal_request_prefix(tmp_path):
    """The compaction summary request reuses the same stable prefix as a
    normal request (system prompt + history up to the snapshot point)."""
    provider = Provider()
    agent = _mk_agent(tmp_path, {"compaction": {"experimentalV2": True}}, provider)
    provider.agent = agent
    _seed(agent, 10)
    prefix = agent._flatten_messages_for_provider()
    await agent.compact()
    assert len(provider.calls) == 2
    for request, tools, disabled, _cache in provider.calls:
        assert request[:len(prefix)] == prefix
        assert tools and disabled
    # The summary instruction is appended after the reused prefix.
    assert provider.calls[0][0][-1]["role"] == "developer"
    assert agent.session_manager.get_last_compaction()["strategy"] == "extractive"


@pytest.mark.asyncio
async def test_codex_compaction_payload_keeps_developer_prefix(tmp_path):
    """The codex adapter serializes the compaction request with the system
    prompt as the first developer message, preserving the cache prefix."""
    from one.providers.codex_responses import CodexResponsesAdapter

    provider = Provider()
    agent = _mk_agent(tmp_path, {"compaction": {"experimentalV2": True}}, provider)
    provider.agent = agent
    _seed(agent, 3)
    prefix = agent._flatten_messages_for_provider()
    adapter = CodexResponsesAdapter()
    payload = adapter._build_payload("gpt-6.2-sol", prefix, "medium", stream=False)
    assert payload["input"][0]["role"] == "developer"
    assert payload["input"][0]["content"][0]["type"] == "input_text"
    assert payload["instructions"] != payload["input"][0]["content"][0]["text"]