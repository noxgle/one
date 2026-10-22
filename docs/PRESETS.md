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
allows inspection, evidence reads, completion, and cooperation-gated questions;
it denies shell and editing tools at dispatch time, including refreshed MCP tools.
It is an application policy, not an OS sandbox and does not confine extensions
that have already run. While active it denies app-mediated manual shell commands
and does not bind or invoke extension hooks; normal session persistence remains
available.

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
