from __future__ import annotations

import json
from typing import Any

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.core.tool_output_pruning import prune_stale_tool_outputs


def _tokens(message: dict[str, Any]) -> int:
    return max(1, len(str(message.get("content", ""))) // 4)


def _tool(tool: str, output: str, **extra: Any) -> dict[str, Any]:
    payload = {"ok": True, "tool": tool, "result": output, **extra}
    return {"role": "toolResult", "content": json.dumps(payload), "timestamp": 1, "toolCallId": "outer-call"}


def _prune(messages: list[dict[str, Any]], recent_tokens: int = 50) -> tuple[list[dict[str, Any]], dict[str, int]]:
    return prune_stale_tool_outputs(
        messages,
        enabled=True,
        recent_tokens=recent_tokens,
        min_result_tokens=10,
        marker="Tool result is stale.",
        estimate_tokens=_tokens,
    )


def test_prunes_old_bash_read_and_mcp_results_but_protects_recent_and_non_tools() -> None:
    old_bash = _tool("bash", "BASH-SECRET-OUTPUT " * 30)
    old_read = _tool("read", "READ-SECRET-OUTPUT " * 30, toolCallId="read-call")
    old_mcp = _tool("remote.search", "MCP-SECRET-OUTPUT " * 30)
    recent = _tool("grep", "RECENT-OUTPUT " * 30)
    messages = [
        {"role": "system", "content": "SYSTEM-SECRET " * 30},
        {"role": "user", "content": "USER-SECRET " * 30},
        {"role": "assistant", "content": "ASSISTANT-SECRET " * 30},
        {"role": "custom", "customType": "plan", "content": "PLAN-SECRET " * 30},
        old_bash,
        old_read,
        old_mcp,
        recent,
    ]

    view, stats = _prune(messages)

    assert stats["count"] == 3
    assert stats["tokensReclaimed"] > 0
    assert view[:4] == messages[:4]
    assert view[-1] == recent
    for index, name, secret in ((4, "bash", "BASH-SECRET-OUTPUT"), (5, "read", "READ-SECRET-OUTPUT"), (6, "remote.search", "MCP-SECRET-OUTPUT")):
        marker = json.loads(view[index]["content"])
        assert marker["tool"] == name
        assert marker["pruned"] is True
        assert marker["originalSizeChars"] == len(messages[index]["content"])
        assert secret not in view[index]["content"]
        assert view[index]["toolCallId"] == "outer-call"
    assert json.loads(view[5]["content"])["toolCallId"] == "read-call"
    # The original in-memory history remains complete and repeat application is stable.
    assert "BASH-SECRET-OUTPUT" in old_bash["content"]
    twice, second_stats = _prune(view)
    assert twice == view
    assert second_stats == {"count": 0, "tokensReclaimed": 0}


class _Loader:
    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
        return "system"


def _agent(session: SessionManager, settings: SettingsManager) -> AgentSession:
    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    return AgentSession(session, settings, registry, _Loader(), model, "off")


def test_default_provider_view_preserves_old_mcp_evidence_across_reload_and_persistence(tmp_path) -> None:
    session_dir = tmp_path / "sessions"
    session = SessionManager.create(str(tmp_path), str(session_dir))
    old = _tool(
        "research.search",
        "ANALYST-FACT: CPI was 3.2 percent on 2026-04-12; "
        "ANALYST-FACT: copper inventory was 184,731 tonnes; " * 100,
    )
    recent = _tool("read", "RECENT-TOOL-OUTPUT " * 100)
    session.append_message(old)
    session.append_message(recent)
    path = session.session_file
    assert path is not None
    original_jsonl = open(path, encoding="utf-8").read()
    settings = SettingsManager.in_memory({"toolOutputPruning": {"recentTokens": 400, "minResultTokens": 10}})
    agent = _agent(session, settings)

    request = agent._flatten_messages_for_provider()
    request_text = "\n".join(str(m["content"]) for m in request)
    raw_size = sum(_tokens(m) for m in agent.messages)
    request_size = sum(_tokens(m) for m in request[1:])

    assert "CPI was 3.2 percent on 2026-04-12" in request_text
    assert "copper inventory was 184,731 tonnes" in request_text
    assert "RECENT-TOOL-OUTPUT" in request_text
    assert request_size == raw_size
    assert agent.get_session_stats()["toolOutputPruning"] == {"count": 0, "tokensReclaimed": 0}
    assert open(path, encoding="utf-8").read() == original_jsonl

    reopened = SessionManager.open(path)
    restored = reopened.build_session_context()["messages"]
    assert restored[0] == old
    assert "CPI was 3.2 percent on 2026-04-12" in restored[0]["content"]
    reloaded_agent = _agent(reopened, settings)
    reloaded_request = reloaded_agent._flatten_messages_for_provider()
    reloaded_text = "\n".join(str(m["content"]) for m in reloaded_request)
    assert "CPI was 3.2 percent on 2026-04-12" in reloaded_text
    assert "copper inventory was 184,731 tonnes" in reloaded_text
    assert reloaded_agent.get_session_stats()["toolOutputPruning"] == {"count": 0, "tokensReclaimed": 0}


def test_explicit_enabled_pruning_still_reduces_provider_view_without_losing_jsonl(tmp_path) -> None:
    session_dir = tmp_path / "sessions"
    session = SessionManager.create(str(tmp_path), str(session_dir))
    old = _tool("research.search", "DURABLE-TOOL-OUTPUT " * 100)
    recent = _tool("read", "RECENT-TOOL-OUTPUT " * 100)
    session.append_message(old)
    session.append_message(recent)
    path = session.session_file
    assert path is not None
    original_jsonl = open(path, encoding="utf-8").read()
    settings = SettingsManager.in_memory(
        {"toolOutputPruning": {"enabled": True, "recentTokens": 400, "minResultTokens": 10, "marker": "stale marker"}}
    )

    agent = _agent(session, settings)
    request = agent._flatten_messages_for_provider()
    request_text = "\n".join(str(m["content"]) for m in request)

    assert "DURABLE-TOOL-OUTPUT" not in request_text
    assert "RECENT-TOOL-OUTPUT" in request_text
    assert agent.get_session_stats()["toolOutputPruning"]["count"] == 1
    assert open(path, encoding="utf-8").read() == original_jsonl


def test_pruning_can_be_disabled() -> None:
    messages = [_tool("bash", "UNPRUNED " * 100)]
    view, stats = prune_stale_tool_outputs(
        messages,
        enabled=False,
        recent_tokens=0,
        min_result_tokens=1,
        marker="marker",
        estimate_tokens=_tokens,
    )
    assert view == messages
    assert view is not messages
    assert view[0] is messages[0]
    assert stats == {"count": 0, "tokensReclaimed": 0}


def test_pruning_uses_copy_on_write_without_mutating_input_data() -> None:
    untouched_data = {"nested": ["unchanged"]}
    untouched = {"role": "user", "content": "recent", "metadata": untouched_data}
    old = _tool("bash", "OLD-OUTPUT " * 100)
    old["metadata"] = {"nested": ["preserved"]}
    messages = [old, untouched]

    view, stats = _prune(messages, recent_tokens=1)

    assert stats["count"] == 1
    assert view is not messages
    assert view[1] is untouched
    assert view[1]["metadata"] is untouched_data
    assert view[0] is not old
    assert view[0]["metadata"] is old["metadata"]
    assert "OLD-OUTPUT" in old["content"]
    assert json.loads(view[0]["content"])["pruned"] is True

    repeated, repeated_stats = _prune(view, recent_tokens=1)
    assert repeated == view
    assert repeated is not view
    assert repeated[0] is view[0]
    assert repeated[1] is view[1]
    assert repeated_stats == {"count": 0, "tokensReclaimed": 0}
