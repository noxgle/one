---
title: "Sessions and configuration"
---

### ChatGPT cache affinity and usage telemetry

ChatGPT/Codex requests send stable, official `session-id` and `thread-id`
headers derived from one non-secret per-session lineage id; subagents and forks
inherit it. This lets provider-side prompt caching key on the same identity
across requests instead of a fresh random id per call. The legacy underscore
`session_id` header remains per-request and unchanged. Because the Codex
backend is private and may change, stable affinity can be disabled without
rebuilding:

```json
{
  "providerProfiles": {
    "chatgpt": { "experimentalStableSessionId": false }
  }
}
```

Project `.one/settings.json` takes precedence over global settings. The flag
affects only ChatGPT; other providers keep their existing behavior. `/stats`
includes a `providerCache` section with request counts, provider-reported
input, cached, output and reasoning totals, `cacheRatio`, and coverage
(`requestsWithUsage`, `ratioRequests`, `usageComplete`). Missing provider usage
is reported as `null` (unknown), never zero. `/stats` also includes a
`toolUsage` map of tool name → call count for the current session, covering
every tool call that reached execution (including cancelled/aborted calls).

### Prompt caching for GPT-6.2 (opt-in)

GPT-6.2 models (`gpt-6.2-sol`, `gpt-6.2-astra`) never cache top-level
`instructions`, so `one` moves the system prompt into the first `developer`
message where it participates in provider-side prompt caching. Prompt caching
is off by default and changes nothing until enabled:

```json
{
  "providerProfiles": {
    "chatgpt": {
      "promptCache": {
        "enabled": true,
        "ttl": "30m",
        "stableBreakpoint": true,
        "toolResultBreakpoints": false,
        "key": "off",
        "prewarm": false
      }
    }
  }
}
```

- `ttl` accepts `30m` (default) or `12h` (Responses only). `24h` is legacy and
  rejected for GPT-6.2.
- `stableBreakpoint` adds an explicit `prompt_cache_breakpoint` after the
  stable developer block so the dynamic tail (plan, date, tools) does not
  invalidate the cached prefix.
- `key` is `off` (default, no per-session key) or `account` (a stable,
  non-secret key derived from the credential so all sessions of one account
  share a cache partition).
- `prewarm` issues a one-time `prompt_cache_options.prewarm` request at the
  start of the first prompt to write the stable prefix into cache without
  generating output.
- If the backend rejects the cache fields (HTTP 400/422), `one` retries once
  without them and disables caching for the session.

Cache telemetry splits writes by TTL: `providerCache` now reports `cacheWrite`,
`cacheWrite30m`, and `cacheWrite12h` alongside the existing counters. No live
benchmark was performed; behavior is unchanged when the flag is off.

### Task checkpoints (opt-in)

In the TUI, run `/checkpoints enable` before a task to retain undo records for
direct `write`, `edit`, and `apply_patch` changes. `/checkpoints list` shows task
IDs; `/checkpoints undo ID` prepares the latest eligible task and displays a
confirmation token. Only `/checkpoints confirm TOKEN` restores files. Optional
relative paths select part of a task; move endpoints must be selected together.
Use `/checkpoints cancel` to cancel and `/checkpoints delete ID` to remove a
retained record. Settings `{"checkpoints": {"enabled": true}}` enable recording
for persistent sessions automatically. Once enabled, a session resumes recording
from its existing sidecar.

Saved file contents can include secrets. Checkpoints are private local data, not
encrypted backups; shell, MCP, extensions and subagent changes are not covered.
Undo refuses conflicts and requires idle state and write capability. It is not
atomic against concurrent external writers. See [the full contract](../library/checkpoints.md)
for limits, retention, platform requirements and safety restrictions.

Named agent presets, including MCP server selection and diagnostics, are documented
in [docs/PRESETS.md](../library/presets.md). Use
`one --preset readonly` for app-enforced read-only inspection (not OS or network sandboxing).
List presets with `/preset`, apply one with `/preset readonly`, or use
Ctrl+P → Select preset. Shift+Tab cycles presets from the main input while idle.
Readonly is application policy, not a sandbox; see the preset documentation
for capability ceilings and saved-session behavior.

Preset YAML may use `tools: ['*']` or `mcp_servers: ['*']` to dynamically
include available tools or MCP servers under inherited parent and CLI ceilings.
The quoted wildcard must be the only list entry, not a scalar or glob; newly
available tools and servers may become accessible later. Omitting fields keeps
legacy behavior, and `mcp_servers: []` still denies MCP access.

