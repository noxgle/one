# Contributing to one

**AI assistance is welcome. The contributor stays responsible for understanding the change and supplying real evidence.**

These rules apply to issues, pull requests, and the agents that help prepare them. You should be able to explain every changed line and answer review questions about it.

`one` is an alpha (`0.1.x`) Python 3.12+ terminal agent (CLI, TUI, JSON-RPC, headless runs). APIs and on-disk storage may change. Linux is the primary platform; Windows is best-effort with no Windows CI.

## Before starting

1. Search [issues](https://github.com/noxgle/one/issues) and
   [pull requests](https://github.com/noxgle/one/pulls), including closed threads. Add new evidence to an existing
   thread when it covers your problem.
2. Read [AGENTS.md](AGENTS.md) and the documentation for the component you want to change. Canonical docs live in
   [`website/src/content/docs/`](website/src/content/docs/); `docs/*.md` are compatibility pointers. `TODO.md` lists
   open work and `CHANGELOG.md` lists shipped behavior.
3. Get maintainer agreement on scope **before** substantial features, refactors, architecture changes, event-contract
   changes, preset or capability-policy changes, new defaults, or dependency changes. Describe the user problem, the
   proposed behavior, and the tradeoffs in an issue first. Agreement on scope does not guarantee a merge. Focused bug
   fixes, test gaps, and documentation corrections can go straight to a PR.
4. Keep one problem per PR. Update that PR after feedback; do not open competing attempts for the same change.
5. Create a feature branch from `main`. `main` is the integration branch. Do not expect contributors to push, tag, or
   publish releases.

Do not resubmit a declined proposal under a different title or through another agent. If new evidence changes the
situation, bring it to the original discussion and let a maintainer decide whether to reopen it.

## Report a problem people can investigate

Open an [issue](https://github.com/noxgle/one/issues/new) with the smallest thing that reproduces the problem:

- Exact steps or command, plus the expected and actual behavior.
- `one --version` output (or the commit), OS, Python version, provider and model, and the relevant settings keys.
- The exact error and the relevant log or session excerpt, with credentials and private content removed.
- Whether you reproduced it yourself. A finding based only on reading code or a scanner is **unconfirmed static
  analysis**: describe the suspected path without presenting it as an observed failure.

Do not invent reproduction steps, errors, affected models, or severity. If you cannot reproduce something, say what you
inspected and what would verify the hypothesis. Feature requests should explain a concrete user need and the current
workaround. Questions should say what you tried and where you got stuck.

Example structure, with placeholders to replace with actual observations:

```text
Problem: <one observed behavior>
Environment: <one --version or commit, OS, Python, provider + model, relevant settings>
Reproduce: <exact command or short sequence>
Expected: <documented or intended behavior>
Actual: <observed behavior and exact error>
Evidence: <sanitized log or session excerpt>
Reproduction status: <reproduced locally OR unconfirmed static analysis>
```

Never paste API keys, OAuth tokens, provider base URLs with secrets, real session contents, or private absolute paths
into an issue. Redact them and say what you redacted.

## Titles

Every issue and pull request title starts with its area in square brackets, then a short description:

`[Core]` `[TUI]` `[CLI]` `[Providers]` `[Tools]` `[MCP]` `[Sessions]` `[Presets]` `[Runs]` `[Docs]` `[Packaging]` `[Other]`

For example `[TUI] keep stream rendering after a 500-line trim` or `[Providers] derive llama.cpp usage from timings`.

## Set up the repository

```bash
git clone https://github.com/noxgle/one.git
cd one
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

`.venv/bin/pip install -e '.[release]'` additionally pulls `build` and `twine` for packaging checks. No Node.js is
needed to install or run the Python application; Node.js is only needed for the documentation portal.

## Isolate state in every shell

Runtime state defaults to `~/.config/one` (respecting `XDG_CONFIG_HOME`). Every shell that runs tests or the CLI must
point it at a scratch directory, and must keep that value for later commands in the same shell:

```bash
export ONE_CODING_AGENT_DIR="$(mktemp -d)"
```

Never point it at your real configuration directory, never read or copy `.one/` or real state files, and never commit
them: they may contain credentials, auth tokens, and session data.

## Testing

`pytest` is the primary gate (`testpaths = ["tests"]`, no conftest). `ruff` is the lint gate. CI runs both, plus
packaging, Docker, and documentation checks on every PR.

### Focused feedback while editing

```bash
scripts/test.sh tools -x          # tool behavior, approval, budget
scripts/test.sh tui -k paste      # one TUI behavior
scripts/test.sh --help            # list every suite
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

`scripts/test.sh` uses `.venv/bin/python` when available and otherwise falls back to `python`. Extra arguments are
passed straight to pytest.

### The full gate before committing or opening a PR

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
git diff --check
```

Focused suites are encouraged during local edits; they do not replace the full command before commit or merge.

### One test, or only the failures

```bash
.venv/bin/python -m pytest tests/test_event_snapshots.py::test_event_snapshot_abort_path
.venv/bin/python -m pytest -q --lf
```

### Writing a test

- Async tests need an explicit `@pytest.mark.asyncio`; there is no automatic asyncio mode.
- Tests must **never** contact real provider APIs or real MCP servers. Inject fake providers, for example
  `agent.providers = {"openai": _Provider(...)}`, and stub MCP managers/clients.
- Prefer in-memory state: `SettingsManager.in_memory()`, `AuthStorage.in_memory()`, `SessionManager.in_memory()`.
- CLI subprocess tests need `PYTHONPATH` pointing at the repository and a scratch `ONE_CODING_AGENT_DIR`.
- Markers: `smoke` for fast local baseline, `snapshot` for golden-rendering tests, `slow` only for deliberately
  long-running tests — never just because a test sits in a broad suite.
- Event tests assert exact sequences and payloads (`tests/test_event_snapshots.py`, `tests/test_agent_*.py`). New
  events must be additive unless the contract is intentionally changed.
- Runtime-boundary changes need `tests/test_tui_runtime_boundary.py` and `tests/test_agent_runtime_facade.py`.
- Preset and capability-policy changes need `tests/test_presets.py`, `tests/test_preset_runtime.py`,
  `tests/test_preset_sessions.py`, `tests/test_subagent_presets.py`, plus the relevant CLI/TUI tests. The focused
  helper suites are fixed lists: run a new test module explicitly when the helper does not include it.
- Run budget and accounting changes need the relevant `tests/test_run_*.py` modules with fake providers.

### TUI and RPC golden snapshots

Snapshots live in `tests/snapshots/tui/*.txt` and `tests/snapshots/rpc/`. Regenerate only when layout or content
changes are expected:

```bash
ONE_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest -q tests/test_tui_snapshots.py
```

Review the snapshot diff before committing. It should contain only the intended rendering changes.

### Packaging and Docker

Run these when a change touches packaging, dependencies, entry points, the image, or installed behavior:

```bash
.venv/bin/pip install -e '.[release]'
.venv/bin/python -m build
.venv/bin/twine check dist/*

python -m venv /tmp/one-wheel-test
/tmp/one-wheel-test/bin/pip install dist/*.whl
/tmp/one-wheel-test/bin/one --version
/tmp/one-wheel-test/bin/one --help

scripts/test-docker.sh
```

Do not publish artifacts. `dist/` is local output, not something to commit.

### Documentation portal

The Astro + Starlight site is a separate local toolchain, not a Python runtime dependency (Node.js 22.23.4+, the
minimum Astro 7 accepts; npm 10.8.2+). From `website/`:

```bash
npm ci
npm run build && npm run check:links
DOCS_SITE=https://one.noxgle.com DOCS_BASE=/docs/ npm run build
DOCS_BASE=/docs/ npm run check:links
```

Generated inventories (CLI, settings, tools) must be regenerated from the repository root, never hand-edited:

```bash
export ONE_CODING_AGENT_DIR="$(mktemp -d)"
.venv/bin/python scripts/generate_docs_reference.py
```

Review the generated diff and commit it alongside the source change; CI rejects drift.

## Show the validation you actually performed

In the PR body, list exact commands, the revision and environment tested, and the observed outcomes. Keep **passed**,
**failed**, and **not run** separate. A command you suggest running is not evidence that it passed, and CI on a
different revision is not validation of your submitted code.

```text
Problem and result: <trigger; behavior before and after the change>
Related discussion: <issue URL and scope agreement, or "none">
Approach: <why this fix; compatibility effects>
Validation:
  Passed: <exact command, revision/environment, observed result, log>
  Failed: <exact command and failure, or "none observed">
  Not run: <check, reason, and what stays unverified>
Limits: <known limitations or remaining work>
```

If you cannot run a required environment (for example macOS or a specific provider), say so in **Not run** and keep the
PR as a draft while asking for targeted help in the same thread. Do not claim compatibility you have not tested.

## Prepare a focused pull request

Lead with the problem and the resulting behavior. Explain why the approach is needed, the important limits, and any
compatibility effects. Link the issue and the scope agreement when applicable. A file-by-file narration or an
AI-generated summary of the diff is not a substitute for this explanation.

- Keep unrelated formatting, cleanup, dependency changes, and speculative abstractions out of the PR.
- Preserve existing behavior outside the intended change. New features are opt-in unless the issue agreed otherwise.
- Update documentation where it changes what a user or developer needs to know. Avoid repeated summaries, speculative
  roadmaps, and generated documentation with no actionable information.
- Read the complete diff before submitting, including generated files and snapshots. Remove debugging output, secrets,
  private paths, and accidental files.
- Prefer a small relevant regression check over tests that only restate the implementation. Documentation-only changes
  need link, example, and consistency checks; they do not require packaging or Docker runs.

## Rules for AI-assisted work

Use agents to investigate, implement, and explain work that you can review. Before submitting, verify their claims
against the source, actual command output, or a cited primary reference.

Never submit fabricated measurements, invented test results, fake logs, or copied claims you have not checked. Do not
turn a repository scan into a batch of speculative issues or PRs: consolidate related findings, establish their effect,
and submit the useful evidence. Do not present an agent's confidence or another model's approval as validation. Keep
review replies concise and address the requested change or evidence directly.

Maintainers may request a smaller change, return a PR to draft, or close a submission that lacks evidence, duplicates
existing work, or ignores agreed scope. A smaller, understandable contribution with honest limits is easier to review
and maintain.

## Contracts that need extra care

- **Event contract.** `tool_call_start/end`, `turn_*`, and snapshot events are stable. New events are additive and must
  not change existing payloads. If a feature must change events, coordinate in an issue first.
- **TUI runtime boundary.** Live session access stays on its owner loop; the Textual UI talks to it through the facade
  and detached JSON DTOs. Do not bypass the facade in UI code. TUI stream mutations must end in `_render_stream()`;
  `_stream_lines` is capped at 500 entries and absolute indexes must be rebased after trimming.
- **Presets and capability policy.** Preserve reserved `default`/`readonly` IDs, idle-only atomic activation, strict
  schema validation, and rejection of nonempty `sandboxes`. Readonly is app-enforced policy, not an OS sandbox. Policy
  is enforced at execution, not only at tool discovery, and CLI/parent ceilings always intersect requested capabilities.
- **Sessions and handoff.** Preserve last-displayed numbering, fresh exact-ID validation, confirmation, session
  sidecars, stale-listener protection, and failed-candidate cleanup in `one/core/agent_session_runtime.py`.
- **Runs and budgets.** `runBudget` is frozen at run start and shared with descendants; legacy session `budget` and
  local tool limits stay separate. Read `one/core/run_context.py`, `run_usage.py`, `run_pricing.py`, and
  `run_history.py` before changing accounting, admission, cancellation, or inspection.
- **CLI exit codes are contractual:** `0` success, `1` failure or not found, `2` usage or validation error.
- **Versioning.** `one/config.py:VERSION` is the single version source and must match any tag. User-visible changes
  need a version bump, an `Unreleased` entry in `CHANGELOG.md`, and regenerated TUI snapshots because the sidebar shows
  the version. Tests-only, refactor-only, and comment-only changes do not bump the version. Never create release tags
  or publish artifacts without an explicit request.

## Adding a tool

1. Implement it in `one/tools/<name>.py`.
2. Register it in `one/tools/index.py` (`all_tools`, plus any grouping).
3. Add its prompt schema in `one/resources/resource_loader.py` (`TOOL_ARG_SCHEMAS`).
4. Add dispatch in `one/core/agent_session.py` (`_execute_tool_by_name`).
5. Add tests in `tests/`.
6. Regenerate the tool inventory with `scripts/generate_docs_reference.py`.

Keep the advertised schema and the actual execution consistent: policy is enforced at execution, not only at tool
discovery.

## Code conventions

- Typed Python 3.12+ with `from __future__ import annotations`.
- Ruff targets `py312`, line length 120, rules `E`, `F`, `I`, `UP` (exceptions in `pyproject.toml`).
- Prefer explicit over implicit: no bare `except:`, use typed `except Exception:`.
- User-facing errors in English, actionable, no stack traces in user-facing output, credentials redacted in error
  messages and RPC responses.
- Extension hook failures emit `extension_load_error` rather than crashing the session.
- POSIX shell and process assumptions are not Windows support.

## Submitting changes: checklist

1. Feature branch from `main`, one problem per PR.
2. Tests for the new behavior, in the suite that owns the component.
3. `.venv/bin/python -m pytest -q`, `.venv/bin/ruff check .`, `git diff --check` all pass locally.
4. Snapshots, generated inventories, `CHANGELOG.md`, and a version bump when the change is user-visible.
5. No private data (keys, tokens, IPs, absolute personal paths, session contents) in the diff.
6. PR title prefixed with its area; body states problem, result, approach, and honest validation.

## Questions?

Open an [issue](https://github.com/noxgle/one/issues) or ask in an existing thread. State what you tried, what you
expected, and where you got stuck.
