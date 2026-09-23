# Done

This file is the concise record of delivered work. It replaces completed roadmap
items and the former detailed implementation diary. Release-by-release
user-facing changes belong in [`CHANGELOG.md`](CHANGELOG.md).

## Recent reliability and context-management work

- Added durable, session-scoped tool/MCP evidence sidecars. Normal provider
  context keeps a bounded preview, while `evidence_read` retrieves sanitized,
  chunked full results by evidence ID after reload without rerunning a tool.
  Storage is append-only with record/session limits and atomic initial writes.
- Added a long-session RPC profiler, including an opt-in large-tool-output
  workload that reports context, pruning, persistence, and timing metrics.
- Added selective provider-context pruning for oversized tool results, then
  changed the default to preserve full tool and MCP evidence after analytical
  failures showed that durable JSONL history is not accessible to the model.
  Pruning remains an explicit opt-in; its copy-on-write provider view never
  mutates persisted history.
- Changed defaults for new configurations: hacker TUI theme, 100 tool steps,
  hidden inline bash output, and fail-fast retry (`maxRetries: 0`).
- Reworked TUI stream delivery into an ordered, coalesced text/thinking event
  accumulator with lifecycle barriers, coalesced UI wakeups, trim-safe state,
  and lifecycle-only sidebar refreshes. Added pilot regressions for silent
  waits, high-rate reasoning, queued input, retries, and concurrent draining.
- Updated llama.cpp documentation for persistent per-model endpoints, context
  windows, tool parsers, and remote-server verification.

## Test-suite organization

- Split the former monolithic TUI and agent tool-calling test modules into
  behavior-focused modules with explicit helpers under `tests/support/`.
  Preserved all 195 original test nodes, snapshot/event/timeout/steering
  coverage, and focused pytest selection without production-code changes.

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

## Reasoning-only tool recovery (0.1.37)

- Commit d1d624d: classified provider responses with no tool call as reasoning-only
  (not tool calls with zero args), added one bounded first-response nudge for
  llama.cpp/Qwen to prevent infinite reasoning loops, and repaired streaming
  non-streaming regressions in post-tool tool-result replay and reasoning-only
  message handling. Verification: 1435 passed, 1 skipped; ruff clean.

## Historical verification at the release-preparation checkpoint

- `794 passed` — full pytest suite.
- `ruff check .` — passed.
- `python -m build` and `twine check dist/*` — passed.
- Fresh wheel installation verified `one --version`, `one --help`,
  `python -m one --help`, and `import one`.
