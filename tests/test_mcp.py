from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import httpx

from one.cli.args import parse_args
from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.mcp import McpClient, McpManager, McpServerConfig, McpTool, HttpMcpClient

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

    async def call_tool(self, name: str, arguments: dict, timeout: float | None = None) -> dict:
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


@pytest.mark.asyncio
async def test_mcp_manager_server_status(tmp_path: Path):
    script = _write_fake_server(tmp_path)
    manager = McpManager([McpServerConfig("fake", sys.executable, [str(script)])])
    await manager.start()
    statuses = manager.server_status()
    assert len(statuses) == 1
    assert statuses[0]["name"] == "fake"
    assert statuses[0]["running"] is True
    assert statuses[0]["tools"] == ["echo_tool"]
    assert statuses[0]["enabled"] is True
    assert statuses[0]["error"] is None
    await manager.close()


@pytest.mark.asyncio
async def test_mcp_manager_disabled_server_not_started(tmp_path: Path):
    script = _write_fake_server(tmp_path)
    manager = McpManager([McpServerConfig("fake", sys.executable, [str(script)], enabled=False)])
    await manager.start()
    statuses = manager.server_status()
    assert len(statuses) == 1
    assert statuses[0]["running"] is False
    assert statuses[0]["tools"] == []
    assert statuses[0]["enabled"] is False
    await manager.close()


@pytest.mark.asyncio
async def test_mcp_manager_disable_server(tmp_path: Path):
    script = _write_fake_server(tmp_path)
    manager = McpManager([McpServerConfig("fake", sys.executable, [str(script)])])
    await manager.start()
    assert manager.has_tool("echo_tool") is True
    removed = await manager.disable_server("fake")
    assert "echo_tool" in removed
    assert manager.tools() == []
    assert manager.has_tool("echo_tool") is False
    statuses = manager.server_status()
    assert statuses[0]["running"] is False
    assert statuses[0]["enabled"] is False
    await manager.close()


@pytest.mark.asyncio
async def test_mcp_manager_enable_server(tmp_path: Path):
    script = _write_fake_server(tmp_path)
    manager = McpManager([McpServerConfig("fake", sys.executable, [str(script)], enabled=False)])
    await manager.start()
    assert manager.tools() == []
    added = await manager.enable_server("fake", sys.executable, args=[str(script)])
    assert "echo_tool" in added
    assert manager.has_tool("echo_tool") is True
    statuses = manager.server_status()
    assert statuses[0]["running"] is True
    assert statuses[0]["enabled"] is True
    await manager.close()


@pytest.mark.asyncio
async def test_mcp_manager_enable_already_running_returns_empty(tmp_path: Path):
    script = _write_fake_server(tmp_path)
    manager = McpManager([McpServerConfig("fake", sys.executable, [str(script)])])
    await manager.start()
    added = await manager.enable_server("fake", sys.executable, args=[str(script)])
    assert added == []
    await manager.close()


@pytest.mark.asyncio
async def test_mcp_manager_disable_nonexistent_returns_empty(tmp_path: Path):
    script = _write_fake_server(tmp_path)
    manager = McpManager([McpServerConfig("fake", sys.executable, [str(script)])])
    await manager.start()
    removed = await manager.disable_server("nonexistent")
    assert removed == []
    await manager.close()


@pytest.mark.asyncio
async def test_mcp_manager_server_status_with_error(tmp_path: Path):
    manager = McpManager([McpServerConfig("ghost", "/nonexistent/binary-xyz")])
    await manager.start()
    statuses = manager.server_status()
    assert len(statuses) == 1
    assert statuses[0]["name"] == "ghost"
    assert statuses[0]["running"] is False
    assert statuses[0]["error"] is not None
    assert "ghost" in statuses[0]["error"]
    await manager.close()


def test_settings_manager_set_mcp_server_enabled():
    sm = SettingsManager.in_memory(initial={"mcpServers": {"srv": {"command": "npx"}}})
    assert sm.get_mcp_servers() == {"srv": {"command": "npx"}}
    sm.set_mcp_server_enabled("srv", False)
    global_settings = sm.get_global_settings()
    assert global_settings["mcpServers"]["srv"]["enabled"] is False
    sm.set_mcp_server_enabled("srv", True)
    global_settings = sm.get_global_settings()
    assert global_settings["mcpServers"]["srv"]["enabled"] is True


def test_mcp_manager_create_reads_enabled_from_settings():
    sm = SettingsManager.in_memory(initial={"mcpServers": {"srv": {"command": "npx", "enabled": False}}})
    manager = McpManager.create(sm)
    assert len(manager._servers) == 1
    assert manager._servers[0].enabled is False
    sm2 = SettingsManager.in_memory(initial={"mcpServers": {"srv": {"command": "npx"}}})
    manager2 = McpManager.create(sm2)
    assert manager2._servers[0].enabled is True


# ---------------------------------------------------------------------------
# HTTP transport tests (httpx.MockTransport — no network).
# ---------------------------------------------------------------------------


