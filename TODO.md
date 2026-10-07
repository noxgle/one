# Active roadmap for `one`

This file contains only current, verified work. Completed implementation history
is available in [GitHub commit history](https://github.com/noxgle/one/commits/main/);
user-facing changes are recorded in [`CHANGELOG.md`](CHANGELOG.md). Do not keep
completed task logs or unprioritized feature proposals here. Record accepted
future work as GitHub issues.

## Project: Current-session live subagent monitor

**Current focus:** polish and verify the monitor already under development on
`feat/tui-live-subagent-monitor`. The cooperative sandbox project below is deferred;
do not start its implementation as part of this active monitor work.

### Goal

Provide a read-only TUI view of direct subagents of the current parent session,
including live response text, without switching the conversation or changing
agent execution. Scope approved by the user.

### Context

`one/core/agent_session.py` creates children in `_run_subagent` and emits
`subagent_start` / `subagent_end`, but has no authoritative child registry.
Its child listener records activity without forwarding response text.
`one/modes/tui_mode.py` exposes subagent availability, not running children.
Its session listener queue already protects against stale events on rebinding.

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
Keep it as an architectural roadmap only while the active priority is polishing and
verifying `Current-session live subagent monitor` above. Begin sandbox implementation
only after the user explicitly re-prioritizes it.

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
