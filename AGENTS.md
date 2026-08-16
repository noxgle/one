# AGENTS.md

## Project

Python 3.12+ **autonomous terminal agent** (`one`): executes assigned tasks (shell/file/code) headless or via CLI/TUI/RPC, with an **optional cooperation mode** — approval gates for mutating tools (`--cooperation`), mid-task steering/abort, and agent-initiated questions (`ask_user` tool). Started as a re-implementation of the `pi` coding agent but now independent — `pi` parity is explicitly out of scope. Roadmap: `TODO.md` (in Polish); the active implementation plan lives in the root `todo.md` (committed together with the feature work). Package name: `one`, source in `one/`.

## Commands

- Setup: `python3 -m venv .venv && .venv/bin/pip install -e .[dev]` (venv already exists; Python >= 3.12 required)
- Tests: `.venv/bin/python -m pytest -q` (335+ tests, ~90s; `testpaths = tests`, no conftest)
- Single test: `.venv/bin/python -m pytest tests/test_event_snapshots.py::test_event_snapshot_abort_path`
- TUI golden snapshots (`tests/snapshots/tui/*.txt`): regenerate with `ONE_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest -q tests/test_tui_snapshots.py`, then review the diff
- CLI: `.venv/bin/one ...` or `python -m one.cli.main ...` (tests use the module form)
- No linter/typecheck/formatter config exists — `pytest` is the only gate.

## State & config (gotchas)

- Agent state lives in `~/.config/one` (one-time auto-migration from legacy `~/.one/agent`); override with `ONE_CODING_AGENT_DIR=/custom/path`.
- Files: `auth.json`, `models.json`, `settings.json`, `sessions/*.jsonl`.
- `models.json` in the agent dir **overrides** the builtin models in `one/core/model_registry.py` (builtins are the fallback). Per-model `url` / `toolParser` for llama.cpp come from here or env.
- MCP servers are configured in `settings.json` under `mcpServers`: stdio transport = `command`/`args` (child process spawned per session), streamable HTTP = `url`, plus an `enabled` flag; `--no-mcp` disables MCP entirely. Tests must never hit real servers — inject fake managers/stub clients instead.
- `settings.json` `tools.timeoutSec` (default 30) is the per-call tool timeout; the model can override it per call via the bash `timeout` arg. `askUser.timeoutSec` (default 0 = no timeout) governs `ask_user`.
- The repo's `.one/` is gitignored real session data (may contain real auth keys) — never read or commit it. In tests and manual runs always set `ONE_CODING_AGENT_DIR` to a scratch dir so you don't touch the real config.
- Auth precedence: runtime > auth file > env var (`<PROVIDER>_API_KEY`, generic fallback for unknown providers). `llama.cpp` needs no key; base URL from `LLAMA_CPP_BASE_URL` env, `--llama-cpp-url` flag, or per-model `url`.

## Architecture

- `one/cli/main.py` — entrypoint; wires SettingsManager, AuthStorage, ModelRegistry, ResourceLoader, SessionManager, tools, then dispatches to a mode.
- `one/core/` — `agent_session.py` (event-emitting session loop), `agent_session_runtime.py`, `auth_storage.py`, `model_registry.py`, `settings_manager.py` (defaults: `tools.maxSteps`/`timeoutSec`, `mcpServers`), `session_manager.py` (`.jsonl` sessions), `event_bus.py`, `sdk.py`.
- `one/mcp/client.py` — `McpManager`: stdio transport (spawns the server per session) + streamable-HTTP transport; `call_tool` defaults to a 120s timeout.
- `one/modes/` — `run_mode` (headless one-shot), `print_mode` (text/json), `rpc_mode`, `tui_mode` (Textual), `interactive_mode`.
- `one/providers/` — adapters `openai_compatible` / `anthropic` / `gemini`; `registry.py` wires them (llama.cpp = OpenAI-compatible).
- `one/tools/` — 11 tools: read, bash, edit, write, grep, find, ls, finish, plan, ask_user, spawn_subagent. New tools must be registered in `tools/index.py` (`all_tools`, plus `coding_tools`/`read_only_tools` groups).
- `plan` tool semantics: persistent per-task plan stored via the `plan` tool, injected into the system prompt every step (`# Active Plan`), cleared on `finish`, persisted in session jsonl (`customType: "plan"`), survives compaction, and is approval-gated in cooperation mode (default `approvalTools` includes `plan`).
- `one/resources/resource_loader.py` — discovers extensions/skills/prompts/themes/AGENTS files.
- `one/resources/extension_runtime.py` — opencode-style extension hooks contract: a `.py` extension exports `register(ctx) -> hooks` (`tool.execute.before/after`, `chat.message`, `experimental.session.compacting`, `dispose`; any throw in `before` = deny via `tool_approval_rejected`). Auto-bound by `bind_extensions()` in `agent_session_runtime.py`; load/bind/hook errors → `extension_load_error` events, never crash the session. Full contract: `docs/EXTENSIONS.md`.

## Testing quirks

- Async tests need `@pytest.mark.asyncio` (no `asyncio_mode = auto` in pyproject).
- No real API calls: tests inject fake providers via `agent.providers = {"openai": _Provider(...)}` (see `tests/test_event_snapshots.py`); CLI-mode tests use `SessionManager.in_memory()` / `SettingsManager.in_memory()` / `AuthStorage.in_memory()`.
- `test_event_snapshots.py` + `test_tool_calling.py` assert the exact compacted event sequence (`agent_start`, `message_*`, `turn_*`, `tool_*`, `retry_*`, `agent_end` with attempt/ok/reason/willRetry/aborted). Changing event emission or payload keys breaks them — keep the event contract stable.
- TUI sidebar/text changes break the golden snapshots in `tests/snapshots/tui/` — regenerate with `ONE_UPDATE_SNAPSHOTS=1`. The terminal hash in the SVG recording is `zlib.adler32` of the content, so it changes whenever content changes (expected, not flakiness).
- MCP tests use stub managers / recording clients; stubs must match `McpManager.call_tool(name, arguments, timeout)` (e.g. `_StubMcpManager`, `_RecordingClient` in `tests/test_mcp.py`).
- CLI tests (`tests/test_auth_and_cli.py`) run `python -m one.cli.main` as a subprocess with `ONE_CODING_AGENT_DIR` + `PYTHONPATH` set to the repo root — follow that pattern for new CLI tests.
- CLI exit codes: 0 ok, 1 not-found, 2 usage errors (asserted by tests).