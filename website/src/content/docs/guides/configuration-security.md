---
title: Configuration precedence and security
---

Settings merge recursively: built-in defaults, global `settings.json`, then
project `.one/settings.json`. A non-object replaces the previous value; some
settings have additional normalization or restrictions. CLI options and runtime
choices may override settings for specific operations; there is no universal
rule that every CLI option maps to a setting. See the [settings inventory](../reference/settings.md).

The global state directory defaults to `~/.config/one`, or `$XDG_CONFIG_HOME/one`;
`ONE_CODING_AGENT_DIR` overrides both. Legacy state may be migrated; see
`one/config.py`. Authentication precedence is runtime input, stored `auth.json`,
then the provider environment variable. Do not commit secrets or paste them into
prompts. Sessions, reports and checkpoint contents can also be sensitive; local
permissions do not make them encrypted backups.

Extensions and MCP servers run with your permissions. For an untrusted repository
start with `one --no-extensions --no-mcp`; project configuration is not a trust
boundary. `readonly` enforces ONE-mediated restrictions, not OS isolation or
network blocking. See [presets](../library/presets.md),
[checkpoints](../library/checkpoints.md), and the [environment reference](../reference/environment.md).
