from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx


def _ensure_url(config: McpServerConfig) -> str:
    """Return config.url, raising if None (caller should only use this for HTTP transport)."""
    if config.url is None:
        raise ValueError(f"HTTP transport requires a URL for MCP server '{config.name}'")
    return config.url


@dataclass
class McpServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    enabled: bool = True


@dataclass
class McpTool:
    name: str
    description: str
    input_schema: dict[str, Any] | None
    server: str


class McpClient:
    """One stdio MCP server connection (JSON-RPC 2.0, newline-delimited JSON)."""

    PROTOCOL_VERSION = "2024-11-05"

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._stderr_tail: list[str] = []

    async def start(self) -> None:
        env = os.environ.copy()
        env.update(self.config.env)
        self._proc = await asyncio.create_subprocess_exec(
            self.config.command,
            *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        # Drain stderr in the background (bounded tail kept for diagnostics).
        asyncio.create_task(self._drain_stderr())
        await self._request("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "one", "version": "0.1"},
        })
        await self._notify("notifications/initialized")

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    if len(self._stderr_tail) > 20:
                        self._stderr_tail.pop(0)
        except Exception:
            pass

    async def _write(self, obj: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError(f"MCP server '{self.config.name}' is not running")
        self._proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _read(self, timeout: float) -> dict[str, Any]:
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError(f"MCP server '{self.config.name}' is not running")
        line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
        if not line:
            raise RuntimeError(f"MCP server '{self.config.name}' closed stdout")
        try:
            return json.loads(line.decode("utf-8", errors="replace"))
        except Exception as e:
            raise RuntimeError(f"MCP server '{self.config.name}' sent invalid JSON: {e}") from e

    async def _request(self, method: str, params: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
        self._next_id += 1
        req_id = self._next_id
        await self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        while True:
            msg = await self._read(timeout)
            if msg.get("id") != req_id:
                continue  # ignore unrelated notifications/other responses
            if "error" in msg:
                err = msg["error"]
                raise RuntimeError(f"MCP server '{self.config.name}' error {err.get('code')}: {err.get('message')}")
            return msg.get("result") or {}

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        await self._write(msg)

    async def list_tools(self, timeout: float = 15.0) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {}, timeout=timeout)
        return result.get("tools") or []

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
        return await self._request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)

    async def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def stderr_tail(self) -> list[str]:
        return list(self._stderr_tail)


