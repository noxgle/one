# Contributing to one

Thank you for your interest in contributing to `one`!

## Quick start

```bash
# Clone and set up
git clone https://github.com/noxgle/one.git
cd one
python3 -m venv .venv
.venv/bin/pip install -e .[dev]

# Run the full test suite (~1230 tests, ~3 minutes)
.venv/bin/python -m pytest -q

# Lint (ruff)
ruff check .

# Single test
.venv/bin/python -m pytest tests/test_event_snapshots.py::test_event_snapshot_abort_path
```

## Architecture overview

- **Package**: `one`, source in `one/`.
- **Entry point**: `one/cli/main.py` — wires settings, auth, models, tools,
  and dispatches to a mode (headless, TUI, interactive, RPC).
- **Core**: `one/core/` — agent session loop, auth storage, model registry,
  settings manager, session management, event bus, SDK.
- **Modes**: `one/modes/` — `run_mode` (headless), `print_mode`, `rpc_mode`,
  `tui_mode` (Textual), `interactive_mode`.
- **Providers**: `one/providers/` — adapters for OpenAI-compatible, Anthropic,
  Gemini, and Codex Responses API.
- **Tools**: `one/tools/` — 14 tools registered in `tools/index.py`.
- **MCP**: `one/mcp/client.py` — stdio and streamable-HTTP transport.
- **Resources**: `one/resources/` — extension/runtime, skills, prompts, themes.

## Testing

- `pytest` is the primary gate (`testpaths = ["tests"]`, no conftest); `ruff` is
  an additional CI lint gate (`pyproject.toml` `[tool.ruff]`).
- Tests live in `tests/` with `testpaths = ["tests"]` (no conftest).
- Async tests need `@pytest.mark.asyncio` (no `asyncio_mode = auto` in pyproject).
- Tests must **never** hit real API servers — inject fake providers via
  `agent.providers = {"openai": _Provider(...)}`.
- Tests use `SettingsManager.in_memory()` / `AuthStorage.in_memory()` and
  `SessionManager.in_memory()` where appropriate.
- All tests must use a scratch `ONE_CODING_AGENT_DIR` to avoid touching real config.

### Build and packaging checks

CI also verifies packaging on every PR:

```bash
python -m build
twine check dist/*
# fresh-venv smoke test
python -m venv /tmp/one-wheel-test
/tmp/one-wheel-test/bin/pip install dist/*.whl
/tmp/one-wheel-test/bin/one --version
/tmp/one-wheel-test/bin/one --help
```

## TUI golden snapshots

TUI snapshot tests live in `tests/snapshots/tui/*.txt`. Regenerate when
sidebar or stream changes:

```bash
ONE_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest -q tests/test_tui_snapshots.py
```

Review the regenerated diff before committing — it should only contain
expected layout/content changes.

## Event contract

The agent event contract (`tool_call_start/end`, `turn_*`, snapshots in
`test_event_snapshots.py`) is stable. When adding features:

1. New events are **additive** — they must not change existing event payloads.
2. Existing event-contract tests assert exact sequences and payloads.
3. If a new feature must change events, coordinate with the test suite first.

## Code conventions

- Python 3.12+ typed code with `from __future__ import annotations`.
- Prefer explicit over implicit: no bare `except:`, use typed `except Exception:`.
- Error messages in English, actionable, no stack traces in user-facing output.
- Sensitive data (API keys, OAuth tokens) must be redacted in error messages
  and RPC responses.

## Versioning

- The single version source is `one/config.py:VERSION` (`0.1.0`). It drives both
  `pyproject.toml` (`tool.setuptools.dynamic.version.attr = "one.config.VERSION"`)
  and `one --version` / `one/mcp/client.py` client identity.
- Bump `VERSION` before tagging `v*` (see `.github/workflows/release.yml`).
- Optional extras: `pip install -e .[dev]` for development, `pip install -e .[release]`
  additionally pulls `build` + `twine` for packaging.

## Adding a tool

1. Implement the tool in `one/tools/<name>.py`.
2. Register in `one/tools/index.py` (`all_tools`, plus any grouping).
3. Add schema in `one/resources/resource_loader.py` (`TOOL_ARG_SCHEMAS`).
4. Add dispatch in `one/core/agent_session.py` (`_execute_tool_by_name`).
5. Add tests in `tests/`.

## Submitting changes

1. Create a feature branch from `main`.
2. Write tests for new behavior.
3. Run the full suite: `.venv/bin/python -m pytest -q` and `ruff check .`.
4. Ensure no private data (IPs, paths, keys) in commits.
5. Open a pull request with a clear description.

## Questions?

Open an issue or start a discussion on GitHub.
