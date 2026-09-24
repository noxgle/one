from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from one.config import VERSION as _ONE_VERSION


def _ensure_url(config: McpServerConfig) -> str:
    """Return config.url, raising if None (caller should only use this for HTTP transport)."""
    if config.url is None:
        raise ValueError(f"HTTP transport requires a URL for MCP server '{config.name}'")
    return config.url


def _nonnegative_float(value: Any, default: float) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _nonnegative_int(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


@dataclass
class McpServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    enabled: bool = True
    restart: bool | None = None
    restart_delay_sec: float = 60.0
    max_restart_attempts: int = 3
    restart_exhaustion: str = "disable"
    retry_interval_sec: float = 300.0

    def __post_init__(self) -> None:
        # HTTP servers have no child-process monitor, so recover their failed
        # connections unless recovery was explicitly configured off. Preserve
        # stdio's historical opt-in behavior.
        if self.restart is None:
            self.restart = bool(self.url)


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
        self._stderr_task: asyncio.Task[None] | None = asyncio.create_task(self._drain_stderr())
        await self._request(
            "initialize",
            {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "one", "version": _ONE_VERSION},
            },
        )
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

        # Cancel the fire-and-forget stderr drain task.
        stderr_task = getattr(self, "_stderr_task", None)
        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass

        if proc is None:
            return

        try:
            # Close stdin to signal the server we're done writing.
            if proc.stdin is not None:
                proc.stdin.close()
                try:
                    await proc.stdin.wait_closed()
                except Exception:
                    pass
        except Exception:
            pass

        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await proc.wait()
            except Exception:
                pass

        # Close stdout/stderr read streams to release transport references.
        # feed_eof() signals EOF so any pending reads fail immediately.
        try:
            if proc.stdout is not None:
                proc.stdout.feed_eof()
        except Exception:
            pass
        try:
            if proc.stderr is not None:
                proc.stderr.feed_eof()
        except Exception:
            pass

    def stderr_tail(self) -> list[str]:
        return list(self._stderr_tail)

    async def wait_for_exit(self) -> int:
        """Wait for the child process to exit without changing its lifecycle."""
        if self._proc is None:
            raise RuntimeError(f"MCP server '{self.config.name}' has no process to wait for")
        return await self._proc.wait()


