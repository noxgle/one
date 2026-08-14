# Project: one — MCP streamable-HTTP transport

## Goal
Add HTTP streamable transport to MCP clients (in addition to existing stdio). Support protocol version `2025-06-18` only — no fallback. A rejected/unsupported version surfaces as a clear RuntimeError.

## Context
- MCP servers can now connect via HTTP POST (streamable-HTTP) using `url` instead of `command`.
- httpx is already a dependency (used by providers).
- Protocol version bumped to `2025-06-18`.

## Phases

### Phase 1: McpServerConfig + HttpMcpClient (one/mcp/client.py)
- [x] Add `url: str | None = None` field to `McpServerConfig` (after `env`).
- [x] New `HttpMcpClient` class mirroring `McpClient`'s public API (`start/list_tools/call_tool/close/stderr_tail`).
- [x] Protocol version `2025-06-18`. POST to `config.url` with `Content-Type: application/json`, `Accept: application/json, text/event-stream`, `MCP-Protocol-Version` headers.
- [x] Session-ID tracking: read `Mcp-Session-Id` from initialize response, echo back in subsequent requests.
- [x] SSE body parsing via `_parse_ssse()` — skip `:` comments, accumulate `event:`/`data:` lines, flush on blank line.
- [x] HTTP errors → RuntimeError with `HTTP <status>: <body>`. No matching response → RuntimeError with "did not respond".
- [x] `_notify("notifications/initialized")` is best-effort (swallow exceptions for 202/empty).
- [x] Export `HttpMcpClient` from `one/mcp/__init__.py`.

### Phase 2: McpManager adapts (one/mcp/client.py)
- [x] `create()`: if `cfg.get("url")` → build `McpServerConfig` with `url`; else existing stdio path. Skip configs with neither.
- [x] `start()`: `HttpMcpClient(config) if config.url else McpClient(config)`.
- [x] `enable_server()`: accept optional `url` param; build config with url.
- [x] `server_status()`: add `"transport": "http" if config.url else "stdio"`.
- [x] `_clients` list type: `list[McpClient | HttpMcpClient]`.

### Phase 3: TUI + interactive rendering (one/modes/tui_mode.py, one/modes/interactive_mode.py)
- [x] MCP list output: append transport — `f"- {s['name']}: {state} ({s.get('transport', 'stdio')}) [{tools_list}]"`.
- [x] `/mcp enable <name>`: check `not cfg.get("command") and not cfg.get("url")` instead of just `command`.
- [x] Pass `url=cfg.get("url")` to `enable_server`.

### Phase 4: README + tests
- [x] README.md: add URL config example with `web-deepsearch`, document `url` vs `command` transport.
- [x] tests/test_mcp.py: 4 HTTP client tests (`httpx.MockTransport`):
  - `test_http_client_initialize_and_session_id` — headers + session-ID echo-back.
  - `test_http_client_call_tool_sse` — SSE body → correct result.
  - `test_http_client_error_status` — HTTP 500 → RuntimeError.
  - `test_http_client_no_matching_response` — wrong ID → "did not respond".
  - `test_http_client_notifications_initialized_202` — implicit via start().
  - `test_mcp_manager_create_http_config` — creates config with `transport == "http"`.
  - `test_mcp_manager_http_connection_error` — connection refused → error in status.
  - `test_mcp_manager_enable_server_http_url` — enable with url raises; disable safe.
- [x] Fake MCP manager doubles in tui/interactive tests: add `"transport": "stdio"`.
- [x] Update MCP list assertions to include `(stdio)`.

### Phase 5: Verification
- [ ] `.venv/bin/python -m pytest -q tests/test_mcp.py tests/test_tui_mode.py tests/test_interactive_mode.py` — all pass.
- [ ] `.venv/bin/python -m pytest -q` — full suite (306 tests) passes.

## Constraints
- Do NOT touch files outside: `one/mcp/client.py`, `one/modes/tui_mode.py`, `one/modes/interactive_mode.py`, `README.md`, `tests/test_mcp.py`, `tests/test_tui_mode.py`, `tests/test_interactive_mode.py`, `todo.md`.
- No new dependencies. No stdio behavior changes. No event contract changes.
