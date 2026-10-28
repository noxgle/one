"""Server ceilings for shared, stub-only MCP subagent runtimes."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one

from __future__ import annotations

from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.presets import PresetError, discover_presets
from one.mcp import McpTool
from tests.test_subagents import _mk_agent, _NativeToolProvider, _StubMcpManager


class _Manager(_StubMcpManager):
    def __init__(self):
        super().__init__()
        self._tools = [
            McpTool(name="alpha_tool", description="ALPHA DESCRIPTION", input_schema={"type": "object"}, server="alpha"),
            McpTool(name="beta_tool", description="BETA DESCRIPTION", input_schema={"type": "object"}, server="beta"),
        ]
        self.callback = None

    def set_tools_changed_callback(self, callback):
        self.callback = callback

    def server_status(self):
        return [{"name": "configured"}]


def _agent(tmp_path, manager, ceiling=None):
    base = _mk_agent(tmp_path)
    return AgentSession(
        base.session_manager, base.settings_manager, base.model_registry, base.resource_loader,
        base.model, "medium", tools=["finish", "spawn_subagent"], mcp_manager=manager,
        mcp_server_ceiling=ceiling,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("options,expected", [({}, None), ({"mcp_servers": []}, frozenset()),
    ({"mcp_servers": ["alpha"]}, frozenset({"alpha"})),
    ({"mcp_servers": ["configured"]}, frozenset({"configured"}))])
async def test_actual_child_visibility_and_shared_callback(tmp_path, monkeypatch, options, expected):
    manager = _Manager()
    agent = _agent(tmp_path, manager)
    callback = manager.callback
    children = []
    completed = []

    async def prompt(child, text, **kwargs):
        children.append(child)
        assert child._mcp_manager is manager
        assert child._mcp_server_ceiling == expected
        allowed = {t.name for t in manager.tools() if expected is None or t.server in expected}
        assert set(child.active_tools) == {"finish", "spawn_subagent"} | allowed
        system = child._build_runtime_system_prompt()
        for tool in manager.tools():
            assert (tool.description in system) == (tool.name in allowed)
            if tool.name in allowed:
                await child._execute_tool_by_name(tool.name, {})
            else:
                with pytest.raises(RuntimeError, match="disabled"):
                    await child._execute_tool_by_name(tool.name, {})
        provider = _NativeToolProvider()
        child.providers = {"openai": provider}
        await child._invoke_provider([{"role": "user", "content": "inspect"}])
        assert set(provider.tool_names[0]) == {"finish", "spawn_subagent"} | allowed
        completed.append(child)

    monkeypatch.setattr(AgentSession, "prompt", prompt)
    await agent._spawn_subagent({"task": "child", **options})
    assert len(children) == len(completed) == 1
    assert manager.callback == callback
    assert set(agent.active_tools) == {"finish", "spawn_subagent", "alpha_tool", "beta_tool"}


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "alpha", {}, 1, True, [None], [1], [""], ["  "], ["missing"]])
async def test_invalid_before_allocation(tmp_path, monkeypatch, value):
    agent = _agent(tmp_path, _Manager())

    async def unexpected(*args, **kwargs):
        pytest.fail("allocated child before validation")

    monkeypatch.setattr(agent, "_run_subagent", unexpected)
    with pytest.raises(RuntimeError, match="mcp_servers|Unknown MCP servers"):
        await agent._spawn_subagent({"tasks": ["one", "two"], "mcp_servers": value})


@pytest.mark.asyncio
async def test_nested_intersection_tools_and_refresh(tmp_path, monkeypatch):
    manager = _Manager()
    agent = _agent(tmp_path, manager, frozenset({"alpha"}))
    children = []
    completed = []

    async def prompt(child, text, **kwargs):
        children.append(child)
        assert child._mcp_server_ceiling == frozenset({"alpha"})
        assert "beta_tool" not in child.active_tools
        if len(children) == 1:
            await child._spawn_subagent({"task": "nested", "mcp_servers": ["alpha", "beta"],
                                         "tools": ["alpha_tool", "finish"]})
        else:
            assert set(child.active_tools) == {"alpha_tool", "finish"}
            manager._tools.append(McpTool(name="fresh", description="new", input_schema={}, server="alpha"))
            child.sync_mcp_tools()
            assert "fresh" not in child.active_tools  # inherited tool-name ceiling
            manager._tools[0].server = "beta"
            # Stale active name must not dispatch or leak definitions.
            with pytest.raises(RuntimeError, match="disabled"):
                await child._execute_tool_by_name("alpha_tool", {})
            assert "ALPHA DESCRIPTION" not in child._build_runtime_system_prompt()
            provider = _NativeToolProvider()
            child.providers = {"openai": provider}
            await child._invoke_provider([{"role": "user", "content": "inspect"}])
            assert provider.tool_names == [["finish"]]
            child.sync_mcp_tools()
            assert child.active_tools == ["finish"]
        completed.append(child)

    monkeypatch.setattr(AgentSession, "prompt", prompt)
    await agent._spawn_subagent({"task": "child"})
    assert len(children) == len(completed) == 2
    assert manager.calls == []


@pytest.mark.asyncio
async def test_refresh_server_filter_and_builtin_collision(tmp_path):
    manager = _Manager()
    agent = _agent(tmp_path, manager, frozenset({"alpha"}))
    manager._tools += [McpTool(name="new_alpha", description="new", input_schema={}, server="alpha"),
                       McpTool(name="finish", description="FORBIDDEN", input_schema={}, server="beta")]
    agent.sync_mcp_tools()
    assert set(agent.active_tools) == {"finish", "spawn_subagent", "alpha_tool", "new_alpha"}
    assert "FORBIDDEN" not in agent._build_runtime_system_prompt()
    await agent._execute_tool_by_name("new_alpha", {})
    assert len(manager.calls) == 1


def test_snapshot_preserves_and_intersects_ceiling(tmp_path):
    manager = _Manager()
    agent = _agent(tmp_path, manager, frozenset({"alpha"}))
    preset = discover_presets(str(tmp_path))["default"]
    state = agent._preset_state(preset, agent.model, agent.temperature, agent.thinking_level,
                                agent._base_tools, agent._tool_policy)
    assert state["mcp_servers"] == ["alpha"]
    restored = _agent(tmp_path, manager)
    restored.restore_preset_state(state)
    assert restored._mcp_server_ceiling == frozenset({"alpha"})
    denied = _agent(tmp_path, manager, frozenset())
    denied.restore_preset_state(state)
    assert denied._mcp_server_ceiling == frozenset()
    legacy: dict[str, Any] = {k: v for k, v in state.items() if k != "mcp_servers"}
    denied.restore_preset_state(legacy)
    assert denied._mcp_server_ceiling == frozenset()
    with pytest.raises(PresetError, match="MCP server ceiling"):
        restored.restore_preset_state({**state, "mcp_servers": [""]})
    assert restored._mcp_server_ceiling == frozenset({"alpha"})


def test_flat_argument_parser(tmp_path):
    agent = _agent(tmp_path, _Manager())
    call = agent._try_parse_tool_call('{"tool":"spawn_subagent","task":"child","mcp_servers":[]}')
    assert call["args"]["mcp_servers"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("servers,tools,expected", [
    (["alpha"], ["beta_tool", "finish"], {"finish"}),
    ([], ["alpha_tool", "finish"], {"finish"}),
    (["alpha"], ["alpha_tool", "finish"], {"alpha_tool", "finish"}),
])
async def test_explicit_tool_names_intersect_servers(tmp_path, monkeypatch, servers, tools, expected):
    agent = _agent(tmp_path, _Manager())
    completed = []

    async def prompt(child, text, **kwargs):
        assert set(child.active_tools) == expected
        child.sync_mcp_tools()
        assert set(child.active_tools) == expected
        completed.append(True)

    monkeypatch.setattr(AgentSession, "prompt", prompt)
    await agent._spawn_subagent({"task": "child", "mcp_servers": servers, "tools": tools})
    assert completed == [True]


@pytest.mark.asyncio
async def test_disjoint_descendant_request_cannot_broaden(tmp_path, monkeypatch):
    agent = _agent(tmp_path, _Manager(), frozenset({"alpha"}))
    completed = []

    async def prompt(child, text, **kwargs):
        assert child._mcp_server_ceiling == frozenset()
        assert set(child.active_tools) == {"finish", "spawn_subagent"}
        completed.append(True)

    monkeypatch.setattr(AgentSession, "prompt", prompt)
    await agent._spawn_subagent({"task": "child", "mcp_servers": ["beta"]})
    assert completed == [True]


def test_model_visible_native_schema():
    from one.tools.index import native_tool_definitions

    definition = native_tool_definitions(["spawn_subagent"])[0]
    schema = definition["parameters"]
    assert "mcp_servers" not in schema.get("required", [])
    assert schema["additionalProperties"] is False
    assert schema["properties"]["mcp_servers"]["type"] == "array"
    assert schema["properties"]["mcp_servers"]["items"] == {"type": "string", "minLength": 1}
    assert "minItems" not in schema["properties"]["mcp_servers"]
    assert "omit to inherit" in definition["description"]
    assert "[] denies MCP" in definition["description"]
