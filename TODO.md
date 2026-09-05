# TODO — Roadmap for the autonomous agent `one`

The project goal is an **autonomous terminal agent** that executes assigned tasks
(shell / files / code) with an **optional cooperation mode** for human interaction (tool
approval gates, mid-task steering, and eventually agent-initiated questions).

The project originated as a re-implementation of the `pi` agent and now evolves independently —
1:1 parity with `pi` is no longer a goal (see "Out of scope").

## Foundation (completed)

- [x] Interface modes: print (one-shot), interactive, TUI (Textual), RPC (JSON-RPC)
- [x] `.jsonl` sessions with branching/forking (session tree, `parentSession` — foundation for subagents) + compaction
- [x] Model → tool → model loop with `turn_*`, `tool_*`, `retry_*` events and live streaming
- [x] Auto-retry, abort (also mid-request), steer/follow-up queues
- [x] Safety limits (`tools.maxSteps`, `tools.timeoutSec`), `toolResult` payload cap
- [x] Cooperation: approval gates for `bash`/`write`/`edit` (`--cooperation`, `/cooperation`, Ctrl+A)
- [x] Providers: `openai`, `anthropic`, `gemini`, `openrouter`, `ollama-cloud`, `llama.cpp` (local, no key)
- [x] Extensions/skills/prompts/themes (loader + opencode-style hook runtime)
- [x] RPC: sessions, model, commands, event streaming, extension UI, `wait_for_idle`

## P0 — Autonomy

- [x] Headless task mode (`one run "task"`): full loop to `finish`, result contract
      (summary + exit code 0/1), step limits, auto-retry, resume after interruption;
      `--json`/`--answer-file`/`--steer-file`, `reports.jsonl` report in agent dir
- [x] Subagents — task delegation: `spawn_subagent` tool (separate sessions with `parentSession`,
       isolated context, concurrent execution, result merging) registered in
       `tools/index.py`; concurrency limits (`subagents.maxConcurrent`) and nesting
       depth (`subagents.maxDepth`)
- [x] Subagent failure diagnostics: preserve non-empty, sanitized errors and child summaries in
      failed `toolResult` payloads; aggregate parallel child status correctly; validate child
      task/tool input and ensure explicit tool sets can complete with `finish`
- [x] Agent→human escalation: `ask_user` tool — agent pauses the task and asks a question; the response
      is fed back into context; channels: interactive, TUI, RPC (`answer_question`), headless
      (`--answer-file`/canned fallback), timeout + abort
- [x] Cooperation policy in autonomous mode: `--cooperation` gates opt-in per run;
      by default the agent runs without questions

## P1 — Integrations and operations

- [x] Task intake: task from file/spec, `@file`, parametrization
- [x] Local Ollama provider: OpenAI-compatible adapter (default `http://localhost:11434/v1`,
      env `OLLAMA_BASE_URL`), local model support; pattern mirrors `llama.cpp` (no API key)
- [x] `/new` in TUI and interactive: create a new session (RPC `new_session` already exists)
      + replay events; alias `/ns`
- [x] TUI: slash-command autocomplete in the input field based on the `/help` command list
      (suggestions on `Tab` cycling, prefix completion for `/`)
- [x] MCP client: connect external MCP servers as tool sources
      (stdio, own JSON-RPC protocol, no new dependencies); server configuration
      in `settings.json` (`mcpServers`), MCP tools available as session tools
- [x] Budget limits: tokens/time with configuration (`budget.maxTokens`/`budget.maxTimeSec`); steps via `tools.maxSteps`
- [x] Task-end report (log `reports.jsonl` in agent dir) + RPC support (`wait_for_idle` exists; steer in headless via `--steer-file`)
- [x] Wire extension widgets/overlays to the TUI layer (dedicated panel for widgets, panel-overlay for overlays; responses via existing input, reset on /new and /fork)
- [x] TUI rendering snapshot/regression tests (3 deterministic full-screen SVG snapshots: base, widget, overlay; goldens in tests/snapshots/tui)

## P2 — Quality and compatibility

- [x] Headless integration tests (E2E subprocess: CLI → local OpenAI-compatible HTTP → finish → result/exit code 0/1 + reports.jsonl)
- [x] RPC snapshot tests (deterministic JSONL: success and error, goldens in tests/snapshots/rpc)
- [x] Auth precedence + provider fallback tests (runtime > auth file > env, placeholder keys, model selection fallback)
- [x] Cross-platform smoke (portable paths/sanitization + POSIX smoke cwd/quoting/pipeline/prefix/exit code; shell tests skip on Windows)
- [x] OpenAI-compatible providers: xAI (Grok), DeepSeek, Mistral, Groq — registry, env keys, builtin models and tests

## Out of scope

- 1:1 parity with `pi` internals (exact event payloads, golden tests from TypeScript). Subscription OAuth (`/login … subscription`) is **in scope** and shipped as a supported feature.
- npm/git package manager like in `pi` (extensions follow their own contract)
