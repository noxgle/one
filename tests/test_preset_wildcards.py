# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from types import SimpleNamespace

import pytest

from one.core.presets import PresetError, parse_preset
from one.core.tool_policy import ToolPolicy
from tests.test_preset_mcp_servers import Manager
from tests.test_preset_runtime import _preset, _session


@pytest.mark.parametrize("field", ["tools", "mcp_servers"])
@pytest.mark.parametrize("value", ["['*', 'read']", "['abc*']", "[*]"])
def test_invalid_wildcard(field, value):
    with pytest.raises(PresetError):
        parse_preset(f"---\nversion: 1\nid: demo\nname: Demo\n{field}: {value}\n---\n")


def test_dynamic_ceiling_delegation_and_restore(tmp_path):
    session = _session(tmp_path, policy=ToolPolicy(frozenset({"read", "finish", "alpha_tool", "later"})))
    session._mcp_manager = manager = Manager()
    session._mcp_server_ceiling = frozenset({"alpha"})
    _preset(tmp_path, "wild", "tools: ['*']\n")
    _preset(tmp_path, "deny", "tools: ['*']\nmcp_servers: []\n")
    _preset(tmp_path, "servers", "mcp_servers: ['*']\n")
    session.activate_preset("wild", str(tmp_path))
    assert "alpha_tool" in session.active_tools and "beta_tool" not in session.active_tools
    assert session._delegated_mcp_servers(None) == frozenset({"alpha"})
    state = session._preset_state(session._preset, session.model, session.temperature, session.thinking_level,
                                  session._base_tools, session._tool_policy)
    manager.items.append(SimpleNamespace(name="later", server="alpha"))
    session.sync_mcp_tools()
    assert "later" in session.active_tools and not session.preset_modified
    session.activate_preset("deny", str(tmp_path))
    assert "alpha_tool" not in session.active_tools
    session.restore_preset_state(state)
    assert "later" in session.active_tools and not session.preset_modified
    session.activate_preset("servers", str(tmp_path))
    assert session._delegated_mcp_servers(None) == frozenset({"alpha"})
    assert "alpha_tool" in session.active_tools and "beta_tool" not in session.active_tools


@pytest.mark.parametrize("changes", [
    {"wildcard_mcp_servers": True, "preset_mcp_servers": []},
    {"preset_mcp_servers": ["alpha*"]},
    {"wildcard_tools": "true"},
    {"tools": ["*"]},
    {"policy": ["*"]},
])
def test_malformed_wildcard_snapshot_fails_closed(tmp_path, changes):
    session = _session(tmp_path)
    session._mcp_manager = Manager()
    _preset(tmp_path, "wild", "tools: ['*']\n")
    session.activate_preset("wild", str(tmp_path))
    state = session._preset_state(session._preset, session.model, session.temperature, session.thinking_level,
                                  session._base_tools, session._tool_policy)
    state.update(changes)
    previous = session._preset
    with pytest.raises(PresetError):
        session.restore_preset_state(state)
    assert session._preset is previous


def test_wildcard_diagnostics_list_servers_not_marker(tmp_path):
    session = _session(tmp_path)
    session._mcp_manager = manager = Manager()
    manager.server_status = lambda: [{"name": "offline", "running": False}]
    _preset(tmp_path, "wild", "mcp_servers: ['*']\n")
    session.activate_preset("wild", str(tmp_path))
    assert session.preset_mcp_diagnostics["requested"] == ["alpha", "beta", "offline"]
    assert session.preset_mcp_diagnostics["unavailable"] == ["offline"]


@pytest.mark.parametrize("ident", ["readonly", "explore"])
def test_reserved_wildcard_snapshot_cannot_grant_mcp(tmp_path, ident):
    session = _session(tmp_path)
    session._mcp_manager = Manager()
    session.activate_preset(ident, str(tmp_path))
    state = session._preset_state(session._preset, session.model, session.temperature, session.thinking_level,
                                  session._base_tools, session._tool_policy)
    state.update(wildcard_tools=True, wildcard_mcp_servers=True)
    session.restore_preset_state(state)
    assert "alpha_tool" not in session.active_tools


def test_null_tools_rejected_but_omission_allowed():
    header = "---\nversion: 1\nid: demo\nname: Demo\n"
    assert parse_preset(header + "---\n").tools is None
    with pytest.raises(PresetError):
        parse_preset(header + "tools: null\n---\n")


def test_manual_policy_survives_restore_and_reconnect(tmp_path):
    session = _session(tmp_path, policy=ToolPolicy(frozenset({"read", "finish", "alpha_tool", "later"})))
    session._mcp_manager = manager = Manager()
    _preset(tmp_path, "wild", "tools: ['*']\n")
    session.activate_preset("wild", str(tmp_path))
    session._base_tools = [x for x in session._base_tools if x != "read"]
    session._tool_policy = ToolPolicy(frozenset({"finish", "alpha_tool"}))
    state = session._preset_state(session._preset, session.model, session.temperature, session.thinking_level,
                                  session._base_tools, session._tool_policy)
    state.pop("dynamic_policy_override")  # legacy snapshot
    session.restore_preset_state(state)
    saved = []
    session._persist_preset_state = lambda state=None: saved.append(state)
    manager.items.append(SimpleNamespace(name="later", server="alpha"))
    for _ in range(3):
        session.sync_mcp_tools()
        assert "later" not in session.active_tools
        assert "read" not in session._base_tools
        assert session._tool_policy.allowed <= frozenset({"finish", "alpha_tool"})
    assert saved[-1]["dynamic_policy_override"] is not None
    session.restore_preset_state(saved[-1])
    session.sync_mcp_tools()
    assert "later" not in session.active_tools
    assert "read" not in session._base_tools


def test_sync_persistence_failure_is_atomic(tmp_path):
    session = _session(tmp_path)
    session._mcp_manager = Manager()
    _preset(tmp_path, "wild", "tools: ['*']\n")
    session.activate_preset("wild", str(tmp_path))
    session._tool_policy = ToolPolicy(frozenset({"finish"}))
    before = (session._tool_policy, session._preset_effective, session.active_tools)
    def fail(state=None):
        raise OSError("failed")
    session._persist_preset_state = fail
    with pytest.raises(OSError):
        session.sync_mcp_tools()
    assert before == (session._tool_policy, session._preset_effective, session.active_tools)
