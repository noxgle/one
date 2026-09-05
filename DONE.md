# Done

This file is the concise record of delivered work. It replaces completed roadmap
items and the former detailed implementation diary. Release-by-release
user-facing changes belong in [`CHANGELOG.md`](CHANGELOG.md).

## Foundation

- Delivered four interfaces: one-shot text/JSON output, interactive REPL,
  Textual TUI, and JSON-RPC.
- Implemented the model → tool → model loop with streaming, retries, abort,
  steering/follow-up queues, session compaction, branching, and JSONL storage.
- Added safety limits for steps, tools, time, tokens, and result payloads.
- Added optional cooperation mode with approval gates for mutating tools.
- Implemented provider adapters for OpenAI-compatible APIs, Anthropic, Gemini,
  ChatGPT/Codex, local llama.cpp, and local Ollama.

## Autonomous execution and integrations

- Shipped `one run "<task>"`, including JSON output, resumption, task files,
  template parameters, headless `ask_user`, steering files, reports, and
  exit-status contracts.
- Added subagents with isolated child sessions, concurrency/depth limits,
  propagated cooperation approvals, and actionable aggregated failures.
- Added the `ask_user`, `plan`, and `apply_patch` tools.
- Added MCP support for stdio and streamable HTTP servers, live server controls,
  sidebar status, and per-call timeout handling.
- Added project extensions, skills, prompt templates, themes, extension hooks,
  and TUI extension widgets/overlays.
- Added budget limits, task intake, model/provider selection, `/providers`,
  `/login refresh`, command history, TUI autocomplete, and configurable default
  mode.

## Tooling and TUI reliability

- Implemented model-controlled bash timeouts with a settings default and clear
  timeout-versus-cancellation results; MCP calls retain their own default timeout.
- Made `apply_patch` collision-safe and rollback-aware: preflight validation,
  symlink/hardlink rejection, same-filesystem staging, backups, and recovery
  diagnostics.
- Hardened `edit` against the common model-generated shape with `path` nested
  inside `edits[0]`.
- Preserved chronological thinking blocks around tool calls and fixed provider
  reasoning whitespace handling.
- Fixed TUI follow-up queueing, waiting-spinner lifecycle, sidebar height,
  markup rendering, plan rendering, and safe rendering during shutdown.

## Authentication, persistence, and SDK

- Added validated provider login: credentials are checked before storage and
  model metadata is fetched and persisted where the provider supports it.
- Added supported subscription OAuth login for Anthropic and ChatGPT/Codex,
  OAuth refresh, model discovery, and complete local logout.
- Added atomic private persistence for configuration, credentials, models,
  sessions, and reports (`0700` directories / `0600` sensitive files on POSIX).
- Added state isolation for SDK `agentDir` usage and reliable config migration
  from legacy paths.
- Added credential redaction in errors and RPC responses.

## Quality, compatibility, and release preparation

- Added headless end-to-end tests, RPC snapshots, TUI golden snapshots,
  cross-platform smoke tests, provider payload tests, and authentication/
  persistence regressions.
- Added 794 automated tests and a Ruff lint gate.
- Added MIT license, changelog, contributing guide, security policy, code of
  conduct, Dependabot, CI, and a tag-triggered release-artifact workflow.
- Added package metadata, `py.typed`, `python -m one` support, sdist manifest,
  wheel/sdist metadata checks, and fresh-virtualenv wheel smoke tests.
- Reworked public documentation for installation, provider configuration,
  headless execution, configuration paths, security boundaries, platform
  support, upgrades, uninstallation, MCP, and extensions.
- Removed personal filesystem paths and private LAN examples from tracked
  documentation and test fixtures.

## Verification at the latest release-preparation checkpoint

- `794 passed` — full pytest suite.
- `ruff check .` — passed.
- `python -m build` and `twine check dist/*` — passed.
- Fresh wheel installation verified `one --version`, `one --help`,
  `python -m one --help`, and `import one`.