Optional workspace presets, including Project, Researcher, and coding-agent
profiles, are available under [`extras/presets/`](https://github.com/noxgle/one/blob/main/extras/presets); copy a
selected preset into the configured agent directory's `presets/` folder to use it.

By default state is private under `~/.config/one`. If `XDG_CONFIG_HOME` is set,
the default becomes `$XDG_CONFIG_HOME/one`; `ONE_CODING_AGENT_DIR` overrides
both locations. State includes `auth.json`, `models.json`, `settings.json`, sessions,
and reports. A project `.one/` directory supplies per-project MCP and extension
configuration. Use `--no-session`, `--session`, `--continue`, `--resume`, or
`--fork` to control session use. Use `--tools <list>` to restrict the agent to
a specific set of tools and `--no-tools` to disable all built-in tools (useful
for agent-only MCP/extension setups).

Session JSONL, durable evidence, and provider context are separate. Provider
context receives bounded tool/MCP previews; durable sessions can retain complete
sanitized results in an evidence sidecar retrievable by the `evidence_read` tool.
See [Cooperation and safety](cooperation-and-safety.md) and the slash-command
help for configuration such as `tools.maxSteps` (`0` means unlimited tool steps)
and optional `toolOutputPruning`.

### Native HTTP retrieval (`web_connect`)

`web_connect` is available in Default and Readonly and does not require `bash`.
To restrict a session to retrieval and evidence, for example:

```bash
one --tools web_connect,evidence_read,finish
```

It accepts only `url` (HTTP(S), no embedded credentials) and optional `mode`:
`auto` (default: HTML to Markdown, JSON and text unchanged), `text` (HTML to
plain text), or `raw` (decoded textual source, not a binary download). HTML
extraction preserves tables, links and fenced code; it never executes JavaScript
or fetches embedded resources. Relative links use the final URL. No search,
custom headers, authentication or non-GET methods are provided.

Experimental compaction V2 is opt-in; file reread is separately opt-in and
native production capability remains disabled. For example, in `settings.json`:

```json
{
  "compaction": {
    "experimentalV2": true,
    "rehydrate": {"enabled": false, "maxFiles": 5, "maxTokens": 12000}
  }
}
```

See [compaction](../library/compaction.md) for limits, privacy, provider compatibility,
rollback and offline evaluation. Project settings override global settings.

Configure web limits in `settings.json` (global/project merge applies):

```json
{
  "webConnect": {
    "timeoutSec": 15,
    "maxResponseBytes": 2000000,
    "maxOutputChars": 12000,
    "maxRedirects": 5,
    "extractMainContent": true,
    "preserveLinks": true,
    "preserveCodeBlocks": true
  }
}
```

`timeoutSec` covers redirects, transfer and processing; an earlier runtime tool
deadline wins. `maxResponseBytes` independently caps encoded transfer bytes and
decoded body bytes. Gzip/deflate are supported; malformed/truncated compression
and byte-limit overflow fail without presenting partial content. `maxOutputChars`
limits the preview, not durable evidence. Retrieve the full permitted output via
the returned `evidenceId` and bounded `evidence_read` calls, following `nextOffset`.
Evidence requires an available session store; a storage failure is reported
rather than returning a fictitious ID. Generic runtime preview limits still apply.

Non-2xx responses, unsupported binary types and invalid JSON in `auto`/`text`
produce errors. `raw` can show invalid JSON source. Missing Content-Type is treated
as text (binary-looking data is rejected); absent/invalid charset falls back to
UTF-8 replacement decoding. Empty successful responses are allowed. Proxy
environment variables are ignored, TLS verification stays enabled, and cookies
are not forwarded between redirects. URL queries/credentials are redacted in tool
runtime diagnostics and metadata, not necessarily in original conversation input
or returned document content. Do not put secrets in URLs or prompts.

**Network access is not sandboxed:** localhost, private networks and metadata
services are reachable if the process can reach them. GET requests may have side
effects. Readonly limits ONE's app-mediated tools, not network reachability;
parent/CLI capability ceilings still apply. Pages and evidence are untrusted
tool data, not instructions. See [SECURITY.md](https://github.com/noxgle/one/blob/main/SECURITY.md).

### Context policy

`context.policy` defaults to `endpoint-max`, using endpoint capacity while
respecting observed server and manual limits. Set `/config context.policy
provider-default` to use a provider-advertised default input budget instead.
Manual smaller limits are retained. Unknown models use an internal estimate only;
see [model capacity metadata](../library/model_catalog.md) for diagnostics and caveats.

### Generation budget

Output limits are selected in this order: `models.json` `outputLimit`, then
model catalogue `outputLimit`, then `settings.json` `generation.maxOutputTokens`.
The last setting is a shared positive-integer fallback for models without known
output capacity (default **16384**), not an override of known limits. For example,
from the TUI:

```text
/config generation.maxOutputTokens 32768
```

This is not the model's context-window size or a guarantee of backend capacity.
The selected limit is clamped to available context;
reasoning tokens can consume the budget even when the visible answer is short.
Larger budgets may increase latency and usage. Changes affect subsequent
requests. See [model capacity and truncated responses](../library/model_catalog.md#generation-limit-and-truncated-responses)
for reserve behavior and output-truncation diagnostics.

For a shared, opt-in budget across a prompt and its subagents, see
[runs and diagnostics](../library/runs.md). `runBudget` is separate from the legacy
session `budget`; estimated usage is not a provider billing guarantee.

### Subagent timeouts

Subagents use `subagents.idleTimeoutSec` (default `1800`) for time without
meaningful provider text/reasoning or tool activity. Set it to `0` only to
disable the idle watchdog. `subagents.maxDurationSec` defaults to `0`, which
disables the separate hard wall-clock cap; a positive value stops even an active
stream at that limit. Existing `subagents.timeoutSec` remains an idle-timeout
alias only when `idleTimeoutSec` is absent.

An idle or maximum-duration timeout returns a failed, incomplete child result
with its session ID and bounded partial diagnostics. It does not claim external
work stopped (`externalState` is `unknown`); inspect that session and external
state before retrying any operation.

### Subagent tools

The optional `spawn_subagent` `tools` list must be non-empty. The child receives
`finish` automatically when that explicit list, or its inherited tool set, omits
it; duplicate tool names are removed while preserving order. Built-in and active
MCP tool names are validated before the child starts.

### TUI session browser

#### Window: Ctrl+P → Sessions

Choose **Sessions** in Ctrl+P to browse persisted sessions for the current
project. The searchable list is ordered by last update and marks the current
session with `●`. Search by title or full ID; the details show the full selected
ID, update time, and message count. Titles may repeat: loading always validates
the exact ID and path, never a title or a replacement row.

Use Up/Down, PageUp/PageDown, or the mouse to navigate. **Load (Enter)** opens
the selected session, **Refresh** reloads the list, and **Back (Esc)** returns
to Ctrl+P with its previous search intact. Opening, searching, or refreshing
this browser does not replace the numbering of a previously displayed
`/sessions` list.

If the conversation draft is nonempty, Load/Enter first asks whether to keep it
and switch; press Load/Enter again to confirm, or Esc to cancel. The draft is
kept after switching. Loading the active session is a no-op. Loading is blocked
while a turn is active; stale selections and failures leave the browser open
with feedback. The list does not load every conversation body, and model
metadata is not currently shown in this browser.

#### Text commands: list, load, rename, delete

Persisted sessions are scoped to the current project/session directory. In the
TUI, `/sessions` lists them newest first with a temporary number, full session
ID, title, age, and message count (`*` marks the active session). The first user prompt automatically becomes the local session
name after whitespace/control-character normalization (up to 64 Unicode
characters); rename it when needed.

```text
/sessions
/sessions 2
/sessions a8c42bd1-9144-42c1-a92d-52ebd540b3dc
/sessions rename 2 Parser investigation
/sessions delete a8c42bd1-9144-42c1-a92d-52ebd540b3dc
yes
```

Load, rename, and delete accept a **number from the last displayed list** or an
**exact full session ID** copied from your list (the UUID above is illustrative).
IDs work without first listing sessions. Titles are descriptive only and may
repeat; neither title selection nor ID-prefix matching is supported. Run
`/sessions` before using a number. Numbers retain the last displayed ordering;
missing or replaced sessions are rejected rather than selecting another session.
Switching, renaming, or deleting invalidates the numbered list; list again before
using another number. `delete` always asks for `yes`; any
other reply cancels it. It deletes the JSONL and its matching durable-evidence
sidecar. Deleting the active session opens a new empty session so the TUI stays
usable. Loading or deleting waits until an active turn has stopped. Use
`--continue` or `--resume` at startup to open the most recently modified session.
