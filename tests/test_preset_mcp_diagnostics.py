# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from types import SimpleNamespace

from one.core.tool_policy import ToolPolicy
from tests.test_preset_mcp_servers import Manager
from tests.test_preset_runtime import _preset, _session


def test_diagnostics_project_status_and_ceilings_without_secrets(tmp_path):
    session = _session(tmp_path)
    manager = Manager()
    manager.server_status = lambda: [
        {"name": "alpha", "running": True, "command": "secret"},
        {"name": "beta", "running": True, "error": "secret"},
        {"name": "offline", "running": False, "error": "secret"},
    ]
    session._mcp_manager = manager
    session._mcp_server_ceiling = frozenset({"alpha", "offline"})
    _preset(tmp_path, "selected", "mcp_servers: [alpha, beta, offline]\n")
    session.activate_preset("selected", str(tmp_path))
    expected = {"requested": ["alpha", "beta", "offline"], "allowed": ["alpha", "offline"],
                "unavailable": ["offline"], "restricted": ["beta"]}
    assert session.preset_mcp_diagnostics == expected
    assert session.preset_effective_settings["mcp_diagnostics"] == expected
    session._tool_policy = ToolPolicy(frozenset({"finish"}))
    assert session.preset_mcp_diagnostics["restricted"] == ["alpha", "beta"]
    manager.items.append(SimpleNamespace(name="new", server="offline"))
    assert session.preset_mcp_diagnostics["restricted"] == ["alpha", "beta", "offline"]
    session.activate_preset("default", str(tmp_path))
    assert session.preset_mcp_diagnostics["requested"] == []
