---
title: "Integrations"
---

### MCP servers

**Ctrl+P → MCP servers** opens a searchable status browser. Enabled configuration
and actual connection state are shown separately; disabled and failed servers
remain visible. Enter or a list click opens details (transport, restart attempts,
and up to 100 tool names), never toggles a server. Use the explicit **Enable
server / Disable server** button to change and persist its state. **Refresh**
only reads status; it does not restart a server. Esc/Back returns to Ctrl+P with
its search and the conversation draft preserved.

Mutations are blocked while streaming, under the Readonly capability policy,
and when MCP is unavailable (`--no-mcp`). Enable can launch a process or connect
to a remote service with the agent's permissions. Errors stay in the window;
connection failures are shown generically rather than exposing raw diagnostic
text, command arguments, environment values, or URLs. The browser does not edit
configuration or offer restart-all. Existing `/mcp` commands remain available.


MCP servers can be configured in global or project `settings.json` with
`mcpServers`. Stdio servers run as child processes; streamable HTTP servers use
their configured URL. Use `--no-mcp` to disable all of them for a run and
`/mcp list|enable|disable` in the TUI. MCP tools are added to the agent prompt.
Stdio JSON-RPC messages are limited to 4 MiB per line, excluding the newline;
larger messages fail with a transport error and the connection is discarded.

Stdio servers can opt into runtime recovery after an unexpected process exit or
stdout EOF; recovery remains off by default for stdio. Streamable HTTP servers
recover failed connections by default because they have no child-process monitor.
Set `"restart": false` on an HTTP server to opt out. The following optional
fields belong under `mcpServers.<name>`:

```json
{
  "restart": true,
  "restartDelaySec": 60,
  "maxRestartAttempts": 3,
  "restartExhaustion": "disable",
  "retryIntervalSec": 300
}
```

After each failure, `one` removes that server's tools immediately and waits
`restartDelaySec` before reconnecting. A successful reconnect restores the
tools. At the attempt limit, the `disable` exhaustion policy stops recovery for
that runtime only (the configured `enabled` value is not changed); `retry`
continues at `retryIntervalSec`. `/mcp disable` cancels pending recovery and
persists `enabled: false` until `/mcp enable` resets recovery and persists
`enabled: true`. Recovery never replays the interrupted tool call.

For web search, the [web-deepsearch MCP server](https://github.com/noxgle/mcp-web-deepsearch)
uses DuckDuckGo and can run over stdio. It needs no API key, but its server is
not sandboxed and may require outbound network access.

### Extensions and skills

Extensions, themes, prompts, and skills are discovered by the resource loader.
See [docs/EXTENSIONS.md](../library/extensions.md) for discovery paths, `--extension`,
package commands, and hooks. See [docs/SKILLS.md](../library/skills.md) for skill
frontmatter, discovery, trust prompts, and invocation. Skills are metadata in
the system prompt and load only when invoked with `/skill:<name>` or RPC
`invoke_skill`; they are not auto-executed.
