# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from types import SimpleNamespace

import pytest

from one.core.agent_session import AgentSession
from one.tools.index import all_tools
from tests.test_preset_runtime import _preset, _session
from tests.test_subagents import _NativeToolProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [[], ["finish"], ["finish", "bash"]])
@pytest.mark.parametrize("wildcard", [False, True])
async def test_mcp_collision_cannot_enable_builtin(tmp_path, monkeypatch, configured, wildcard):
    seed = _session(tmp_path)
    calls = []

    class Manager:
        def tools(self):
            return [SimpleNamespace(name=name, server="alpha", input_schema={"type": "object"},
                                    description="fake MCP") for name in ("bash", "alpha_tool")]

        def has_tool(self, name):
            return name in ("bash", "alpha_tool")

        async def call_tool(self, name, args, **kwargs):
            calls.append(("mcp", name))
            return {"ok": True}

    async def bash(*args):
        calls.append(("builtin", "bash"))
        return {"ok": True}

    monkeypatch.setattr(all_tools["bash"], "fn", bash)
    session = AgentSession(
        seed.session_manager, seed.settings_manager, seed.model_registry, seed.resource_loader,
        seed.model, "medium", tools=configured, mcp_manager=Manager(),
    )
    if wildcard:
        _preset(tmp_path, "wild", "tools: ['*']\n")
        session.activate_preset("wild", str(tmp_path))
    else:
        session.sync_mcp_tools()
    # Re-discovery must not change the boundary.
    session.sync_mcp_tools()
    assert ("bash" in session.active_tools) == ("bash" in configured)
    assert "alpha_tool" in session.active_tools

    provider = _NativeToolProvider()
    monkeypatch.setattr(session.model_registry, "get_api_key_and_headers", lambda model: {"ok": True, "apiKey": "test"})
    session.providers = {"openai": provider}
    await session._invoke_provider([{"role": "user", "content": "test"}])
    assert ("bash" in provider.tool_names[-1]) == ("bash" in configured)
    assert provider.tool_names[-1].count("bash") <= 1
    assert "alpha_tool" in provider.tool_names[-1]

    if "bash" in configured:
        assert (await session._execute_tool_by_name("bash", {"command": "ignored"}))["ok"]
        assert calls == [("builtin", "bash")]
    else:
        with pytest.raises(RuntimeError, match="disabled"):
            await session._execute_tool_by_name("bash", {"command": "ignored"})
        assert calls == []
    assert (await session._execute_tool_by_name("alpha_tool", {}))["ok"]
    assert calls[-1] == ("mcp", "alpha_tool")
