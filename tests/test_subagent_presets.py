"""Offline regression coverage for explicit subagent preset selection."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.test_subagents import _Loader, _mk_agent


@pytest.mark.asyncio
async def test_child_preset_resolves_configuration_without_mutating_parent(tmp_path: Path) -> None:
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "focused.md").write_text(
        "---\nversion: 1\nid: focused\nname: Focused\nmodel: openai/gpt-4o\n"
        "temperature: 0.2\nthinking: high\ntools: [read, finish]\n---\nCHILD ROLE\n",
        encoding="utf-8",
    )
    agent = _mk_agent(tmp_path, tools=["read", "bash", "finish", "spawn_subagent"])
    agent.resource_loader = _Loader()
    agent.resource_loader.agent_dir = str(tmp_path)  # type: ignore[attr-defined]
    original = (agent.model, agent.temperature, agent.thinking_level, list(agent.active_tools))
    captured: dict[str, Any] = {}

    async def capture(task: str, model: Any, tools: list[str], depth: int, **kwargs: Any) -> dict[str, Any]:
        captured.update(task=task, model=model, tools=tools, depth=depth, **kwargs)
        return {"ok": True, "summary": "done"}

    agent._run_subagent = capture  # type: ignore[method-assign]
    result = await agent._spawn_subagent({"task": "child", "preset": "focused"})

    assert result["ok"] is True
    assert (captured["model"].provider, captured["model"].id) == ("openai", "gpt-4o")
    assert captured["temperature"] == 0.2
    assert captured["thinking_level"] == "high"
    assert captured["tools"] == ["read", "finish"]
    assert captured["preset"].body == "CHILD ROLE\n"
    assert (agent.model, agent.temperature, agent.thinking_level, agent.active_tools) == original


def test_flat_subagent_call_accepts_explicit_preset(tmp_path: Path) -> None:
    agent = _mk_agent(tmp_path, tools=["spawn_subagent", "finish"])
    assert agent._try_parse_tool_call('{"tool":"spawn_subagent","task":"child","preset":"focused"}') == {
        "tool": "spawn_subagent", "args": {"task": "child", "preset": "focused"}, "toolCallId": None,
    }


def test_child_preset_metadata_is_bounded_literal_and_only_when_spawn_visible(tmp_path: Path) -> None:
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "review.md").write_text(
        "---\nversion: 1\nid: review\nname: Review <literal>\n"
        "description: Review changed files concisely\n---\nSECRET BODY MUST NOT APPEAR\n",
        encoding="utf-8",
    )
    agent = _mk_agent(tmp_path, tools=["spawn_subagent", "finish"])
    agent.resource_loader.agent_dir = str(tmp_path)  # type: ignore[attr-defined]
    prompt = agent._build_runtime_system_prompt()
    assert '"review": "Review <literal>"' in prompt
    assert "Review changed files concisely" in prompt
    assert "SECRET BODY MUST NOT APPEAR" not in prompt
    assert str(tmp_path) not in prompt

    agent._subagents_hard_disabled = True
    assert "# Child Presets" not in agent._build_runtime_system_prompt()


@pytest.mark.asyncio
async def test_delegation_caps_explicit_tools_and_overrides_preset(tmp_path: Path) -> None:
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "focused.md").write_text(
        "---\nversion: 1\nid: focused\nname: Focused\ntools: [read, finish]\n---\nROLE\n",
        encoding="utf-8",
    )
    agent = _mk_agent(tmp_path, tools=["bash", "finish", "spawn_subagent"])
    agent.resource_loader.agent_dir = str(tmp_path)
    captured: dict[str, Any] = {}

    async def capture(task: str, model: Any, tools: list[str], depth: int, **kwargs: Any) -> dict[str, Any]:
        captured.update(tools=tools, **kwargs)
        return {"ok": True}

    agent._run_subagent = capture
    await agent._spawn_subagent({"task": "x", "preset": "focused", "tools": ["bash", "read", "finish"]})
    assert captured["tools"] == ["bash", "finish"]
    assert captured["preset"].tools == ("bash", "finish")
    assert captured["preset"].body == "ROLE\n"
    from one.core.agent_session import AgentSession
    from one.core.session_manager import SessionManager
    from one.core.tool_policy import ToolPolicy

    child = AgentSession(
        SessionManager(agent.session_manager.cwd, agent.session_manager.session_dir, None, False),
        agent.settings_manager, agent.model_registry, agent.resource_loader, agent.model, agent.thinking_level,
        tools=captured["tools"], preset=captured["preset"], tool_policy=ToolPolicy().intersect(set(captured["tools"])),
    )
    assert child.active_tools == ["bash", "finish"]
    await agent._spawn_subagent({"task": "x", "preset": "readonly", "tools": ["bash", "finish"]})
    assert captured["tools"] == ["finish"]
    agent._active_tools = ["finish", "spawn_subagent"]
    await agent._spawn_subagent({"task": "x", "tools": ["bash", "finish"]})
    assert captured["tools"] == ["finish"]


@pytest.mark.asyncio
async def test_delegation_remap_ceiling_survives_nested_and_restore(tmp_path: Path) -> None:
    from one.core.agent_session import AgentSession
    from one.core.session_manager import SessionManager
    from one.core.tool_policy import ToolPolicy
    from tests.test_preset_mcp_servers import Manager

    presets = tmp_path / "presets"
    presets.mkdir()
    for name in ("alpha", "beta"):
        (presets / f"{name}.md").write_text(
            f"---\nversion: 1\nid: {name}\nname: {name}\nmcp_servers: [{name}]\n---\nROLE\n",
            encoding="utf-8",
        )
    agent = _mk_agent(tmp_path, tools=["finish", "spawn_subagent"])
    agent.resource_loader.agent_dir = str(tmp_path)
    manager = Manager()
    manager.has_tool = lambda name: any(t.name == name for t in manager.items)
    manager.items[0].name = "shared"
    agent._mcp_manager = manager
    agent.activate_preset("alpha", str(tmp_path))
    assert "shared" in agent.active_tools
    manager.items[0].server = "beta"
    captured: dict[str, Any] = {}

    async def capture(task: str, model: Any, tools: list[str], depth: int, **kwargs: Any) -> dict[str, Any]:
        captured.update(tools=tools, **kwargs)
        return {"ok": True}

    agent._run_subagent = capture
    await agent._spawn_subagent({"task": "x", "preset": "beta", "tools": ["shared", "finish", "spawn_subagent"]})
    assert "shared" not in captured["tools"]
    assert captured["mcp_server_ceiling"] == frozenset({"alpha"})
    child = AgentSession(
        session_manager=SessionManager(agent.session_manager.cwd, agent.session_manager.session_dir, None, False),
        settings_manager=agent.settings_manager, model_registry=agent.model_registry,
        resource_loader=agent.resource_loader, model=agent.model, thinking_level=agent.thinking_level,
        tools=captured["tools"], preset=captured["preset"], mcp_manager=manager,
        register_mcp_tools_callback=False, mcp_server_ceiling=captured["mcp_server_ceiling"],
        tool_policy=ToolPolicy().intersect(set(captured["tools"])),
    )
    child.providers = agent.providers
    state = child._preset_state(child._preset, child.model, child.temperature, child.thinking_level, child._base_tools, child._tool_policy)
    child.activate_preset("beta", str(tmp_path))
    child.restore_preset_state(state)
    assert child._mcp_server_ceiling == frozenset({"alpha"})
    child._run_subagent = capture
    await child._spawn_subagent({"task": "nested", "preset": "beta", "tools": ["finish"]})
    assert captured["mcp_server_ceiling"] == frozenset()
    assert "shared" not in child.active_tools


@pytest.mark.asyncio
async def test_child_wildcard_inherits_only_parent_mcp_tools(tmp_path: Path) -> None:
    from tests.test_preset_mcp_servers import Manager

    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "wild.md").write_text(
        "---\nversion: 1\nid: wild\nname: Wild\ntools: ['*']\n---\n", encoding="utf-8"
    )
    agent = _mk_agent(tmp_path, tools=["finish", "spawn_subagent"])
    agent.resource_loader.agent_dir = str(tmp_path)
    agent._mcp_manager = Manager()
    agent._mcp_server_ceiling = frozenset({"alpha"})
    agent.sync_mcp_tools()
    captured: dict[str, Any] = {}

    async def capture(task: str, model: Any, tools: list[str], depth: int, **kwargs: Any) -> dict[str, Any]:
        captured.update(tools=tools, **kwargs)
        return {"ok": True}

    agent._run_subagent = capture
    await agent._spawn_subagent({"task": "child", "preset": "wild"})
    assert "alpha_tool" in captured["tools"]
    assert "beta_tool" not in captured["tools"]
    assert captured["mcp_server_ceiling"] == frozenset({"alpha"})
