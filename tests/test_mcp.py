from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from one.cli.args import parse_args
from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.mcp import McpClient, McpManager, McpServerConfig, McpTool

FAKE_SERVER_SRC = '''\
import asyncio
import json
import sys

async def handle():
    stdout = sys.stdout.buffer
    stderr = sys.stderr.buffer
    buf = b""
    while True:
        ch = sys.stdin.buffer.read(1)
        if not ch:
            break
        buf += ch
        if ch == b"\\n":
            line = buf.decode("utf-8")
            buf = b""
            msg = json.loads(line)
            method = msg.get("method", "")
            rid = msg.get("id")

            if method == "initialize":
                resp = {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "fake-mcp", "version": "0.1"},
                    },
                }
            elif method == "notifications/initialized":
                # server-to-client notification, no response
                resp = None
            elif method == "tools/list":
                resp = {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "tools": [{
                            "name": "echo_tool",
                            "description": "Echo back a string",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string"}
                                }
                            },
                        }]
                    },
                }
            elif method == "tools/call":
                args = msg.get("params", {}).get("arguments", {})
                tool_name = msg.get("params", {}).get("name", "")
                if tool_name == "echo_tool":
                    text = args.get("text", "")
                    resp = {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [{"type": "text", "text": "echo: " + str(text)}]
                        },
                    }
                else:
                    resp = {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [{"type": "text", "text": "boom"}],
                            "isError": True,
                        },
                    }
            else:
                resp = {
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": "Method not found"},
                }

            if resp is not None:
                stdout.write((json.dumps(resp) + "\\n").encode("utf-8"))
                stdout.flush()

asyncio.run(handle())
'''


def _write_fake_server(tmp_path: Path) -> Path:
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(FAKE_SERVER_SRC, encoding="utf-8")
    return script


@pytest.mark.asyncio
async def test_mcp_manager_lists_and_calls_tool(tmp_path: Path):
    script = _write_fake_server(tmp_path)
    manager = McpManager([McpServerConfig("fake", sys.executable, [str(script)])])
    await manager.start()
    assert manager.errors() == []
    tools = manager.tools()
    assert len(tools) == 1
    assert tools[0].name == "echo_tool"
    assert tools[0].description == "Echo back a string"
    assert tools[0].server == "fake"
    assert manager.has_tool("echo_tool") is True
    result = await manager.call_tool("echo_tool", {"text": "hi"})
    assert result["ok"] is True
    assert "echo: hi" in result["output"]
    await manager.close()


@pytest.mark.asyncio
async def test_mcp_manager_call_error_tool_raises(tmp_path: Path):
    script = _write_fake_server(tmp_path)
    manager = McpManager([McpServerConfig("fake", sys.executable, [str(script)])])
    await manager.start()
    try:
        with pytest.raises(RuntimeError):
            await manager.call_tool("other_tool", {})
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_mcp_manager_missing_command_reports_error(tmp_path: Path):
    manager = McpManager([McpServerConfig("ghost", "/nonexistent/binary-xyz")])
    await manager.start()
    errs = manager.errors()
    assert len(errs) > 0
    assert "ghost" in errs[0]
    assert manager.tools() == []
    await manager.close()


@pytest.mark.asyncio
async def test_mcp_manager_unknown_tool_raises(tmp_path: Path):
    script = _write_fake_server(tmp_path)
    manager = McpManager([McpServerConfig("fake", sys.executable, [str(script)])])
    await manager.start()
    try:
        with pytest.raises(RuntimeError):
            await manager.call_tool("nope", {})
    finally:
        await manager.close()


def test_settings_get_mcp_servers():
    sm = SettingsManager.in_memory(initial={"mcpServers": {"srv": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"], "env": {"A": "1"}}}})
    result = sm.get_mcp_servers()
    assert result == {"srv": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"], "env": {"A": "1"}}}

    # Empty when no mcpServers key
    sm2 = SettingsManager.in_memory()
    assert sm2.get_mcp_servers() == {}


def test_mcp_manager_create_parses_settings():
    sm = SettingsManager.in_memory(initial={"mcpServers": {"srv": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"], "env": {"A": "1"}}}})
    manager = McpManager.create(sm)
    assert len(manager._servers) == 1
    assert manager._servers[0].name == "srv"
    assert manager._servers[0].command == "npx"
    assert len(manager._servers[0].args) == 2
    assert manager._servers[0].env == {"A": "1"}


class _StubMcpManager:
    def __init__(self) -> None:
        self._tools = [McpTool(name="echo_tool", description="Echo", input_schema={"type": "object"}, server="fake")]
        self.called: list[tuple[str, dict]] = []

    def tools(self) -> list:
        return self._tools

    def has_tool(self, name: str) -> bool:
        return any(t.name == name for t in self._tools)

    async def call_tool(self, name: str, arguments: dict) -> dict:
        self.called.append((name, arguments))
        return {"ok": True, "tool": name, "args": arguments, "output": "echo: ok", "content": [{"type": "text", "text": "echo: ok"}], "isError": False}


class _TestLoader:
    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
        return "You are a coding agent."


@pytest.mark.asyncio
async def test_mcp_tool_in_session_active_tools_and_dispatch(tmp_path: Path):
    stub = _StubMcpManager()
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(
        session_manager=session,
        settings_manager=settings,
        model_registry=registry,
        resource_loader=_TestLoader(),
        model=model,
        thinking_level="medium",
        tools=["read", "bash"],
        mcp_manager=stub,  # type: ignore[arg-type]
    )
    assert "echo_tool" in agent._active_tools
    result = await agent._execute_tool_by_name("echo_tool", {"text": "hi"})
    assert result["ok"] is True
    assert len(stub.called) == 1
    assert stub.called[0] == ("echo_tool", {"text": "hi"})
    prompt = agent._build_runtime_system_prompt()
    assert "# MCP Tools" in prompt
    assert "echo_tool" in prompt


def test_cli_no_mcp_flag_parses():
    parsed = parse_args(["--no-mcp"])
    assert parsed.no_mcp is True
    assert parsed.errors == []
