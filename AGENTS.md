# AGENTS.md

## Project

Python 3.12+ **autonomous terminal agent** (`one`): executes assigned tasks (shell/file/code) headless or via CLI/TUI/RPC, with an **optional cooperation mode** — approval gates for mutating tools (`--cooperation`), mid-task steering/abort, and agent-initiated questions (`ask_user` tool). Started as a re-implementation of the `pi` coding agent but now independent — `pi` parity is explicitly out of scope. Open work lives in `TODO.md`; delivered work is summarized in `DONE.md`. Distribution name: `one-agent` (PyPI); import name, console script, and source dir: `one`.

## Commands

- Setup: `python3 -m venv .venv && .venv/bin/pip install -e .[dev]` (venv already exists; Python >= 3.12 required)
- Tests: `.venv/bin/python -m pytest -q` (~1000+ tests; `testpaths = tests`, no conftest)
- Single test: `.venv/bin/python -m pytest tests/test_event_snapshots.py::test_event_snapshot_abort_path`
- TUI golden snapshots (`tests/snapshots/tui/*.txt`): regenerate with `ONE_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest -q tests/test_tui_snapshots.py`, then review the diff
- RPC golden snapshots (`tests/snapshots/rpc/*.jsonl`): same `ONE_UPDATE_SNAPSHOTS=1` flag
- CLI: `.venv/bin/one ...` or `python -m one.cli.main ...` (tests use the module form)
- Lint/format: `ruff` configured (`pyproject.toml`: line-length 120, target py312, `E,F,I,UP`) and enforced as a CI gate (`.github/workflows/ci.yml`); keep both `pytest` and `ruff` green.

## State & config (gotchas)

- Agent state lives in `~/.config/one` (one-time auto-migration from legacy `~/.one/agent`); override with `ONE_CODING_AGENT_DIR=/custom/path`.
- Files: `auth.json`, `models.json`, `settings.json`, `sessions/*.jsonl`.
- `models.json` in the agent dir **overrides** the builtin models in `one/core/model_registry.py` (builtins are the fallback). Per-model `url` / `toolParser` for llama.cpp come from here or env.
- MCP servers are configured in `settings.json` under `mcpServers`: stdio transport = `command`/`args` (child process spawned per session), streamable HTTP = `url`, plus an `enabled` flag; `--no-mcp` disables MCP entirely. Tests must never hit real servers — inject fake managers/stub clients instead.
- `settings.json` `tools.timeoutSec` (default 30) is the per-call tool timeout; the model can override it per call via the bash `timeout` arg. `askUser.timeoutSec` (default 0 = no timeout) governs `ask_user`.
- `settings.json` retry modes are `off`/`on`/`unlimited` (`get_retry_mode`/`set_retry_mode`); `tools.maxSteps: 0` means unlimited tool steps. `set_retry_enabled(True)` preserves `unlimited` — never assume it writes `on`.
- The repo's `.one/` is gitignored real session data (may contain real auth keys) — never read or commit it. In tests and manual runs always set `ONE_CODING_AGENT_DIR` to a scratch dir so you don't touch the real config.
- Auth precedence: runtime > auth file > env var (`<PROVIDER>_API_KEY`, generic fallback for unknown providers). `llama.cpp` needs no key; base URL from `LLAMA_CPP_BASE_URL` env, `--llama-cpp-url` flag, or per-model `url`.
- `/login [status|refresh <provider>|provider [apiKey] [model] [subscription]]` (interactive + TUI) validates the key BEFORE storing (`validate_and_fetch` in `one/core/provider_login.py`): 401/403 → `Authorization failed`, key NOT stored; on success it fetches the provider's model list (`ProviderAdapter.list_models`, `GET {base}/v1/models` for OpenAI-compatible / Gemini models endpoint; Anthropic has no list endpoint → minimal-chat fallback), registers the models in-memory and persists them to `models.json` (`ModelRegistry.register_models`/`persist_models`). NO_AUTH providers (`llama.cpp`/`ollama`) fetch their list without a key.

