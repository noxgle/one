# AGENTS.md

## Project

Python 3.12+ re-implementation of the `pi` coding agent (CLI + JSON-RPC + Textual TUI). The original TypeScript `pi` is the parity reference — `TODO.md` (in Polish) lists the remaining gaps. Package name: `one`, source in `one/`.

## Commands

- Setup: `python3 -m venv .venv && .venv/bin/pip install -e .[dev]` (venv already exists; Python >= 3.12 required)
- Tests: `.venv/bin/python -m pytest -q` (68 tests, ~7s, `testpaths = tests`, no conftest)
- Single test: `.venv/bin/python -m pytest tests/test_event_snapshots.py::test_event_snapshot_abort_path`
- CLI: `.venv/bin/one ...` or `python -m one.cli.main ...` (tests use the module form)
- No linter/typecheck/formatter config exists — `pytest` is the only gate.

## State & config (gotchas)

- Agent state lives in `~/.config/one` (one-time auto-migration from legacy `~/.one/agent`); override with `ONE_CODING_AGENT_DIR=/custom/path`.
- Files: `auth.json`, `models.json`, `settings.json`, `sessions/*.jsonl`.
- `models.json` in the agent dir **overrides** the builtin models in `one/core/model_registry.py` (builtins are the fallback). Per-model `url` / `toolParser` for llama.cpp come from here or env.
- The repo's `.one/` is gitignored real session data (may contain real auth keys) — never read or commit it. In tests and manual runs always set `ONE_CODING_AGENT_DIR` to a scratch dir so you don't touch the real config.
- Auth precedence: runtime > auth file > env var (`<PROVIDER>_API_KEY`, generic fallback for unknown providers). `llama.cpp` needs no key; base URL from `LLAMA_CPP_BASE_URL` env, `--llama-cpp-url` flag, or per-model `url`.

## Architecture

- `one/cli/main.py` — entrypoint; wires SettingsManager, AuthStorage, ModelRegistry, ResourceLoader, SessionManager, tools, then dispatches to a mode.
- `one/core/` — `agent_session.py` (event-emitting session loop), `agent_session_runtime.py`, `auth_storage.py`, `model_registry.py`, `session_manager.py` (`.jsonl` sessions), `event_bus.py`, `sdk.py`.
- `one/modes/` — `print_mode` (text/json), `rpc_mode`, `tui_mode` (Textual), `interactive_mode`.
- `one/providers/` — adapters `openai_compatible` / `anthropic` / `gemini`; `registry.py` wires them (llama.cpp = OpenAI-compatible).
- `one/tools/` — 7 tools (read, bash, edit, write, grep, find, ls). New tools must be registered in `tools/index.py` (`all_tools`, plus `coding_tools`/`read_only_tools` groups).
- `one/resources/resource_loader.py` — discovers extensions/skills/prompts/themes/AGENTS files.

## Testing quirks

- Async tests need `@pytest.mark.asyncio` (no `asyncio_mode = auto` in pyproject).
- No real API calls: tests inject fake providers via `agent.providers = {"openai": _Provider(...)}` (see `tests/test_event_snapshots.py`); CLI-mode tests use `SessionManager.in_memory()` / `SettingsManager.in_memory()` / `AuthStorage.in_memory()`.
- `test_event_snapshots.py` + `test_tool_calling.py` assert the exact compacted event sequence (`agent_start`, `message_*`, `turn_*`, `tool_*`, `retry_*`, `agent_end` with attempt/ok/reason/willRetry/aborted). Changing event emission or payload keys breaks them — keep the event contract stable.
- CLI tests (`tests/test_auth_and_cli.py`) run `python -m one.cli.main` as a subprocess with `ONE_CODING_AGENT_DIR` + `PYTHONPATH` set to the repo root — follow that pattern for new CLI tests.
- CLI exit codes: 0 ok, 1 not-found, 2 usage errors (asserted by tests).
