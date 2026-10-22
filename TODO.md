# Project: Named presets for main agents and subagents

## Goal

Replace the temperature-derived `Mode` display with named, reusable presets containing
configuration and a replacement system prompt in one file. Support built-in Default
and enforced Readonly presets and explicit preset selection for subagents.

**Status:** Architecture and planning approved by the user; implementation not started.
This project is additive to the existing roadmap retained below. Sandbox implementation
remains deferred. All paths listed as affected files are future implementation targets.

## Context

- `one/core/temperature.py` currently maps temperatures to coder/balanced/creative/
  experimental labels; `one/modes/tui_mode.py` displays these as `Mode`.
- `one/resources/resource_loader.py:get_system_prompt` composes the current base
  prompt, tool instructions, project context and skills. Its skill frontmatter parser
  silently tolerates malformed YAML and must not be used unchanged for presets.
- `one/core/agent_session.py` owns prompt composition, tool dispatch, MCP tool refresh
  and child creation. Children currently share settings/resources and inherit model,
  thinking and temperature unless supported explicit overrides are supplied.
- TUI mutations are dispatched by `one/core/tui_runtime_host.py:ui_operation` on the
  independently owned runtime. UI must not mutate live session state directly.
- Research compared YAML/TOML frontmatter and centralized resolution versus scattered
  setters. The first research attempt failed with `Loading model`; a second completed.
  Its recommendations were reviewed against source. Unsafe Default fallbacks, ignored
  sandbox requirements and reliance on tool visibility alone were rejected. TOML needs
  no added dependency on Python 3.12 (`tomllib`), contrary to the research report.

## Scope

### In Scope

- One Markdown file per preset: YAML configuration frontmatter followed by prompt body.
- User discovery under `get_agent_dir()/presets`, respecting XDG and
  `ONE_CODING_AGENT_DIR`; built-ins packaged with the application.
- Provider/model, temperature, thinking, purpose/description and future sandbox list.
- Built-in Default preserving current behavior and Readonly enforcing tool restrictions.
- CLI `--preset`, TUI/interactive `/preset`, explicit optional subagent `preset`.
- Atomic idle-time activation, modified-state display, durable resume/fork semantics,
  offline security tests and documentation.

### Non-Goals

- OS sandboxing, process workers, network/filesystem confinement or sandbox grants.
- Automatic task-based preset selection, preset inheritance chains, prompt templating
  languages, marketplace/import/download support or a graphical preset editor.
- Removing legacy temperature aliases, changing execution-mode names (TUI/RPC/run),
  bypassing approvals or changing cooperative execution semantics.
- Constraining arbitrary third-party code already executing inside the application.

## Assumptions

- Users create/edit their own preset files with an editor; selection/listing is provided
  in the app. No preset CRUD tool is exposed to the model in this project.
- Default/Readonly are reserved IDs; user collisions are errors, not silent overrides.
- Omitted model/temperature/thinking use current resolved session settings. Applying
  Default does not unexpectedly reset model settings to hard-coded values.
- Readonly blocks model-driven and app-mediated manual shell execution while active;
  normal session/evidence/config persistence remains allowed and documented.
- Active Readonly sessions disable extension execution hooks before binding; switching
  from a session that has already executed extensions cannot make those extensions
  sandboxed. Require a clean extension-disabled session to enter Readonly in that case.
- Main-session users may explicitly leave Readonly when idle. A child cannot lift the
  immutable restriction ceiling inherited at creation, even by selecting Default.

## Open Questions

- No blocking product questions remain. Confirm wording of modified-preset display and
  diagnostics during UI review; proposed label is `Preset: <name> (modified)`.
- Existing `--system-prompt` interactions need an explicit compatibility audit in P1:
  retain existing behavior for implicit Default; reject ambiguous simultaneous explicit
  replacement-prompt sources rather than silently choosing one.

## Tech Stack

- **Python 3.12+ / dataclasses:** immutable definitions and resolved session snapshots.
- **PyYAML (existing dependency):** safe, bounded frontmatter parsing with strict schema.
- **Textual / runtime facade:** selection and detached sidebar projections.
- **pytest / Ruff:** offline fixtures, policy regression tests and reviewed snapshots.

## Constraints

- No new dependency is expected. Never inspect real user configuration/auth/session data
  for tests; use scratch `ONE_CODING_AGENT_DIR`, fake providers and stub MCP managers.
- Preserve runtime ownership, existing lifecycle event contracts and provider normalization.
- CLI hard restrictions and parent policy ceilings always win over preset requests.
- Nonempty future `sandboxes` rejects activation; unsupported security requirements
  must never be ignored or represented as enforced.

## Architecture

Discover `*.md` definitions in packaged `one/resources/presets/` and the user preset
directory. A strict loader produces an immutable definition; a resolver combines it
with explicit inputs, existing settings and the session's non-escalatable policy ceiling.
The runtime owns the resulting snapshot. A policy component controls both advertised
tools and actual execution; changing `_active_tools` once is not a security boundary.

File contract: frontmatter fields `version: 1`, `id`, `name`, optional `model` in
`provider/model` form, `temperature`, `thinking`, `description`, `tools`, and `sandboxes`.
`thinking` uses existing string levels, including `on`/`off`, not YAML booleans or a
new integer scheme. `tools` is an optional narrowing allowlist; it cannot grant a
capability excluded by CLI settings or inherited policy. `sandboxes` is a list of
identifiers, defaulting to empty. IDs are safe lowercase slugs matching filenames;
missing optional fields mean inherit, not zero/off. Unknown keys/schema versions,
duplicate keys/IDs, malformed metadata, invalid types, nonfinite temperatures and
unresolvable models produce actionable errors. Bound file/header sizes and reject
unsafe aliases/tags and paths escaping the configured preset roots.

The body is a replacement role/system prompt, not an automatically submitted user
task. Description advertises the intended task class; the actual task remains the
user's message or parent's spawn task. Compose body separately from invariant tool
protocol, runtime header/context, project guidance, skills, cooperation and output
safety instructions. Default must preserve the existing composed prompt behavior,
including dynamic tool schemas and timeout values; do not freeze a generated prompt
in a static file or discard project/skill context when replacing the base role.

Readonly uses a built-in immutable policy ceiling: read/read_image/grep/find/ls,
session-scoped evidence_read, finish and existing cooperation-gated ask_user; delegation
is allowed only to equivalently restricted children. Exclude write/edit/apply_patch,
arbitrary Bash, unclassified MCP/custom tools and extension execution. Internal
persistence is distinct from model-accessible mutation. Shared dispatch checks must
hold after MCP refresh, extension processing, native/JSON calls and parallel batches.

Activation validates all fields/model capabilities and prepares the next snapshot
before committing any state on the owner loop. Reject activation while the parent or
its direct children/approval waits are active; do not queue a hidden mid-turn change.
Presets do not write their settings back into shared global defaults. Manual setting
changes retain the selected preset ID plus effective overrides/modified status.

For children: explicit arguments override preset configuration, which overrides current
inherited settings; restrictions always intersect. Without `preset`, preserve current
configuration/prompt inheritance and explicit override behavior. Changing a child's
configuration must not mutate the parent, siblings or global settings.

Persist versioned effective preset state (ID, provenance/content hash, body, effective
configuration and policy ceiling) in session storage, using existing private persistence
primitives. Resume/fork uses the stored snapshot, not whatever a same-named file now
contains. Missing/changed files may produce a warning but never weaken saved restrictions.
Unsupported saved state fails closed. Explicit reapplication is needed to adopt edits.

## Architecture Decisions

### ADR-P01: Markdown with YAML frontmatter

**Decision:** Configuration and prompt are two parts of one `.md` file.
**Alternatives:** TOML frontmatter using stdlib `tomllib`; JSON header or sidecars.
**Rationale:** Matches existing skills authoring conventions and uses existing PyYAML.
**Trade-offs:** YAML coercion and duplicate keys require strict validation; the permissive
skill parser is not a sufficient preset parser.
**Consequences:** No sidecar prompt files, added dependencies or executable interpolation.

### ADR-P02: Centralized resolution and mandatory execution policy

**Decision:** Immutable resolved snapshots, owner-loop transaction and a shared runtime
authorization gate; restrictions survive tool refresh and delegation.
**Alternatives:** Sequential calls to current setters; prompt-only Readonly; visibility-only
filtering; restart-only selection.
**Rationale:** Prevents partial application, global setting leaks and delegation bypass.
**Trade-offs:** Requires coordinated prompt, storage and tool-dispatch changes.
**Consequences:** Idle-time switching is supported; no OS sandbox claim and no claim that
already-loaded arbitrary extension code becomes confined.

### ADR-P03: Snapshot persistence and fail-closed activation

**Decision:** Persist effective configuration/prompt/policy, reserve built-in IDs and reject
invalid or unsupported presets without changing the current state.
**Alternatives:** Save only ID and reload from disk; silently fall back to Default; allow
user definitions to shadow Readonly; ignore future sandbox configuration.
**Rationale:** Resume and file edits must not silently change authority or permissions.
**Trade-offs:** Session files contain additional potentially sensitive prompt content and
need schema/version handling; edited definitions require explicit reapplication.
**Consequences:** Preserve private-file permissions and sanitized diagnostics.

## Phases

### Phase P1: Strict preset format and packaged built-ins

**Objective:** Load one-file definitions deterministically without executing prompts.
**Prerequisites:** Approved architecture; existing roadmap remains untouched.
**Expected outcome:** Validated discovery/resolution and installed built-in resources.
**Estimated effort:** 1–2 working days. **Confidence:** High.

- [ ] **Task P1: Define and validate the preset contract**
  - **Description:** Add definition/resolution types and strict YAML/body parsing;
    implement discovery, collision/path/size rules and configuration precedence. Audit
    replacement-prompt CLI compatibility. Package only Default and Readonly initially;
    optional model fields inherit current settings. Arrange Default's body/template so
    the next phase preserves existing dynamic composition without duplicated prompts.
  - **Files:** Proposed `one/core/presets.py`, `one/resources/presets/default.md`,
    `one/resources/presets/readonly.md`, `tests/test_presets.py`;
    `one/config.py`, `one/resources/resource_loader.py`, `pyproject.toml`.
  - **Dependencies:** None.
  - **Acceptance Criteria:** Single-file YAML/body format; correct config-dir overrides;
    no new dependencies; reserved built-ins cannot be shadowed; invalid definitions
    cannot activate; `thinking: on` is handled as the existing string enum, not a bool;
    nonempty sandboxes fail activation; wheel contains both built-ins.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_presets.py` with scratch
    roots, malformed/duplicate YAML, explicit boolean thinking, valid string levels,
    unknown fields, traversal/symlink escape, oversized files, absent fields, collisions
    and provider/model validation. Later inspect built wheel resource contents.

### Phase P2: Atomic runtime configuration and enforced Readonly

**Objective:** Apply prompt/configuration and permissions consistently in every call path.
**Prerequisites:** P1.
**Expected outcome:** Default-compatible prompts and non-bypassable application tool policy.
**Estimated effort:** 3–5 working days. **Confidence:** Medium.

- [ ] **Task P2a: Separate prompt components and apply resolved configuration**
  - **Description:** Refactor base-role replacement independently of tool/runtime/project/
    skills/safety sections; preserve implicit Default output. Add owner-loop activation
    transaction with full prevalidation, effective-settings snapshot and modified flag;
    reject busy/active-child changes, preserve old state on validation failure and avoid
    global settings writes. Use existing provider/model capability normalization and
    show unsupported effective thinking/temperature honestly rather than pretending.
  - **Files:** `one/resources/resource_loader.py`, `one/core/agent_session.py`,
    `one/core/agent_session_runtime.py`, `one/core/tui_runtime_host.py`,
    proposed `tests/test_preset_runtime.py`; `tests/test_threaded_agent_runtime.py`.
  - **Dependencies:** P1.
  - **Acceptance Criteria:** Default preserves legacy dynamic prompt/context behavior;
    custom body replaces base role exactly once; invariant sections remain; failed or
    busy activation leaves model/prompt/tools/settings unchanged; UI gets detached state.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_preset_runtime.py tests/test_threaded_agent_runtime.py`;
    prompt equivalence fixtures, capability mismatches, busy/approval/child races and
    prevalidation failure injection; assert global settings and sibling state unchanged.

