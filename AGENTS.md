# Agent instructions

## Project and workflow

- Python 3.12+ autonomous terminal agent; distribution is `one-agent`, import and
  console command are `one`.
- `main` is the integration branch. Create a feature branch for changes. Do not
  commit, push, merge, tag, or create a PR unless explicitly requested.
- Read `TODO.md` for open work, `DONE.md` for delivered work, and
  `CONTRIBUTING.md` for contributor/release detail before substantial changes.

## Commands

- Setup: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`.
- Full verification, in order: `.venv/bin/python -m pytest -q`,
  `.venv/bin/ruff check .`, then `git diff --check`.
- Focus one test: `.venv/bin/python -m pytest tests/test_file.py::test_name`.
- TUI snapshots: `ONE_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest -q
  tests/test_tui_snapshots.py`; review snapshot diffs before committing.
- Package verification: `.venv/bin/pip install -e '.[release]' &&
  .venv/bin/python -m build && .venv/bin/twine check dist/*`.
- CLI: `.venv/bin/one ...`, `python -m one ...`, or
  `python -m one.cli.main ...`.
- TUI sessions use `/sessions`, `/sessions <number|full exact name>`,
  `/sessions rename ...`, and `/sessions delete ...`; preserve exact-name
  matching, confirmation, session sidecars, and stale-listener protection.

## Safety and state

- Runtime state is under `~/.config/one`; set `ONE_CODING_AGENT_DIR` to a
  temporary directory for tests and manual runs.
- Never read, copy, or commit `.one/` or real state files: they may contain
  credentials, auth tokens, and session data.
- Extensions and MCP servers execute with agent permissions and are not
  sandboxed. Use `one --no-extensions --no-mcp` for untrusted repositories.
- Auth precedence is runtime input, stored `auth.json`, then provider
  environment variable. `llama.cpp` and `ollama` need no API key.
- MCP tests must use stub managers/clients; never contact real MCP servers or
  provider APIs from tests.

## Architecture

- `one/cli/main.py` wires settings, auth, models, resources, sessions, tools,
  and dispatches TUI, text, JSON, RPC, and `one run` modes.
- `one/core/agent_session.py` owns the synchronous event-emitting agent loop;
  `one/core/session_manager.py` owns JSONL sessions and evidence sidecars.
- `one/core/agent_session_runtime.py` owns runtime session creation, switching,
  forking, and model restoration.
- `one/providers/` contains OpenAI-compatible, Anthropic, Gemini, and Codex
  Responses adapters; llama.cpp uses the OpenAI-compatible adapter.
- `one/tools/index.py` is the tool registration source of truth. A new tool
  also needs its schema/resource, dispatch path, and tests.
- Normal `toolResult` messages are bounded provider-context previews. Complete
  sanitized tool/MCP results live in session evidence sidecars and are retrieved
  with `evidence_read`; optional preview pruning is configured under
  `toolOutputPruning`.
- Compaction retains a rolling summary plus recent messages; preserve its event
  contract when changing context handling.
- Extension hook failures emit `extension_load_error` rather than crashing the
  session; contract details are in `docs/EXTENSIONS.md`.

## Testing gotchas

- Async tests require explicit `@pytest.mark.asyncio`; pytest has no automatic
  asyncio mode.
- Event tests assert exact sequences/payloads, especially
  `tests/test_event_snapshots.py` and `tests/test_agent_*.py`; new events must
  be additive unless the contract is intentionally changed.
- TUI stream mutations must end in `_render_stream()`. `_stream_lines` is capped
  at 500 entries and absolute indexes must be rebased after trimming.
- TUI paste has separate keyboard and terminal-paste paths; test with Textual
  `pilot.press`/`Paste`, not direct action calls.
- Mock display, clipboard, X11, and Wayland probes; never assert ambient host
  state. Follow `tests/test_clipboard_image.py`.
- CLI subprocess tests need `PYTHONPATH` pointing at the repo and a scratch
  `ONE_CODING_AGENT_DIR`.
- CLI exit codes are contractual: 0 success, 1 failure/not found, 2 usage or
  validation error.
- POSIX shell/process assumptions are not Windows support. Windows is currently
  best-effort with no Windows CI; Windows 11 compatibility is future work and
  Windows 10 is deferred.

## Releases and user-visible changes

- `one/config.py:VERSION` is the single version source and must match any tag.
- User-visible changes require a version bump, an `Unreleased` `CHANGELOG.md`
  entry, and regenerated TUI snapshots because the sidebar displays the version.
- Tests-only, refactor-only, and comment-only changes do not bump the version.
- Never create release tags or publish artifacts unless explicitly requested;
  publishing is currently disabled.