class HttpMcpClient:
    """MCP server connection over HTTP streamable transport (JSON-RPC 2.0, buffered POST)."""

    PROTOCOL_VERSION = "2025-06-18"

    def __init__(self, config: McpServerConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(120.0), follow_redirects=True)
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
                event_name = line[len("event:") :].strip()
                continue
            if line.startswith("data:"):
                data_lines.append(line[len("data:") :])
                continue
            if line == "":
                _flush()
        _flush()  # flush trailing event at EOF
        return messages

    async def _post(self, payload: dict[str, Any], timeout: float = 15.0) -> tuple[list[dict[str, Any]], httpx.Headers]:
        url = _ensure_url(self.config)
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.PROTOCOL_VERSION,
        }
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        resp = await self._client.post(url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code >= 400:
            body = resp.text[:300] if resp.text else ""
            raise RuntimeError(f"MCP server '{self.config.name}' HTTP {resp.status_code}: {body}")
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return self._parse_sse(resp.text), resp.headers
        body_text = resp.text.strip()
        if body_text:
            return [resp.json()], resp.headers
        return [], resp.headers

    async def _request(self, method: str, params: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
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
                raise RuntimeError(f"MCP server '{self.config.name}' error {err.get('code')}: {err.get('message')}")
            return msg.get("result") or {}
        raise RuntimeError(f"MCP server '{self.config.name}' did not respond to '{method}'")

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        await self._post(payload)

    async def start(self) -> None:
        await self._request(
            "initialize",
            {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "one", "version": _ONE_VERSION},
            },
            timeout=15.0,
        )
        try:
            await self._notify("notifications/initialized")
        except Exception:
            pass  # some servers return 202 with empty body

    async def list_tools(self, timeout: float = 15.0) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {}, timeout=timeout)
        return result.get("tools") or []

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
        return await self._request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)

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
        self._runtime: dict[str, dict[str, Any]] = {
            server.name: {"state": "disabled" if not server.enabled else "configured", "attempts": 0}
            for server in servers
        }
        self._restart_tasks: dict[str, asyncio.Task[None]] = {}
        self._monitor_tasks: dict[str, asyncio.Task[None]] = {}
        self._closing = False
        self._tools_changed_callback: Any = None

    @classmethod
    def create(cls, settings_manager: Any) -> McpManager:
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
                    restart=bool(cfg.get("restart", bool(url))),
                    restart_delay_sec=_nonnegative_float(cfg.get("restartDelaySec", 60), 60.0),
                    max_restart_attempts=_nonnegative_int(cfg.get("maxRestartAttempts", 3), 3),
                    restart_exhaustion=(
                        str(cfg.get("restartExhaustion", "disable"))
                        if str(cfg.get("restartExhaustion", "disable")) in {"disable", "retry"}
                        else "disable"
                    ),
                    retry_interval_sec=_nonnegative_float(cfg.get("retryIntervalSec", 300), 300.0),
                )
            )
        return cls(servers)

    def set_tools_changed_callback(self, callback: Any) -> None:
        """Install the session hook used to refresh the agent's MCP tool catalog."""
        self._tools_changed_callback = callback

    def _notify_tools_changed(self) -> None:
        if self._tools_changed_callback is not None:
            try:
                self._tools_changed_callback()
            except Exception:
                pass

    def _clear_error(self, name: str) -> None:
        self._errors = [error for error in self._errors if not error.startswith(f"MCP server '{name}':")]

    def _add_tools(self, config: McpServerConfig, raw_tools: list[dict[str, Any]]) -> list[str]:
        self._tools = [tool for tool in self._tools if tool.server != config.name]
        added: list[str] = []
        for tool in raw_tools:
            mcp_tool = McpTool(
                name=str(tool.get("name") or ""), description=str(tool.get("description") or ""),
                input_schema=tool.get("inputSchema"), server=config.name,
            )
            self._tools.append(mcp_tool)
            added.append(mcp_tool.name)
        return added

    async def _connect(self, config: McpServerConfig) -> list[str]:
        client: McpClient | HttpMcpClient = HttpMcpClient(config) if config.url else McpClient(config)
        try:
            await client.start()
            added = self._add_tools(config, await client.list_tools())
            self._clients = [existing for existing in self._clients if existing.config.name != config.name]
            self._clients.append(client)
            self._clear_error(config.name)
            self._runtime[config.name] = {"state": "running", "attempts": 0}
            if isinstance(client, McpClient):
                self._monitor_tasks[config.name] = asyncio.create_task(self._monitor_stdio(client))
            self._notify_tools_changed()
            return added
        except Exception:
            await client.close()
            raise

    async def _monitor_stdio(self, client: McpClient) -> None:
        try:
            exit_code = await client.wait_for_exit()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self._closing:
                await self._server_failed(client.config.name, client, error)
        else:
            if not self._closing and any(existing is client for existing in self._clients):
                await self._server_failed(
                    client.config.name,
                    client,
                    RuntimeError(f"exited with code {exit_code}"),
                )
        finally:
            current = asyncio.current_task()
            if current is not None and self._monitor_tasks.get(client.config.name) is current:
                self._monitor_tasks.pop(client.config.name, None)

    @staticmethod
    def _failure_message(
        name: str, client: McpClient | HttpMcpClient | None, error: Exception
    ) -> str:
        message = f"MCP server '{name}': {error}"
        if client is not None:
            try:
                stderr_tail = client.stderr_tail()
            except Exception:
                stderr_tail = []
            if stderr_tail:
                message += f"; stderr: {' | '.join(stderr_tail)}"
        return message

    async def _server_failed(self, name: str, client: McpClient | HttpMcpClient | None, error: Exception) -> None:
        """Remove a failed runtime connection; configuration remains intact."""
        if self._closing:
            return
        if client is not None and not any(existing is client for existing in self._clients):
            return
        config = next((server for server in self._servers if server.name == name), None)
        if config is None:
            return
        removed_tools = [tool for tool in self._tools if tool.server == name]
        self._tools = [tool for tool in self._tools if tool.server != name]
        failed_clients = [existing for existing in self._clients if existing.config.name == name]
        self._clients = [existing for existing in self._clients if existing.config.name != name]
        self._errors = [entry for entry in self._errors if not entry.startswith(f"MCP server '{name}':")]
        self._errors.append(self._failure_message(name, client, error))
        runtime = self._runtime.setdefault(name, {"attempts": 0})
        runtime["state"] = "failed"
        if removed_tools:
            self._notify_tools_changed()
        await asyncio.gather(*(failed.close() for failed in failed_clients), return_exceptions=True)
        if config.restart and config.enabled:
            self._schedule_restart(config)

    def _schedule_restart(self, config: McpServerConfig) -> None:
        task = self._restart_tasks.get(config.name)
        if task is None or task.done():
            self._restart_tasks[config.name] = asyncio.create_task(self._restart_loop(config))

    async def _restart_loop(self, config: McpServerConfig) -> None:
        try:
            while not self._closing and config.enabled:
                runtime = self._runtime.setdefault(config.name, {"attempts": 0})
                attempts = int(runtime.get("attempts", 0))
                exhausted = attempts >= config.max_restart_attempts
                if exhausted and config.restart_exhaustion == "disable":
                    runtime["state"] = "disabled"
                    return
                delay = config.retry_interval_sec if exhausted else config.restart_delay_sec
                runtime["state"] = "retrying"
                await asyncio.sleep(delay)
                if self._closing or not config.enabled:
                    return
                try:
                    await self._connect(config)
                    return
                except Exception as error:
                    runtime["attempts"] = attempts + 1
                    runtime["state"] = "failed"
                    self._errors = [entry for entry in self._errors if not entry.startswith(f"MCP server '{config.name}':")]
                    self._errors.append(self._failure_message(config.name, None, error))
        finally:
            current = asyncio.current_task()
            if current is not None and self._restart_tasks.get(config.name) is current:
                self._restart_tasks.pop(config.name, None)

    async def start(self) -> None:
        for config in self._servers:
            if not config.enabled:
                continue
            try:
                await self._connect(config)
            except Exception as e:
                await self._server_failed(config.name, None, e)

    def tools(self) -> list[McpTool]:
        return list(self._tools)

    def has_tool(self, name: str) -> bool:
        return any(t.name == name for t in self._tools)

    def errors(self) -> list[str]:
        return list(self._errors)

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        tool = next((t for t in self._tools if t.name == name), None)
        if tool is None:
            raise RuntimeError(f"Unknown MCP tool: {name}")
        client = next((c for c in self._clients if c.config.name == tool.server), None)
        if client is None:
            raise RuntimeError(f"MCP server '{tool.server}' is not running")
        # AgentSession supplies the normalized timeout. Retain the compatibility
        # fallback for direct manager callers.
        try:
            result = await client.call_tool(name, arguments, timeout=120.0 if timeout is None else timeout)
        except Exception as error:
            await self._server_failed(tool.server, client, error)
            raise
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
        self._closing = True
        tasks = [*self._restart_tasks.values(), *self._monitor_tasks.values()]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._restart_tasks = {}
        self._monitor_tasks = {}
        await asyncio.gather(*(c.close() for c in self._clients), return_exceptions=True)
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
        previous = next((server for server in self._servers if server.name == name), None)
        config = McpServerConfig(name=name, command=command or "", args=args or [], env=env or {}, url=url, enabled=True,
                                  restart=previous.restart if previous else bool(url),
                                 restart_delay_sec=previous.restart_delay_sec if previous else 60.0,
                                 max_restart_attempts=previous.max_restart_attempts if previous else 3,
                                 restart_exhaustion=previous.restart_exhaustion if previous else "disable",
                                 retry_interval_sec=previous.retry_interval_sec if previous else 300.0)
        # Replace any existing config with the same name.
        self._servers = [s for s in self._servers if s.name != name]
        self._servers.append(config)
        task = self._restart_tasks.pop(name, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        monitor = self._monitor_tasks.pop(name, None)
        if monitor is not None and not monitor.done():
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        self._runtime[name] = {"state": "configured", "attempts": 0}
        try:
            return await self._connect(config)
        except Exception as e:
            raise RuntimeError(f"MCP server '{name}': {e}")

    async def disable_server(self, name: str) -> list[str]:
        """Stop an MCP server and return the names of removed tools."""
        task = self._restart_tasks.pop(name, None)
        monitor = self._monitor_tasks.pop(name, None)
        for pending in (task, monitor):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(*(pending for pending in (task, monitor) if pending is not None), return_exceptions=True)
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
        self._runtime[name] = {"state": "disabled", "attempts": 0}
        self._clear_error(name)
        self._notify_tools_changed()
        return removed

    def server_status(self) -> list[dict]:
        """Return status info for every configured server."""
        statuses: list[dict] = []
        for config in self._servers:
            running = any(c.config.name == config.name for c in self._clients)
            tools = [t.name for t in self._tools if t.server == config.name]
            error = next((e for e in self._errors if config.name in e), None)
            transport = "http" if config.url else "stdio"
            runtime = self._runtime.get(config.name, {})
            statuses.append(
                {
                    "name": config.name,
                    "command": config.command,
                    "enabled": config.enabled,
                    "running": running,
                    "tools": tools,
                    "error": error,
                    "transport": transport,
                    "runtimeState": runtime.get("state", "configured"),
                    "restartEnabled": config.restart,
                    "restartAttempts": runtime.get("attempts", 0),
                    "maxRestartAttempts": config.max_restart_attempts,
                    "restartExhaustion": config.restart_exhaustion,
                }
            )
        return statuses
