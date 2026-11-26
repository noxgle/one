# Agent instructions

## Project and workflow

- Python 3.12+ autonomous terminal agent; distribution is `one-agent`, import and
  console command are `one`.
- `main` is the integration branch. Create a feature branch for changes. Do not
  commit, push, merge, tag, or create a PR unless explicitly requested.
- If present, read `TODO.md` for current open work. Before substantial changes,
  also read `CHANGELOG.md` for user-facing changes, Git history for implementation
  details, and `CONTRIBUTING.md` for contributor/release guidance.

## Commands

- Run commands from the repository root with Python 3.12+.
- Setup: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`.
- Before tests or manual CLI runs, isolate state in the current shell with
  `export ONE_CODING_AGENT_DIR="$(mktemp -d)"`; preserve that environment for
  subsequent commands (each new shell needs its own export).
- Focused suites: `scripts/test.sh tools -x`, `scripts/test.sh tui -k paste`,
  or `scripts/test.sh --help` for all suites. These use `.venv/bin/python`
  when available. `failed`/`last` uses `--lf` and can run everything without
  a last-failure cache; `quick` is not a substitute for full verification.
- Full verification, in order: `.venv/bin/python -m pytest -q`,
  `.venv/bin/ruff check .`, then `git diff --check`.
- Focus one test: `.venv/bin/python -m pytest
  tests/test_event_snapshots.py::test_event_snapshot_abort_path`.
- TUI snapshots: `ONE_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest -q
  tests/test_tui_snapshots.py`; review snapshot diffs before committing.
- Package verification: `.venv/bin/pip install -e '.[release]' &&
  .venv/bin/python -m build && .venv/bin/twine check dist/*`.
- CLI: `.venv/bin/one ...`, `python -m one ...`, or
  `python -m one.cli.main ...`.
- TUI sessions use `/sessions`, `/sessions <number|full ID>`,
  `/sessions rename ...`, and `/sessions delete ...`; titles are descriptive only.
  Preserve last-displayed numbering, fresh exact-ID validation, confirmation,
  session sidecars, and stale-listener protection.
- `/handoff <goal> | <brief>` creates an isolated session with user-authored
  context and the effective preset snapshot, not copied history/evidence/native
  state. Preserve idle/source checks and failed-candidate cleanup in
  `one/core/agent_session_runtime.py`.

## Safety and state

- Runtime state defaults to `~/.config/one`, respects `XDG_CONFIG_HOME`, and
  can be overridden with `ONE_CODING_AGENT_DIR`; use a temporary directory
  for tests and manual runs.
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
- `one/core/agent_session.py` owns the async event-emitting agent loop;
  `one/core/session_manager.py` owns JSONL sessions and evidence sidecars.
- `one/core/agent_session_runtime.py` owns runtime session creation, switching,
  forking, and model restoration.
- `one/modes/` contains TUI, interactive, print, headless run, and RPC modes.
  Explicit TUI mode uses `one/core/threaded_agent_runtime.py`,
  `agent_runtime_facade.py`, and `tui_runtime_host.py`: keep live session
  access on its owner loop and pass detached JSON DTOs to the Textual UI.
- `one/providers/` contains OpenAI-compatible, Anthropic, Gemini, and Codex
  Responses adapters; llama.cpp uses the OpenAI-compatible adapter.
- `one/tools/index.py` is the tool registration source of truth. A new tool
  also needs its prompt schema in `one/resources/resource_loader.py`
  (`TOOL_ARG_SCHEMAS`), dispatch in `AgentSession._execute_tool_by_name`,
  and tests. Keep advertised schemas and actual execution consistent.
- `one/mcp/client.py` handles MCP transports; `one/resources/` holds resource
  loading, extension runtime, default skills, and packaged presets.
- Normal `toolResult` messages are bounded provider-context previews. Complete
  sanitized tool/MCP results live in session evidence sidecars and are retrieved
  with `evidence_read`; optional preview pruning is configured under
  `toolOutputPruning`.
- Compaction retains a rolling summary plus recent messages; preserve its event
  contract when changing context handling.
- Runs and descendants share an opt-in `runBudget`, frozen at run start; legacy
  session `budget` and local tool limits remain separate. Manual compaction is
  a separate run. See `website/src/content/docs/library/runs.md` and `one/core/run_context.py`,
  `run_usage.py`, `run_pricing.py`, and `run_history.py` before changing
  accounting, admission, cancellation, or inspection. Test the relevant
  `tests/test_run_*.py` modules with fake providers.
- `tools.maxSteps` defaults to 100; 0 means unlimited model-loop steps. A step
  can contain multiple tool calls, and other budgets still apply. Each attempt captures
  its limit at the start. TUI Info displays the effective limit and Ctrl+U in
  the command input toggles it, persisting global settings while project
  overrides retain precedence. See `tests/test_tui_commands.py` and
  `tests/test_tui_runtime_boundary.py` for this behavior.
- Extension hook failures emit `extension_load_error` rather than crashing the
  session; contract details are in `website/src/content/docs/library/extensions.md`.

## Presets and capability policy

- `one/core/presets.py` strictly loads Markdown with YAML frontmatter;
  `one/core/tool_policy.py` defines capability ceilings. See
  `website/src/content/docs/library/presets.md`
  and `one/resources/presets/` before changing preset behavior.
- Preserve reserved `default`/`readonly` IDs, idle-only atomic activation,
  strict schema validation, and rejection of nonempty `sandboxes`. Readonly
  is app-enforced policy, not an OS sandbox; already-executed extensions are
  not confined by switching presets.
- Enforce policy at execution, not just tool discovery. CLI and parent ceilings
  always intersect requested capabilities, including child presets and MCP
  refreshes. Readonly also blocks app-mediated manual shell execution and
  extension hooks, but allows internal session persistence.
- Resume/fork restores saved effective preset snapshots, not changed preset
  files. Explicit reapplication adopts file edits; manual overrides retain
  preset identity and modified-state display.
- Cover changes with `tests/test_presets.py`, `tests/test_preset_runtime.py`,
  `tests/test_preset_sessions.py`, `tests/test_subagent_presets.py`, and the
  relevant CLI/TUI tests. Focused helper suites are fixed lists: run new test
  modules explicitly when the helper does not include them.

## Code conventions

- Follow typed Python 3.12+ code and `from __future__ import annotations`.
  Ruff targets `py312`, with line length 120 and `E`, `F`, `I`, `UP` rules
  (exceptions are in `pyproject.toml`).
- Keep user-facing errors actionable and in English; redact credentials in
  diagnostics and RPC output. Prefer in-memory settings/auth/session managers
  and fake providers in tests.

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
- Runtime-boundary changes need `tests/test_tui_runtime_boundary.py` and
  `tests/test_agent_runtime_facade.py`; do not bypass the facade in UI code.
- POSIX shell/process assumptions are not Windows support. Windows is currently
  best-effort with no Windows CI; Windows 11 compatibility is future work and
  Windows 10 is deferred.

## Documentation portal and landing

- Canonical English documentation is in `website/src/content/docs/`: `guides/`
  for tasks, `handbook/` for usage, `library/` for in-depth material, `reference/`
  for inventories, and `maintainers/` for deployment and audit status. `docs/*.md`
  are compatibility pointers; do not restore parallel article copies there.
- Read `website/src/content/docs/maintainers/migration.md` and the implementation
  status near the end of `TODO.md`. Migrated material and generated inventories
  are not a completed semantic audit; keep explicit coverage gaps truthful.
- Astro + Starlight is a separate local toolchain, not a Python runtime dependency.
  Use Node.js 22.23.4+ (the minimum Astro 7 accepts) and npm 10.8.2+. From `website/`: `npm ci`, `npm run dev`,
  `npm run build`, `npm run check:links`, and `npm run preview`. Production search
  is indexed during build; verify it in preview, not just dev.
- For production-path checks, from `website/` run
  `DOCS_SITE=https://one.noxgle.com DOCS_BASE=/docs/ npm run build`, then
  `DOCS_BASE=/docs/ npm run check:links`. Repeat the base for preview. The docs CI
  also checks root hosting and `/one/`; it builds/checks only, never deploys.
- New article links should use relative Markdown paths. `website/astro.config.mjs`
  rewrites these into base-aware portal URLs. The built-link checker validates
  internal URLs, fragments and assets, not external links or browser behavior.
- Regenerate CLI/settings/tool inventories from the repo root with isolated state:
  `export ONE_CODING_AGENT_DIR="$(mktemp -d)"` then
  `.venv/bin/python scripts/generate_docs_reference.py`. Commit reviewed generated
  Markdown alongside source changes; CI rejects drift. Change the generator rather
  than hand-editing its output. Environment reference is maintained manually.
- `landing_page/` is the separate static product page. Its generation/update brief
  is `landing_page/prompt.md`. Documentation links use
  `https://one.noxgle.com/docs/`; quickstart uses `/docs/library/quickstart/`,
  not the old GitHub README/document URLs. Do not rebuild the landing in Astro.
- Deployment entry point is `landing_page/deploy.sh`, delegating to `deploy.py`.
  From the repo root, `--landing` (also the default) uploads landing assets;
  `--docs` locally installs/builds/checks and uploads only static `website/dist/`;
  `--all` prepares both before uploads. The server needs no Node.js/Python runtime.
  No real deployment without explicit user authorization.
- `--dry-run` never contacts the deployment server, but docs builds may contact
  npm. Use `--env-file` with a scratch, non-secret config for validation rather
  than reading production `landing_page/.env`. See `deploy.env.example` and
  `website/README.md`; never print or commit credentials. `PUBLIC_SITE_URL` is
  the HTTPS origin, separate from `FTP_HOST`; `DOCS_BASE` defaults to `/docs/`
  and maps to `FTP_DIR/docs/`.
- FTPS is required by default. Explicit `--allow-self-signed` or
  `FTP_ALLOW_SELF_SIGNED=true` disables certificate AND hostname verification,
  warns, and still requires TLS. Use only when the user accepts that risk; never
  silently enable it or downgrade to `FTP_TLS=false`. Uploads do not delete stale
  remote files, provide backups, or switch releases atomically; failures may leave
  a partial site. Verify public pages/assets after authorized publication.
- Deploy regression tests must stub subprocess/network access. Run
  `.venv/bin/python -m pytest -q tests/test_landing_deploy.py` with isolated state
  and `bash -n landing_page/deploy.sh`; do not use production uploads as tests.
- Ignore generated `website/dist/`, `website/node_modules/` and `website/.astro/`;
  keep `website/package-lock.json` tracked for reproducible `npm ci`.

## Releases and user-visible changes

- `one/config.py:VERSION` is the single version source and must match any tag.
- User-visible changes require a version bump, an `Unreleased` `CHANGELOG.md`
  entry, and regenerated TUI snapshots because the sidebar displays the version.
- Tests-only, refactor-only, and comment-only changes do not bump the version.
- Never create release tags or publish artifacts unless explicitly requested;
  `.github/workflows/release.yml` publishes to PyPI and creates a GitHub
  Release on `v*` tag pushes after verification.
