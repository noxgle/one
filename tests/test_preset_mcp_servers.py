# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from types import SimpleNamespace

import pytest

from one.core.presets import PresetError
from tests.test_preset_runtime import _preset, _session


class Manager:
    def __init__(self):
        self.items = [SimpleNamespace(name="alpha_tool", server="alpha"), SimpleNamespace(name="beta_tool", server="beta")]

    def tools(self):
        return self.items

    def server_status(self):
        return [{"name": name} for name in ("alpha", "beta", "offline")]


def test_activation_refresh_remap_and_default(tmp_path):
    session = _session(tmp_path)
    manager = Manager()
    session._mcp_manager = manager
    _preset(tmp_path, "selected", "tools: [read, finish]\nmcp_servers: [alpha]\n")
    _preset(tmp_path, "empty", "mcp_servers: []\n")
    _preset(tmp_path, "legacy")
    session.activate_preset("selected", str(tmp_path))
    assert "alpha_tool" in session._active_tools and "beta_tool" not in session._active_tools
    manager.items.append(SimpleNamespace(name="new_tool", server="alpha"))
    session.sync_mcp_tools()
    assert "new_tool" in session._active_tools
    assert not session.preset_modified
    manager.items[0].server = "beta"
    assert not session._mcp_server_permits("alpha_tool")
    session.activate_preset("empty", str(tmp_path))
    assert "new_tool" not in session._active_tools
    session.activate_preset("default", str(tmp_path))
    assert "new_tool" in session._active_tools and "beta_tool" in session._active_tools
    session.activate_preset("legacy", str(tmp_path))
    assert "beta_tool" in session._active_tools


def test_unknown_and_disconnected(tmp_path):
    session = _session(tmp_path)
    session._mcp_manager = Manager()
    _preset(tmp_path, "offline", "mcp_servers: [offline]\n")
    _preset(tmp_path, "unknown", "mcp_servers: [missing]\n")
    session.activate_preset("offline", str(tmp_path))
    with pytest.raises(PresetError, match="Unknown MCP servers"):
        session.activate_preset("unknown", str(tmp_path))
    assert session._preset.id == "offline"


def test_restore_explicit_and_legacy(tmp_path):
    session = _session(tmp_path)
    manager = Manager()
    session._mcp_manager = manager
    _preset(tmp_path, "selected", "mcp_servers: [alpha]\n")
    session.activate_preset("selected", str(tmp_path))
    state = session._preset_state(session._preset, session.model, session.temperature, session.thinking_level, session._base_tools, session._tool_policy)
    manager.items.append(SimpleNamespace(name="later", server="alpha"))
    session.restore_preset_state(state)
    assert "later" in session._active_tools
    assert not session.preset_modified
    state.pop("preset_mcp_servers")
    session.restore_preset_state(state)
    assert "later" not in session._active_tools


@pytest.mark.asyncio
async def test_dispatch_rejects_disallowed_and_remapped_before_manager_call(tmp_path):
    session = _session(tmp_path)
    manager = Manager()
    calls = []
    manager.has_tool = lambda name: calls.append(("lookup", name)) or True

    async def call_tool(name, args, **kwargs):
        calls.append((name, args))
        return {"ok": True}

    manager.call_tool = call_tool
    session._mcp_manager = manager
    _preset(tmp_path, "selected", "mcp_servers: [alpha]\n")
    session.activate_preset("selected", str(tmp_path))
    with pytest.raises(RuntimeError, match="disabled"):
        await session._execute_tool_by_name("beta_tool", {})
    manager.items[0].server = "beta"
    assert "alpha_tool" in session._active_tools  # stale discovery
    with pytest.raises(RuntimeError, match="disabled"):
        await session._execute_tool_by_name("alpha_tool", {})
    assert calls == []


@pytest.mark.parametrize("ident", ["readonly", "explore"])
def test_programmatic_reserved_preset_cannot_grant_mcp(tmp_path, ident):
    from one.core.presets import PresetDefinition
    from one.core.tool_policy import ToolPolicy

    session = _session(tmp_path)
    session._mcp_manager = Manager()
    preset = PresetDefinition(ident, ident, "", "test", mcp_servers=("alpha",))
    base, policy = session._resolve_preset_tools(preset, session._configured_base_tools, ToolPolicy())
    assert not policy.permits("alpha_tool")
    assert "alpha_tool" not in session._computed_active_tools(base, policy, preset=preset)


def test_inherited_tool_policy_limits_explicit_server_grants(tmp_path):
    from one.core.tool_policy import ToolPolicy

    session = _session(tmp_path, policy=ToolPolicy(frozenset({"read", "finish", "alpha_tool"})))
    session._mcp_manager = Manager()
    _preset(tmp_path, "selected", "mcp_servers: [alpha, beta]\n")
    session.activate_preset("selected", str(tmp_path))
    assert "alpha_tool" in session._active_tools
    assert "beta_tool" not in session._active_tools


@pytest.mark.parametrize("failure", ["discovery", "persistence"])
def test_failed_activation_is_atomic(tmp_path, monkeypatch, failure):
    session = _session(tmp_path)
    manager = Manager()
    session._mcp_manager = manager
    _preset(tmp_path, "first", "mcp_servers: [alpha]\n")
    _preset(tmp_path, "second", "mcp_servers: [beta]\n")
    session.activate_preset("first", str(tmp_path))
    before = (session._preset, session._tool_policy, session.model, session.temperature,
              session.thinking_level, list(session._base_tools), list(session._active_tools), session._preset_effective)

    def fail(*args, **kwargs):
        raise RuntimeError(failure)

    if failure == "discovery":
        monkeypatch.setattr(manager, "tools", fail)
    else:
        monkeypatch.setattr(session.session_manager, "append_preset_state", fail)
    with pytest.raises(RuntimeError, match=failure):
        session.activate_preset("second", str(tmp_path))
    assert before == (session._preset, session._tool_policy, session.model, session.temperature,
                      session.thinking_level, session._base_tools, session._active_tools, session._preset_effective)


@pytest.mark.parametrize("selection", ["alpha", ["alpha", "alpha"], ["*"], [" alpha"]])
def test_malformed_server_snapshot_rejected_atomically(tmp_path, selection):
    session = _session(tmp_path)
    session._mcp_manager = Manager()
    _preset(tmp_path, "selected", "mcp_servers: [alpha]\n")
    session.activate_preset("selected", str(tmp_path))
    state = session._preset_state(session._preset, session.model, session.temperature, session.thinking_level,
                                  session._base_tools, session._tool_policy)
    before = (session._preset, session._tool_policy, list(session._active_tools), session._preset_effective)
    state["preset_mcp_servers"] = selection
    with pytest.raises(PresetError, match="selection"):
        session.restore_preset_state(state)
    assert before == (session._preset, session._tool_policy, session._active_tools, session._preset_effective)