class HttpMcpClient:
    """MCP server connection over HTTP streamable transport (JSON-RPC 2.0, buffered POST)."""

    PROTOCOL_VERSION = "2025-06-18"

    def __init__(self, config: McpServerConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(120.0), follow_redirects=True
        )
        self._owns_client = client is None
        self._session_id: str | None = None
        self._next_id = 0

    @staticmethod
    def _parse_sse(text: str) -> list[dict[str, Any]]:
        """Parse SSE-formatted text into a list of JSON-RPC messages."""
        messages: list[dict[str, Any]] = []
        event_name: str | None = None
        data_lines: list[str] = []

        def _flush() -> None:
            nonlocal event_name, data_lines
            if event_name is None or event_name == "message":
                joined = "\n".join(data_lines)
                if joined:
                    try:
                        messages.append(json.loads(joined))
                    except (json.JSONDecodeError, ValueError):
                        pass
            event_name = None
            data_lines = []

        for line in text.splitlines():
            if line.startswith(":"):
                continue  # comment / keepalive
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
                continue
            if line.startswith("data:"):
                data_lines.append(line[len("data:"):])
                continue
            if line == "":
                _flush()
        _flush()  # flush trailing event at EOF
        return messages

    async def _post(
        self, payload: dict[str, Any], timeout: float = 15.0
    ) -> tuple[list[dict[str, Any]], httpx.Headers]:
        url = _ensure_url(self.config)
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.PROTOCOL_VERSION,
        }
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        resp = await self._client.post(
            url, headers=headers, json=payload, timeout=timeout
        )
        if resp.status_code >= 400:
            body = resp.text[:300] if resp.text else ""
            raise RuntimeError(
                f"MCP server '{self.config.name}' HTTP {resp.status_code}: {body}"
            )
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return self._parse_sse(resp.text), resp.headers
        body_text = resp.text.strip()
        if body_text:
            return [resp.json()], resp.headers
        return [], resp.headers

    async def _request(
        self, method: str, params: dict[str, Any], timeout: float = 15.0
    ) -> dict[str, Any]:
        self._next_id += 1
        req_id = self._next_id
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        messages, headers = await self._post(payload, timeout=timeout)
        session_id = headers.get("mcp-session-id")
        if session_id is not None:
            self._session_id = session_id
        for msg in messages:
            if msg.get("id") != req_id:
                continue
            if "error" in msg:
                err = msg["error"]
                raise RuntimeError(
                    f"MCP server '{self.config.name}' error {err.get('code')}: {err.get('message')}"
                )
            return msg.get("result") or {}
        raise RuntimeError(f"MCP server '{self.config.name}' did not respond to '{method}'")

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        await self._post(payload)

    async def start(self) -> None:
        await self._request("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "one", "version": "0.1"},
        }, timeout=15.0)
        try:
            await self._notify("notifications/initialized")
        except Exception:
            pass  # some servers return 202 with empty body

    async def list_tools(self, timeout: float = 15.0) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {}, timeout=timeout)
        return result.get("tools") or []

    async def call_tool(
        self, name: str, arguments: dict[str, Any], timeout: float = 120.0
    ) -> dict[str, Any]:
        return await self._request(
            "tools/call", {"name": name, "arguments": arguments}, timeout=timeout
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def stderr_tail(self) -> list[str]:
        return []


class McpManager:
    """Owns all configured MCP servers; exposes their tools to the session."""

    def __init__(self, servers: list[McpServerConfig]) -> None:
        self._servers = servers
        self._clients: list[McpClient | HttpMcpClient] = []
        self._tools: list[McpTool] = []
        self._errors: list[str] = []

    @classmethod
    def create(cls, settings_manager: Any) -> "McpManager":
        servers: list[McpServerConfig] = []
        raw = settings_manager.get_mcp_servers() or {}
        for name, cfg in raw.items():
            if not isinstance(cfg, dict):
                continue
            command = cfg.get("command")
            url = cfg.get("url")
            # URL-based (HTTP transport) or command-based (stdio). Skip neither.
            if not url and not command:
                continue
            servers.append(
                McpServerConfig(
                    name=str(name),
                    command=str(command) if command else "",
                    args=[str(a) for a in (cfg.get("args") or [])],
                    env={str(k): str(v) for k, v in (cfg.get("env") or {}).items()},
                    url=str(url) if url else None,
                    enabled=bool(cfg.get("enabled", True)),
                )
            )
        return cls(servers)

    async def start(self) -> None:
        for config in self._servers:
            if not config.enabled:
                continue
            client: McpClient | HttpMcpClient
            if config.url:
                client = HttpMcpClient(config)
            else:
                client = McpClient(config)
            try:
                await client.start()
                raw_tools = await client.list_tools()
                for t in raw_tools:
                    self._tools.append(
                        McpTool(
                            name=str(t.get("name") or ""),
                            description=str(t.get("description") or ""),
                            input_schema=t.get("inputSchema"),
                            server=config.name,
                        )
                    )
                self._clients.append(client)
            except Exception as e:
                self._errors.append(f"MCP server '{config.name}': {e}")
                await client.close()

    def tools(self) -> list[McpTool]:
        return list(self._tools)

    def has_tool(self, name: str) -> bool:
        return any(t.name == name for t in self._tools)

    def errors(self) -> list[str]:
        return list(self._errors)

    async def call_tool(
        self, name: str, arguments: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        tool = next((t for t in self._tools if t.name == name), None)
        if tool is None:
            raise RuntimeError(f"Unknown MCP tool: {name}")
        client = next((c for c in self._clients if c.config.name == tool.server), None)
        if client is None:
            raise RuntimeError(f"MCP server '{tool.server}' is not running")
        result = await client.call_tool(name, arguments, timeout=timeout or 120.0)
        # Normalize MCP result into the session tool-result contract.
        content = result.get("content") or []
        texts = [str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("type") == "text"]
        output = "\n".join(texts)
        if result.get("isError"):
            raise RuntimeError(output or f"MCP tool '{name}' failed")
        return {
            "ok": True,
            "tool": name,
            "args": arguments,
            "output": output,
            "content": [{"type": "text", "text": output}],
            "isError": False,
        }

    async def close(self) -> None:
        for client in self._clients:
            await client.close()
        self._clients = []

    async def enable_server(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        url: str | None = None,
    ) -> list[str]:
        """Start a previously disabled MCP server and return the list of added tool names."""
        # If a client for this name is already running, nothing to do.
        if any(c.config.name == name for c in self._clients):
            return []
        config = McpServerConfig(
            name=name,
            command=command or "",
            args=args or [],
            env=env or {},
            url=url,
            enabled=True,
        )
        # Replace any existing config with the same name.
        self._servers = [s for s in self._servers if s.name != name]
        self._servers.append(config)
        client: McpClient | HttpMcpClient
        if config.url:
            client = HttpMcpClient(config)
        else:
            client = McpClient(config)
        try:
            await client.start()
            raw_tools = await client.list_tools()
            added: list[str] = []
            for t in raw_tools:
                mc = McpTool(
                    name=str(t.get("name") or ""),
                    description=str(t.get("description") or ""),
                    input_schema=t.get("inputSchema"),
                    server=name,
                )
                self._tools.append(mc)
                added.append(mc.name)
            self._clients.append(client)
            # Remove any stale error for this server.
            self._errors = [e for e in self._errors if name not in e]
            return added
        except Exception as e:
            await client.close()
            raise RuntimeError(f"MCP server '{name}': {e}")

    async def disable_server(self, name: str) -> list[str]:
        """Stop an MCP server and return the names of removed tools."""
        # Remove tools for this server.
        removed = [t.name for t in self._tools if t.server == name]
        self._tools = [t for t in self._tools if t.server != name]
        # Find and close the client.
        for client in self._clients:
            if client.config.name == name:
                await client.close()
                break
        self._clients = [c for c in self._clients if c.config.name != name]
        # Mark the config as disabled.
        for config in self._servers:
            if config.name == name:
                config.enabled = False
                break
        return removed

    def server_status(self) -> list[dict]:
        """Return status info for every configured server."""
        statuses: list[dict] = []
        for config in self._servers:
            running = any(c.config.name == config.name for c in self._clients)
            tools = [t.name for t in self._tools if t.server == config.name]
            error = next((e for e in self._errors if config.name in e), None)
            transport = "http" if config.url else "stdio"
            statuses.append({
                "name": config.name,
                "command": config.command,
                "enabled": config.enabled,
                "running": running,
                "tools": tools,
                "error": error,
                "transport": transport,
            })
        return statuses