def _make_http_handler() -> tuple:
    """Returns (handler, calls). Emulates a streamable-HTTP MCP server."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        payload = json.loads(request.content)
        method = payload.get("method", "")
        rid = payload.get("id")
        if method == "initialize":
            return httpx.Response(
                200,
                headers={
                    "Mcp-Session-Id": "sess-1",
                    "Content-Type": "application/json",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "fake-http", "version": "0.1"},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "tools": [
                            {
                                "name": "echo_tool",
                                "description": "Echo",
                                "inputSchema": {"type": "object"},
                            }
                        ]
                    },
                },
            )
        if method == "tools/call":
            sse = (
                "event: keepalive\n\n"
                + "event: message\n"
                + "data: "
                + json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [{"type": "text", "text": "pong"}]
                        },
                    }
                )
                + "\n\n"
            )
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=sse.encode(),
            )
        return httpx.Response(
            400,
            json={
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32601, "message": "method not found"},
            },
        )

    return handler, calls


@pytest.mark.asyncio
async def test_http_client_initialize_and_session_id():
    handler, calls = _make_http_handler()
    client = HttpMcpClient(
        McpServerConfig("fake-http", "", url="http://x/mcp"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        await client.start()
        assert len(calls) == 2  # initialize + notifications/initialized
        init_req = calls[0]
        assert "MCP-Protocol-Version" in init_req.headers
        assert init_req.headers["MCP-Protocol-Version"] == "2025-06-18"
        assert "text/event-stream" in init_req.headers["Accept"]
        tools = await client.list_tools()
        assert len(calls) == 3
        tools_req = calls[2]
        # The session ID from the initialize response should be echoed back.
        assert tools_req.headers["Mcp-Session-Id"] == "sess-1"
        assert len(tools) == 1
        assert tools[0]["name"] == "echo_tool"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_client_call_tool_sse():
    handler, _calls = _make_http_handler()
    client = HttpMcpClient(
        McpServerConfig("fake-http", "", url="http://x/mcp"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        await client.start()
        result = await client.call_tool("echo_tool", {"text": "hi"})
        assert result["content"][0]["text"] == "pong"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_client_error_status():
    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    client = HttpMcpClient(
        McpServerConfig("ghost", "", url="http://x/mcp"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(error_handler)),
    )
    try:
        with pytest.raises(RuntimeError) as exc_info:
            await client.start()
        assert "HTTP 500" in str(exc_info.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_client_no_matching_response():
    def bad_id_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload.get("method", "")
        rid = payload.get("id")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rid,  # matches initialize so start() succeeds
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "fake-http", "version": "0.1"},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 9999,  # never matches tools/list
                "result": {},
            },
        )

    client = HttpMcpClient(
        McpServerConfig("ghost", "", url="http://x/mcp"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(bad_id_handler)),
    )
    try:
        await client.start()
        with pytest.raises(RuntimeError) as exc_info:
            await client.list_tools()
        assert "did not respond" in str(exc_info.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_client_notifications_initialized_202():
    """start() should not raise even when the initialized notification returns 202."""
    handler, _calls = _make_http_handler()
    client = HttpMcpClient(
        McpServerConfig("fake-http", "", url="http://x/mcp"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        await client.start()  # should succeed
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Manager-level HTTP tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_manager_create_http_config():
    sm = SettingsManager.in_memory(
        initial={"mcpServers": {"ws": {"url": "http://127.0.0.1:8000/mcp", "enabled": True}}}
    )
    manager = McpManager.create(sm)
    assert len(manager._servers) == 1
    assert manager._servers[0].url == "http://127.0.0.1:8000/mcp"
    assert manager._servers[0].command == ""
    statuses = manager.server_status()
    assert len(statuses) == 1
    assert statuses[0]["transport"] == "http"
    assert statuses[0]["enabled"] is True


@pytest.mark.asyncio
async def test_mcp_manager_http_connection_error():
    manager = McpManager(
        [McpServerConfig("ghost", "", url="http://127.0.0.1:1/mcp")]
    )
    await manager.start()
    statuses = manager.server_status()
    assert len(statuses) == 1
    assert statuses[0]["transport"] == "http"
    assert statuses[0]["error"] is not None
    assert manager.tools() == []
    await manager.close()


@pytest.mark.asyncio
async def test_mcp_manager_enable_server_http_url():
    manager = McpManager(
        [McpServerConfig("ws", "", url="http://127.0.0.1:1/mcp", enabled=False)]
    )
    await manager.start()
    # enable_server with url should raise connection error.
    with pytest.raises(RuntimeError):
        await manager.enable_server("ws", "", url="http://127.0.0.1:1/mcp")
    # disable_server with url should not crash.
    removed = await manager.disable_server("ws")
    assert removed == []
    statuses = manager.server_status()
    assert statuses[0]["transport"] == "http"
    await manager.close()


# ---------------------------------------------------------------------------
# McpManager.call_tool timeout forwarding test
# ---------------------------------------------------------------------------


class _RecordingClient:
    def __init__(self, name: str) -> None:
        self.config = McpServerConfig(name=name, command="")
        self.calls: list[tuple] = []

    async def call_tool(self, name: str, arguments: dict, timeout: float = 120.0) -> dict:
        self.calls.append((name, arguments, timeout))
        return {"content": [{"type": "text", "text": "ok"}]}


@pytest.mark.asyncio
async def test_mcp_manager_call_tool_forwards_timeout():
    manager = McpManager([])
    client = _RecordingClient("demo")
    manager._clients = [client]
    manager._tools = [McpTool(name="t1", description="", input_schema=None, server="demo")]

    await manager.call_tool("t1", {"a": 1}, timeout=42)
    assert client.calls == [("t1", {"a": 1}, 42)]

    await manager.call_tool("t1", {"a": 2})
    assert client.calls[1][2] == 120.0