- [ ] **Task P2b: Enforce Readonly at execution boundaries**
  - **Description:** Introduce policy intersection and mandatory authorization shared by
    tool advertisement, dispatch and MCP refresh. Audit native/JSON/parallel execution,
    extension hooks, direct app shell actions, resource reload and built-in tool side
    effects. Disable extensions for clean Readonly startup; reject unsafe in-place entry
    after extension execution. Keep normal storage functioning and retain finish path.
  - **Files:** Proposed `one/core/tool_policy.py`, `tests/test_readonly_policy.py`;
    `one/core/agent_session.py`, `one/core/tui_runtime_host.py`, `one/tools/index.py`,
    `one/resources/resource_loader.py`, `one/cli/main.py`.
  - **Dependencies:** P2a.
  - **Acceptance Criteria:** Prohibited tool bodies/hooks/shells never run; MCP enable/
    refresh cannot add capabilities; approvals cannot authorize a forbidden action;
    restrictions cannot be changed through model-controlled inputs; no sandbox claim.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_readonly_policy.py`;
    spy counters assert zero forbidden execution for direct/JSON/native/parallel calls,
    mutation attempts after reload, unknown MCP tools, shell operations and extension
    activation; scratch history/evidence still persists normally.

### Phase P3: Durable session identity and explicit child selection

**Objective:** Preserve effective presets across child creation, resume and fork.
**Prerequisites:** P2a–P2b.
**Expected outcome:** No inheritance leaks or security downgrade from reloaded files.
**Estimated effort:** 2–3 working days. **Confidence:** Medium.

- [ ] **Task P3a: Persist and restore effective preset snapshots**
  - **Description:** Add versioned additive session entries/state for effective prompt,
    configuration, provenance and restriction ceiling. Restore at the correct branch
    point on fork; load legacy sessions as their existing Default-compatible state.
    Detect unsupported/corrupt snapshots without silently relaxing restrictions.
  - **Files:** `one/core/session_manager.py`, `one/core/agent_session_runtime.py`,
    `one/core/agent_session.py`, proposed `tests/test_preset_sessions.py`;
    `tests/test_session_branching_export.py`.
  - **Dependencies:** P2a–P2b.
  - **Acceptance Criteria:** Resume/fork works after definition edits/deletion; historical
    branch selection restores its own effective snapshot; legacy sessions still load;
    failures preserve current state and private persistence contracts.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_preset_sessions.py tests/test_session_branching_export.py`;
    round trips, changed/missing files, invalid versions, multiple preset changes and
    fork-before/after-change fixtures; inspect export for intentional sensitive content.