## Architecture

- `one/cli/main.py` — entrypoint; wires SettingsManager, AuthStorage, ModelRegistry, ResourceLoader, SessionManager, tools, then dispatches to a mode.
- `one/core/` — `agent_session.py` (event-emitting session loop), `agent_session_runtime.py`, `auth_storage.py`, `model_registry.py`, `settings_manager.py` (defaults: `tools.maxSteps`/`timeoutSec`, `mcpServers`), `session_manager.py` (`.jsonl` sessions), `event_bus.py`, `sdk.py`.
- `one/mcp/client.py` — `McpManager`: stdio transport (spawns the server per session) + streamable-HTTP transport; `call_tool` defaults to a 120s timeout.
- `one/modes/` — `run_mode` (headless one-shot), `print_mode` (text/json), `rpc_mode`, `tui_mode` (Textual), `interactive_mode`.
- `one/providers/` — adapters `openai_compatible` / `anthropic` / `gemini` / `codex_responses`; `registry.py` wires them (llama.cpp = OpenAI-compatible). Codex sends images as Responses-API `input_image` parts; chatgpt `gpt-5.6-*` builtins are `input_image=True` and the registry merge never lets persisted `false` downgrade them.
- `one/tools/` — 13 tools: read, bash, edit, write, grep, find, ls, finish, plan, ask_user, spawn_subagent, apply_patch, read_image. New tools must be registered in `tools/index.py` (`all_tools`, plus `coding_tools`/`read_only_tools` groups).
- `plan` tool semantics: persistent per-task plan stored via the `plan` tool, injected into the system prompt every step (`# Active Plan`), cleared on `finish`, persisted in session jsonl (`customType: "plan"`), survives compaction, and is approval-gated in cooperation mode (default `approvalTools` includes `plan`).
- `one/resources/resource_loader.py` — discovers extensions/skills/prompts/themes/AGENTS files.
- `one/resources/extension_runtime.py` — opencode-style extension hooks contract: a `.py` extension exports `register(ctx) -> hooks` (`tool.execute.before/after`, `chat.message`, `experimental.session.compacting`, `dispose`; any throw in `before` = deny via `tool_approval_rejected`). Auto-bound by `bind_extensions()` in `agent_session_runtime.py`; load/bind/hook errors → `extension_load_error` events, never crash the session. Full contract: `docs/EXTENSIONS.md`.

## TUI key/event gotchas (hard-earned — read before touching input/rendering)

- **MRO double-dispatch:** `_OneTextualApp._on_key` calls `await super()._on_key(event)` explicitly, then `MessagePump` invokes `App._on_key` again via MRO — the trailing `event.prevent_default()` suppresses the duplicate. Any new bubbled key handling must account for this or bindings fire twice (backspace/delete/arrows did). Keys consumed at widget level (`shift+enter`, `ctrl+v`) use `event.stop()` + `prevent_default()` + `return` and never reach the app.
- **Render invalidation:** mutating `_stream_lines` in place does NOT refresh the UI — every mutation path must end in `_render_stream()` (directly or via `_trim_stream()`). Missing it freezes the spinner with no test failure unless a widget-level assertion exists.
- **Absolute stream indexes go stale on trim:** `_stream_lines` is capped at 500 entries dropped from the front. `_trim_stream()` rebases `_assistant_live_start_idx` and `_active_tool_block`; any new absolute index into the stream needs the same treatment.
- **Paste has two paths:** `ctrl+v` key (custom `action_paste`) vs terminal bracketed-paste (`events.Paste` → `_CommandTextArea._on_paste` override). Both must stay single-insert; test with real `pilot.press` / `post_message(Paste)`, never direct `action_*` calls.
- TUI shortcuts source of truth is `TUI_SHORTCUTS` + `BINDINGS` in `tui_mode.py` (currently `Ctrl+Z` cooperation, `Ctrl+R` retry-cycle, `Ctrl+Shift+V` paste image). Golden SVG snapshots are viewport-limited — overlay entries below the fold are covered by pilot tests, not goldens.

