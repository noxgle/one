# Agent instructions

## Project

- Python 3.12+ autonomous terminal agent; source/import/CLI name is `one`, distribution is `one-agent`.
- `main` is the integration branch. Create a feature branch for changes; do not commit, push, merge, or tag unless requested.
- Read `TODO.md` for open work, `DONE.md` for delivered work, and `CONTRIBUTING.md` for contributor detail.

## Commands

- Setup: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`.
- Full verification: `.venv/bin/python -m pytest -q` then `.venv/bin/ruff check .` and `git diff --check`.
- Focused test: `.venv/bin/python -m pytest tests/test_file.py::test_name`.
- TUI snapshots: `ONE_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest -q tests/test_tui_snapshots.py`; review the diff before committing.
- Build checks: `.venv/bin/pip install -e '.[release]' && .venv/bin/python -m build && .venv/bin/twine check dist/*`.
- CLI entrypoints: `.venv/bin/one ...` or `python -m one.cli.main ...`.

## State and safety

- Runtime state is under `~/.config/one`; set `ONE_CODING_AGENT_DIR` to a scratch directory for tests/manual runs.
- Never read, copy, or commit `.one/` or real state files: they can contain auth keys and session data.
- Extensions and MCP servers execute with the agent's permissions and are not sandboxed; use `one --no-extensions --no-mcp` for untrusted repositories.
- Auth precedence is runtime > stored `auth.json` > provider environment variable. `llama.cpp`/`ollama` need no key.
- MCP tests must use stub managers/clients; never contact real MCP servers or provider APIs from tests.

## Architecture

- `one/cli/main.py` wires settings, auth, models, resources, sessions, tools, and dispatches to headless, text, TUI, or RPC modes.
- `one/core/agent_session.py` owns the event-emitting synchronous agent loop; `session_manager.py` persists session JSONL.
- `one/providers/` contains OpenAI-compatible, Anthropic, Gemini, and Codex Responses adapters; llama.cpp uses the OpenAI-compatible adapter.
- `one/tools/index.py` is the registration source of truth; adding a tool also requires its schema/resource and dispatch path.
- Active provider context is intentionally different from durable evidence: normal
  `toolResult` messages are bounded previews, while sanitized complete tool/MCP
  results are stored in a session sidecar and retrieved by `evidence_read` when
  needed. Optional stale-preview pruning is configured under `toolOutputPruning`.
- Compaction retains a rolling summary plus recent messages; preserve its event contract when changing context handling.
- `one/resources/extension_runtime.py` implements extension hooks; failures emit `extension_load_error` rather than crashing the session. Contract details are in `docs/EXTENSIONS.md`.

## Testing gotchas

- Async tests require explicit `@pytest.mark.asyncio`; pytest has no auto asyncio mode.
- Event tests assert exact sequences and payloads, especially
  `test_event_snapshots.py` and `test_agent_*.py`; new events should be additive.
- TUI stream mutations must end in `_render_stream()`; `_stream_lines` is capped at 500 entries and absolute indexes must be rebased after trimming.
- TUI paste has separate keyboard and terminal-paste paths; test with Textual `pilot.press`/`Paste`, not direct action calls.
- Do not assert ambient display/clipboard/X11/Wayland state; mock platform probes as `tests/test_clipboard_image.py` does.
- CLI tests run subprocesses with `PYTHONPATH` pointing at the repo and a scratch `ONE_CODING_AGENT_DIR`.
- CLI exit codes are contractual: 0 success, 1 not found/failure, 2 usage/validation error.

## Releases

- `one/config.py:VERSION` is the single version source and must match any `vX.Y.Z` tag.
- User-visible changes require a version bump, an `Unreleased` `CHANGELOG.md` entry, and regenerated TUI snapshots because the sidebar displays the version.
- Tests-only, refactor-only, and comment-only changes do not bump the version. Never create release tags; publishing is currently disabled.
