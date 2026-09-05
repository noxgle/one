# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
pre-1.0 (breaking changes may occur in 0.x releases).

## [0.1.0] — 2026-08-30

### Added

- **Autonomous terminal agent** with 4 modes: headless (`run`), text, JSON-RPC, and Textual TUI.
- **12 tools**: read, bash, edit, write, grep, find, ls, finish, plan, spawn_subagent, ask_user, apply_patch.
- **Cooperation mode** (`--cooperation`, Ctrl+A in TUI) — approval gates for mutating tools.
- **Provider adapters** for OpenAI-compatible, Anthropic, Gemini, and Codex Responses API (ChatGPT subscription).
- **Subscription OAuth login** for Anthropic (Claude Pro/Max) and ChatGPT/Codex (Plus/Pro).
- **MCP server support** — stdio and streamable-HTTP transports; tools exposed live to the agent.
- **Project extensions** (`.one/extensions/*.py`) with hook contract inspired by opencode.
- **Session management** — `.jsonl` format, compaction, branching, navigation.
- **Thinking/reasoning streaming** — raw reasoning deltas rendered chronologically with tool boundaries.
- **Bash timeout semantics** — model-controlled per-command timeout with clear "Command timed out" results.
- **Plan tool** — persistent execution plan injected into the system prompt every step, persisted in session jsonl.
- **apply_patch tool** — unified-diff patches with collision-safe, rollback-aware staging.
- **TUI features** — full-height sidebar, live streaming, themes, MCP panel, thinking visualization, slash command history.
- **SDK** — programmatic agent session creation with `agentDir` isolation.
- **Atomic, private persistence** — `0700`/`0600` file modes, atomic writes via `os.replace`.
- **Complete credential removal** (`/logout`) — removes API keys and OAuth tokens locally.
- **RPC login validation** — validates credentials before storage; consistent with TUI/interactive.

### Changed

- All user-facing text, help, and documentation in English.
- `one run` flag parsing fixed — all value-taking flags retain their values.
- Edit tool robustness — tolerates model-side `path` nesting inside `edits[0]`.

### Security

- Trust boundary documented: extensions and MCP servers are not sandboxed.
- Private LAN IPs, stale claims, and sensitive paths removed from documentation.
- Token redaction in error messages and RPC responses.

## Unreleased

<!-- Add changes here as they are developed -->
