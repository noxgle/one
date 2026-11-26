---
title: "Contributing to ONE"
---

How to join the project: set up a checkout, run the tests that own your change,
and report the validation you actually performed. The repository
[`CONTRIBUTING.md`](https://github.com/noxgle/one/blob/main/CONTRIBUTING.md) is
the authoritative contributor policy and [`AGENTS.md`](https://github.com/noxgle/one/blob/main/AGENTS.md)
is the working reference for agents editing the code. This page mirrors both;
when they disagree, the repository files win.

AI assistance is welcome. The contributor stays responsible for understanding the
change and supplying real evidence, and should be able to explain every changed
line and answer review questions about it.

## Before starting

1. Search existing [issues](https://github.com/noxgle/one/issues) and
   [pull requests](https://github.com/noxgle/one/pulls), including closed threads,
   and add new evidence to an existing thread when it covers your problem.
2. Read `AGENTS.md` and the documentation for the component you want to change.
   `TODO.md` lists open work; `CHANGELOG.md` lists shipped behavior.
3. Agree on scope in an issue **before** substantial features, refactors,
   architecture changes, event-contract changes, preset or capability-policy
   changes, new defaults, or dependency changes. Agreement on scope does not
   guarantee a merge. Focused bug fixes, test gaps, and documentation corrections
   can go straight to a pull request.
4. One problem per pull request, on a feature branch created from `main`. Update
   that branch after feedback instead of opening a competing attempt.

## Set up a checkout

```bash
git clone https://github.com/noxgle/one.git
cd one
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

`.venv/bin/pip install -e '.[release]'` additionally pulls `build` and `twine`
for packaging checks. No Node.js is required to install or run the Python
application; Node.js 22.23.4+ is only needed to build this documentation portal,
see [Maintain this portal](../maintainers/migration.md).

## Isolate state in every shell

Runtime state defaults to `~/.config/one` and respects `XDG_CONFIG_HOME`. Every
shell that runs tests or the CLI must point it at a scratch directory, and keep
that value for later commands in the same shell:

```bash
export ONE_CODING_AGENT_DIR="$(mktemp -d)"
```

Never point it at your real configuration directory, and never read, copy, or
commit `.one/` or real state files: they may contain credentials, auth tokens,
and session data.

## Testing

`pytest` is the primary gate (`testpaths = ["tests"]`, no conftest) and `ruff` is
the lint gate. CI runs both, plus packaging, Docker, and documentation checks on
every pull request.

### Focused feedback while editing

```bash
scripts/test.sh tools -x
scripts/test.sh tui -k paste
scripts/test.sh --help
```

| Suite | Covers |
| --- | --- |
| `quick` | Fast smoke baseline: settings and SDK. Not a substitute for the full suite. |
| `mcp` | MCP client and shutdown tests |
| `tui` | Textual TUI tests, including golden snapshots |
| `providers` | Provider adapter and payload tests |
| `tools` | Tool behavior, approval, budget, image tools |
| `auth` | Authentication, credentials, OAuth, config paths |
| `extensions` | Extension runtime/UI, resources, skills |
| `cli` | Headless/run mode, intake, image, cross-platform, diagnostic CLI |
| `rpc` | JSON-RPC mode and snapshots |
| `core` | Agent loop, settings, events, sessions |
| `sessions` | Session manager and session branching |
| `failed`, `last` | Previously failing tests (`pytest --lf`); may run everything with no last-failure cache |
| `all`, `full` | Complete suite, equivalent to `.venv/bin/python -m pytest -q` |

`scripts/test.sh` uses `.venv/bin/python` when available and otherwise falls back
to `python`. Extra arguments are passed straight to pytest.

### The full gate before committing or opening a pull request

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
git diff --check
```

Focused suites are encouraged during local edits; they do not replace the full
command before commit or merge.

### One test, or only the failures

```bash
.venv/bin/python -m pytest tests/test_event_snapshots.py::test_event_snapshot_abort_path
.venv/bin/python -m pytest -q --lf
```

### Writing a test

- Async tests need an explicit `@pytest.mark.asyncio`; there is no automatic
  asyncio mode.
- Tests never contact real provider APIs or real MCP servers. Inject fake
  providers, for example `agent.providers = {"openai": _Provider(...)}`, and stub
  MCP managers and clients.
- Prefer in-memory state: `SettingsManager.in_memory()`, `AuthStorage.in_memory()`,
  and `SessionManager.in_memory()`.
- CLI subprocess tests need `PYTHONPATH` pointing at the repository and a scratch
  `ONE_CODING_AGENT_DIR`.
- Markers are `smoke` for the fast local baseline, `snapshot` for golden-rendering
  tests, and `slow` only for deliberately long-running tests.
- Event tests assert exact sequences and payloads. New events must be additive
  unless the contract is intentionally changed. See
  [architecture](../library/architecture.md).
- Preset and capability-policy changes need the preset suites plus the relevant
  CLI/TUI tests; run budget changes need the `tests/test_run_*.py` modules. The
  focused helper suites are fixed lists, so run a new test module explicitly when
  the helper does not include it.

### Golden snapshots

TUI and RPC snapshots live in `tests/snapshots/`. Regenerate only when layout or
content changes are expected, and review the diff before committing:

```bash
ONE_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest -q tests/test_tui_snapshots.py
```

### Packaging, Docker, and the portal

Run these when a change touches packaging, dependencies, entry points, the
container image, installed behavior, or documentation:

```bash
.venv/bin/pip install -e '.[release]'
.venv/bin/python -m build
.venv/bin/twine check dist/*
scripts/test-docker.sh
```

See the [release checklist](../library/release_checklist.md) for the full
pre-publication sequence. Generated inventories must be regenerated from the
repository root, never hand-edited:

```bash
export ONE_CODING_AGENT_DIR="$(mktemp -d)"
.venv/bin/python scripts/generate_docs_reference.py
```

Review the generated diff and commit it alongside the source change; CI rejects
drift in `website/src/content/docs/reference`.

## Report the validation you performed

Lead a pull request with the problem and the resulting behavior, then the
approach and the honest validation. List exact commands, the revision and
environment tested, and the observed outcomes, keeping **passed**, **failed**, and
**not run** separate. A command you suggest running is not evidence that it
passed, and CI on a different revision is not validation of your submitted code.
If you cannot run a required environment, say so under **Not run** and keep the
pull request as a draft while asking for targeted help.

Title every issue and pull request with its area in square brackets, for example
`[TUI]`, `[CLI]`, `[Providers]`, `[Tools]`, `[Presets]`, `[Docs]`, `[Packaging]`.

## Contracts that need extra care

- **Event contract** — `tool_call_start`/`tool_call_end`, `turn_*`, and snapshot
  events are stable; new events are additive.
- **TUI runtime boundary** — live session access stays on its owner loop and the
  UI talks through the facade and detached JSON DTOs. See
  [developer workflow](../guides/developer-workflow.md).
- **Presets and capability policy** — see [presets](../library/presets.md).
  Readonly is app-enforced policy, not an OS sandbox.
- **Sessions and handoff** — see [sessions and configuration](sessions-and-configuration.md).
- **Runs and budgets** — see [runs](../library/runs.md). `runBudget` is frozen at
  run start and shared with descendants.
- **CLI exit codes** — `0` success, `1` failure or not found, `2` usage or
  validation error. See [CLI reference](../reference/cli.md).
- **Versioning** — `one/config.py:VERSION` is the single version source.
  User-visible changes need a version bump, an `Unreleased` entry in
  `CHANGELOG.md`, and regenerated TUI snapshots because the sidebar shows the
  version. Tests-only, refactor-only, and comment-only changes do not bump it.
  Never create release tags or publish artifacts without an explicit request.

## Adding a tool

1. Implement it in `one/tools/<name>.py`.
2. Register it in `one/tools/index.py`.
3. Add its prompt schema in `one/resources/resource_loader.py` (`TOOL_ARG_SCHEMAS`).
4. Add dispatch in `one/core/agent_session.py` (`_execute_tool_by_name`).
5. Add tests, then regenerate the [tool inventory](../reference/tools.md).

Keep the advertised schema and the actual execution consistent: policy is enforced
at execution, not only at tool discovery. See [tools](../reference/tools.md).

## Code conventions

Typed Python 3.12+ with `from __future__ import annotations`. Ruff targets
`py312` with a 120-character line length and the `E`, `F`, `I`, and `UP` rule
groups. Prefer explicit over implicit: no bare `except:`. User-facing errors are
in English, actionable, free of stack traces, and redacted of credentials.
Extension hook failures emit `extension_load_error` rather than crashing the
session, see [extensions](../library/extensions.md).

## Platform support

Linux and macOS are exercised in CI (Python 3.12 and 3.13). Windows is currently
best-effort and is not an officially supported platform yet; shell-oriented
behavior remains experimental pending Windows CI. Windows 11 is the planned
future target, while Windows 10 support is deferred. POSIX shell examples in
these docs do not imply Windows support.
