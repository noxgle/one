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

IDs must be lowercase filename-matching slugs. Unknown or duplicate YAML keys,
invalid types, duplicate IDs, path escapes, non-finite temperatures, and nonempty
`sandboxes` are rejected. Sandboxes are not implemented and are never claimed as
enforced.

The required schema is `version: 1`, `id`, and `name`; optional `model` is a
resolvable `provider/model`, while `temperature`, `thinking`, `description`,
`tools`, and `sandboxes` have the types shown above. The Markdown body is the
replacement role only, not a task template.

`default` and `readonly` are packaged reserved IDs. Default retains the normal
dynamic prompt, project guidance, skills, and current model settings. Readonly
allows inspection, HTTP(S) GET via `web_connect`, evidence reads, completion,
and cooperation-gated questions;
it denies shell and editing tools at dispatch time, including refreshed MCP tools.
It is an application policy, not an OS sandbox and does not confine extensions
that have already run. While active it denies app-mediated manual shell commands
and does not bind or invoke extension hooks; normal session persistence remains
available. Both Default and Readonly include `web_connect`. GET may have side
effects and access localhost/private services; Readonly is not a network sandbox.
Existing saved effective preset snapshots are not expanded automatically; reapply
the preset to adopt the new tool list (subject to parent/CLI ceilings).

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
configuration; preset values override ordinary defaults, while parent and CLI tool
ceilings always win.