- [ ] **Task P3b: Resolve subagent presets without escalating permissions**
  - **Description:** Add optional `preset` to spawn schema, descriptions and dispatch.
    Resolve before child/session creation; explicit config overrides beat preset values,
    but policy intersects the parent ceiling and existing CLI restrictions. Preserve
    omission semantics, parallel task behavior, monitor identities and child watchdogs.
    Advertise concise preset metadata/purposes, not every prompt body, for model selection.
  - **Files:** `one/tools/index.py`, `one/tools/spawn_subagent.py`,
    `one/core/agent_session.py`, `one/resources/resource_loader.py`,
    proposed `tests/test_subagent_presets.py`; `tests/test_subagents.py`,
    `tests/test_spawn_subagent_errors.py`.
  - **Dependencies:** P3a.
  - **Acceptance Criteria:** Explicit child preset works for single/parallel spawn;
    omission preserves prior behavior; child Default/custom tool lists cannot escape
    parent Readonly; `--no-subagents` always wins; parent/sibling/global state unchanged;
    invalid preset creates no partially configured child.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_subagent_presets.py tests/test_subagents.py tests/test_spawn_subagent_errors.py`;
    parent Readonly → child Default/custom/MCP/Bash attempts, nested delegation, explicit
    model override precedence, parallel isolation and cancellation/monitor regressions.

### Phase P4: User-facing selection and compatibility

**Objective:** Expose presets without conflating temperature aliases or execution modes.
**Prerequisites:** P3a–P3b.
**Expected outcome:** Consistent CLI/TUI/interactive selection and honest status.
**Estimated effort:** 2–3 working days. **Confidence:** Medium.

- [ ] **Task P4: Add preset selection and replace Mode presentation**
  - **Description:** Wire `--preset <id>` through shared bootstrap for applicable CLI
    modes including headless execution. Add `/preset` listing and `/preset <id>` in
    TUI/interactive, completion/help and palette entry. Publish preset name/modified
    state through detached sidebar projections; display temperature separately. Keep
    temperature aliases and shortcuts compatible; document RPC state/selection support
    using its existing validated operation conventions, not a separate resolver.
  - **Files:** `one/cli/main.py`, `one/modes/tui_mode.py`,
    `one/modes/interactive_mode.py`, `one/modes/rpc_mode.py`,
    `one/core/tui_runtime_host.py`, `one/core/agent_runtime_facade.py`,
    `tests/test_tui_commands.py`, `tests/test_interactive_mode.py`,
    `tests/test_rpc_mode.py`, `tests/test_run_mode.py`,
    proposed `tests/test_cli_presets.py`.
  - **Dependencies:** P3a–P3b.
  - **Acceptance Criteria:** Mode no longer means temperature in UI; active preset and
    effective temperature/thinking/model are visible; modified status is accurate;
    CLI explicit config > preset > existing defaults, security restrictions always win;
    unknown/invalid selections produce contractual exit codes or nonmutating UI errors.
  - **Verification:** Focused pytest for listed test files; real Textual pilot selection,
    busy rejection, reload, manual edits, session rebind, narrow terminal rendering and
    Readonly shell rejection. CLI subprocess fixtures use scratch config/PYTHONPATH;
    RPC and headless fixtures use fake providers only.

### Phase P5: Release gates and independent policy review

**Objective:** Ship documented one-file presets with audited fail-closed behavior.
**Prerequisites:** P4.
**Expected outcome:** Reviewed implementation ready for user-authorized integration.
**Estimated effort:** 1–2 working days. **Confidence:** High.

- [ ] **Task P5: Document, version and independently verify presets**
  - **Description:** Document schema/sample files, config paths, precedence, Default
    compatibility, Readonly limits/extensions/manual-shell policy, child selection,
    persistence, malformed definitions and unsupported sandboxes. Add Unreleased entry,
    bump then-current version, regenerate/review snapshots and verify packaged resources.
    Obtain independent review of authorization, prompt separation and ownership paths.
  - **Files:** `README.md`, `docs/QUICKSTART.md`, `docs/architecture.md`,
    proposed `docs/PRESETS.md`; `CHANGELOG.md`, `one/config.py`,
    `tests/test_tui_snapshots.py`, `tests/snapshots/tui/*.txt`; P1–P4 files for review fixes.
  - **Dependencies:** P4.
  - **Acceptance Criteria:** No unresolved policy bypass or downgrade; docs make no OS
    sandbox guarantee; Default compatibility verified; only intended snapshot changes;
    installed distribution discovers built-ins; full regression gates pass.
  - **Verification:** Future implementer runs snapshot regeneration with
    `ONE_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest -q tests/test_tui_snapshots.py`
    and reviews diffs, then `.venv/bin/python -m pytest -q`,
    `.venv/bin/ruff check .`, `git diff --check` in order. Use existing release build/
    wheel checks to inspect packaged presets. No real provider or MCP traffic.

## Rollout & Rollback

Implement on a feature branch only when execution is authorized; no automatic commit,
merge, release or deployment. No preset flag means implicit Default with prior behavior.
New state entries are additive. Before rollback, close active agents; verify the target
version understands saved restrictions. Never resume a Readonly snapshot in an older
version that ignores its policy; retain a backup and require a supported reader instead.
Do not delete user preset files or rewrite historical sessions as a rollback shortcut.

## Observability

Expose preset ID/name, modified flag, source kind, snapshot version and effective
restriction status in existing state/status projections. Add metadata-only change/error
diagnostics without raw prompts, credentials or full configuration payloads. Existing
event contracts remain stable; any new preset events are additive and bounded.

## Security Considerations

Presets are data, not executable templates. Safe YAML loading alone is not sufficient:
validate types, duplicate keys, resource bounds and allowed roots. Render names/purposes
literally in the terminal. Do not log prompt bodies. Reserve Readonly identity and enforce
restrictions independently of its wording. Extensions/MCP processes remain privileged
external code; this feature denies app-mediated capability use, not arbitrary same-user
host access. No claim that Readonly prevents provider requests or internal persistence.

## Risks & Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| YAML coercion or permissive skill parser | Wrong configuration / silent fallback | High | Strict versioned parser, enum tests and duplicate-key rejection |
| MCP refresh / native / parallel / child bypass | Unauthorized mutation | High | Central execution gate, policy intersection and forbidden-body spies |
| Extension or manual shell escape | Misleading Readonly guarantee | High | Clean extension-disabled startup, reject unsafe transition, shell gate and explicit boundaries |
| Replacement loses tool/project/skill context | Broken protocol or behavior regression | Medium | Separate prompt components and Default equivalence fixtures |
| Setters mutate shared defaults or partial state | Parent/sibling corruption | Medium | Immutable resolution, prevalidation and owner-loop transaction |
| Resume reloads weaker edited/missing preset | Security downgrade | Medium | Persist effective snapshot, fail closed, no automatic Default fallback |
| Future sandbox list looks enforced | False security expectation | Medium | Reject nonempty list until supported; keep sandbox roadmap deferred |
| Snapshot stores sensitive prompt content | Local disclosure | Medium | Existing private persistence, safe diagnostics and documented export behavior |

## Project Acceptance Criteria

- [ ] Every preset is a single file with distinct configuration and prompt sections.
- [ ] User discovery respects configured agent directory; installed built-ins are available.
- [ ] Default preserves current system-prompt behavior and current setting inheritance.
- [ ] Custom prompt replaces base role without removing invariant runtime instructions.
- [ ] Readonly denies prohibited execution across all app-mediated paths and delegation.
- [ ] Main and child configuration remain isolated; parent restrictions cannot be weakened.
- [ ] Invalid/unsupported presets leave current state unchanged; sandbox references fail closed.
- [ ] Resume/fork preserves effective prompt/configuration/policy after file edits/deletion.
- [ ] UI says Preset, shows effective settings and marks overrides accurately.
- [ ] Offline tests, independent review, snapshots, lint and packaging gates pass.
- [ ] Sandbox implementation and unrelated existing roadmap work remain out of scope.

## Estimated Timeline

Approximately 9–15 implementer working days: format → prompt/policy → persistence and
children → interfaces → independent review. Overall confidence Medium; runtime dispatch,
extension lifecycle and legacy prompt compatibility are the main uncertainty drivers.
All verification commands above are instructions for a future implementer, not commands
executed during planning. Planning modified only this repository-root `TODO.md`.

---

# Active roadmap for `one`

This file contains only current, verified work. Completed implementation history
is available in [GitHub commit history](https://github.com/noxgle/one/commits/main/);
user-facing changes are recorded in [`CHANGELOG.md`](CHANGELOG.md). Do not keep
completed task logs or unprioritized feature proposals here. Record accepted
future work as GitHub issues.

## Project: Responsive TUI with an independently owned agent runtime

**Status: Complete.** The approved responsive-TUI implementation and independent
R6 verification are complete. The sandbox roadmap remains deferred and is not
part of this work.

### Goal

Keep keyboard input and history browsing responsive while the main agent and
subagents execute tools, stream output, compact context and persist evidence.
Separate UI ownership from execution and verify improvement with latency measurements,
not only functional tests or subjective claims.

### Context

Source inspection found that `TuiMode.run()` and the detached task awaiting
`AgentSession.prompt()` still share Textual's asyncio loop. Read-only tool bodies
already use `asyncio.to_thread`; Bash and MCP already use asynchronous transports.
However, synchronous persistence/fsync, evidence processing, local mutations,
extension hooks and context preparation can still block the shared loop.

UI-local costs also remain: `build_sidebar_snapshot()` computes context usage and
full statistics repeatedly; `_refresh_sidebar()` performs a synchronous Git probe;
child monitor invalidations trigger sidebar work; drains process a whole event batch
without a time budget; live text is repeatedly wrapped and retained transcript
`Content` is rebuilt. The 30 Hz timer is not a hard paint-rate limit. These
paths were profiled and addressed by the completed work below.

### Scope

#### In Scope

- Dedicated agent-owned asyncio loop in a runtime thread; Textual remains UI-owned.
- Transport-neutral command/event/snapshot boundary, suitable for later process use.
- Cheap revisioned sidebar and monitor display projections; asynchronous cached Git probe.
- Bounded/coalesced UI ingress, frame scheduling and incremental transcript presentation.
- Main and child approval/questions, ordinary prompt/steer/follow-up, abort, settings,
  model changes, resource reload, sessions and shutdown through the runtime boundary.
- Deterministic offline responsiveness benchmarks and regression tests.

#### Non-Goals

- Implementing the deferred subagent sandbox, changing autonomy or granting permissions.
- Running Textual widgets on a worker thread; indiscriminately threading async Bash/MCP.
- Changing approval/execution/event contracts to hide delays or weakening fsync durability.
- Promising instantaneous cancellation of writes, remote actions or running read threads.
- Shipping a separate process immediately without evidence that the thread solution
  fails the agreed latency gates; pretending threads remove GIL contention.
- New dependencies, process-level security claims or new platform support claims.

### Assumptions

- Runtime thread is the first execution backend. If measured CPU/GIL contention still
  fails the gates after bounded rendering, switch the same facade to a process in a
  separately approved follow-up; do not mark this project successful merely for adding
  a thread.
- UI consumes copied, allowlisted DTOs only, not shared session/messages/settings
  objects. All runtime state has one owner.
- Proposed targets are reference-environment targets, not universal terminal guarantees.
  Freeze Python/Textual versions, terminal size, hardware and workload in R1.
- Preserve the current 500-entry viewport contract; add byte and physical-row bounds
  to prevent one giant thinking/live entry bypassing presentation limits.

### Open Questions

- R1 established the reference offline benchmark and byte/backlog limits; the
  recorded R6 profile results are the completion evidence for this project.
- Exact thread-vs-process cutoff is empirical: inability to meet R1 gates under CPU
  stress is a blocker requiring process-backend scope review, not a silent fallback.
- Some third-party extensions assume main-thread execution; identify and document
  compatibility constraints rather than invoking runtime hooks from both loops.

### Tech Stack

- **Python 3.12+ / asyncio / threading:** independent runtime loop and safe transport.
- **Textual / Content:** UI-thread-only input, layout, viewport and inline actions.
- **Existing providers/MCP/extensions/session manager:** created and disposed by runtime.
- **pytest and offline fake providers:** deterministic functional and latency workloads.

### Constraints

- No real state, provider APIs or MCP servers in tests/benchmarks. Seed scratch state.
- Do not change `AgentSession._emit` globally to asynchronous delivery: retain existing
  listeners/event contracts and add a fast adapter at the TUI boundary.
- Create/start/use/close MCP and provider transports on their owner loop. Moving just
  `prompt()` while reusing objects started by CLI on the UI loop is not isolation.
- Command intake must continue while a prompt awaits execution/approval. Never serialize
  control requests behind completion of the whole turn.
- Report durable success only after required persistence succeeds; drain before shutdown.

### Architecture

Introduce an `AgentRuntimeFacade` with thread-safe asynchronous command submission and
bounded event/snapshot delivery. The dedicated runtime thread owns `AgentSession`,
runtime host, providers, MCP, extensions, storage, settings/auth changes, child tasks,
approval/question futures and watchdogs. A bootstrap spec carries configuration inputs;
loop-bound clients are constructed inside the runtime loop. Audit `one/cli/main.py`
mode dispatch so non-TUI modes retain their existing ownership and cleanup contracts.

Textual's UI thread owns widgets, input draft/history, scroll intent, theme, display
buffers and frame scheduler. UI callbacks only enqueue commands; they never read full
runtime history, call `get_session_stats()` while painting, or set runtime asyncio
futures/events directly. Schedule replies onto the owner loop using thread-safe loop
submission. Do not block UI with `Future.result()` or wait for a worker thread to join
synchronously.

Every message has a session epoch, sequence/revision and relevant command/turn/tool/
approval/question ID. Runtime publishes detached safe DTOs; no raw credentials,
evidence or large argument dictionaries enter UI transport. Ordered delta runs may
coalesce without crossing lifecycle barriers. Bounded ingress applies backpressure
or sends explicit overflow/resync state; it never silently drops approvals, questions,
errors or terminal events. Abort/answers/shutdown have a separate bounded control lane
so output pressure cannot prevent them being accepted.

Prompt submission acknowledges actual started/steer/follow-up delivery. Session changes
are guarded runtime transactions: release waits, handle old work, publish a new epoch
and initial bounded transcript. Approval futures remain runtime-owned; UI displays the
current FIFO request and rejection-reason field, then sends an ID-bound decision.
Late/duplicate/stale decisions cannot approve another request. Model/config/theme
commands return authoritative revisions; no direct UI mutation of shared settings.
Runtime shutdown rejects waits, aborts/reaps tasks, drains storage, closes owned MCP/
providers/extensions, and stops the loop. Dispose the authoritative current session,
not the session captured when TUI first started.

Sidebar projections contain only necessary display fields, calculated/cached by runtime
revision. Avoid duplicate context estimates and full historical scans per UI refresh;
keep exact context/model/pruning semantics. Git branch is cached by cwd and refreshed
asynchronously with a bounded timeout/TTL. Monitor output changes must not invalidate
unrelated sidebar fields; UI never blocks acquiring a live monitor lock or deep-copying
every child under it during input handling.

UI reduction and painting are separate. Limit a reduction slice to approximately
5–8 ms and schedule at most one transcript paint per 33 ms frame; lifecycle ordering
is preserved in model state rather than forcing full painting at each event. Immutable
completed blocks cache layout by width/theme revision; active text uses append/replace-
tail chunks and incremental wrapping. Bound previews before JSON display formatting,
not after wrapping a multi-megabyte payload. Keep spinner outside the transcript rebuild.
Index trusted inline actions by stable block/row IDs. Width-aware retained anchors
preserve scroll while history survives; user movement always supersedes deferred follow.

### Architecture Decisions

#### ADR-R01: Runtime thread with transport-neutral facade

**Decision:** Dedicated runtime loop/thread first; no direct shared UI/session state.
**Alternatives:** More scattered `to_thread` calls on one loop; a separate runtime
process immediately; moving only the provider coroutine into a thread.
**Rationale:** Removes blocking execution I/O from Textual while preserving a reusable
runtime API and avoiding per-tool mutable-state races.
**Trade-offs:** GIL still permits CPU contention; synchronous runtime work may delay
abort acknowledgment even though UI stays responsive. Ownership migration is substantial,
not a one-line listener change.
**Consequences:** Measure CPU stress; process follow-up if gates fail. Async MCP remains
async on runtime loop; no thread wrapping of its transport objects.

#### ADR-R02: Snapshot-driven UI and budgeted incremental rendering

**Decision:** UI paints cheap projections with bounded work and cached block layouts.
**Alternatives:** Compute sidebar/history statistics during painting; rebuild entire
transcript on every delta; move agent only and keep current renderer untouched.
**Rationale:** Execution separation alone cannot fix UI-local layout/serialization costs.
**Trade-offs:** Requires stable identities, invalidation and explicit queue bounds.
**Consequences:** Preserve literal rendering, link security, lifecycle ordering, selection
and scroll contracts; test both throughput and input latency.

### Phases

#### Phase R1: Measured offline baseline and responsiveness contract

**Objective:** Reproduce input/scroll lag and quantify the hot paths.
**Prerequisites:** Existing monitor/scroll behavior as baseline.
**Expected outcome:** Reproducible baseline and reference benchmark configuration.
**Estimated effort:** 1–2 days. **Confidence:** High.

- [ ] **Task R1: Add offline latency instrumentation and stress fixtures**
  - **Description:** Measure named production paths separately from synthetic stress
    probes: actual `_render_stream`/`_format_chat_panel`, event accumulator drain,
    sidebar/context/stats flattening, scratch persistence/evidence and Git helper with
    I/O stubs. Report virtual-driver `after_refresh` as distinct from physical terminal
    flush; mark unavailable offline paths unmeasured rather than relabeling toy work.
    Use in-memory and scratch-only durable history with increasing history and the
    identical 500-line visible viewport/delta burst.
  - **Files:** Proposed `scripts/profile_tui_responsiveness.py`,
    `tests/test_tui_responsiveness.py`, `tests/support/tui.py`,
    `one/modes/tui_mode.py` (opt-in timing only).
  - **Dependencies:** None.
  - **Acceptance Criteria:** No live APIs/state; metrics include p95/p99/max, not just
    averages; capture actual paint latency; record reference versions/size/hardware;
    identify blocking paths before architectural conclusions.
  - **Verification:** Offline benchmark with 50/500/5,000/50,000 durable entries,
    narrow/wide viewports, rapid alternating deltas and silent-provider periods.
    Future `.venv/bin/python -m pytest -q tests/test_tui_responsiveness.py`.

#### Phase R2: Runtime protocol and owner-loop execution

**Objective:** Remove direct UI access to mutable agent state.
**Prerequisites:** R1.
**Expected outcome:** Runtime facade starts/controls/disposes all execution on one worker loop.
**Estimated effort:** 4–7 days. **Confidence:** Medium.

- [ ] **Task R2: Introduce command/event facade and runtime backend**
  - **Description:** Define copied bounded DTOs, IDs/epochs, async acknowledgments,
    control lane and runtime-owned snapshots. Construct loop-bound resources in the
    worker; dispatch active prompt as a tracked task while intake continues. Preserve
    non-TUI modes and public event semantics. Define unavailable/startup-failed/closed
    states, graceful shutdown and mutation outcome handling.
  - **Files:** Proposed `one/core/agent_runtime_facade.py`,
    `one/core/threaded_agent_runtime.py`, `one/core/agent_session_runtime.py`,
    `one/cli/main.py`, `tests/test_agent_runtime_facade.py`,
    `tests/test_threaded_agent_runtime.py`, `tests/test_mcp_shutdown.py`.
  - **Dependencies:** R1.
  - **Acceptance Criteria:** UI/runtime loop identity verified; provider/MCP objects
    never cross owner loops; blocked runtime I/O does not freeze UI; controls accepted
    during active turns; disposal drains owned work; no unsupported thread-kill claim.
  - **Verification:** Stubbed startup, MCP lifecycle, timeout, crash/error and shutdown
    tests; ordered event snapshots; non-TUI CLI/RPC/run regression tests.

#### Phase R3: TUI commands, approvals and session lifecycle via facade

**Objective:** Preserve all human-facing operations across the runtime boundary.
**Prerequisites:** R2.
**Expected outcome:** TUI never calls live session state directly during painting/input.
**Estimated effort:** 3–5 days. **Confidence:** Medium.

- [x] **Task R3: Route TUI interactions through commands and projections** *(completed: the full TUI command, approval, question, and session lifecycle surface now uses facade commands and detached runtime projections; independent full-suite and final review passed.)*
  - **Description:** Migrate prompt/steer/follow, abort, approval/question replies,
    settings/model/resource actions and new/fork/load sessions. Keep runtime futures
    private and responses epoch/ID-bound. Preserve common main/child approval FIFO,
    reason field, inert Enter and independent main prompt. Handle failure/rebind/close
    without orphaned waits or stale approvals. Seed bounded transcripts/monitor snapshots.
  - **Files:** `one/modes/tui_mode.py`, `one/core/agent_runtime_facade.py`,
    `one/core/agent_session_runtime.py`, `tests/test_tui_cooperation.py`,
    `tests/test_tui_commands.py`, `tests/test_tui_input.py`,
    `tests/test_tui_streaming.py`, `tests/test_session_branching_export.py`.
  - **Dependencies:** R2.
  - **Acceptance Criteria:** No cross-loop Future/Event mutation; stale child/session
    replies rejected; main/child actions correlated; input remains responsive while
    awaiting decision; exact-name and session-sidecar behavior preserved.
  - **Verification:** Real Textual pilots while fake runtime is busy/blocked; concurrent
    child approvals, reason entry, duplicate/late answers, coop disable, abort, failed
    session switch and shutdown. Include live settings/model changes during streams.

#### Phase R4: Cheap sidebar and bounded event/paint scheduling

**Objective:** Make UI work independent of token rate and durable history size.
**Prerequisites:** R3.
**Expected outcome:** No synchronous Git/context/stats calls on the UI paint path.
**Estimated effort:** 2–3 days. **Confidence:** High.

- [x] **Task R4: Publish cached sidebar/monitor projections and budget UI drains** *(completed: cached projections, bounded/coalesced ingress, budgeted drains, and frame-scheduled painting are verified by the independent full suite and offline profile.)*
  - **Description:** Remove repeated runtime history scans from sidebar painting;
    cache/invalidate display snapshots accurately; async Git probe by cwd/TTL; separate
    child text updates from running count. Coalesce ingress deltas by bounded chunks,
    preserve barriers, schedule slices and one paint per frame. Define pressure/resync
    behavior and keep critical controls deliverable without silent loss.
  - **Files:** `one/modes/tui_mode.py`, `one/core/agent_runtime_facade.py`,
    `one/core/threaded_agent_runtime.py`, `one/core/agent_session.py`,
    `one/core/subagent_monitor.py`, `tests/test_tui_responsiveness.py`,
    `tests/test_tui_streaming.py`, `tests/test_session_stats.py`.
  - **Dependencies:** R3.
  - **Acceptance Criteria:** Paint never computes provider context or launches Git;
    slow probes do not stall input; queue work/buffers bounded; pressure cannot reorder
    or drop approval/error/terminal state; steady-state paint rate ≤30 Hz.
  - **Verification:** Benchmark/sidebar call-count assertions under child token bursts,
    increasing history, slow Git and producer flood; exact lifecycle/compaction tests.

#### Phase R5: Incremental transcript, bounded live blocks and stable viewport

**Objective:** Avoid full-message/full-transcript work per token or spinner tick.
**Prerequisites:** R4.
**Expected outcome:** Cached completed blocks, cheap live-tail updates, stable history browsing.
**Estimated effort:** 3–5 days. **Confidence:** Medium.

- [x] **Task R5: Refactor presentation cache and renderer** *(completed: bounded live presentation, incremental rendering, and stable viewport behavior are verified by the independent full suite and offline profile.)*
  - **Description:** Introduce stable block IDs and width-aware layout caches; chunk
    live text and wrap only changed tails; bound bytes/physical rows before expensive
    formatting, including thinking and large tool arguments/results. Separate spinner
    refresh from transcript. Index trusted inline actions without per-line scans.
    Preserve user anchor and selection under append/trim/reflow; invalidate deferred
    follow on later user input and session changes.
  - **Files:** `one/modes/tui_mode.py`, proposed `one/modes/tui_transcript.py`,
    `tests/test_tui_rendering.py`, `tests/test_tui_navigation.py`,
    `tests/test_tui_streaming.py`, `tests/test_tui_responsiveness.py`,
    `tests/test_tui_snapshots.py`.
  - **Dependencies:** R4.
  - **Acceptance Criteria:** Work for unchanged history bounded/cached; long single
    entry cannot bypass bounds; literal task/result text cannot create actions; `[view]`
    opens correct child; reading history never fights auto-follow; retained anchors
    survive heavily wrapped front removal and resize when possible.
  - **Verification:** Actual pilot wheel/typing/resize/click/selection under output flood;
    Unicode, repeated text, >256 wrapped removed rows, multi-megabyte bodies and
    coalesced completion bursts; reviewed deterministic snapshots.

#### Phase R6: Latency gates, independent review and integration readiness

**Objective:** Verify comfort of use under load and regressions across modes.
**Prerequisites:** R1–R5.
**Expected outcome:** Measured latency targets met, or explicit process-backend follow-up blocker.
**Estimated effort:** 2–3 days. **Confidence:** Medium.

- [x] **Task R6: Validate responsiveness and lifecycle guarantees** *(completed: independent review found no blockers; the paired schema-v5 profile passed all 8 detached cases and approval, compaction, and critical-event checks. Worst detached drain p99 was 4.99 ms; keypress p99 2.48 ms; wheel p99 0.041 ms; input-to-refresh p99 1.45 ms; paint p99/max 11.98 ms; and maximum queue usage was 38 items/20.8 KiB. The paired legacy route is a synthetic in-process reference, not a historical binary; virtual-driver timings are not physical flush measurements.)*
  - **Description:** Repeat R1 workloads; independent concurrency/performance/security
    review of owner-loop transport, critical queues, approval correlation and mutations.
    Document async/thread architecture and limitations; add Unreleased entry, bump
    then-current version and regenerate snapshots by repo policy. If CPU/GIL stress
    fails targets, record evidence and request process-backend scope rather than claim
    completion. Do not weaken measurements or disable safety to pass.
  - **Files:** `README.md`, `docs/architecture.md`, `docs/QUICKSTART.md`,
    `CHANGELOG.md`, `one/config.py`, `tests/test_tui_snapshots.py`,
    `tests/snapshots/tui/base.txt`, `tests/snapshots/tui/overlay.txt`,
    `tests/snapshots/tui/widget_panel.txt`, `tests/snapshots/tui/agents.txt`, R1–R5 files.
  - **Dependencies:** R5.
  - **Acceptance Criteria:** Reference keyboard/wheel p95 ≤50 ms and p99 ≤100 ms;
    no regular ordinary stalls >250 ms; UI loop p99 lag ≤50 ms; normally ≤5–8 ms
    reduction slices; one transcript paint/frame ≤30 Hz; no critical event loss or
    unbounded backlog; review has no unresolved blockers.
  - **Verification:** Offline repeatable benchmark comparing baseline/final values;
    CPU and slow-I/O stress, multiple children, long histories, approval/compaction;
    full future `.venv/bin/python -m pytest -q`, `.venv/bin/ruff check .`,
    `git diff --check` in order; platform lifecycle tests and snapshot diff review.

### Rollout & Rollback

Develop on a feature branch with facade contracts first; preserve text/JSON/RPC/run
modes. No session-data migration expected. Shutdown/drain authoritative runtime before
rollback; do not leave mutation threads/transports active. Commit, merge, push and
release only with user authorization. Do not fall back to shared-loop execution silently
and still report the UI as decoupled.

### Observability

Opt-in metadata-only loop/drain/paint/queue/probe/persistence timing. Reference workloads
report p95/p99/max and counts; production instrumentation must stay lightweight.
Display runtime disconnected/busy/approval states honestly. Do not log raw prompt,
argument, evidence, reason-field or credential content as performance telemetry.

### Security Considerations

UI receives safe bounded projections; approvals and questions use IDs plus epochs.
Runtime stays privileged and authoritative; transport separation is not sandboxing.
Never auto-approve during migration or replay ambiguous mutations after transport loss.
Preserve evidence durability, sanitization and secret-redaction contracts.

### Risks & Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Loop-bound MCP/provider reused on wrong loop | Hangs or broken I/O | High | Runtime-owned creation/use/close; identity tests |
| Direct mutable session access from UI | Races and stale decisions | High | Facade-only commands and detached revisioned projections |
| Thread GIL contention | Input still lags under CPU work | Medium | CPU benchmarks; transport-neutral process follow-up if gates fail |
| Faster producer floods UI | Jank or memory growth | High | Ingress coalescing/backpressure, time slices, frame cap |
| Full-history render/stat scans remain | Latency grows with sessions | High | Cheap snapshots, incremental caches, scaling benchmarks |
| Approval/session reply miscorrelated | Wrong action approved | Medium | IDs/epochs/FIFO; stale/duplicate rejection tests |
| Mutation continues after cancellation | Inconsistent external state | Medium | Honest completion semantics, owned tracked work, drain/reap |
| Renderer refactor breaks clicks/scroll | Loss of usability | Medium | Actual pilot clicks/wheel/typing and stable block anchors |

### Project Acceptance Criteria

- [ ] UI loop remains available while agent execution performs blocked I/O.
- [ ] Keyboard/wheel/paint latency meets reference p95/p99 gates during realistic work.
- [ ] Runtime owns all execution resources; UI manipulates no live agent state.
- [ ] Sidebar and monitor refresh do not compute full history or synchronous Git in UI.
- [ ] Streaming/event bursts have bounded queue/slice/paint work and preserve critical events.
- [ ] Completed transcript layout is cached; live text/thinking budgets prevent scaling stalls.
- [ ] Scroll, `[view]`, approval reason and independent prompt input behave as before.
- [ ] Session/abort/rebind/shutdown correctly release waits and dispose current runtime.
- [ ] Durability, tool outcomes, non-TUI modes and existing event contracts remain verified.
- [ ] Independent review and full regression/performance gates pass; sandbox stays deferred.

### Estimated Timeline

Approximately 15–25 implementer working days for the thread-backed facade and renderer,
including baseline/review uncertainty; confidence medium. This is an ownership and
presentation refactor, not a two-thread wrapper around `prompt()`. Process-backend
implementation is a separately scoped follow-up only if measured thread/GIL results
require it. Research compared shared-loop offloads, runtime thread and runtime process;
unmeasured percentage improvements and optimistic one-line migration estimates were
not adopted.

## Project: Current-session live subagent monitor

**Relationship to completed work:** existing monitor requirements were preserved during
the completed responsiveness project above. They are not a reason to reimplement already
present functionality. The cooperative sandbox project below remains deferred.

### Goal

Provide a read-only TUI view of direct subagents of the current parent session,
including live response text, without switching the conversation or changing
agent execution. Scope approved by the user.

### Context

The original implementation created children in `_run_subagent` and emitted
`subagent_start` / `subagent_end` without a live observation registry. The current
code now has `one/core/subagent_monitor.py`, live response/tool observations and
the `/agents` view. The requirements below describe behavior to preserve and verify,
not a claim that the already-implemented monitor is still missing. Session listener
generation protection must survive the responsiveness refactor.

### Scope

#### In Scope

- Current parent's direct children; task, model, elapsed time, status, current tool.
- One tool-call entry per call, with tool name, timeout, bounded safe arguments,
  running/completed state, duration and details-gated visible result/error.
- Sidebar running count and a keyboard-accessible `/agents` panel.
- Clicking an application-generated child entry in the main conversation opens
  the same panel with that specific child selected (additional user-approved scope).
- Selected child's live response plus tool lifecycle/result preview in one bounded
  agent-activity frame, following the same visible-output/detail-toggle policy as the main TUI.
- Cooperation approvals use a separate fail-closed explicit approve/reject surface
  that does not capture ordinary prompt input; ordinary prompt input remains
  available and never resolves an approval.
- Small bounded terminal-history window for success, failure, timeout, cancellation.
- Operation with persistent and in-memory sessions.

#### Non-Goals

- Other processes/sessions, recursive agent trees, transcript-file polling.
- Subagent stop, retry, steering or other execution controls in the `/agents`
  monitor. Cooperation approvals are handled only by their separate approval surface.
- Reasoning streams, raw (unprojected) tool arguments, raw `rawResult`/evidence
  payloads, or live-child session switching. Only bounded, sanitized argument text
  and the existing user-visible tool output/error projection are in scope; never
  mirror arbitrary result objects.
- New dependencies, monitor persistence, changes to parser or delegation limits.

### Assumptions

- Existing Python 3.12+, asyncio, Textual and pytest are sufficient.
- All active children remain represented within the existing concurrency limits;
  only completed entries are evicted.
- Proposed defaults: 32 KiB aggregate visible preview text and at most 64 timeline
  entries per child, 20 retained completed entries, selected-preview redraws at
  most 10 times/second. Treat these as named constants, verify bounds, and adjust
  only with measured justification.
- Late opening shows retained response/tool-output tails, never an unlimited transcript.

### Open Questions

- `/agents` is the guaranteed entry point; the selected-child view action is inline
  beside each `spawn_subagent` status row. Left/Right selects an agent; Up/Down scrolls
  its single activity frame. Ctrl+S remains the separate availability toggle.
- No blocking product questions remain; recursive monitoring is explicitly deferred.

### Tech Stack

- **Python / asyncio:** child lifecycle and session-owned observation state.
- **Textual:** panel, keyboard navigation and queued UI rendering.
- **pytest / Ruff:** stubbed lifecycle tests, TUI pilots, snapshots and lint.

### Constraints

- Preserve existing lifecycle event payloads; add monitor-specific events/API.
- Monitoring must not affect watchdog activity, retries, approvals or tool execution.
- Do not read real runtime state or contact providers/MCP servers in tests.
- Preserve session exact-name behavior, listener generations and parent stream limits.

### Architecture

Add a session-owned monitor registry in `one/core/subagent_monitor.py`, wired by
`one/core/agent_session.py`. Each entry contains child session ID, task label,
  model identity, monotonic start/end timing, activity/status, bounded preview timeline
  with one mutable-in-place entry per tool call, truncation state and revision. It does
  not retain child session/task handles for
control purposes. Timeline records are allowlisted display blocks: coalesced assistant
  text, one stable tool-call status row updated from running to completed, a bounded safe
  display-arguments projection, and the same `outputText`/error projection the main
 TUI presents when details are enabled. Never copy raw `args`, `rawResult`, evidence IDs/full
evidence, `content` objects, output file paths, image blobs or arbitrary result fields.

The existing child subscription updates only allowlisted observations: visible
response text, tool name/status/duration, visible tool output/error and lifecycle.
Handle final-message/finish-summary output as well as streaming providers, with
replacement/delta semantics that avoid duplicates or serialized tool-call prose.
On completion clear the active-tool indicator and show waiting/responding status.
Tool output obeys the same detail visibility setting as the main TUI. Pair tool end events
by call ID (or FIFO fallback) to update the single row in place. Keep tool
results as safe display text only; do not forward reasoning, arguments, arbitrary
result objects or hidden evidence.

Maintain copied snapshots under a short-held lock. State is updated before an
additive identity-tagged invalidation notification is emitted; notify outside the
lock. TUI subscribes before fetching its initial snapshot and uses revisioned
snapshots as the source of truth, so queued old notifications cannot regress state.
Coalesce monitor invalidations into one dirty flag per bound-session generation,
not an unbounded per-token queue. Read the latest snapshot on the UI thread.

The panel owns one selected-agent activity frame and timeline scroll state. Existing
queued listener delivery is reused without touching widgets from provider threads.
Rebinding clears selection, pending monitor refresh and cached state, then seeds
the new session snapshot. Closing the panel does not stop children. Completed
entries remain inspectable until bounded eviction or parent-session replacement.

Main-stream child entries use the existing child session IDs and trusted render
metadata to attach application-generated click actions. One parallel spawn batch
must expose a separate entry for each child, not one ambiguous link on the batch.
Click activation and `/agents` share the same panel-opening/selection path. Validate
both bound-session generation and registry membership on activation. Retained
completed children may still be inspected; evicted entries show a short unavailable
notice and never fall back to loading a persisted session.

### Architecture Decisions

#### ADR-001: Registry snapshots plus coalesced invalidations

**Decision:** Use authoritative session snapshots and additive notifications.

**Alternatives:** An event-only TUI cache; polling saved session transcripts.

**Rationale:** Late opening must recover active children and response tails;
in-memory sessions must work. Snapshots avoid dependence on witnessing every token.

**Trade-offs:** Requires synchronization and revision handling, but bounds queued
updates and keeps monitoring independent of persistence and the main transcript.

**Consequences:** Preserve old lifecycle payloads and isolate monitor state from
provider messages. Pure notifications need not contain sensitive preview text.

#### ADR-002: Bounded selected-child activity and result preview

**Decision:** Show one selected direct child's ordered, bounded visible activity
timeline, including response prose, tool lifecycle and main-TUI-visible tool result
or error text. Honor the existing details visibility setting.

**Alternatives:** Merge child output into the main transcript; show status only;
mirror full raw tool payloads; expose recursive children and controls immediately.

**Rationale:** Meets the approved live-view scope without interleaving conversations
or introducing execution-control semantics.

**Trade-offs:** Older timeline blocks/text are discarded with a visible truncation
indicator; buffers exist for unselected children so selection and late opening work.
Tool result text adds privacy and memory exposure even though it is visible for the
main agent, so it must honor the main TUI details toggle and aggregate byte cap.

**Consequences:** Render model and tool output as literal untrusted text, sanitize
terminal controls, and redact recognized secret patterns using existing project
utilities. No blanket guarantee of arbitrary secret detection; previews remain local.

#### ADR-003: Application-generated inline Content actions for main-stream child targets

**Decision:** Use application-generated inline `Content.from_markup` action spans
inside the existing single `Static` conversation renderer, with out-of-band
monotonic token → generation/child-ID/retained-line metadata.

**Alternatives:** Rich `@click` spans bridged through the `Static` renderer;
mouse-coordinate/logical-line hit testing; changing the renderer to one widget per
transcript line.

**Rationale:** A real Textual 8.2.8 `pilot.click` test verified that actions in
`Content.from_markup` dispatch from `Static`; the prior Rich-to-Content bridge
did not preserve them. This keeps the target beside its spawn status without
coordinate hit testing or parsing task/model text as markup.

**Trade-offs:** Only fixed app-created suffix markup may be parsed; every ordinary
transcript value is passed as a literal `Content` variable. `/agents` remains the
guaranteed keyboard-accessible route.

**Consequences:** Never parse task labels, response/result text, or model/tool output
as markup or infer actions from textual prefixes. Attach action metadata to the short
`Tool:` status row (never a task-argument body line), use fixed action markup plus
literal `Content` variables, and keep identity/generation metadata separate from
display text.
Remove/rebase metadata when the 500-line stream is trimmed, cleared or rebuilt; reject
stale clicks after session replacement. A registry entry can be evicted while a link
remains visible, so activation must re-check availability and show a safe unavailable
notice.

### Phases

#### Phase 1: Authoritative child lifecycle state

**Objective:** Make direct-child state inspectable independently of TUI mounting.
**Prerequisites:** None.
**Expected outcome:** Consistent snapshots for active and recently completed children.
**Estimated effort:** 0.75–1 day.
**Confidence:** High.

- [ ] **Task M1: Add bounded registry and wire lifecycle**
  - **Description:** Implement copied, revisioned snapshots; register before child
    execution and finalize exactly once after local cleanup. Distinguish completed,
    failed, timed-out and cancelled; preserve cancellation propagation and retain
    `externalState: unknown` where remote activity cannot be confirmed stopped.
    Retain all active children and evict only oldest completed entries.
  - **Files:** `one/core/subagent_monitor.py`, `one/core/agent_session.py`,
    `tests/test_subagent_monitor.py`, `tests/test_subagents.py`,
    `tests/test_spawn_subagent_errors.py`.
  - **Dependencies:** None.
  - **Acceptance Criteria:** Parallel children have unique entries; snapshots cannot
    mutate internal state; each terminal path clears running status exactly once;
    existing start/end payloads and execution results remain unchanged.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_subagent_monitor.py tests/test_subagents.py tests/test_spawn_subagent_errors.py`;
    stub cancellation, idle timeout, hard timeout and simultaneous completion.

#### Phase 2: Bounded live response and tool-result observation

**Objective:** Supply safe live activity/results without altering parent/provider context.
**Prerequisites:** M1.
**Expected outcome:** Streamed/non-streamed response text and allowlisted tool
start/end/output projections update the monitor.
**Estimated effort:** 1–1.5 days.
**Confidence:** Medium.

- [ ] **Task M2: Connect allowlisted child text and tool observations**
  - **Description:** Wire child response deltas/final summaries, tool start/end
    status, duration and main-TUI-equivalent `outputText`/error display values into
    a bounded ordered preview timeline. Reuse/extract the main TUI's bounded
    user-visible result projection instead of duplicating raw event formatting.
    Respect `get_bash_show_output()`/`Ctrl+O`
    when deciding whether to show result text. Keep callbacks lightweight and
    notifications outside registry locks. Clear current tool after completion and
    return activity to a waiting/responding state. Exclude reasoning, arguments,
    `rawResult`, evidence payloads, image blobs/refs, file paths and arbitrary result
    objects. A safe bounded display-arguments projection may show the operation (for example
    bash command or read path/options), but raw argument mappings remain excluded; redact
    credentials and summarize write/content, image/evidence, and payload fields. Sanitize terminal controls across
    delta boundaries. Preserve watchdog behavior.
  - **Files:** `one/core/subagent_monitor.py`, `one/core/agent_session.py`,
    `one/modes/tui_mode.py`, `tests/test_subagent_monitor.py`, `tests/test_subagents.py`,
    `tests/test_event_snapshots.py`, `tests/test_tui_streaming.py`.
  - **Dependencies:** M1.
  - **Acceptance Criteria:** Concurrent text/tool blocks never cross child IDs; final
    summaries are not duplicated; tool-call JSON is not rendered as response prose;
    status/tool indicators transition correctly; only main-TUI-visible result/error
    fields appear and only when details are enabled; result timeline and text obey
    aggregate entry/byte bounds; no reasoning, args, `rawResult`, evidence, image data
    or arbitrary result objects enter monitor snapshots/notifications/provider messages;
    late snapshots include retained current output.
  - **Verification:** Focused pytest for affected files; fake streamed/non-streamed
    providers and `read`/`bash`/failure/large-output cases; assert details on/off behavior,
    control-character sanitization, split-secret redaction and exact event contracts.
    Confirm no changes to tool execution, evidence retrieval or timeout semantics.

#### Phase 3: Current-session TUI panel

**Objective:** Expose running agents and selected live response/tool output without session switching.
**Prerequisites:** M1, M2.
**Expected outcome:** `/agents` and clicking a child entry open the same read-only monitor.
**Estimated effort:** 1.5–2.5 days, including M3a.
**Confidence:** Medium.

- [ ] **Task M3: Add panel, sidebar count and safe refresh lifecycle**
  - **Description:** Add running count, `/agents` command and palette entry; support
    direct-child selection, one-frame response/tool-result timeline scrolling and closing by keyboard.
    Use the same safe result-block formatter and details-output toggle as the main TUI.
    Fit narrow terminals
    with a usable stacked layout. Coalesce refreshes to a bounded dirty flag and
    redraw cadence; compute elapsed time from monotonic timestamps. Subscribe then
    snapshot on binding, reject obsolete revisions/generations, clear on new/fork/load,
    and handle selected-entry eviction and empty-state views. Never use `/sessions`
    to attach to a child or mutate the main stream for preview output. Keep `/agents`
    read-only, with no subagent stop, retry or steering controls. Cooperation approvals
    remain on a separate fail-closed surface: explicit buttons or Alt+A/Alt+R approve
    or reject, while ordinary prompt input retains existing queue/steering semantics
    and never resolves an approval.
  - **Files:** `one/modes/tui_mode.py`, `tests/test_tui_commands.py`,
    `tests/test_tui_streaming.py`, `tests/test_tui_rendering.py`,
    `tests/test_tui_navigation.py`, `tests/test_tui_snapshots.py`.
  - **Dependencies:** M2.
  - **Acceptance Criteria:** Opening mid-run recovers current state; selection keeps
    each child's response and tool-result stream isolated; detail visibility agrees
    with the main TUI; panel is non-controlling, with no subagent stop/retry/steering
    controls; parent input/stream remain usable; pending cooperation approval is
    resolved only by its explicit Approve/Reject buttons or Alt+A/Alt+R, and its tool
    never runs first; ordinary prompt input follows existing queue/steering semantics
    without resolving approval; queued child approvals are FIFO; disabling cooperation
    rejects/releases every pending waiter; parent `ask_user` behavior remains
    unchanged and child `ask_user` is locally auto-answered rather than routed as a
    parent question; rebind rejects old-session updates; rapid deltas do not grow
    pending queues; labels/preview cannot inject markup, ANSI escapes or fake widget
    content.
  - **Verification:** Textual pilot tests for parallel children, late open, selection,
    response/tool result blocks, detail toggle, close/reopen, resizing, session rebind
    and completion races; regression tests for explicit approval via buttons and
    Alt+A/Alt+R, prompt queue/steering during pending approval, no pre-approval tool
    execution, FIFO child approvals, cooperation-disable rejection/release of all
    waiters, and unchanged parent/locally auto-answered child `ask_user` behavior;
    manual check at narrow and standard terminal sizes with fake providers;
    deterministic monitor snapshot.

- [ ] **Task M3a: Open selected preview by clicking a main-stream child entry**
  - **Description:** Render an inline application-generated `[view]` target per child
    on the active `spawn_subagent` tool line, carrying an out-of-band
    session-ID/generation token. Use only fixed `Content.from_markup` action markup;
    ordinary child text remains literal and is never parsed. Route activation to the M3 panel with exact child selection;
    preserve main conversation scroll position and close/focus behavior. Keep metadata
    synchronized with trimming and transcript reset. Show a safe unavailable notice
    for evicted children; never load/fork their sessions. Verify native action support
    before using it; do not assume link spans are keyboard-focusable.
  - **Files:** `one/modes/tui_mode.py`, `tests/test_tui_streaming.py`,
    `tests/test_tui_navigation.py`, `tests/test_tui_commands.py`,
    `tests/test_tui_rendering.py`, `tests/test_tui_snapshots.py`.
  - **Dependencies:** M3.
  - **Acceptance Criteria:** Clicking either of two concurrent children selects the
    correct live preview; wrapped/scrolled/resized labels retain correct targeting;
    a retained completed child is inspectable; evicted/old-generation targets cannot
    select another child; fake action markup or label prefixes in model/tool content
    create no actions; closing preview does not stop execution or jump the transcript.
  - **Verification:** Real Textual `pilot.click` tests on rendered targets, including
    wrapping, resize, scroll and stream trimming; stale activation after session rebind;
    untrusted labels containing quotes/brackets/action markup; completion/eviction races.
    Direct action-method tests alone do not establish click functionality.

#### Phase 4: Regression gates and independent review

**Objective:** Deliver reviewed, documented behavior with compatible existing features.
**Prerequisites:** M1–M3a.
**Expected outcome:** Feature ready for integration, not automatically published.
**Estimated effort:** 0.5–1.25 days.
**Confidence:** High.

- [ ] **Task M4: Document, version and verify the feature**
  - **Description:** Document `/agents`, its visible tool-result preview and scope in
    TUI usage docs; add an Unreleased entry and bump the current version according to
    repo policy. Regenerate/review
    snapshots including a deterministic monitor view. Perform independent code review
    for lifecycle races, bounds, result-output privacy and rendering safety; fix
    findings before integration.
  - **Files:** `README.md`, `CHANGELOG.md`, `one/config.py`,
    `tests/test_tui_snapshots.py`, `tests/snapshots/tui/base.txt`,
    `tests/snapshots/tui/overlay.txt`, `tests/snapshots/tui/widget_panel.txt`,
    proposed `tests/snapshots/tui/agents.txt`; M1–M3a files if review requires fixes.
  - **Dependencies:** M3a.
  - **Acceptance Criteria:** Docs describe read-only current-session direct-child
    scope and bounded response/tool-result preview, plus the separate fail-closed
    cooperation approval surface; regression coverage confirms explicit
    buttons/Alt+A/Alt+R approval, prompt queue/steering isolation, no pre-approval
    tool execution, FIFO child approvals, cooperation-disable release of all pending
    waiters, and unchanged parent/locally auto-answered child `ask_user` behavior;
    snapshots contain only intended changes; review has no unresolved blocking
    findings; complete regression suite passes.
  - **Verification:** `ONE_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest -q tests/test_tui_snapshots.py`
    with diff review, then `.venv/bin/python -m pytest -q`,
    `.venv/bin/ruff check .`, `git diff --check`, in that order.

### Rollout & Rollback

Use an opt-in opened panel and always-visible compact count, preserving existing
`/subagents` controls and main TUI output visibility settings. No state migration or
remote discovery is required. Integrate
on a feature branch; commits, merges, pushes and releases require user authorization.
Rollback the feature changes as a unit without modifying saved child transcripts.

### Observability

Use the monitor snapshot for running count, activity, elapsed time, safe visible
tool result blocks and terminal status. Timeout/cancel views must not claim external
processes certainly stopped. Do not add raw preview logging or provider-context
messages for UI monitoring.

### Security Considerations

Response/task labels and tool outputs are untrusted text: bound, sanitize and render
literally. Forward only allowlisted main-TUI display fields, respecting detail
visibility. Do not expose real state files, credentials, reasoning, raw arguments,
`rawResult`, evidence, image blobs or arbitrary tool/MCP payloads. Ensure observation
exceptions cannot fail child execution and UI subscriptions are detached/invalidated
on session replacement.

### Risks & Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Snapshot/subscription race | Missing or regressed child state | Medium | Subscribe before copied snapshot; revisions and latest-state refresh |
| Token bursts/unbounded queues | Memory growth, frozen TUI | High | Bounded tails/history, coalesced invalidations, capped redraw cadence |
| Old-session callback | Wrong agent shown in new session | Medium | Existing generation lock plus cleared monitor cache/selection |
| Duplicate final text or tool-call JSON | Misleading preview | Medium | Explicit delta/replacement/final-summary tests, prose-only extraction |
| Timeout/cancellation ambiguity | Incorrect success/stopped claim | Medium | Explicit statuses; exactly-once cleanup; external state remains unknown |
| Untrusted response rendering | Markup/control injection or disclosure | Medium | Literal rendering, sanitization, allowlist, compatible redaction |
| Tool-result preview disclosure | Sensitive output visible in child panel | Medium | Match main TUI details toggle; use bounded display projection, redact, omit args/rawResult/evidence/images |
| Stale or forged conversation click target | Wrong child preview or action injection | Medium | Trusted metadata, current generation/membership validation, real click tests |
| Research unavailable | Unreviewed concurrency trade-offs | Medium | External research agent failed to load; require targeted independent implementation review |

### Project Acceptance Criteria

- [ ] Current-session direct children are discoverable even after panel opens late.
- [ ] Selected child's bounded response and enabled tool-result timeline update live,
  show final output, and obey the main TUI details-visibility setting.
- [ ] Parallel streams remain isolated from each other and the parent's transcript.
- [ ] Running count and terminal states remain correct under races and cancellation.
- [ ] Panel has no execution controls; `/subagents` behavior is unchanged.
- [ ] Clicking a child entry in the main conversation opens its exact preview;
  concurrent, wrapped and stale targets are handled safely.
- [ ] Rebinding rejects stale state; memory and update queues remain bounded.
- [ ] Tool results appear only through the bounded allowlisted main-TUI display
  projection and obey its detail-visibility toggle; no reasoning, args, `rawResult`,
  evidence/image payloads or arbitrary tool objects enter previews.
- [ ] Untrusted response/result text is sanitized and rendered literally.
- [ ] Independent review and full test/lint/snapshot verification pass.

### Estimated Timeline

Approximately 4–6.5 working days for one implementer, including 0.5–1 day for
click-entry behavior and bounded tool-result observation, with sequential milestones:
lifecycle registry → live observation → TUI → review/regression gates. Confidence
is medium; stream extraction and concurrency edge cases drive the uncertainty.
Research delegation failed with `Loading model`; alternatives above were assessed
from repository inspection, not a completed external research report. A subsequent
focused design comparison for clickable entries completed and informed ADR-003;
installed-version action dispatch still requires implementation-time verification.

## Deferred roadmap: Cooperative subagent policies and replaceable process sandbox

**Status: Deferred.** This is future work, not the current implementation target.
Keep it as an architectural roadmap following completion of the responsive TUI
and independently owned runtime project above, preserving the existing monitor.
Begin sandbox implementation only after the user explicitly re-prioritizes it.

### Goal

In `COOPERATIVE ON`, the main agent is human-supervised; child agents run as
separate policy-restricted workers, ask the main model questions, and request
additional capabilities through the main agent plus explicit human approval.
`COOPERATIVE OFF` retains the existing autonomous parent/child execution path.

The user explicitly selected application policy → separate process → later OS
hardening, superseding the earlier request for an immediately OS-enforced sandbox.
The MVP is **operational process isolation and application policy enforcement**,
not an OS security sandbox. This distinction must be visible in configuration,
diagnostics and documentation; neither a new process nor a private cwd prevents
arbitrary code from accessing same-user host files or networking.

### Context

`one/core/agent_session.py` currently creates children in-process with shared
provider objects, working directory, settings/cooperation state, approval callback
and MCP manager. Child `ask_user` is auto-answered with best judgment. The main
loop awaits `spawn_subagent`, so enqueueing a question for its ordinary next turn
would create a circular wait. `one/tools/bash.py` launches an unrestricted shell;
`one/tools/common.py` resolves paths without project containment. No real OS sandbox
exists. Existing child capabilities may expand through built-in validation and MCP
registration. The live monitor project above stays intact and observes this new
execution path; it does not become a permission or approval authority.

### Scope

#### In Scope

- Separate workers for ON-mode children and a replaceable sandbox/backend API.
- Policies for filesystem, network, secrets, processes and tools.
- Default project read access, private scratch writes and restricted scratch commands.
- Parent-model answers to child questions, serviced while spawn is pending.
- Explicit per-child/per-task grants proposed by the main model and approved by human.
- Scoped network/MCP/command additions; external host effects stay parent-mediated.
- Safe ON/OFF transitions, cancellation, bounded IPC and monitor integration.

#### Non-Goals

- Claiming the Application backend is an OS-enforced filesystem/network boundary.
- Implementing Docker, native Windows or native macOS isolation in this MVP.
- Installing sandbox tools or changing host security configuration automatically.
- Persistent grants, cross-session grants, auto-approval or unrestricted child host shell.
- Changing the existing autonomous OFF path or adding nested protected-worker spawning.
- Expanding Windows support claims without platform verification/CI.

### Assumptions

- Default scratch commands use an explicit execution policy, not arbitrary host Bash.
  Broader Bash is an additional scoped capability; a cwd alone is not confinement.
- Every grant-expanding operation requires a main-agent decision and human approval.
  Approval specifies capability scope and whether it covers one action or the remaining
  task; a general grant cannot silently authorize unrelated host effects.
- Provider API access is an infrastructure channel brokered by the host, not a grant
  of general outbound network access to the worker.
- ON → OFF does not remove policy from existing workers. OFF → ON stops/reaps existing
  unprotected children before protected mode becomes active; failed cleanup blocks
  activation and new spawns rather than claiming protection.
- Project read access does not deliberately include `.one/`, one runtime state,
  credential files or secret-bearing repository files. Exclusions require explicit
  configuration and tests; application checks do not prevent OS-level bypass.

### Open Questions

- Default command allowlist and project-secret exclusion list must be finalized in
  S1 with documented examples; unknown tools/commands are denied or escalated.
- Windows descendant termination/resource limits remain best-effort until validated.
- Backend-specific resource guarantees and stronger OS enforcement are future work.

### Tech Stack

- **Python 3.12+ / asyncio:** supervisor, broker, worker, bounded subprocess pipes.
- **Existing provider adapters:** host-owned authentication and proxied model calls.
- **Existing approval interfaces:** human decisions in TUI, interactive, run and RPC.
- **pytest:** stubbed provider/MCP tests plus local worker-process integration tests.

### Constraints

- Keep OFF behavior and existing tool/session/event contracts unless a reviewed
  additive protocol/API is introduced. Do not reuse UI monitor buffers as IPC.
- No auth storage, provider clients, MCP transports or extension runtime in workers.
- Capability validation is enforced on actual dispatch, not just prompt/tool visibility.
- Reject unsupported required enforcement strength; never silently substitute a weaker
  backend. The user-selected Application backend must advertise its limitations.

### Architecture

`Sandbox` composes immutable `FilesystemPolicy`, `NetworkPolicy`, `SecretsPolicy`,
`ProcessPolicy` and a validated tool/command capability set with an execution backend.
Each backend reports its capabilities and enforcement level (`application`,
`os-enforced` or stronger), availability and unsupported requirements. First implement
`ApplicationSandboxBackend`; reserve interfaces for `DockerSandboxBackend`,
`WindowsSandboxBackend` and `MacSandboxBackend` without speculative placeholder code.

`ProcessSandbox` supervises an exec-launched worker using `spawn`, `sendTask`,
`terminate`, timeout/cwd/environment configuration and result/exit observation.
Use Python naming conventions for concrete APIs. Cwd/environment setters configure
a validated pre-spawn spec only; timeout setters may tighten running limits but cannot
relax them outside the grant protocol. Never silently mutate a running worker policy.
Prefer one portable implementation with thin Linux/Mac/Windows adapters only where
process-tree termination and startup behavior differ.

Worker startup is fresh exec (no fork-inherited credentials), minimal environment,
allowlisted descriptors, private scratch directory, sanitized model/task/settings
snapshot and no host auth/bootstrap discovery. Resolve read paths against project
scope and write paths against scratch; distinguish host project vs scratch paths.
Filesystem tools enforce containment and exclusions, including symlinks. Where
practical use descriptor-based operations to reduce TOCTOU; document remaining
application-layer limitations. Never recursively expose whole home/config directories.

The worker runs child reasoning through a provider proxy over bounded stdio IPC.
Only the host injects provider auth. Commands execute in the worker's scratch with
an explicit allowlist and filtered environment; default policy denies direct MCP,
extensions and network tools. Arbitrary sandbox Bash under the Application backend
can bypass Python-level checks: refuse it as a default capability, surface that risk
for an explicitly approved broader grant, and require an OS backend when hard
filesystem/network guarantees are requested.

Choose one versioned framing format: a size-prefixed UTF-8 JSON frame over pipes,
not mixed JSONL plus Content-Length. Validate length before allocation, protocol
version, frame type, child/task/request identity, sequence and schema. Protocol-only
stdout; bounded, sanitized stderr separate from conversation. Provisional caps:
1 MiB frame, 64 queued requests per worker and 64 KiB stderr tail, with bounded
stream chunks for large results. Excess critical requests fail explicitly; never
drop approval/answer/cancel/result frames to make room. Define cancellation and
duplicate/late-result handling before introducing new event payloads.

A parent-owned `SubagentRequestBroker` handles `question`, `capability_request` and
`external_action_request` while spawn awaits workers. Parent-model decision calls
use a separate serialized decision lane and immutable parent-context snapshot, with
bounded untrusted child question/evidence. They do not recursively call parent
`prompt`, mutate primary messages concurrently or execute arbitrary model-produced
tools. The primary loop remains suspended at spawn; the independent broker services
decisions so children do not wait for its next ordinary turn. If a genuine human
decision is needed, escalate explicitly through the existing human channel.

Main model answers questions or proposes scoped actions/grants. Human approval binds
the canonical final effective operation after permitted hook transformations, child ID,
task ID, grant revision, resources, limits and expiry. A capability grant only authorizes
its declared scope; host writes/commands/network/MCP effects are executed through a
parent-owned dispatcher, never unrestricted child tools. MCP grants expose named proxy
operations, not server connections/secrets. Approved network access uses a scoped host
proxy; no child credentials or general network access is introduced by model proxying.
Changing mount/OS policy later may require backend restart or a restricted per-command
process; backend reports this rather than pretending rules changed in place.

Grant records are immutable, parent-owned and expire with the task or earlier expiry;
workers cannot self-grant or widen tool lists. Revocation blocks new requests. Pending
actions have correlation IDs and execution state: deny/cancel before execution, return
known outcome when executed, report `externalState: unknown` on lost acknowledgment.
Do not retry non-idempotent actions automatically. Timeout/model failure is no permission
and no fabricated best-judgment answer. Abort rejects outstanding requests and reaps
workers/descendants with bounded grace; retain evidence without putting secrets in logs.

### Architecture Decisions

#### ADR-S01: Replaceable policy and execution layers

**Decision:** Application policies and a separate exec worker now; backend capability
contracts from day one for future container/native OS isolation.
**Alternatives:** Immediate Linux-only bubblewrap; OCI-only workers; tool restrictions
without a process; separate incompatible per-platform agent implementations.
**Rationale:** Matches the user's portability-first MVP while preserving an upgrade path.
**Trade-offs:** Process crash/memory separation is real, but filesystem/network protection
is application-mediated and bypassable by arbitrary code at the same OS identity.
**Consequences:** Advertise actual guarantees; deny unmet strong requirements; do not
recommend disabling cooperation as the remedy for a failed protected spawn.

#### ADR-S02: Full worker reasoning with host provider and request broker

**Decision:** Child AgentSession runs in a fresh worker, uses a provider proxy, and
requests parent decisions through an independent broker with a serialized decision lane.
**Alternatives:** Parent-side child reasoning with isolated tool executor; suspending and
checkpoint/restarting child sessions; reusing the parent's ordinary next turn.
**Rationale:** Full workers align with the requested lifecycle API and isolate crashes;
the broker resolves parent-await/child-await cycles without replaying tool execution.
**Trade-offs:** More IPC/session cleanup complexity than a tool executor; model calls
cost tokens and must obey global budgets and adapter concurrency constraints.
**Consequences:** No worker auth discovery, mutable parent-context access or recursive
spawn from the broker; question and decision failures are explicit and bounded.

#### ADR-S03: Human-approved task-scoped capability leases

**Decision:** Main model proposes; human approves immutable scope for one child/task.
**Alternatives:** Child directly prompts human; main model alone grants privileges;
global tool enablement or persistent grants.
**Rationale:** Preserves human-supervised external actions without silently broadening
child autonomy or giving it host secrets/MCP clients.
**Trade-offs:** More approval steps; a broad lease needs clear risk/scope presentation.
**Consequences:** A lease is not TOCTOU protection by itself. Validate canonical effective
actions and current policy at dispatch; no external effect before approval.

### Phases

#### Phase S1: Policy/backend contracts and authority model

**Objective:** Define enforceable MVP capabilities and extensible backend API.
**Prerequisites:** Approved user semantics above.
**Expected outcome:** Validated specs distinguish guarantees and deny escalation by default.
**Estimated effort:** 2–3 days. **Confidence:** Medium.

- [ ] **Task S1: Introduce immutable policies and backend protocol**
  - **Description:** Define policy specs, task-scoped grant schema, backend capabilities,
    availability/strength checks, pre-spawn setters and mode-selection routing. Finalize
    scratch command allowlist, path mapping and secrets exclusions. Explicitly prevent
    tools/MCP auto-registration from exceeding child permissions.
  - **Files:** Proposed `one/core/sandbox/policy.py`, `one/core/sandbox/backend.py`,
    `one/core/sandbox/__init__.py`, `one/core/settings_manager.py`, `tests/test_sandbox_policy.py`.
  - **Dependencies:** None.
  - **Acceptance Criteria:** Invalid/unknown scopes are denied; grants cannot outlive
    task; unsupported OS guarantees rejected; OFF defaults unchanged.
  - **Verification:** Future `.venv/bin/python -m pytest -q tests/test_sandbox_policy.py tests/test_settings.py`;
    malicious tool list/MCP registrations and alias/path cases.

#### Phase S2: Portable process lifecycle and bounded IPC

**Objective:** Launch credential-minimized workers with reliable host supervision.
**Prerequisites:** S1.
**Expected outcome:** Task/result transport works without host bootstrap in workers.
**Estimated effort:** 3–5 days. **Confidence:** Medium.

- [ ] **Task S2: Build Application backend, supervisor and protocol**
  - **Description:** Implement validated fresh-process launch, private scratch, filtered
    env/fds, framing/handshake/backpressure, bounded stderr, result correlation, graceful
    termination and descendant cleanup. Add thin platform adapters only for actual
    differences. Never share auth/MCP objects or inherit protocol-unsafe stdout logs.
  - **Files:** Proposed `one/core/sandbox/application_backend.py`,
    `one/core/sandbox/process.py`, `one/core/sandbox/protocol.py`,
    `one/core/subagent_worker.py`, `tests/test_sandbox_process.py`, `tests/test_sandbox_protocol.py`.
  - **Dependencies:** S1.
  - **Acceptance Criteria:** Malformed/oversized frames rejected without unbounded
    allocation; worker crash and timeout resolve parent waits; repeated termination
    safe; scratch cleanup defined; environment/descriptors carry no seeded secrets.
  - **Verification:** Local disposable process tests on Linux/macOS; Windows CI/manual
    evidence before claiming support. Test stdin EOF, stderr flood, protocol pollution,
    process grandchildren, cancellation during startup and no lingering handles.

#### Phase S3: Worker tools and provider proxy

**Objective:** Preserve child reasoning while enforcing application scopes.
**Prerequisites:** S2.
**Expected outcome:** Worker can read allowed project data, write scratch and run default
commands; uses host model transport without credential delivery.
**Estimated effort:** 3–5 days. **Confidence:** Medium.

- [ ] **Task S3: Wire child execution and scoped tool dispatch**
  - **Description:** Add credential-free worker initialization/provider adapter, host
    provider service with bounded streaming and parent budget accounting, project/scratch
    filesystem gates and restricted command execution. Disable raw MCP/extensions and
    nested protected spawn; don't run default shell commands with unrestricted host env.
  - **Files:** `one/core/agent_session.py`, `one/tools/index.py`,
    proposed `one/core/sandbox/tool_policy.py`, `one/providers/worker_proxy.py`,
    `one/core/subagent_worker.py`, `tests/test_sandbox_tools.py`, `tests/test_subagents.py`.
  - **Dependencies:** S2.
  - **Acceptance Criteria:** Actual tool dispatch rejects path escapes/secret reads/
    forbidden tools; read project/write scratch work; providers see correct child model
    settings; worker receives no seeded API/OAuth values. Document that bypass via
    arbitrary OS code remains possible under Application backend.
  - **Verification:** Stubbed provider tests, symlink/`..`/absolute-path races, explicit
    forbidden tool/MCP calls, denied network commands, environment leakage tests;
    `.venv/bin/python -m pytest -q tests/test_sandbox_tools.py tests/test_subagents.py`.

#### Phase S4: Questions, capability grants and external-action mediation

**Objective:** Resolve child requests without circular waits or unsafe approval bypass.
**Prerequisites:** S3.
**Expected outcome:** Main model answers questions and human approves scoped escalations.
**Estimated effort:** 4–6 days. **Confidence:** Medium.

- [ ] **Task S4: Add correlated request broker and approval-bound grants**
  - **Description:** Replace ON child's best-judgment fallback with typed parent request;
    service it during spawn via serialized snapshot-based model decisions. Implement
    grant/action schemas, bounded waits, parent execution after final-args validation
    and human approval, scoped MCP/network proxies, idempotency states and revocation.
    Preserve direct OFF flow. Model answer is not authorization; child cannot self-grant.
  - **Files:** Proposed `one/core/subagent_request_broker.py`,
    `one/core/sandbox/grants.py`, `one/core/sandbox/action_dispatch.py`,
    `one/core/agent_session.py`, `one/core/subagent_worker.py`, `tests/test_subagent_broker.py`,
    `tests/test_approval.py`, `tests/test_ask_user.py`.
  - **Dependencies:** S3.
  - **Acceptance Criteria:** Child question reaches main model while primary loop awaits
    child; parallel requests stay correlated; no external action before explicit human
    grant; changed effective args require new approval; rejection/timeout produces no
    side effect; lost result cannot trigger blind retry; task completion revokes grants.
  - **Verification:** Fake model/human/MCP services; pending parent ask_user plus child
    question; cancellation, repeated IDs, spoofed child/task, hook-modified args, proxy
    scope violations, revoked leases and duplicate external action requests.

#### Phase S5: Mode transitions and human-facing status

**Objective:** Apply ON/OFF semantics safely across running children and interfaces.
**Prerequisites:** S4.
**Expected outcome:** No unprotected child remains active after completed OFF → ON.
**Estimated effort:** 2–4 days. **Confidence:** Medium.

- [ ] **Task S5: Add transition barrier and integrated observation**
  - **Description:** Serialize toggles/spawns; OFF → ON enters transition, stops/reaps
    unprotected children, then activates policy mode (failed reaping blocks activation).
    ON → OFF changes future spawns only, retains worker policy and rejects pending
    privilege expansions. Keep provider IPC for existing workers operational. Report
    question/approval/worker status through safe additive events and `/agents`; bind
    approvals to child identity without consuming main prompt input.
  - **Files:** `one/core/agent_session.py`, `one/core/agent_session_runtime.py`,
    `one/core/subagent_monitor.py`, `one/modes/tui_mode.py`, `one/modes/interactive_mode.py`,
    `one/modes/rpc_mode.py`, `one/modes/run_mode.py`, `tests/test_tui_cooperation.py`,
    `tests/test_subagent_mode_transitions.py`, `tests/test_rpc_mode.py`.
  - **Dependencies:** S4.
  - **Acceptance Criteria:** Toggle/spawn races cannot bypass protected routing;
    old workers never gain privileges through parent OFF; broker/cancel/unmount paths
    release waits; monitor and approvals show correct child scope; autonomous OFF
    regression tests unchanged.
  - **Verification:** Simultaneous toggle/spawn/pending-question/approval tests; stubbed
    worker failure-to-reap; TUI pilots and RPC tests; no real state/provider/server access.

#### Phase S6: Security review and release gates

**Objective:** Verify behavior and publish honest backend guarantees, not security claims.
**Prerequisites:** S1–S5.
**Expected outcome:** Reviewed MVP with explicit future OS-hardening boundary.
**Estimated effort:** 2–3 days. **Confidence:** Medium.

- [ ] **Task S6: Document guarantees and run adversarial/platform verification**
  - **Description:** Independent security/concurrency review; docs describing scopes,
    escalation approval, default command policy, process-only limitations and platform
    termination support. Bump then-current version/add Unreleased entry/regenerate TUI
    snapshots according to repo policy; document how future backends plug in without
    implementing them. Fix blocking findings before integration.
  - **Files:** `README.md`, `docs/architecture.md`, `CHANGELOG.md`, `one/config.py`,
    `tests/test_tui_snapshots.py`, `tests/snapshots/tui/agents.txt`,
    `tests/snapshots/tui/base.txt`, `tests/snapshots/tui/overlay.txt`,
    `tests/snapshots/tui/widget_panel.txt`, S1–S5 files as needed.
  - **Dependencies:** S5.
  - **Acceptance Criteria:** No unresolved blocking review findings; no claim of OS
    filesystem/network/secret containment with Application backend; platform evidence
    matches support claims; full OFF and ON regression suites pass.
  - **Verification:** Future `.venv/bin/python -m pytest -q`, `.venv/bin/ruff check .`,
    `git diff --check` in order; platform local-worker tests, adversarial IPC/policy
    matrix and reviewed regenerated snapshots. No real MCP/provider credentials.

### Rollout & Rollback

Expose configured backend and enforcement strength before protected spawn; default
ON uses the explicitly documented Application MVP, OFF legacy path is unchanged.
No automatic installation, grants migration or broad downgrade. Pending grants are
ephemeral; workers must be terminated before rolling back their protocol/policy code.
Runtime state and completed evidence are preserved. Feature branches and explicit
user authorization are required for commits/merges/pushes/releases.

### Observability

Add identity-correlated worker lifecycle, policy level, question state, grant scope,
approval and action outcome diagnostics; reuse monitor text limits/coalescing.
Log decisions and IDs, not raw credentials, full IPC payloads or unrestricted stderr.
Distinguish intentional child model-proxy connectivity from network capability grants.

### Security Considerations

The parent is a privileged confused-deputy boundary: every worker request is untrusted,
schema-checked and scoped by current task/grant identity. Application checks and frozen
objects alone do not prevent arbitrary-code filesystem/network access, TOCTOU or
credential-file reads at the same OS identity. No credential values are intentionally
passed via env/IPC, but hard non-access guarantees require a stronger backend.
Process groups and Windows Job Objects alone are not filesystem/network sandboxes.
Unknown capabilities are denied, timeouts do not authorize work, approval is bound to
final effective arguments, and ambiguous external outcomes are never auto-replayed.

### Risks & Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Process-only MVP called a security sandbox | False safety expectations | High | Explicit backend strength/limits in UI/config/docs |
| Child waits on parent's suspended turn | Deadlock | High | Independent bounded broker decision lane, no recursive prompt |
| Broker privileges abused by child | Host side effects or data exposure | High | Typed scoped requests, main review, final-args human approval |
| Command or symlink bypasses application policy | Host file/network access | Medium | Default command allowlist/path gates; document limits; stronger backend required for hard guarantee |
| Toggle races/shared old callback authority | Ungated actions | Medium | Transition barrier, task-scoped policy separate from parent mode |
| IPC flood/stderr contamination | Resource exhaustion or parser corruption | Medium | Size-first framing, bounded queues/chunks, explicit backpressure/errors |
| Worker survives timeout or host shutdown | Unexpected continued work | Medium | Descendant supervision, bounded reap, honest unknown external state |
| Unknown outcome retried | Duplicate external effect | Medium | Correlated execution state, no blind retries |

### Project Acceptance Criteria

- [ ] ON spawns separate workers using explicit advertised backend enforcement level.
- [ ] Default tools read approved project scope and write/run permitted commands only
  in scratch through application gates, with documented OS bypass limitations.
- [ ] Workers receive no intentionally supplied credentials/MCP/extension handles.
- [ ] Child questions reach the main model during pending spawn without deadlock.
- [ ] Privilege expansions are human-approved, bounded to one child/task and revocable.
- [ ] External effects execute through parent authority under canonical approval.
- [ ] ON → OFF keeps existing policies; OFF → ON reaps unprotected children first.
- [ ] OFF behavior remains compatible and fully autonomous as previously implemented.
- [ ] IPC, cancellation, process lifetime and monitor buffers remain bounded.
- [ ] Security/concurrency review and platform/full test gates pass.

### Estimated Timeline

Approximately 16–26 implementer working days plus platform/security review uncertainty:
contracts → worker/IPC → scoped tools/provider proxy → broker/grants → transitions/UI
→ verification. Medium confidence; grant mediation, Windows cleanup and tool-policy
compatibility drive uncertainty. Future Docker/native OS hardening is not included.
Research compared full workers vs isolated executors, broker vs parent-loop suspension,
and native/container backends. Recommendations were synthesized against inspected
repository contracts; speculative backend guarantees/benchmarks were not adopted.

## Release readiness

- [ ] **Choose the next maturity target and support contract.** Decide whether
  the next public release remains alpha, becomes beta, or is intended to be
  stable; define supported Python/platform versions and compatibility promises
  for the SDK, tool contracts, settings, and persisted sessions.
- [ ] **Complete a fresh release-readiness audit** for the selected candidate:
  full test/lint/build/package checks, installed wheel and sdist verification,
  archive-content review, redacted full-history secret scan, dependency and
  license/fixture review, and maintainer confirmation of GitHub security and
  branch-protection settings. Follow [`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md).
- [ ] **Verify release automation before creating a tag.** The current
  `.github/workflows/release.yml` workflow runs on `v*` tags and publishes the
  package to PyPI as well as creating a GitHub Release. Confirm the PyPI trusted
  publishing environment and ensure the version, tag, changelog, classifiers,
  README, and maturity claims all agree.

## Engineering and maintenance

- [ ] **Decide Windows support scope.** Linux and macOS are covered by CI;
  Windows remains best-effort with no Windows CI. If Windows 11 becomes an
  officially supported platform, add CI and verify installation, sessions,
  process/timeouts, and basic TUI behavior before changing the support claim.
- [ ] **Choose a dependency-security audit policy** and add repeatable automated
  scanning (for example, `pip-audit`) with an explicit exception/update policy.

## Backlog policy

Only prioritized, evidence-backed work belongs in this file. Historical design
specifications and completed checklists were removed; they remain retrievable
from the repository's Git history. Add future proposals as GitHub issues when
they are accepted for prioritization.
