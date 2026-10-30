# Presets

A preset is one Markdown file in `$ONE_CODING_AGENT_DIR/presets` (or the normal
XDG agent directory). It has YAML frontmatter and an optional replacement role:

```md
---
version: 1
id: reviewer
name: Reviewer
model: openai/gpt-4.1
thinking: high
temperature: 0.2
tools: [read, grep, find, ls, evidence_read, finish]
mcp_servers: []
sandboxes: []
description: Careful code review.
---
Review the requested change and report evidence and risks.
```

## Included optional presets

The repository's [`extras/presets/`](../extras/presets/) directory contains
four optional workspace presets: `coder-junior-qwen`,
`coder-mid-gpt5-6-terra`, `project`, and `researcher`. These are source files,
not packaged built-ins: copy a selected file into
`$ONE_CODING_AGENT_DIR/presets/` (or the equivalent directory under
`XDG_CONFIG_HOME`) to make it discoverable. The application does not scan
`extras/` automatically.

The coding presets and Researcher include editing and shell tools; Project is
planning-only and has no `bash`, `edit`, or `apply_patch` capability. The
presets may specify model IDs that are not available in every installation;
configure the corresponding provider/model before selecting one. As with any
preset, tool lists narrow the capabilities already granted by CLI options and
parent policy; they cannot grant extra authority. Review these prompt files
before use, especially when copying them from another project version.

IDs must be lowercase filename-matching slugs. Preset files are limited to
256 KiB. Unknown or duplicate YAML keys, invalid types, duplicate IDs, path
escapes (including symlink escapes), non-finite temperatures, and nonempty
`sandboxes` are rejected. `sandboxes` must be a list of non-empty strings and
must be empty; sandboxing is not implemented or enforced.

The required schema is integer `version: 1`, `id`, and `name` (non-empty, at
most 120 characters). Optional `model` must have the form `provider/model`;
the provider/model must be available when the preset is applied. `temperature`
must be finite and is normalized using the shared temperature rules. `thinking`
must be one of `off`, `on`, `minimal`, `low`, `medium`, `high`, or `xhigh`.
`description` is at most 500 characters. `tools`, when present, is a non-empty
list of unique tool names; `sandboxes` is as described above. The optional
`mcp_servers` is a list of unique, non-empty, already-trimmed server names.
The Markdown body is the replacement role only, not a task template.

`mcp_servers` selects configured MCP servers independently of the builtin
`tools` allowlist. If omitted, it preserves the legacy behavior: MCP access is
not restricted by this preset field and remains governed by the effective
parent/CLI server ceiling and tool policy. An explicit list restricts MCP tools
to the named servers; `mcp_servers: []` denies MCP access. Names must match
configured servers when the preset is applied; unavailable or parent/CLI-
restricted selections are reported and cannot widen inherited access. This
server selection is separate from the `tools` list: listing a server does not
override tool-level or other applicable policy. Reserved packaged IDs cannot
be overridden by user files.

Either allowlist can instead use a quoted wildcard as its sole list entry:

```yaml
tools: ['*']
mcp_servers: ['*']
```

Quote `*` because unquoted YAML treats it as an alias; the value must be a list,
not the scalar `'*'`. A wildcard cannot be mixed with names and is not a glob:
`[read, '*']` and `['server*']` are invalid. `tools: ['*']` admits available
tools dynamically, while `mcp_servers: ['*']` admits MCP tools from available
servers separately from the builtin tool list. Both remain subject to inherited
parent and CLI tool and server ceilings, as well as other applicable policy.
For example, `tools: ['*']` with `mcp_servers: []` still denies MCP servers.
Omitting `tools` retains its existing behavior; `tools: []` remains invalid.
Omitting `mcp_servers` retains the legacy tool allowlist behavior described
above, unless `tools: ['*']` is set: with that wildcard and no explicit
`mcp_servers`, the preset selects all currently available servers, subject to
inherited server ceilings. `mcp_servers: []` still denies MCP access.

Wildcards are dynamic, not a frozen list: newly available tools or servers can
become accessible later, including after MCP refresh or reconnect, within those
ceilings. Prefer explicit names when future access would be undesirable.

`default`, `readonly`, and `explore` are packaged reserved IDs. User files may
not replace any of these IDs; a collision makes preset discovery fail. Default retains the normal
dynamic prompt, project guidance, skills, and current model settings. Readonly
allows inspection, HTTP(S) GET via `web_connect`, evidence reads, completion,
and cooperation-gated questions; it denies shell and editing tools at dispatch
time, including refreshed MCP tools. This is app-enforced read-only behavior,
not a network restriction: `web_connect` can access localhost and private
services reachable to the process, and GET requests may have side effects.
It is an application policy, not an OS sandbox and does not confine extensions
that have already run. While active it denies app-mediated manual shell commands
and does not bind or invoke extension hooks; normal session persistence remains
available. Both Default and Readonly include `web_connect`.
Existing saved effective preset snapshots are not expanded automatically; reapply
the preset to adopt the new tool list (subject to parent/CLI ceilings).

`explore` is a focused project investigation role. It searches and reads local
resources, traces relevant behavior, and reports findings with path and line
evidence. Its app-enforced ceiling excludes shell, editing, HTTP, and MCP tools;
it allows read-only delegation subject to the same inherited ceiling. As with
Readonly, manual shell commands and extension hooks are blocked, and activation
requires a clean session if extensions have already executed. It is not an OS
sandbox. Use `one --preset explore ...`, `/preset explore`, or request
`preset: "explore"` when spawning a subagent. Existing session snapshots retain
their saved effective settings; explicitly reapply the preset to adopt changes.

Use `one --preset readonly ...`, or `/preset` to list and `/preset readonly` to
apply an idle session preset in TUI or interactive mode. RPC exposes
`list_presets` and `set_preset`. RPC request `id` is only the correlation key;
use `{"type":"set_preset","id":"request-7","presetId":"readonly"}`
(or `preset`) to select a preset. `--preset` cannot be combined with the replacement
`--system-prompt`. Subagents may request `preset`; explicit child model/tools win
over configuration, while parent and CLI capability ceilings always intersect.
Preset files are data, not templates.

In the TUI main input, **Shift+Tab** cycles through the `/preset` discovery order
and wraps from the last preset to the first. It preserves the draft and cursor,
updates the sidebar, and uses the same idle-only activation checks as `/preset`.
If activation fails, the error is displayed and the current preset is retained.
The next Shift+Tab advances past that failed candidate, so an unavailable model
does not trap keyboard cycling. Explicit `/preset id` selection still validates
and applies only the requested preset.
Dialogs and open overlays keep their normal Shift+Tab focus navigation.

For a new session the order is explicit CLI provider/model, temperature mode or
temperature, and thinking setting; then preset fields; then the existing resolved
settings. A resumed or forked session restores its saved effective preset snapshot
(including its prompt and restrictions) rather than re-reading a changed file.
Manual model, thinking, temperature, or tool changes retain the preset identity and
are shown as `modified`. Selecting a preset is rejected while the session is busy.
Explicit CLI/subagent model, temperature, and thinking values override preset
configuration; preset values override ordinary defaults. For subagents, explicit
child model/tools values take precedence over corresponding preset configuration.
Parent and CLI capability ceilings always win, including for tools and MCP
servers selected by a preset. Saved effective session snapshots retain their
server ceilings; changing a preset file does not expand them automatically.
