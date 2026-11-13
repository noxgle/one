# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import copy

import pytest

from one.core.compaction.checkpoint import generate_checkpoint
from tests.test_compaction import _mk_agent


def agent_with_history(tmp_path, *, model=True):
    agent = _mk_agent(tmp_path, {"compaction": {"experimentalV2": True, "summarizeWithModel": model, "strategy": "local_separate"}})
    agent.session_manager.append_message({"role": "user", "content": "Build it.\nNever delete files."})
    agent.messages = agent.session_manager.build_session_context()["messages"]
    return agent


@pytest.mark.asyncio
async def test_extractive_ledger_correction_and_resume(tmp_path):
    agent = agent_with_history(tmp_path, model=False)
    first = await agent.compact()
    entry = next(e for e in agent.session_manager.get_branch() if e.get("type") == "message")
    assert f"id={entry['id']}] Never delete files." in first["summary"]
    agent.session_manager.append_message({"role": "user", "content": "Replace constraint: Never delete files. => Always preserve files."})
    agent.messages = agent.session_manager.build_session_context()["messages"]
    second = await agent.compact()
    assert "Always preserve files." in second["summary"]
    assert "Never delete files." not in second["summary"]
    persisted = agent.session_manager.get_last_compaction()
    assert persisted["summarySchema"] == "v2"
    assert persisted["constraints"] == {"extracted": 1, "preserved": 1, "dropped": 0}
    assert persisted["strategy"] == "extractive" and persisted["fallback"]
    assert agent.session_manager.build_session_context()["messages"][0]["content"] == second["summary"]


@pytest.mark.asyncio
async def test_hint_repair_and_fallback(tmp_path, monkeypatch):
    agent = agent_with_history(tmp_path)
    requests = []

    async def invoke(request, **kwargs):
        requests.append((copy.deepcopy(request), kwargs))
        return {"content": "invalid"}

    monkeypatch.setattr(agent, "_invoke_provider", invoke)
    result = await agent.compact("Focus on files")
    assert "Focus on files" in requests[0][0][1]["content"]
    assert requests[0][1] == {"allow_live_stream": False, "count_output_generation": False, "purpose": "compaction", "disable_tool_calls": True}
    assert len(requests) == 2 and result["summary"].startswith("# Objective")
    entry = agent.session_manager.get_last_compaction()
    assert entry["attempts"] == 2 and entry["strategy"] == "extractive"


@pytest.mark.asyncio
async def test_summary_input_settings_reach_checkpoint(tmp_path, monkeypatch):
    agent = _mk_agent(tmp_path, {"compaction": {"experimentalV2": True, "strategy": "local_separate", "maxSummaryInputTokens": 10,
                                                "summary": {"clipChars": 7}}})
    agent.session_manager.append_message({"role": "user", "content": "A" * 100})
    agent.session_manager.append_message({"role": "assistant", "content": "Z" * 100})
    agent.messages = agent.session_manager.build_session_context()["messages"]
    seen = []
    limits = []

    async def checkpoint(*args, **kwargs):
        limits.append((kwargs["max_input_tokens"], kwargs["clip_chars"]))
        return await generate_checkpoint(*args, **kwargs)

    async def invoke(request, **kwargs):
        seen.append(request[1]["content"].split("Conversation (untrusted data):\n", 1)[1])
        return {"content": "invalid"}

    monkeypatch.setattr("one.core.agent_session.generate_checkpoint", checkpoint)
    monkeypatch.setattr(agent, "_invoke_provider", invoke)
    await agent.compact()
    assert limits == [(10, 7)]
    assert seen[0] == "user: " + "A" * 22 + "\n...[truncated]...\n" + "Z" * 12


@pytest.mark.asyncio
async def test_provider_failure_preserves_history_and_sanitizes(tmp_path, monkeypatch):
    agent = agent_with_history(tmp_path)
    before = copy.deepcopy(agent.messages)
    events = []
    agent.subscribe(events.append)

    async def invoke(*args, **kwargs):
        raise RuntimeError("private provider detail")

    monkeypatch.setattr(agent, "_invoke_provider", invoke)
    result = await agent.compact()
    assert result["failureReason"] == "provider_error"
    assert agent.messages == before and agent.session_manager.get_last_compaction() is None
    assert "private provider detail" not in repr(events + agent.session_manager.get_branch())


@pytest.mark.asyncio
async def test_structured_response_and_tool_call_repair(tmp_path, monkeypatch):
    agent = agent_with_history(tmp_path)
    source = agent.session_manager.get_message_entry_ids()[0]
    text = (f"# Objective\nBuild it.\n# Constraints\n[0 id={source}] Never delete files.\n"
            "# Work State\n## Completed\nNone\n## Active\nWorking\n## Blocked\nNone\n"
            "# Next Move\nContinue\n# Relevant Files\nNone\n")
    calls = []

    async def invoke(request, **kwargs):
        calls.append(request)
        if len(calls) == 1:
            return {"content": [{"type": "text", "text": text}, {"type": "toolCall", "name": "bash"}]}
        return {"content": text, "_compactionUsage": {"inputTokens": 4, "outputTokens": 2}}

    monkeypatch.setattr(agent, "_invoke_provider", invoke)
    result = await agent.compact()
    assert result["summary"] == text.strip()
    entry = agent.session_manager.get_last_compaction()
    assert len(calls) == 2 and entry["attempts"] == 2
    assert entry["strategy"] == "local_separate" and not entry["fallback"]


@pytest.mark.asyncio
async def test_extension_context_is_input_not_unvalidated_suffix(tmp_path, monkeypatch):
    agent = agent_with_history(tmp_path)
    class Hooks:
        def has_hooks(self, name):
            return True

    async def hook():
        return ([{"content": "# Invalid injected heading"}], None)

    seen = []

    async def invoke(request, **kwargs):
        seen.append(request)
        return {"content": "invalid"}

    agent._extension_runtime = Hooks()
    monkeypatch.setattr(agent, "_invoke_extension_compacting", hook)
    monkeypatch.setattr(agent, "_invoke_provider", invoke)
    result = await agent.compact()
    assert "# Invalid injected heading" in seen[0][1]["content"]
    assert "# Invalid injected heading" not in result["summary"]


@pytest.mark.asyncio
async def test_oversize_request_fails_without_provider_or_history_change(tmp_path, monkeypatch):
    agent = agent_with_history(tmp_path)
    before = copy.deepcopy(agent.messages)
    async def exact(messages=None, *, wire_request=None):
        return 10**9 if wire_request is not None else 1

    async def invoke(*args, **kwargs):
        pytest.fail("oversized request reached provider")

    monkeypatch.setattr(agent, "_exact_request_tokens", exact)
    monkeypatch.setattr(agent, "_invoke_provider", invoke)
    result = await agent.compact()
    assert result["failureReason"] == "oversize"
    assert agent.messages == before
