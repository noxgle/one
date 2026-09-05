from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session_runtime import AgentSessionRuntimeHost, create_agent_session_runtime
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.tools.index import all_tools


class _Loader:
    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
        return "You are a coding agent."

    async def reload(self) -> None:
        return


def _mk_agent(tmp_path) -> Any:
    from one.core.agent_session import AgentSession

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory()
    session = SessionManager.in_memory(str(tmp_path))
    return AgentSession(session, settings, registry, _Loader(), model, "medium")


@pytest.mark.asyncio
async def test_navigate_tree_moves_leaf_and_rebuilds_context(tmp_path):
    agent = _mk_agent(tmp_path)
    sm = agent.session_manager
    sm.append_message({"role": "user", "content": "u1"})
    sm.append_message({"role": "assistant", "content": "a1"})
    a1_id = sm.get_leaf_id()
    assert a1_id is not None
    sm.append_message({"role": "user", "content": "u2"})
    u2_id = sm.get_leaf_id()
    assert u2_id is not None
    sm.append_message({"role": "assistant", "content": "a2"})
    agent.messages = sm.build_session_context()["messages"]

    # Navigating to an earlier user message repositions the branch to its parent.
    result = await agent.navigate_tree(u2_id)
    assert result["cancelled"] is False
    assert sm.get_leaf_id() == a1_id
    assert [m["content"] for m in agent.messages] == ["u1", "a1"]
    assert result["editorText"] == "u2"


@pytest.mark.asyncio
async def test_navigate_tree_to_first_user_message_resets_context(tmp_path):
    agent = _mk_agent(tmp_path)
    sm = agent.session_manager
    sm.append_message({"role": "user", "content": "u1"})
    e1 = sm.get_leaf_id()
    assert e1 is not None
    sm.append_message({"role": "assistant", "content": "a1"})
    agent.messages = sm.build_session_context()["messages"]

    # The first user message has no message parent: navigating to it repositions
    # the branch before the first message (empty context).
    result = await agent.navigate_tree(e1)
    assert result["cancelled"] is False
    assert sm.get_leaf_id() == sm.get_entry(e1).get("parentId")
    assert agent.messages == []


@pytest.mark.asyncio
async def test_navigate_tree_with_summary_and_label(tmp_path):
    agent = _mk_agent(tmp_path)
    sm = agent.session_manager
    sm.append_message({"role": "user", "content": "u1"})
    sm.append_message({"role": "assistant", "content": "a1"})
    a1_id = sm.get_leaf_id()
    assert a1_id is not None
    sm.append_message({"role": "user", "content": "u2"})
    agent.messages = sm.build_session_context()["messages"]

    result = await agent.navigate_tree(a1_id, {"summarize": True, "customInstructions": "Branch note", "label": "LBL"})
    assert result["cancelled"] is False
    assert any(m.get("customType") == "branch_summary" and m.get("content") == "Branch note" for m in agent.messages)
    labels = [e for e in sm.get_entries() if e.get("type") == "label"]
    assert labels and labels[0]["label"] == "LBL"


@pytest.mark.asyncio
async def test_navigate_tree_unknown_entry_raises(tmp_path):
    agent = _mk_agent(tmp_path)
    with pytest.raises(ValueError):
        await agent.navigate_tree("does-not-exist")


async def _make_host(tmp_path) -> tuple[AgentSessionRuntimeHost, SessionManager]:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory()
    loader = _Loader()
    bootstrap = {
        "agentDir": str(tmp_path / "agent"),
        "authStorage": auth,
        "modelRegistry": registry,
        "settingsManager": settings,
        "resourceLoader": loader,
        "model": model,
        "thinkingLevel": "medium",
        "scopedModels": [],
        "tools": [all_tools[t] for t in ["read"]],
    }
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    runtime = await create_agent_session_runtime(
        bootstrap,
        {"cwd": str(tmp_path), "sessionManager": sm, "resourceLoader": loader},
    )
    return AgentSessionRuntimeHost(bootstrap, runtime), sm


@pytest.mark.asyncio
async def test_runtime_host_fork_creates_new_session(tmp_path):
    host, sm = await _make_host(tmp_path)
    sm.append_message({"role": "user", "content": "u1"})
    sm.append_message({"role": "assistant", "content": "a1"})
    target = sm.get_leaf_id()
    assert target is not None
    old_file = sm.session_file
    old_id = sm.session_id

    result = await host.fork(target)
    assert result["cancelled"] is False
    assert result["selectedText"] is None  # target is an assistant message

    new_sm = host.session.session_manager
    assert new_sm.session_id != old_id
    assert new_sm.session_file != old_file
    assert [m["content"] for m in new_sm.build_session_context()["messages"]] == ["u1", "a1"]


@pytest.mark.asyncio
async def test_runtime_host_new_session_uses_model_changed_during_session(tmp_path):
    """A /model change persisted as default must survive /new."""
    host, _sm = await _make_host(tmp_path)
    selected = host.session.model_registry.find("openai", "gpt-4o")
    assert selected is not None

    await host.session.set_model(selected)
    host.session.settings_manager.set_default_provider(selected.provider)
    host.session.settings_manager.set_default_model(selected.id)

    await host.new_session()

    model = host.session.model
    assert model is not None
    assert (model.provider, model.id) == ("openai", "gpt-4o")


@pytest.mark.asyncio
async def test_runtime_host_fork_returns_user_text(tmp_path):
    host, sm = await _make_host(tmp_path)
    sm.append_message({"role": "user", "content": "u1"})
    sm.append_message({"role": "assistant", "content": "a1"})
    # The runtime may have appended model/thinking entries; find the user message entry.
    user_id = next(
        e["id"] for e in sm.get_entries()
        if e.get("type") == "message" and e.get("message", {}).get("role") == "user"
    )

    result = await host.fork(user_id)
    assert result["selectedText"] == "u1"


@pytest.mark.asyncio
async def test_export_to_html_escapes_and_renders(tmp_path):
    agent = _mk_agent(tmp_path)
    agent.messages.append({"role": "user", "content": "<script>alert(1)</script> & \"quotes\""})
    agent.messages.append({"role": "assistant", "content": [{"type": "text", "text": "hello <b>world</b>"}]})
    agent.messages.append(
        {
            "role": "toolResult",
            "content": '{"ok": true, "tool": "ls", "args": {"path": "."}, "result": "a.txt\\nb.txt"}',
        }
    )

    path = await agent.export_to_html(str(tmp_path / "out.html"))
    html = Path(path).read_text(encoding="utf-8")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;world&lt;/b&gt;" in html
    assert "session id" in html
    assert "msg-toolResult" in html  # role block rendered
    assert "a.txt" in html


def test_parse_args_export_format():
    from one.cli.args import parse_args

    p = parse_args(["--export", "x.jsonl", "--export-format", "jsonl"])
    assert p.export == "x.jsonl"
    assert p.export_format == "jsonl"
    p2 = parse_args(["--export", "x.jsonl"])
    assert p2.export_format == "html"