## Testing quirks

- Async tests need `@pytest.mark.asyncio` (no `asyncio_mode = auto` in pyproject).
- `filterwarnings = error::DeprecationWarning:one.*` — any deprecated-stdlib use inside `one/` fails the suite; fix the call, don't touch the filter.
- No real API calls: tests inject fake providers via `agent.providers = {"openai": _Provider(...)}` (see `tests/test_event_snapshots.py`); CLI-mode tests use `SessionManager.in_memory()` / `SettingsManager.in_memory()` / `AuthStorage.in_memory()`.
- `test_event_snapshots.py` + `test_tool_calling.py` assert the exact compacted event sequence (`agent_start`, `message_*`, `turn_*`, `tool_*`, `retry_*`, `agent_end` with attempt/ok/reason/willRetry/aborted). Changing event emission or payload keys breaks them — keep the event contract stable.
- TUI sidebar/text changes break the golden snapshots in `tests/snapshots/tui/` — regenerate with `ONE_UPDATE_SNAPSHOTS=1`. The terminal hash in the SVG recording is `zlib.adler32` of the content, so it changes whenever content changes (expected, not flakiness).
- MCP tests use stub managers / recording clients; stubs must match `McpManager.call_tool(name, arguments, timeout)` (e.g. `_StubMcpManager`, `_RecordingClient` in `tests/test_mcp.py`).
- CLI tests (`tests/test_auth_and_cli.py`) run `python -m one.cli.main` as a subprocess with `ONE_CODING_AGENT_DIR` + `PYTHONPATH` set to the repo root — follow that pattern for new CLI tests.
- Headless-CI rule: never assert on ambient environment (no `DISPLAY`, no X11/Wayland tools, no clipboard backends). `tests/test_clipboard_image.py` mocks `_is_x11`/`_is_wayland`/`_is_macos` for this reason — do the same for any new env-dependent test.
- CLI exit codes: 0 ok, 1 not-found, 2 usage errors (asserted by tests).

## Git & CI

- Remote `origin` = `github.com/noxgle/one`, branch `main`. CI (`.github/workflows/ci.yml`) runs lint + build + tests (ubuntu/macos × 3.12/3.13) on every push to `main` and on PRs; Dependabot bumps GitHub Actions weekly.
- Tags `vX.Y.Z` must equal `one/config.py:VERSION` (release workflow gates on tag==version); publishing stays disabled until explicitly approved.
- Only commit, amend, push, or create PRs when explicitly requested.

## Versioning (agent bumps VERSION itself)

- After every **user-visible** change (behavior fix/feat, CLI, TUI, providers, user docs), bump `VERSION` in `one/config.py` — no separate request needed:
  - patch (`0.1.x`) — fix with no API/CLI change; minor (`0.x.0`) — new feature/command; major — breaking change, maintainer decision only, never bump alone.
  - Behavior-only work (tests-only, refactors, comments) does NOT bump the version.
- Every bump must include, in the same change:
  1. Version-pinned tests follow `VERSION` — prefer `from one.config import VERSION` over literals (literals in `test_cross_platform_smoke.py` / `test_oauth_production.py` broke the 0.1.1 bump).
  2. Regenerated TUI goldens — the sidebar renders `Version: {VERSION}`, so any bump invalidates `tests/snapshots/tui/*.txt` (`ONE_UPDATE_SNAPSHOTS=1`, review the diff).
  3. A `CHANGELOG.md → Unreleased` entry (Fixed/Added/Changed).
- Never create the `vX.Y.Z` tag yourself — tagging is a maintainer release decision (tag must equal `VERSION` at tag time).
