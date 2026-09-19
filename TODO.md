# TODO — Roadmap for the autonomous agent `one`

Completed work is recorded in [`DONE.md`](DONE.md). User-facing release notes
are in [`CHANGELOG.md`](CHANGELOG.md).

## Public release review (pre-GitHub, read-only audit 2026-09-12)

Full doc-vs-code audit on post-merge `main`, worktree clean. Secrets scan clean
(placeholders/fakes only; `.one/` untracked and gitignored). All P1 items were
implemented and verified in build mode: full suite 1042 passed, ruff clean.
P0 (maintainer decisions: contact, repo+tag) and P2 remain open.

### P0 — blockers (must fix before anything public)

- [x] **P0-1:** Replace the `TODO-MAINTAINER: security@example.com` placeholder in
  `SECURITY.md:17-18` and `SECURITY.md:89-90` with a real reporting channel
  (GitHub Security Advisories URL already at `SECURITY.md:16`, or a real
  maintainer email). Two occurrences.
  - **Details:** Both occurrences now use `s.wielgosz@noxgle.com`; committed in `883b9a6`.
- [ ] **P0-2:** Do not publish until the repo exists: no `origin` remote, no tags.
  `README.md:3` CI badge, `README.md:39` clone URL, and `pyproject.toml:65-69`
  project URLs all point at `github.com/picon/one` (404 until push).
  Decide: create repo + push + tag `v0.1.0` (must equal `one/config.py:VERSION`),
  or keep everything local.
- [x] **P0-3:** Fix `CHANGELOG.md` release-state: `## [0.1.0] — 2026-08-30` claims a
  release but no `v0.1.0` tag exists (violates `.github/workflows/release.yml:43-51`
  tag==version gate). Either retitle to `Unreleased` or create the tag.
  Move `## Unreleased` above `## [0.1.0]` (Keep-a-Changelog order; currently at line 43).
  - **Details:** Tag path chosen: annotated tag `v0.1.0` created on `883b9a6`,
  matching `one/config.py:VERSION = "0.1.0"`. `## Unreleased` moved above
  `## [0.1.0]` per Keep-a-Changelog (uncommitted follow-up with the contact change).

### P1 — wrong or missing docs (code is correct, docs lie)

- [x] **P1-1:** `README.md:204` "Codex provider rejects images" is false since Task 10:
  Codex sends `input_image` parts (`one/providers/codex_responses.py:97-118,164`;
  `one/core/model_registry.py:66-71`). Document support + `MissingBlobError` behavior.
- [x] **P1-2:** `README.md:210-211` Ctrl+A cooperation is half wrong: TUI is now
  `Ctrl+Z` (`one/modes/tui_mode.py:134,750,2539`); interactive stays `Ctrl+A`
  (`one/modes/interactive_mode.py:134,461,531`). Split per-mode. Same for
  `CHANGELOG.md:15` historical note → add `Unreleased` entry for Ctrl+A→Ctrl+Z.
- [x] **P1-3:** `README.md:264` `/retry <on|off>` → `<on|off|unlimited>` and add the
  missing `/retry-cycle` row (`one/modes/tui_mode.py:1436,1847-1871`;
  `one/core/settings_manager.py:204-216`).
- [x] **P1-4:** `one/modes/interactive_mode.py:529` help line `/retry <on|off>` is
  stale → `/retry <on|off|unlimited> | /retry-cycle` (TUI `:1436` already correct).
- [x] **P1-5:** `README.md:262` `/login` form is wrong → use
  `/login [status|refresh <provider>|provider [apiKey] [model] [subscription]]`
  (matches `tui_mode.py:1432,2613`, `interactive_mode.py:528`).
- [x] **P1-6:** `README.md:372` `--extensions <path>` → singular `--extension <path>`
  (`one/cli/args.py:134`; `docs/EXTENSIONS.md:27` already correct).
- [x] **P1-7:** `AGENTS.md:26` `/login` omits `status|refresh|subscription` → full form.
- [x] **P1-8:** `AGENTS.md:35` "12 tools" → 13 (`one/tools/index.py:30-32` has
  `read_image`; `CONTRIBUTING.md:35` and `CHANGELOG.md:47` already say 13).
- [x] **P1-9:** `AGENTS.md:15` "pytest is the only gate" contradicts
  `CONTRIBUTING.md:41-42` and `.github/workflows/ci.yml:9-23` (ruff IS a CI gate).
- [x] **P1-10:** `AGENTS.md:5` package name `one` vs `pyproject.toml:6`
  `one-agent` → distribution `one-agent`, import/script `one` (as `README.md:56`).
- [x] **P1-11:** Stale test counts (`AGENTS.md:10`, `CONTRIBUTING.md:14` say ~790;
  actual 1042) → drop exact numbers or recount.
- [x] **P1-12:** `scripts/install.sh:26-31` needs a source checkout but
  `README.md:388-392` implies it creates config in `~/.config/one` (config is
  lazy-created on first run, `one/config.py:74-96`). Clarify both.
- [x] **P1-R1:** Reviewer pass on Task 16 (backspace `prevent_default` fix) mirroring
  Review 1 checklist (event propagation, Textual `>=0.74.0` compat, no regressions).

### P1 — missing docs for shipped behavior (add to README/CHANGELOG)

- [x] **P1-13:** `maxSteps=0` = unlimited tool steps (`agent_session.py:1532-1541`,
  `settings_manager.py:358`) — only in `TODO.md` Open Questions today.
- [x] **P1-14:** `tool start (timeout Ns)` TUI rendering (`agent_session.py:785`,
  `tui_mode.py:2756-2760`) — note it is TUI-only.
- [x] **P1-15:** Version display (TUI sidebar `tui_mode.py:1307`, CLI banner
  `cli/main.py:467`, `--version`).
- [x] **P1-16:** TUI shortcut table/pointer (`Ctrl+R`, `Ctrl+Shift+V` terminal
  text paste, `Ctrl+Alt+V` image paste, `Ctrl+V`, `Ctrl+Z` from
  `TUI_SHORTCUTS`, `tui_mode.py:129-142`).
- [x] **P1-17:** `CHANGELOG.md` Unreleased: add all user-visible tui-ux-batch changes
  (retry unlimited + `/retry-cycle` + `Ctrl+R`, Codex `input_image`, `Ctrl+Z`,
  `Ctrl+Shift+V`, `maxSteps=0`, timeout display, `/login [subscription]` help,
  version display, paste/spinner/backspace/duplicate-render fixes).
- [x] **P1-18:** `README.md:499-505` doc list omits `docs/RELEASE_CHECKLIST.md`;
  `MANIFEST.in` omits it too — include it or state intentional exclusion.

### P2 — nice-to-have before public

- [ ] **P2-1:** `.gitignore:9` `.config/one/` never matches `~/.config/one` — comment or drop.
- [ ] **P2-2:** Windows classifier (`pyproject.toml:37`) vs best-effort status with no
  Windows CI — keep with experimental note or drop classifier.
- [ ] **P2-3:** Decide what of `TODO.md:236-342` (NO-GO audit, estimates) stays public;
  confirm `LICENSE:3` holder string `Copyright (c) 2026 picon`.
- [ ] **P2-4:** De-duplicate `AGENTS.md` vs `CONTRIBUTING.md` overlap (setup/tests/
  architecture drifted already); make AGENTS canonical for agents, CONTRIBUTING for
  humans, cross-link.

### Verified clean (no action needed)

Auth precedence `runtime > stored > env` consistent across docs and
`one/core/auth_storage.py:174-184`. Steer/follow-up text-only and print
image-only rejects match code. Seed Codex models match registry. CI matrix
(ubuntu+macos × 3.12/3.13) matches README and `requires-python`. `release.yml`
tag-gate + disabled publish as documented. Secrets scan clean
(placeholders in `README.md:87,93,289` + test fakes only; no `/home/picon` in
tracked files). `MANIFEST.in` packaging claims verified clean.

## Next engineering work

- [ ] **Normalize provider-native tool calls.** Evaluate a common adapter layer
  for native OpenAI-compatible, Anthropic, and Gemini function/tool-call
  payloads. The current JSON-in-text parser remains the production contract
  until this work is designed, implemented, and covered by compatibility tests.
- [ ] **Windows shell support and CI.** Add Windows CI and either implement
  equivalent shell/process-group semantics or explicitly limit shell features
  on Windows. Until then Windows remains best-effort/experimental.
- [ ] **Dependency security audit automation.** Add `pip-audit` (or an
  equivalent reproducible vulnerability scanner) to local release checks and
  CI after selecting its lockfile/allowlist policy.

## Project: OpenCode ports (`feat/opencode-ports`)

### Goal

Port selected OpenCode behaviors to `one` without changing its core contract
(JSON-in-text tool calls, synchronous session loop, flat text history, no
phases). Work happens on branch `feat/opencode-ports` (based on `main`);
no merge/push without approval.

### Scope

#### In Scope

- Package A (Tasks A1–A3): `invalid` tool catch-all, `<system-reminder>`
  per-read instruction injection, subagent recursion guard.
- Package B (Task B1, after A): fuzzy `edit` matching + inline diagnostics.

#### Non-Goals

- Native function-calling (kills local-model support; see "Next engineering work").
- Async session runtime / status API (sync loop is a deliberate testability choice).
- Structured message parts (would rewrite the event contract and snapshots).
- Plan-mode agent or any phase system (explicitly out of scope).
- Full per-agent permission framework (at most 1–2 targeted rules later, separate decision).

### Assumptions

- OpenCode behaviors are reimplemented natively, not copied (license/API drift).
- Every task keeps the event contract stable unless stated; fake-provider tests only.

### Open Questions

- Reminder budget for A2 in monorepos (dedup per turn + char cap TBD in design).
- Whether B1 shows the matched snippet in the tool result (recommended: yes).

### Package A (first)

- [ ] **Task A1:** `invalid` tool catch-all for malformed tool calls.
  - **Description:** Route unknown tool names (after lowercase repair attempt) to a
    synthetic tool result: "unknown tool `<name>`; available: ...". The model
    self-corrects next step via the normal loop — no new retry logic.
  - **Files:** `one/core/agent_session.py` (parse/dispatch path), tool-result tests.
  - **Dependencies:** None.
  - **Acceptance Criteria:** Unknown tool name yields a helpful tool result, never
    a crash or silent drop; valid calls unaffected.
  - **Verification:** Fake-provider tests (unknown name, case-variant repair, valid
    call control); `test_event_snapshots.py` green if contract touched.

- [ ] **Task A2:** `<system-reminder>` instruction injection on `read`.
  - **Description:** When `read` touches a file under a directory containing an
    `AGENTS.md`/`CLAUDE.md` not yet loaded this turn, append its content to the
    tool result as a `<system-reminder>` block. Deduplicate per turn; cap chars.
  - **Files:** `one/tools/` read path, `one/resources/resource_loader.py`,
    `one/core/agent_session.py` (turn-scoped claim set).
  - **Dependencies:** None.
  - **Acceptance Criteria:** Subdirectory instructions reach the model exactly once
    per turn; no reminder when already in system prompt; monorepo context bounded.
  - **Verification:** Fake-filesystem tests (first read injects, second doesn't,
    cap enforced); existing read/image tests green.

- [ ] **Task A3:** Subagent recursion guard.
  - **Description:** Forbid `spawn_subagent` inside a subagent (or enforce
    depth ≤ 1) with a clear model-facing error. Check current `maxDepth`/
    `maxConcurrent` semantics first — implement the minimal restriction.
  - **Files:** `one/tools/` subagent path, `one/core/agent_session.py`.
  - **Dependencies:** None.
  - **Acceptance Criteria:** Nested spawn fails fast with an explicit error; depth-0
    spawning unchanged.
  - **Verification:** Fake-provider nesting tests; existing subagent tests green.

### Package B (after A)

- [ ] **Task B1:** Fuzzy `edit` matching + inline diagnostics.
  - **Description:** Pass `oldString` through ordered fallback strategies (exact →
    line-trimmed → whitespace-normalized → indentation-flexible → fuzzy/Levenshtein)
    and report the matched snippet plus language diagnostics inline in the tool
    result. Never silently edit the wrong location: on ambiguity, fail with
    candidates shown.
  - **Files:** `one/tools/edit.py` (or equivalent), related tests.
  - **Dependencies:** Package A (shares tool-result conventions).
  - **Acceptance Criteria:** Minor whitespace/indent mismatches succeed; ambiguous
    matches fail loudly with candidates; diagnostics included where available.
  - **Verification:** Strategy-matrix tests (each fallback level + ambiguity case);
    full suite green.

### Risks & Mitigations

| Risk | Mitigation |
|------|--------|
| Reminder context bloat | Per-turn dedup + char cap |
| Wrong-location fuzzy edit | Fail-loud on ambiguity, show matched snippet |
| Parser behavior drift | Keep `_try_parse_tool_call` contract; snapshot tests |

### Project Acceptance Criteria

- [ ] Package A implemented on `feat/opencode-ports`, each task with tests.
- [ ] Package B implemented after A, with strategy-matrix tests.
- [ ] `ruff` clean, full `pytest` green, no unrelated behavior changes.
- [ ] No merge/push without explicit approval.

## Project: Prompt-cache performance (`perf/prompt-cache`)

### Goal

Cut per-step prefill cost and time-to-first-token so prompt processing feels as
fast as OpenCode. Work happens on branch `perf/prompt-cache` (based on `main`);
no merge/push without approval.

### Context

Read-only analysis (2026-09-12) confirmed three structural causes, all fixable
without rewriting the loop or the JSON-in-text contract:

1. No prompt caching anywhere, and every request carries fresh timestamps
   (`Current time: <iso>` at the START of the system prompt,
   `resource_loader.py:55`; `# Current Date … HH:MM` at the end,
   `agent_session.py:229`). The prefix changes every minute, so OpenAI-style
   automatic prefix caching (~1024 identical tokens) never hits — every step
   of every turn pays a full prefill. Anthropic has no `cache_control`
   breakpoints at all (verified: no `cache_control` in any adapter).
2. `_invoke_provider` buffers ~48 chars before rendering anything, delaying the
   first visible token even though the provider is already streaming.
3. Nudge and retry-after-failure are extra full-prefill provider calls.

Session `usage` already tracks `cacheRead`/`cacheWrite` — today `cacheRead` is
effectively always 0, which is the baseline to beat.

### Scope

#### In Scope

- Stable cacheable prefix (date without minutes, or clock moved behind the
  cache breakpoint) for OpenAI-compatible + Codex backends.
- Anthropic `cache_control` breakpoints (system block + conversation tail).
- Smaller/faster stream-flush threshold for prose (keep JSON-vs-prose detection).
- Nudge only when actually needed (measure first; change only if it fires often).

#### Non-Goals

- Native function-calling (kills local-model support).
- Changing the JSON-in-text contract, event payloads, or history flattening.
- Provider-specific system prompts (separate decision; keep one template).
- Any change to retry/backoff semantics or budgets.

### Assumptions

- Providers honor standard caching (OpenAI automatic prefix cache ≥1024 tokens;
  Anthropic `cache_control: {"type":"ephemeral"}` breakpoints).
- Timestamp precision finer than one day is not load-bearing for model quality
  (verify: date-only prompt must not degrade answers in regression tests).

### Open Questions

- Exact breakpoint placement for Anthropic (whole system vs split stable/dynamic).
- Whether Codex `prompt_cache_key` + stable prefix is enough without extra work.
- Flush threshold value that keeps tool-JSON suppression reliable (10 chars?).

### Tasks

- [x] **Task C1:** Stable cacheable prompt prefix.
  - **Description:** Remove sub-day clock precision from the cacheable prefix:
    date-only in `_build_header` / `# Current Date`, or move the live clock
    behind the cache breakpoint (end of system prompt). Keep human-readable date.
  - **Files:** `one/resources/resource_loader.py`, `one/core/agent_session.py`,
    prompt snapshot/assertion tests.
  - **Dependencies:** None.
  - **Acceptance Criteria:** Two prompts built a minute apart share an identical
    prefix through the end of the stable block; date still visible to the model.
  - **Verification:** Unit test comparing built prompts across a clock tick;
    existing prompt tests green.
  - **Details:** Date-only prefix (removes sub-minute `Current time:` drift); clock-tick stability test added comparing prompts built 60s apart — identical prefix confirmed, 6 tests.

- [x] **Task C2:** Anthropic `cache_control` breakpoints.
  - **Description:** Add `cache_control: {"type":"ephemeral"}` to the system block
    and (if within provider limits) the conversation tail in the Anthropic
    adapter. Count breakpoints against the provider maximum.
  - **Files:** `one/providers/anthropic.py`, adapter payload tests.
  - **Dependencies:** C1 (stable prefix maximizes breakpoint value).
  - **Acceptance Criteria:** Payload carries breakpoints; second turn on the same
    session reports `cacheRead > 0` against a fake transport echoing usage.
  - **Verification:** Fake-transport wire tests asserting breakpoint placement and
    simulated cache-hit accounting.
  - **Details:** System prompt + tail `cache_control: {"type":"ephemeral"}` added (list-shaped system block); usage mapping wired; 8 wire tests.

- [x] **Task C3:** Faster first-token flush.
  - **Description:** Lower the pre-render buffer threshold (measure JSON-detection
    reliability at ~10 chars) so prose paints earlier. Keep suppression of
    streamed tool-JSON intact.
  - **Files:** `one/core/agent_session.py` (`_invoke_provider` streaming path),
    streaming/suppression tests.
  - **Dependencies:** None.
  - **Acceptance Criteria:** Prose renders earlier; tool-JSON still never flashes
    as assistant text (existing suppression tests + new threshold tests).
  - **Verification:** Streaming unit tests with chunked tool-JSON vs prose.
  - **Details:** Flush threshold lowered from 48→16 chars; 10 adversarial chunk-split suppression tests added, all green.

- [x] **Task C4:** Measure and trim nudge usage.
  - **Description:** Instrument how often the step-0 nudge fires and whether it
    converts to a tool call; only then decide to narrow/keep it. No behavior
    change until data exists.
  - **Files:** Metrics/event counters only (no prompt changes yet).
  - **Dependencies:** None.
  - **Acceptance Criteria:** Nudge fire/convert rates known from test + manual runs.
  - **Verification:** Counter assertions in fake-provider tests.
  - **Details:** Nudge fire/convert counters added to stats + events; 1063 tests passed.

### Risks & Mitigations

| Risk | Mitigation |
|------|--------|
| Date-only degrades answers | Regression tests with time-sensitive prompts |
| Breakpoint misuse costs more | Count breakpoints; verify simulated accounting first |
| Flush shows tool-JSON flash | Threshold tests with adversarial chunk splits |

### Project Acceptance Criteria

- [ ] C1–C3 implemented on `perf/prompt-cache`, each with tests; C4 measured.
- [ ] Real-provider spot check shows `cacheRead > 0` on turn 2+ and lower TTFT
      (separately authorized run, never in automated tests).
- [ ] `ruff` clean, full `pytest` green, no unrelated behavior changes.
- [ ] No merge/push without explicit approval.

## Project: TUI/UX fix batch (`fix/tui-ux-batch`)

### Goal

Fix seven reported UX gaps without changing agent behavior otherwise:
Ctrl-C handling of approval prompts, version visibility, unlimited retry mode
with shortcut, Ctrl+Shift+V shortcut listing, Codex image support, visible
bash timeout in TUI, and the `/login` help text. Work happened on branch
`fix/tui-ux-batch`, merged into `main` as `d3aa680` (plus follow-ups through
Task 16 in `29a17a5`); the branch is kept for reference. No push without approval
(no remote is configured).

### Scope

#### In Scope

- Approval-prompt cancellation via Ctrl-C (TUI + interactive/headless paths).
- Version display in TUI and at CLI startup from `one/config.py` VERSION.
- Third retry mode (unlimited) + TUI shortcut; existing on/off preserved.
- Ctrl+Shift+V entry in the TUI shortcuts overlay.
- Image serialization in `codex_responses` provider (or explicit reject).
- Timeout display in TUI `tool start` lines for bash.
- `/help` text: `/login [status|refresh <provider>|provider [apiKey] [model] [subscription]]`.

#### Non-Goals

- Prompt/architecture redesign; phase system stays out.
- Changing approval semantics beyond cancellation (approvalTools unchanged).
- Enabling PyPI publishing or any remote git action.

### Assumptions

- `VERSION` in `one/config.py` remains the single version source.
- Retry setting persists via existing settings conventions.
- Codex Responses API supports image parts; if not, explicit reject is acceptable.
- TUI golden snapshots may need regeneration for intentional text changes.

### Open Questions

- `maxSteps=0` means unlimited tool steps independently of retry mode; retry
  mode only controls provider-call retry behavior.
- Which TUI surface shows the version (header vs sidebar vs footer)?
- Confirm the exact Codex Responses API image-part contract against the current
  upstream/OpenCode implementation before enabling the adapter path.

### Tasks

- [x] **Task 1:** Ctrl-C cancels pending approval prompts.
  - **Description:** Make Ctrl-C dismiss an open `[Approve] <tool> {...}` prompt in TUI and interactive/headless approval paths; treat as rejection with reason (e.g. "aborted by user") fed back to the model like other rejections. Must not kill the session.
  - **Files:** `one/modes/tui_mode.py`, `one/modes/interactive_mode.py`, approval-callback wiring, related tests.
  - **Acceptance:** Ctrl-C on a pending approval rejects the call, model receives the rejection, session stays usable.
  - **Verification:** New fake-approval regression tests; `.venv/bin/python -m pytest -q tests/test_tui_cooperation.py tests/test_approval.py`.
  - **Details:** `_on_key` override in `_OneTextualApp` intercepts `ctrl+c` when `_approval_pending` is set, puts `("no", "aborted by user")` into the approval queue and stops the event; interactive_mode already returns `(False, "aborted by user")` on Ctrl-C at line 138.

- [x] **Task 2:** Show app version in TUI and at CLI startup.
  - **Description:** Render `VERSION` from `one/config.py` in the TUI (header/sidebar/footer) and in CLI startup output (one-shot/interactive banner).
  - **Files:** `one/modes/tui_mode.py`, `one/cli/main.py`, TUI snapshot files if changed.
  - **Acceptance:** Version visible in TUI and CLI startup; single source of truth.
  - **Verification:** Rendering tests; regenerate snapshots with `ONE_UPDATE_SNAPSHOTS=1` only for intentional changes and review the diff.
  - **Details:** `one m {VERSION}` imported in both files; TUI sidebar renders `f"Version: {VERSION}"` (tui_mode.py:1193); CLI prints `one v{VERSION}` at startup (main.py:467).

- [x] **Task 3:** Unlimited retry mode + TUI shortcut.
  - **Description:** Add a third retry mode (unlimited provider-call retries) next to on/off, persisted in settings; add a TUI keyboard shortcut to cycle/switch retry mode without breaking existing bindings (incl. provider switching).
  - **Files:** settings manager, retry logic, `one/modes/tui_mode.py` bindings/shortcuts overlay, related tests.
  - **Acceptance:** Three modes selectable, persisted, shortcut works; tests stay bounded (fake providers, no infinite loops).
  - **Verification:** Mode-cycling, persistence, and bounded-unlimited behavior tests.
  - **Details:** Initial implementation added `get_retry_mode` / `set_retry_mode` and `/retry`/`/retry-cycle`; follow-up work is required only for the binding change to `Ctrl+R` (not `Ctrl+Shift+R`). `maxSteps` remains an independent setting.

- [x] **Task 4:** List Ctrl+Shift+V in TUI shortcuts.
  - **Description:** Historical record: the then-existing paste-image binding (`action_paste_image`) was missing from the shortcuts overlay/help. The current mapping is Ctrl+Shift+V for terminal text paste and Ctrl+Alt+V for image paste.
  - **Files:** `one/modes/tui_mode.py` (shortcuts overlay), TUI snapshot files if changed.
  - **Acceptance:** Historical acceptance was an image-paste description for Ctrl+Shift+V; the current overlay instead lists Ctrl+Shift+V for terminal text paste and Ctrl+Alt+V for image paste.
  - **Verification:** Snapshot/command-list test update.
  - **Details:** Historical implementation extended `TUI_SHORTCUTS` with `("Ctrl+Shift+V", "paste image from the system clipboard")` and `("Ctrl+Shift+R", "cycle retry mode")`; the current mapping is Ctrl+Shift+V terminal text paste and Ctrl+Alt+V image paste.

- [x] **Task 5:** Codex image support.
  - **Description:** Serialize current-turn images in `codex_responses` native format; if the API cannot carry images, reject image-bearing requests with a clear unsupported-image error (never silent text-only).
  - **Files:** `one/providers/codex_responses.py`, capability/policy tests.
  - **Acceptance:** Images delivered or explicit error; no silent omission.
  - **Verification:** Fake-transport wire-payload tests (or explicit-reject tests).
  - **Details:** Current implementation still rejects all Codex images and `gpt-5.6-sol` is not marked vision-capable, producing `Model 'gpt-5.6-sol' does not support image input`. Replace the rejection with a tested Responses API multimodal payload, preserving explicit failures for invalid/missing attachments.

- [x] **Task 6:** Visible bash timeout in TUI tool lines.
  - **Description:** Include the effective timeout in TUI `tool start` lines, e.g. `tool start (timeout 30s): bash {"command": ...}` (use per-call timeout when the model overrides it).
  - **Files:** `one/modes/tui_mode.py`, TUI snapshot files if changed.
  - **Acceptance:** Timeout shown for bash (and only where meaningful).
  - **Verification:** Snapshot/unit tests for the rendered line.
  - **Details:** `agent_session.py` emits `effectiveTimeout`, but the TUI currently discards it at `tool_call_start`; implement the render change and add a direct TUI event regression test.

- [x] **Task 7:** Fix `/login` help text.
  - **Description:** Change `/help` login line to `/login [status|refresh <provider>|provider [apiKey] [model] [subscription]]`.
  - **Files:** help-text source in TUI/interactive mode, related tests.
  - **Acceptance:** Help shows the `[subscription]` argument.
  - **Verification:** Help-text assertion test.
  - **Details:** Login help string updated in TUI slash-help, TUI action_help, and interactive mode — all now include `[subscription]`; tests assert the string in `tests/test_tui_commands.py` and `tests/test_interactive_mode.py`.

### Follow-up corrections requested

- [x] **Task 8:** Support unlimited tool steps with `maxSteps=0`.
  - **Description:** Make `maxSteps=0` mean unlimited tool steps, independently of retry mode. Keep abort handling, budget enforcement, `finish`, and ordinary no-tool model completion as termination conditions. Preserve bounded behavior for positive `maxSteps` values and do not create unbounded live tests.
  - **Files:** `one/core/agent_session.py`, `one/core/settings_manager.py` if normalization is needed, retry/tool-loop tests.
  - **Dependencies:** None; independent of Task 3.
  - **Acceptance Criteria:** With `maxSteps=0`, a fake provider can execute more than the default step limit; no `tool_step_limit` is emitted; abort and budget limits still terminate the loop. With `maxSteps>0`, existing bounded behavior remains unchanged regardless of retry mode.
   - **Verification:** Add `maxSteps=0` and positive-limit fake-provider tests with deterministic success/abort endpoints; run the relevant agent-session and event-snapshot tests.
  - **Details:** `maxSteps=0` open-ended loop via `itertools.count`, no `tool_step_limit`; 12 tests in `tests/test_unlimited_steps.py`.

- [x] **Task 9:** Change retry-cycle shortcut to Ctrl+R.
  - **Description:** Replace every user-visible and Textual binding occurrence of Ctrl+Shift+R with Ctrl+R, without changing the cycle action or provider-switching bindings.
  - **Files:** `one/modes/tui_mode.py`, `tests/test_tui_commands.py`, `tests/snapshots/tui/base.txt`, `tests/snapshots/tui/overlay.txt`, `tests/snapshots/tui/widget_panel.txt`.
  - **Dependencies:** Task 3.
  - **Acceptance Criteria:** `ctrl+r` invokes `action_cycle_retry_mode`; overlay shows `Ctrl+R`; `ctrl+shift+r` is not registered for this action.
   - **Verification:** Textual pilot keypress test, binding assertions, and reviewed regenerated snapshots.
  - **Details:** `ctrl+r` binding; pilot keypress test cycles off→on→unlimited→off; no conflicts.

- [x] **Task 10:** Enable Codex image input using the Responses API.
  - **Description:** Mark supported Codex models, including `gpt-5.6-sol`, as image-capable; prevent stale persisted capability metadata from downgrading known built-in support; replace the unconditional `UnsupportedImageError` with validated `input_text` + `input_image` content using attachment data URLs/base64 and the native Codex Responses payload. Preserve explicit errors for unsupported models and invalid/missing blobs.
  - **Files:** `one/core/model_registry.py`, `one/providers/codex_responses.py`, `one/core/agent_session.py` if capability normalization is needed, `tests/test_capability_error_policy.py`, `tests/test_image_attachments.py`, `tests/test_provider_payloads.py` or the existing provider-payload test module.
  - **Dependencies:** Existing attachment storage/security work; confirm current upstream/OpenCode payload contract before implementation.
  - **Acceptance Criteria:** A fake Codex transport receives the image bytes as a Responses API image part; `gpt-5.6-sol` no longer fails the local capability gate; no image is silently converted to filename-only text; invalid attachments make no HTTP request.
   - **Verification:** Non-stream and stream fake-HTTP wire-payload tests, model-registry persistence round trip, capability-policy tests, and full image attachment suite.
  - **Details:** `input_image` parts in Codex payload, vision-protected registry merge, fake-transport wire tests.

- [x] **Task 11:** Render effective tool timeout in the TUI.
  - **Description:** Include `event["effectiveTimeout"]` in the `tool_call_start` line, e.g. `tool start (timeout 30s): bash {...}`, using the per-call timeout when overridden. Keep non-time-limited tools readable and preserve existing tool output/error rendering.
  - **Files:** `one/modes/tui_mode.py`, `tests/test_tui_rendering.py`, TUI snapshots if the rendered fixtures change.
  - **Dependencies:** Existing `effectiveTimeout` event field.
  - **Acceptance Criteria:** A TUI event containing `effectiveTimeout` visibly renders the timeout; timeout overrides show the override value; tools with no timeout do not show a misleading value.
   - **Verification:** Direct TUI event/render test plus targeted snapshot tests.
  - **Details:** `tool start (timeout Ns)` rendering, plain format when timeout None.

- [x] **Task 12:** Prevent duplicated multiline assistant/tool responses.
  - **Description:** Trace and fix duplicate rendering in both paths: suppress repeated final assistant content after streamed/thinking deltas, and make active tool-block tracking resilient when the 500-line TUI history is trimmed so `tool_call_end` cannot append a second status block. Preserve legitimate separate tool output blocks.
  - **Files:** `one/modes/tui_mode.py`, possibly `one/core/agent_session.py` for event semantics, `tests/test_tui_streaming.py`, `tests/test_event_snapshots.py` only if the event contract must change.
  - **Dependencies:** None; coordinate with Task 11 when updating the same TUI event renderer.
  - **Acceptance Criteria:** A multiline streamed response appears once; reasoning/final content is not duplicated; after more than 500 rendered lines, a tool has exactly one status block; genuine tool output still appears once.
   - **Verification:** TUI regression with streamed multiline events, reasoning plus final text, and a tool start/end sequence crossing the trim boundary; targeted and full test suites.
  - **Details:** message_end live-delta suppression + `_trim_stream` invalidating stale tool-block indexes, 4 new tests, snapshots regenerated.

- [x] **Task 13:** Prevent repeated assistant responses across failed attempts/retries.
  - **Description:** Fix the remaining TUI duplication where the same multiline assistant response is displayed two or three times after a provider/MCP-related attempt fails and retry starts. A streamed partial assistant block remains visible because the failed attempt has no terminating `message_end`; the next `message_start` currently resets bookkeeping without removing the old live block. Add bounded cleanup of the currently tracked live assistant block before starting a new assistant message and defensively at `auto_retry_start`, without deleting legitimate tool output, retry status, or separate assistant messages. Preserve the existing event contract unless a terminating event is strictly necessary.
  - **Files:** `one/modes/tui_mode.py`, possibly `one/core/agent_session.py` only if explicit failed-stream termination is required, `tests/test_tui_retry.py`, `tests/test_event_snapshots.py` only if event semantics change.
  - **Dependencies:** Task 12's live-block and trim-safe rendering helpers.
  - **Acceptance Criteria:** When a provider streams a multiline response, fails, and retries, the visible response appears exactly once after the successful retry. The same holds for two failed attempts followed by success. Distinctive lines from the Docker/Playwright MCP example are not repeated; legitimate retry indicators, tool output, and the final answer remain visible.
  - **Verification:** Add a fake-provider TUI regression that emits the multiline text in chunks, raises after streaming on the first (and then second) attempt, and succeeds on the following attempt. Assert each distinctive line occurs once in `"\n".join(app._stream_lines)`; run `tests/test_tui_retry.py`, `tests/test_event_snapshots.py`, and the full suite.
   - **Details:** Investigation found no duplicate TUI subscription. The likely root cause is stale `_assistant_live_start_idx`/live buffer state across an unterminated failed stream: `message_start` resets indexes but leaves the already-rendered block, so every retry appends another copy.
   - **Details:** Stale live block discarded at `message_start`/`auto_retry_start` via tracked start index + line count; `_trim_stream` rebases live/tool-block indexes on front-trim; 4 new regression tests (one-fail, two-fail, trim-rebase, trim-eats-block); full suite 1027 passed.

- [x] **Task 14:** Prevent duplicated text when pasting into the TUI.
   - **Details:** ctrl+v consumed at widget level (stop+prevent) plus _on_paste override with shared truncation; 5 real-dispatch regression tests; image paste untouched.
   - **Description:** Trace and fix the interaction between the custom `Ctrl+V`/clipboard action and Textual's native `events.Paste`/`TextArea._on_paste()` path. A single terminal paste must insert the clipboard contents exactly once, without breaking multiline paste, truncation, keyboard typing, or submit behavior. Preserve image-paste handling on Ctrl+Alt+V; Ctrl+Shift+V is terminal text paste.
  - **Files:** `one/modes/tui_mode.py`, `tests/test_tui_input.py`, possibly Textual-version compatibility code only if required.
  - **Dependencies:** None.
  - **Acceptance Criteria:** One user paste produces one text insertion; a paste followed by Enter submits one prompt containing the text once; multiline and 10,240-character truncation behavior remain correct; image paste remains separate and functional.
  - **Verification:** Add real Textual pilot/event tests for `ctrl+v`, `events.Paste`, both paths emitted for one paste, and end-to-end paste-then-submit. Run tests against the supported Textual version range where practical.
  - **Details:** Likely duplication boundary is `ctrl+v` → custom `_CommandTextArea.action_paste()` plus native bracketed-paste `TextArea._on_paste()`; current tests call `action_paste()` directly and do not exercise dispatch.

- [x] **Task 15:** Restore visible waiting-icon animation in the TUI.
  - **Description:** Ensure every waiting-animation frame mutation refreshes the `#stream` widget. `_tick_waiting()` currently updates an existing spinner line in `_stream_lines` but can omit `_render_stream()`, leaving the visible icon frozen or absent after recent stream-trimming changes. Preserve waiting predicates, cleanup on `turn_end`, and retry behavior.
  - **Files:** `one/modes/tui_mode.py`, `tests/test_tui_streaming.py`, TUI snapshots only if rendering output intentionally changes.
  - **Dependencies:** None.
  - **Acceptance Criteria:** While the model is waiting, successive timer ticks visibly change the rendered spinner/icon; the animation starts after a turn begins, pauses/clears at the correct lifecycle events, and does not duplicate spinner lines.
  - **Verification:** Add a TUI test that arms waiting, invokes `_tick_waiting()` multiple times, and compares both `_stream_lines` and the rendered `#stream` widget; cover normal turn, retry delay, queued prompt, and turn completion.
  - **Details:** Investigation identified a regression introduced in `ac3ca07`: `_tick_waiting()` rewrites the existing spinner line but does not call `_render_stream()` on that branch. Retry backoff also intentionally clears `_turn_active`, so retry animation behavior must be tested explicitly.
  - **Details:** _tick_waiting in-place spinner rewrite now calls _render_stream() each tick; widget-level regression tests for animation and turn-end cleanup.

### Post-fix code review

- [x] **Review 1:** Review Tasks 14–15 before merge.
  - **Details:** Independent review: APPROVE, no blockers; 3 minor cosmetic findings applied; full suite green.
  - **Objective:** Perform an independent code review after both fixes are implemented, with emphasis on event propagation, state ownership, render invalidation, and regressions introduced by the recent retry/live-block changes.
  - **Review Scope:** `one/modes/tui_mode.py`, related `one/core/agent_session.py` event emission, changed tests, TUI/RPC snapshots, and the complete diff from the previous commit.
  - **Required Checks:**
    - Confirm one paste event cannot reach both custom and native insertion paths.
    - Confirm spinner state mutation always invalidates the visible widget.
    - Confirm live assistant/tool block cleanup still preserves legitimate output and retry indicators.
    - Check Textual API compatibility with the declared `textual>=0.74.0` constraint.
    - Check no duplicate event subscriptions, timers, callbacks, or key bindings.
    - Check keyboard accessibility and preservation of `Ctrl+Z`, `Ctrl+R`, and `Ctrl+Shift+V` behavior.
    - Check event contract and snapshot changes for unrelated churn.
    - Check error handling, cancellation, and cleanup paths for stuck TUI state.
  - **Files:** Review-only; no additional file path is predetermined. Record findings in the implementation review/commit, not in a new project artifact.
  - **Acceptance Criteria:** No open high-severity findings; all paste and animation regressions have deterministic tests; full suite and lint pass; intentional snapshot changes are reviewed; changes are ready for maintainer approval.
  - **Verification:** `.venv/bin/python -m pytest -q`, `.venv/bin/ruff check one tests`, `git diff --check`, targeted TUI tests, and manual TUI smoke test with multiline paste, `Ctrl+Z`, `Ctrl+R`, `Ctrl+Shift+V`, model waiting, retry, and MCP output.

- [x] **Task 16:** Fix backspace/delete/arrows double-dispatch in TUI input.
  - **Description:** `_OneTextualApp._on_key` calls `await super()._on_key(event)` explicitly and then `MessagePump` invokes `App._on_key` a second time via MRO (nothing sets `prevent_default`), so every bubbled binding action (backspace/delete_left, delete/delete_right, arrows) fires twice. Add `event.prevent_default()` after the explicit super call (backward-compatible with `textual>=0.74.0`); do not remove the super call. Verify `ctrl+z`, `ctrl+c`+approval, `ctrl+v`, and `shift+enter` paths are unaffected.
  - **Files:** `one/modes/tui_mode.py`, `tests/test_tui_input.py`.
  - **Dependencies:** None.
  - **Acceptance Criteria:** One `backspace` press deletes exactly one char; `delete` and arrows also single-step; printable typing, selection deletion, multiline join, paste paths, spinner, and shortcuts unchanged.
  - **Verification:** Real `pilot.press` regression tests (backspace/delete/arrows/printable/multiline/selection/repeat + no-regression for Task 14/15 and shortcuts); full suite and ruff.
  - **Details:** Reproduced pre-fix via Pilot: `hello@(0,5)+backspace → hel`, `delete → llo`, `left → 2 steps`. Task 14 masked this only for stopped keys.
  - **Details:** Fixed with `event.prevent_default()` after explicit `super()._on_key(event)`; 8 real-dispatch regression tests; full suite 1042 passed, ruff clean.

- [x] **Task 17:** Restore terminal paste after SSH security confirmation clears focus.
   - **Details:** App-level `events.Paste` fallback refocuses `#input` only when no widget is focused, then uses the command input's guarded insertion path. Focused input remains the sole direct insertion path; other focused widgets retain their paste events.

### Risks & Mitigations

| Risk | Mitigation |
|---|---|
| TUI snapshot churn | Regenerate only intentional changes, review diffs |
| Shortcut conflicts | Check existing bindings before adding |
| Unlimited retry hangs | Keep abort, budget, `finish`, and normal completion as hard stops; bound tests with fakes |
| Codex image payload incompatibility | Verify the exact Responses API contract with fake wire-payload tests before enabling real requests |
| Duplicate TUI rendering | Test both streamed final-message suppression and stale tool-block indexes after history trimming |
| Repeated streamed responses across retries | Remove only the tracked live assistant block at failed-attempt/retry boundaries; cover one- and two-failure fake-provider cases |

### Project Acceptance Criteria

- [x] All seven fixes implemented on `fix/tui-ux-batch`, each with tests.
- [x] Follow-up corrections Tasks 8–12 implemented and covered by regressions.
- [x] Task 13 repeated-response regression implemented and covered by retries with failed streamed attempts.
- [x] Tasks 14–15 paste and waiting-animation fixes implemented and covered by regressions.
- [x] Task 16 backspace/delete/arrows double-dispatch fixed and covered by regressions.
- [x] Independent post-fix code review completed with no open high-severity findings (Review 1: Tasks 14–15 APPROVE; Task 16 diff self-reviewed, needs reviewer pass — see P1-R1 below).
- [x] `ruff` clean, full `pytest` green (1042 passed on `29a17a5` and post-merge `main`), no unrelated behavior changes.
- [x] Merge to `main` done (`d3aa680`); no push without explicit approval (no remote configured).

### Estimated Timeline

1–2 engineering days; largest uncertainty is Ctrl-C/approval plumbing across modes.

## Publication checklist (external, intentionally deferred)

### Release readiness audit — multimodal-only branch

**Decision: NO-GO for release approval.** Static source/documentation audit found the blockers below. Historical green test results do not cover these paths. This audit did not run tests, build packages, inspect git history, read private `.one/`/user configuration, or inspect remote GitHub settings. No claim of a secret-free history or verified release artifact is made.

**Goal:** Release the original phase-free terminal agent with explicitly documented, tested image behavior and safe storage. Preserve the original tool approval/extension controls; do not reintroduce phase-based MCP restrictions.
**Scope:** Multimodal correctness/security, documentation, artifact validation, and publication gates. No automatic publication, git-history rewrite, or new prompt architecture.

**Assumptions and open questions:** Decide whether main-prompt images must survive session restart/export or are deliberately transient. Current JSONL stores text without attachment refs, contrary to attachment-module comments. Decide supported image modes/adapters; unsupported combinations must fail explicitly, never silently drop images. Real-model and clipboard acceptance require a separately authorized isolated run.

#### Phase R1 — Storage and session safety (release blockers)

**Objective:** Prevent unsafe path access, loss of existing blobs, and stuck sessions. **Prerequisites:** None. **Expected outcome:** Invalid refs and failed imports cannot access/delete unrelated files; failed prompts remain recoverable. **Effort:** 2–4 engineering days. **Confidence:** High on identified defects; Medium on estimate.

- [x] **Task R1.1:** Harden blob addressing and writes.
  - **Description:** (as in original plan; implemented)
  - **Files:** `one/core/attachments.py`, `one/core/clipboard_image.py`, `tests/test_image_attachments.py`, `tests/test_clipboard_image.py`.
  - **Acceptance:** Forged refs cannot escape storage; concurrent writers cannot corrupt content; POSIX directories/files use 0700/0600; truncated RIFF/PNG/JPEG and oversized Base64 are rejected before provider invocation.
  - **Verification:** Traversal/absolute/symlink fixtures; concurrent same-content imports; permissive-umask tests; write/rename failure injection; real PNG, JPEG, VP8/VP8L/VP8X dimension and size-boundary fixtures.
  - **Details:** Implemented: strict 64-char lowercase-hex hash validation, realpath containment, regular-file verification, exclusive mkstemp temp files with 0o600/0o700 modes and fsync, post-read size recheck, Base64 output limit enforced, strict WebP FOURCC check. 39 new tests; 104 tests pass in image attachment suites.
  - **Evidence:** Previously `attachments.py` joined unchecked hashes, used default modes and shared temp names; `_MAX_BASE64_BYTES` was not enforced.

- [x] **Task R1.2:** Repair lifecycle recovery, rollback, and error privacy.
  - **Description:** Guarantee streaming/image-state cleanup on capability rejection, cancellation and every exception; preserve terminal event semantics. RPC rollback must remove only blobs created by its transaction, not pre-existing deduplicated blobs. Enclose CLI import/dispatch in resource cleanup and dispose runtime sessions on early returns. Track temp-directory ownership explicitly instead of inferring it from path strings. Sanitize error text and all event payloads, not only successful tool-result args.
  - **Details:** Cooperative reset + rollback + privacy tests in `tests/test_image_cli_rpc.py` (cooperative rejection, deduplicated rollback, CLI disposal) and `tests/test_capability_error_policy.py` (capability rejection leaves `is_streaming=False`, recovery permitted).
  - **Files:** `one/core/agent_session.py`, `one/modes/rpc_mode.py`, `one/cli/main.py`, `one/tools/read_image.py`, `tests/test_capability_error_policy.py`, `tests/test_image_cli_rpc.py`, `tests/test_read_image_tool.py`, `tests/test_event_snapshots.py`.
  - **Dependencies:** R1.1 storage transaction semantics.
  - **Acceptance:** Cooperative rejection leaves `is_streaming=False` and permits another prompt; batch `[existing_blob, missing_file]` preserves the existing blob; no owned temporary image files remain after CLI termination; failed absolute-path reads do not disclose full source paths in JSONL/events.
  - **Verification:** End-to-end fake-provider recovery and lifecycle tests, deduplicated rollback with existing consumers, CLI success/failure/cancel disposal assertions, recursive event and persisted-message privacy checks.

#### Phase R2 — Complete supported image flows

**Objective:** Ensure accepted images actually reach the selected provider. **Prerequisites:** R1. **Expected outcome:** Explicit success/failure contracts for each mode and adapter; documented retention. **Effort:** 2–4 engineering days. **Confidence:** Medium.

- [x] **Task R2.1:** Repair TUI/CLI/RPC input and tool discoverability.
  - **Description:** Fix the remaining `dir(sys.modules)` check in `action_paste_image` (it always reports unavailable). Keep clipboard reads off the UI thread and bound captured output. Extract multiple paths without mutating text at stale match offsets; handle `~` and session-relative paths. Validate combined clipboard/file/API counts centrally. Forward or explicitly reject `--image` for TUI/interactive/RPC startup and image-only print mode. Reject image-bearing steer/follow-up requests rather than silently dropping their images if queues remain text-only. Add only the `read_image {path}` schema to the original prompt, without redesigning it.
  - **Details:** TUI pilot/action + count validation + schema tests in `tests/test_tui_input.py`, `tests/test_tui_image_input.py` (Textual paste, submit with refs, image-count rejection, `_extract_image_paths` multi-path handling).
  - **Files:** `one/modes/tui_mode.py`, `one/modes/print_mode.py`, `one/modes/rpc_mode.py`, `one/cli/main.py`, `one/core/agent_session.py`, `one/core/clipboard_image.py`, `one/resources/resource_loader.py`, `tests/test_tui_input.py`, `tests/test_tui_image_input.py`, `tests/test_image_cli_rpc.py`, `tests/test_system_prompt.py`.
  - **Dependencies:** R1.2; supported-mode decision.
  - **Acceptance:** Actual bound paste action reaches import; actual submit passes refs to provider; multiple matched paths leave correct text; queued attachments never disappear silently; original bash/MCP behavior unchanged.
  - **Verification:** Textual pilot/action tests with fake clipboard and provider, CLI dispatch tests for each supported mode, startup RPC attachment test, combined 4+1 count rejection and real loader schema assertion.

- [x] **Task R2.2:** Align capability, provider, and persistence contracts.
  - **Description:** Preserve known vision capabilities through model login, persistence and reload; validate capability booleans strictly. Missing, corrupt or unreadable blobs must fail before HTTP rather than become text only. Serialize images in Codex or explicitly reject them; similarly reject incapable custom adapters. Resolve durable-vs-transient product policy before implementing restart/export and retention; never GC from only the current turn's references.
  - **Details:** `strict_bool` validation + `MissingBlobError`/`UnsupportedImageError` + registry round-trip (register/persist/reload preserves vision caps) tests in `tests/test_image_attachments.py`, `tests/test_provider_payloads.py`, `tests/test_login_validation.py`.
  - **Files:** `one/core/model_registry.py`, `one/core/agent_session.py`, `one/core/session_manager.py`, `one/providers/openai_compatible.py`, `one/providers/anthropic.py`, `one/providers/gemini.py`, `one/providers/codex_responses.py`, `tests/test_provider_payloads.py`, `tests/test_image_attachments.py`, `tests/test_persistence.py`, `tests/test_login_validation.py`.
  - **Dependencies:** R1; explicit image-retention and adapter support decisions.
  - **Acceptance:** Register/persist/reload preserves known vision support; required missing attachments produce no network request; every supported adapter includes the intended current-turn image; retention behavior matches docs and never deletes a referenced blob.
  - **Verification:** Fake HTTP transport wire-payload tests for stream/nonstream, tool-loaded images and missing blobs; registry round-trip tests; persistent restart/branch/export/import tests or explicit unsupported-behavior tests.

#### Phase R3 — Publication evidence and documentation

**Objective:** Verify the exact candidate and distributed artifacts, not just the source checkout. **Prerequisites:** R1/R2 fixed or explicitly removed from release scope. **Expected outcome:** Recorded, reproducible release decision. **Effort:** 1–2 engineering days plus external settings verification. **Confidence:** Medium; history and dependencies have not been scanned.

- [x] **Task R3.1:** Correct public claims and release CI.
  - **Description:** Document images, capability settings, supported modes, limits, privacy and retention; update CLI help, tool count and changelog. Correct auth precedence (runtime, stored, env), extension flag spelling, overbroad atomic/private-storage promises, and security reporting contact. Smoke-test wheel imports outside checkout with PYTHONPATH unset; inspect wheel/sdist contents and test extracted sdist. Gate tags on verified tests and tag/version equality; keep publishing disabled until explicit approval.
  - **Details:** README/CHANGELOG/help/CI/MANIFEST updates applied; wheel imports resolve installed package; archives exclude state/blobs/credentials/caches.
  - **Files:** `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `docs/EXTENSIONS.md`, `one/cli/args.py`, `.github/workflows/ci.yml`, `.github/workflows/release.yml`, `MANIFEST.in`.
  - **Dependencies:** Finalized R2 contracts.
  - **Acceptance:** Docs agree with behavior; artifact imports resolve installed package, not checkout; archives exclude state/blobs/credentials/caches; version matches tag and tests cover the exact release candidate.
  - **Verification:** Future implementer runs build and `twine check`, fresh wheel/sdist installation outside source tree, archive allowlist review, `pip check`, workflow validation and documentation-to-code checks.

- [ ] **Task R3.2:** Run isolated verification and full-history disclosure audit.
  - **Description:** Remove inherited provider credentials from test environment, use scratch ONE_CODING_AGENT_DIR and fake MCP/providers. Fix CLI image tests that currently tolerate network-level failures. Run lint/full regression, dependency vulnerability/license audit, and redacted secret scans covering all intended public refs/history plus release artifacts. Never print secret values; if found, rotate/revoke before any separately approved history fix.
  - **Details:** Test-isolation portion done (scrub env vars, scratch agent dir, --no-mcp, deterministic OPENAI_API_KEY, _PROVIDER_KEY_NAMES constant, RELEASE_CHECKLIST.md created). Secret/history scans (Gitleaks, pip-audit, license review) and GitHub security controls (branch protection, security advisories, publishing) remain **manual maintainer steps** — not automated.
  - **Files:** `tests/test_image_cli_rpc.py`, `.github/workflows/ci.yml`, `pyproject.toml` only if tooling changes are needed, `docs/RELEASE_CHECKLIST.md`; release evidence in root `TODO.md` (no raw scanner findings).
  - **Dependencies:** R3.1 and candidate commit selected.
  - **Acceptance:** Fresh full-suite/lint/build evidence recorded; no unexplained scanner findings; license/fixture provenance reviewed; remote branch protection/private reporting verified by authorized maintainer.
  - **Verification:** `.venv/bin/python -m pytest -q`, `.venv/bin/ruff check .`, build/twine/pip checks; full-history redacted Gitleaks (syntax checked against installed version), resolved-environment pip-audit, license review. Do not treat `.gitignore` as proof of clean history.
  - **Progress (R3.2 test-isolation portion — done here):**
    - `tests/test_image_cli_rpc.py` rewritten: all CLI subprocess tests use
      `_run_cli` helper which scrubs every provider API-key env var, sets
      scratch `ONE_CODING_AGENT_DIR`, passes `--no-mcp`, and injects a
      deterministic `OPENAI_API_KEY` value. No ambient credentials can leak.
      The `_PROVIDER_KEY_NAMES` constant lists all known key env vars.
    - Grep confirmed: no other image/CLI tests use `os.environ.copy()` for
      credential passthrough. `test_clipboard_image.py` uses
      `patch.dict(clipboard_image.os.environ, ...)` which is correct (patches
      the module under test, not the subprocess env).
    - `docs/RELEASE_CHECKLIST.md` created with exact reproducible commands for:
      full pytest, ruff, build, twine check, pip check, fresh wheel/sdist install
      outside checkout (PYTHONPATH unset), archive allowlist inspection,
      full-history Gitleaks with scanner-version syntax note, pip-audit, and
      license/fixture provenance review.
    - Secret/history scanning and GitHub security controls remain **manual
      maintainer steps** — not automated. The checklist records this explicitly.

**Risk and rollout:** Existing helper-only tests masked real UI/CLI failures. Require end-to-end fake-provider coverage and a separately authorized sanitized manual smoke test. Publish only after blocker closure and explicit maintainer approval; keep experimental branches local unless independently reviewed. Do not rewrite history, merge, tag, push, or enable publishing as part of this plan.

**Release acceptance:**
- [ ] R1 storage/security/lifecycle blockers closed with regression coverage.
- [ ] Supported R2 paths deliver images or explicitly fail; no silent omission.
- [ ] Documentation and image-retention promises match observed behavior.
- [ ] Candidate tests, standalone wheel/sdist checks and scans recorded freshly.
- [ ] Public refs/history and GitHub security controls approved by maintainer.

**Estimated total:** 5–10 engineering days, dependent on persistence scope and security findings. This is a remediation estimate, not verification already done.

- [ ] Create the public GitHub repository, add the `origin` remote, and push
  `main`. This requires explicit maintainer action and was intentionally not
  performed during local release preparation.
- [ ] Enable GitHub Security Advisories and branch protection for `main` after
  the repository exists.
- [ ] Create the first GitHub release/tag (`v0.1.0`) after reviewing the public
  repository. The release workflow builds artifacts but intentionally does not
  publish to PyPI.

## Out of scope

- 1:1 parity with `pi` internals (exact event payloads and TypeScript golden
  tests). Subscription OAuth (`/login … subscription`) is in scope and shipped.
- An npm/git extension package manager; extensions use the documented Python
  file/package contract.

## Skill contract and behavior (pi-inspired, implementation only)

### Goal

Allow the agent to create, discover, load, and use reusable skills using the
same contract and user-visible behavior as pi, without copying pi's internal
implementation.

### Contract

- A skill is a directory containing a required `SKILL.md`; optional `scripts/`,
  `references/`, and `assets/` directories are supported as ordinary skill
  resources.
- `SKILL.md` begins with YAML frontmatter containing required fields:
  `name` and `description`.
- `name` is 1–64 characters and contains only lowercase letters, digits, and
  single hyphens; it cannot begin or end with a hyphen.
- `description` is non-empty and at most 1024 characters; it explains both the
  capability and when it should be used.
- Optional frontmatter fields are `license`, `compatibility`, `metadata`,
  `allowed-tools`, and `disable-model-invocation`; unknown fields are ignored.
- Project skills live in `.one/skills/<skill-name>/`; global skills live in
  `<agent_dir>/skills/<skill-name>/` and are created only after the user
  explicitly requests global scope. The current `~/.agents/skills/` discovery
  compatibility remains supported.

### Required behavior

- At startup, discover valid skills and expose only their names and
  descriptions in the system prompt (progressive disclosure); do not inject
  full skill bodies into every prompt.
- When a task matches a skill, the agent loads the full `SKILL.md` with the
  existing read path and may then load referenced resources on demand using
  paths relative to the skill directory.
- Register skills as explicit `/skill:<name>` commands. Arguments following
  the command are appended to the skill invocation as user input.
- Provide `/reload` to rediscover skills, extensions, prompts, themes, and
  context files without restarting the session.
- The agent may create or update a skill using existing `write`/`edit` tools,
  but must use the contract above, choose project scope by default, and ask
  before choosing global scope.
- Skill writes remain subject to the existing cooperation approval gate for
  mutating tools. Skill files are instructions, not a sandbox or a permission
  boundary; display a trust warning for unreviewed skills and executable
  resources.
- Invalid skills produce non-fatal diagnostics and are omitted from the active
  skill list. Duplicate names produce a diagnostic and keep the first skill by
  deterministic discovery order.
- `--skill` remains additive and can explicitly load a skill even when
  automatic discovery is disabled by `--no-skills`.

### Implementation tasks

- [x] **Implement pi-compatible skill parsing and diagnostics**
  - **Files:** `one/resources/resource_loader.py`, `tests/test_system_prompt.py`,
    new skill-loader tests.
  - **Acceptance:** Frontmatter is parsed and validated; names/descriptions are
    exposed; malformed, missing-description, invalid-name, and duplicate skills
    are diagnosed without crashing the session.
  - **Verification:** Loader fixture matrix and `pytest` skill/system-prompt
    tests.

- [x] **Add progressive disclosure and skill command behavior**
  - **Files:** `one/resources/resource_loader.py`, `one/core/agent_session.py`,
    `one/modes/interactive_mode.py`, `one/modes/tui_mode.py`, `one/modes/rpc_mode.py`,
    `tests/test_system_prompt.py`, mode/RPC tests.
  - **Acceptance:** Prompt contains only skill metadata; `/skill:<name>` loads
    the requested body with arguments; unknown skills fail clearly; RPC exposes
    the same command metadata.
  - **Verification:** Fake-loader command tests and TUI/interactive/RPC command
    dispatch tests.

- [x] **Add explicit skill creation and reload workflow**
  - **Files:** `one/resources/resource_loader.py`, `one/core/agent_session.py`,
    `one/modes/interactive_mode.py`, `one/modes/tui_mode.py`, `README.md`,
    `docs/SKILLS.md`, relevant tests.
  - **Acceptance:** The agent can create a valid project skill through existing
    approved file tools; global creation requires explicit user intent; a
    successful write can be followed by `/reload` and immediate skill use.
  - **Verification:** Cooperation approval tests, project/global path tests,
    reload tests, and documentation assertions.

- [x] **Document and test the public skill contract**
  - **Files:** `README.md`, `docs/SKILLS.md`, `CHANGELOG.md`, TUI snapshots if
    help text changes.
  - **Acceptance:** Documentation contains the frontmatter example, discovery
    scopes, progressive disclosure, `/skill:<name>`, `/reload`, trust warning,
    and explicit global-scope rule.
  - **Verification:** Documentation review, full `pytest`, and `ruff`.

### Non-goals

- Do not copy pi's internal TypeScript implementation.
- Do not add a separate skill package manager in this phase.
- Do not execute skill scripts automatically; the model must explicitly invoke
  them through existing tools and existing approval/security policies.
- Do not inject complete skill contents into the permanent system prompt.

## Plan completion guard

### Goal

Prevent the agent from treating plan creation as task completion or claiming
unverified work after creating a plan.

### Prompt requirement

Add the following text to the system prompt immediately after the planning rules:

> After creating a plan, do not call finish immediately. Execute the planned
> steps and verify the result first. A plan is not task completion.

### Implementation task

- [x] **Block finish immediately after plan creation**
  - **Description:** Track whether the current task has just created a plan.
    If the next tool call is `finish` and no other tool has executed since the
    plan was created, reject that finish call with a model-facing error:
    `The plan was just created. Execute and verify a planned step before finishing.`
    Clear the guard after any non-`plan` tool call, including read-only
    inspection. Do not affect tasks that have no plan or tasks that execute a
    real step before finishing. Preserve plan clearing on a valid terminal
    `finish` and the existing event contract.
  - **Files:** `one/resources/resource_loader.py`, `one/core/agent_session.py`,
    `tests/test_plan_tool.py`, `tests/test_system_prompt.py`.
  - **Dependencies:** None.
  - **Acceptance Criteria:** The system prompt contains the exact planning
    warning above; a fake-provider sequence `plan → finish` cannot terminate
    the task; the model receives the rejection and can continue; `finish`
    succeeds after a non-plan step; ordinary finish-without-plan behavior is
    unchanged.
  - **Verification:** Add deterministic fake-provider tests for
    `plan → finish`, `plan → read → finish`, and `finish` without a plan; run
    `.venv/bin/python -m pytest -q tests/test_plan_tool.py tests/test_system_prompt.py`.

### Additional integrity requirement

- [x] **Prevent unverified completion claims**
  - **Description:** Strengthen the system prompt and, where practical, the
    completion path so the agent does not claim that files were changed or
    commands were verified unless the corresponding tool succeeded and the
    resulting state was observed. Keep this as a reporting-integrity rule; do
    not require a specific VCS implementation.
  - **Files:** `one/resources/resource_loader.py`, relevant completion tests.
  - **Dependencies:** Plan completion guard.
  - **Acceptance Criteria:** Completion summaries distinguish planned actions
    from completed and verified actions; no unverified file-change claim is
    emitted in the regression scenario.
  - **Verification:** Fake-provider test with `plan → finish` and an
    unverified claim; assert the first finish is rejected and the final summary
    only reflects observed tool results.

## Skill invocation syntax clarification

### Goal

Prevent the model from confusing the user-facing `/skill:<name>` command with
a filesystem path passed to the `read` tool.

### Required behavior

- The system prompt must state that `/skill:<name>` is a UI command handled by
  TUI/interactive mode, not a tool call and never a `read` path.
- When the model invokes a skill autonomously, it must call `read` with the
  exact `SKILL.md` `filePath` listed in the `# Skills` metadata.
- If `read` receives exactly `/skill:<name>` or `skill:<name>`, the agent must
  return a clear model-facing diagnostic with the matching skill and its real
  `SKILL.md` path when available, rather than attempting filesystem access.
- The fallback must not mutate session state, invoke the skill automatically,
  or reinterpret ordinary filesystem paths.

### Implementation task

- [ ] **Disambiguate and recover skill invocation syntax**
  - **Description:** Clarify the system prompt and add a safe read-dispatch
    fallback for `/skill:<name>`/`skill:<name>`. Preserve explicit UI
    `/skill:<name>` invocation and the existing progressive-disclosure flow.
  - **Files:** `one/resources/resource_loader.py`, `one/core/agent_session.py`
    or `one/tools/read.py`, `tests/test_skills.py`,
    `tests/test_system_prompt.py`, `docs/SKILLS.md`, `README.md`.
  - **Dependencies:** Existing skill loader and `/skill:<name>` command flow.
  - **Acceptance Criteria:** A model-generated
    `read({"path":"/skill:analiza-rynkow"})` does not produce a misleading
    file-not-found result; it explains the syntax error and points to the
    discovered skill path. A real path is unchanged, and explicit UI skill
    invocation still executes exactly once.
  - **Verification:** Fake-loader regression tests for valid, unknown, and
    path-like skill names; exact system-prompt assertion; targeted skill,
    system-prompt, TUI, interactive, and RPC tests.

## Steering checkpoint correction

### Problem

TUI displays `Queued (steer).` immediately, but the current session loop keeps
the steering message in `_steering` until the entire turn ends. The message is
therefore consumed only after all provider/tool activity and `finish`, instead
of before the next provider request. This makes mid-run steering appear to
work in the UI while failing to influence the active turn.

### Required behavior

- Preserve immediate TUI/interactive queue feedback and `queue_update` events.
- Consume pending steering at a safe provider boundary during the active turn:
  after the current provider response/tool transaction and before the next
  provider request.
- Do not wait for the whole turn to finish before consuming steering.
- Do not consume steering after terminal `finish`, abort, timeout, or another
  terminal error; leave it available for an explicit later user action.
- Preserve FIFO ordering, follow-up queue semantics, tool execution order,
  event snapshots, and ordinary no-queue behavior.

### Implementation task

- [ ] **Consume steer at the next provider checkpoint**
  - **Description:** Add a turn-local checkpoint/helper in
    `one/core/agent_session.py` that safely removes the next steering message
    and injects it into the provider-visible conversation before the next
    provider request. Ensure steering queued while a provider call or tool is
    running is applied at that next boundary rather than only in the final
    post-turn drain. Keep terminal paths (`finish`, abort, timeout, terminal
    error) from draining queues automatically.
  - **Files:** `one/core/agent_session.py`, `one/modes/tui_mode.py`,
    `one/modes/interactive_mode.py`, `tests/test_event_snapshots.py`,
    `tests/test_agent_steering.py`, `tests/test_tui_streaming.py`,
    `tests/test_interactive_mode.py`.
  - **Dependencies:** Existing mid-run delivery policy and queue handling in
    the `Subagent timeout and post-timeout diagnostics` implementation tasks.
  - **Acceptance Criteria:** In a fake-provider run with a long tool call,
    input submitted during that call is shown as queued immediately and is
    present in the next provider request before the agent can reach terminal
    `finish`; a second queued steer remains FIFO; finish/abort/timeout leave
    pending queues untouched; follow-up messages retain their existing
    explicit FIFO behavior.
  - **Verification:** Add deterministic fake-provider tests that block a tool,
    submit steer, release the tool, and assert provider-call order and prompt
    contents; add TUI pilot and interactive regression tests for the visible
    queue status; run event snapshots and the full test suite.

## Subagent timeout and post-timeout diagnostics

### Goal

Prevent long-running infrastructure tasks from being killed by the shared
30-second tool timeout while still guaranteeing that a hung provider, SSH
operation, or subprocess cannot block the parent agent indefinitely.

### Architecture decision

- Keep a timeout as a circuit breaker; do not allow unlimited execution.
- Separate the timeout for one tool invocation (`tools.timeoutSec`, currently
  30 seconds) from the timeout for the complete `spawn_subagent` task.
- Give `spawn_subagent` its own configurable timeout, recommended default
  `subagents.timeoutSec = 1800` seconds (30 minutes), with an optional per-call
  override for unusually long Docker/build operations.
- On timeout, abort the subagent, allow a short cleanup grace period, collect
  diagnostics, and return a typed timeout result. Do not automatically start a
  follow-up prompt or assume that the external operation was rolled back.
- Preserve the subagent session ID so the parent or user can inspect/resume the
  work after checking the real external state.

### Implementation tasks

- [ ] **Separate subagent and tool deadlines**
  - **Description:** Add `subagents.timeoutSec` to settings and use it for the
    complete `spawn_subagent` operation instead of passing
    `tools.timeoutSec` as its total deadline. Keep `tools.timeoutSec` as the
    default for individual tools executed inside the subagent, while allowing
    explicit per-call overrides.
  - **Files:** `one/core/settings_manager.py`, `one/core/agent_session.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** A Docker/build subagent can run longer than 30
    seconds; a genuinely stuck subagent still terminates at its configured
    deadline; existing bash/tool timeout behavior is unchanged.
  - **Verification:** Add tests for the default and configured subagent
    timeout, including a task lasting longer than `tools.timeoutSec` but shorter
    than `subagents.timeoutSec`.

- [ ] **Implement controlled timeout cleanup and typed diagnostics**
  - **Description:** On a subagent deadline, call `abort()`, wait for cleanup
    with a bounded grace period, dispose resources, and return an explicit
    `SubagentTimeout`/equivalent result containing session ID, elapsed time,
    last event, last tool name, last assistant text, and sanitized error data.
    Ensure child tasks and subprocesses are cancelled/reaped.
  - **Files:** `one/core/agent_session.py`, `one/core/event_bus.py` only if a
    new event type is required, `tests/test_subagents.py`,
    `tests/test_provider_timeout_regression.py`
  - **Dependencies:** Separate subagent and tool deadlines
  - **Acceptance Criteria:** Timeout always emits `subagent_end`; no pending
    child task remains; diagnostics contain no API keys/passwords; the parent
    receives a failed tool result rather than hanging or silently retrying.
  - **Verification:** Regression test with a blocked subagent and a blocked
    child process; assert cleanup, terminal events, session ID, and redaction.

- [ ] **Add post-timeout external-state inspection workflow**
  - **Description:** Document and, where safe, expose a follow-up diagnostic
    action that checks the real state after timeout (for example `docker ps -a`,
    container inspect, SSH connectivity, and the last command output). The
    agent must distinguish “operation timed out” from “operation failed” and
    must not delete/recreate resources without explicit confirmation.
  - **Files:** `one/core/agent_session.py`, `one/modes/interactive_mode.py`,
    `one/modes/tui_mode.py`, `one/modes/rpc_mode.py`, relevant tests and docs
  - **Dependencies:** Controlled timeout cleanup and typed diagnostics
  - **Acceptance Criteria:** A timed-out Docker task reports that external
    state is unknown, offers inspection rather than destructive recovery, and
    leaves the user with an actionable session ID/diagnostic summary.
  - **Verification:** Fake SSH/Docker integration tests covering completed,
    partially completed, failed, and still-running external operations.

### Risks and assumptions

- A timeout cannot prove that a remote command stopped or that a Docker pull
  was rolled back; external state must always be queried separately.
- A larger subagent deadline increases the maximum wait for a real hang, so the
  progress/heartbeat and explicit abort path are required alongside it.
- Existing queued steering/follow-up messages must remain pending after a
  terminal `finish` or timeout until the user explicitly chooses the next
  action.

## Future version: autonomous provider-timeout recovery

### Goal

Make provider timeouts recoverable in autonomous mode without blindly starting
duplicate requests against a local llama.cpp/Ollama server that may still be
processing the timed-out request. Work on a new branch based on `main`,
recommended name `feat/autonomous-provider-timeout-recovery`; do not merge or
push without explicit approval.

### Context

`one` currently wraps each provider call in `asyncio.wait_for` and converts a
timeout into a generic `RuntimeError`. The normal retry handler can then start
another provider request. With local OpenAI-compatible servers, the server may
continue prompt processing after the client-side cancellation, causing
overlapping generations and confusing TUI state. Pi treats provider
`error`/`aborted` results as terminal at the agent-loop boundary. OpenCode has
typed header/stream timeouts and aborts the transport, but its documented issue
history shows that automatic retry can still reproduce this exact local-provider
problem.

### Scope

#### In Scope

- Typed provider timeout and cancellation state.
- Different recovery policies for interactive and autonomous execution.
- Bounded autonomous recovery with backoff, observability, and optional model or
  provider fallback.
- Provider capability/configuration for local servers that cannot confirm remote
  request cancellation.
- Regression tests for local timeout, retry suppression, cleanup, and TUI state.

#### Non-Goals

- Guaranteeing that an arbitrary OpenAI-compatible server stopped computing after
  a client disconnect.
- Destructive restart of llama.cpp, Ollama, Docker, or other external services.
- Removing retry support for unrelated transient provider errors.

### Assumptions

- A provider timeout only proves that the local deadline expired; remote state is
  otherwise unknown.
- Tool execution starts only after a complete assistant response, so a repeated
  provider generation must not execute the same tool twice automatically.
- Autonomous mode may recover without user input, but recovery must be bounded
  and must preserve the session for later inspection/resume.

### Open Questions

- Should the default autonomous policy prefer a larger second deadline or a
  configured fallback model/provider?
- Can the llama.cpp deployment expose a reliable health/slot/request-status
  endpoint that is safe to query after disconnect?
- Should provider timeout policy be global, per provider, or per model?

### Architecture Decisions

#### ADR-001: Treat provider timeout as a distinct recoverable state

**Decision:** Add a typed timeout error/result carrying provider, model, attempt,
elapsed time, whether streaming started, and whether local cancellation
completed. Keep ordinary transient errors on the existing retry path.

**Alternatives:** Keep timeout as generic `RuntimeError`; make every timeout
terminal; retry every timeout immediately.

**Rationale:** The autonomous controller needs more information than an error
string to avoid duplicate local requests while still recovering from slow
cloud providers.

**Trade-offs:** Adds event and test-contract surface, but prevents timeout
classification from being accidentally changed by message matching.

#### ADR-002: Use mode-aware, bounded recovery

**Decision:** Interactive mode reports the timeout and returns control to the
user. Autonomous mode runs a bounded recovery policy: cancel/close transport,
apply a grace period, optionally inspect provider readiness, then retry with
backoff or switch to an explicitly configured fallback. Exhaustion produces a
terminal error with the preserved session ID.

**Alternatives:** Always stop; always retry immediately; restart the local model
server automatically.

**Rationale:** Autonomous work must solve recoverable failures itself, while
immediate retries are unsafe when the old local request may still be active.

**Trade-offs:** Recovery can take longer and cannot prove remote cancellation;
bounded attempts prevent indefinite autonomous loops.

### Implementation Tasks

- [ ] **Introduce typed provider-timeout results and stable events**
  - **Description:** Replace the generic timeout error with a typed error/result
    and add stable metadata for attempt, provider/model, elapsed time, stream
    phase, cancellation outcome, and retry decision. Preserve existing event
    ordering for non-timeout failures.
  - **Files:** `one/core/agent_session.py`, `one/core/event_bus.py` if a new
    event is required, provider adapter modules, `tests/test_event_snapshots.py`,
    `tests/test_provider_timeout_regression.py`.
  - **Dependencies:** None.
  - **Acceptance Criteria:** Header/prompt-processing timeout, SSE idle timeout,
    user abort, and ordinary provider error are distinguishable without parsing
    display text; local provider task and transport cleanup are observable.
  - **Verification:** Deterministic fake-provider tests assert typed metadata,
    terminal event ordering, cancellation, and no active local chat task.

- [ ] **Implement autonomous timeout recovery policy**
  - **Description:** Add configurable bounded recovery with grace period,
    exponential backoff, maximum attempts, and optional fallback model/provider.
    Prevent immediate retry against a provider marked as possibly still busy;
    preserve the conversation/session and never execute tools from an incomplete
    response.
  - **Files:** `one/core/agent_session.py`, `one/core/settings_manager.py`,
    `one/core/model_registry.py` if fallback resolution needs registry support,
    `tests/test_provider_timeout_regression.py`, retry/event snapshot tests.
  - **Dependencies:** Typed provider-timeout results and stable events.
  - **Acceptance Criteria:** Autonomous mode recovers from a slow provider via a
    bounded policy, does not spin indefinitely, does not issue an immediate
    duplicate local request, and ends with an actionable terminal diagnostic
    after exhaustion.
  - **Verification:** Fake local server/provider tests cover first-token timeout,
    SSE idle timeout, successful delayed recovery, fallback recovery, exhausted
    attempts, and queued-message preservation.

- [ ] **Expose timeout state and recovery actions in clients**
  - **Description:** Make TUI, interactive, RPC, and headless output distinguish
    “provider timed out; recovering”, “provider may still be processing”, and
    “recovery exhausted”. Expose safe retry/switch/stop actions where applicable.
  - **Files:** `one/modes/tui_mode.py`, `one/modes/interactive_mode.py`,
    `one/modes/rpc_mode.py`, relevant docs and snapshots/tests.
  - **Dependencies:** Autonomous timeout recovery policy.
  - **Acceptance Criteria:** TUI never displays an ambiguous timeout while the
    agent silently continues; autonomous progress and final outcome are visible;
    RPC/headless consumers receive machine-readable state.
  - **Verification:** TUI pilot tests, RPC event assertions, snapshot review,
    and headless fake-provider integration tests.

### Project Acceptance Criteria

- [ ] Autonomous mode recovers from a slow local provider without unbounded
  retries or uncontrolled duplicate requests.
- [ ] Interactive mode gives the user control after a provider timeout.
- [ ] Every timeout path cleans up local tasks/streams and resets session state.
- [ ] Session history remains valid and resumable after timeout/recovery failure.
- [ ] Full `pytest` suite and `ruff` pass; event and TUI snapshots are reviewed.

## Follow-up: measure any remaining TUI input stalls

The ordered text/thinking accumulator, bounded UI wakeups, lifecycle barriers,
trim-safe state, and lifecycle-only sidebar refresh were delivered. If stalls
recur, diagnose before introducing runtime threads or offloading persistence.

- [ ] Add capped opt-in timings for loop lag, enqueue age, pending bytes, render,
  request preparation, and persistence—never token/output content or synchronous
  per-token logging. Reproduce silent waiting, reasoning, mixed streaming, and
  message-boundary stalls with deterministic Textual pilots.
- [ ] Use those measurements to justify any further UI/runtime architecture work;
  retain explicit ordering, cancellation, persistence, and event-contract tests.

## Test-suite organization refactor

### Goal

Improve test discoverability and reduce maintenance cost by splitting the two
largest test modules into focused, topic-based modules while preserving test
behavior, snapshot contracts, explicit isolation, and the existing pytest/ruff
workflow. Work on a new branch, recommended name
`refactor/test-suite-organization`; no production behavior change and no
version bump.

### Context

The suite currently contains roughly 29k lines across 50+ files. The largest
modules concentrate unrelated concerns:

- `tests/test_tui_mode.py` — approximately 4.6k lines.
- `tests/test_tool_calling.py` — approximately 1.1k lines.

Both files contain local fake providers, session factories, and tests for
multiple independent behaviors. The first refactor should be conservative:
split only these two modules, then consider extracting shared helpers after the
new boundaries are proven stable.

### Scope

#### In Scope

- Split `tests/test_tui_mode.py` into focused modules for input/streaming,
  rendering, commands, cooperation/approval, retry, and navigation/session UI.
- Split `tests/test_tool_calling.py` into focused modules for basic tool calls,
  retries/abort, timeout/limits, steering/follow-up, and tool-call parsing.
- Move only genuinely shared fakes/factories into explicit modules under
  `tests/support/`, using imports rather than broad implicit fixtures.
- Preserve test names where practical and keep `pytest` node selection usable.
- Run the full suite after each split and review all changes for accidental test
  weakening.

#### Non-Goals

- No production-code changes.
- No test behavior changes, relaxed assertions, deleted coverage, or snapshot
  regeneration unless a path/module reference genuinely requires it.
- Do not reorganize every test file in one pass.
- Do not introduce a large global `conftest.py` or hidden mutable shared state.

### Architecture Decisions

#### ADR-003: Organize tests by behavior, not implementation file size

**Decision:** Place tests in modules named after observable behavior and user
workflow, keeping fixtures close to the tests that use them. Extract a helper
only when it is shared by multiple resulting modules and remains behavior
neutral.

**Alternatives:** Keep large files; split mechanically by line ranges; create a
single global fixture layer for all tests.

**Rationale:** Behavioral boundaries make focused runs understandable and avoid
creating arbitrary coupling to private implementation layout.

**Trade-offs:** Imports and helper locations will change, and some duplicate
small setup code may intentionally remain to preserve test clarity.

#### ADR-004: Use explicit support helpers instead of implicit fixtures

**Decision:** Prefer small modules under `tests/support/` with explicit helper
imports. Add `conftest.py` only if a later measured need justifies it.

**Alternatives:** Put all fakes in `tests/conftest.py`; duplicate every fake in
each module; use a new fixture framework.

**Rationale:** The repository currently has no `conftest.py`, and explicit
dependencies make test isolation and ownership visible.

**Trade-offs:** Test modules retain a few imports and factory calls, but hidden
state leakage is less likely.

### Phases

#### Phase 1: Inventory and establish refactor safeguards

**Objective:** Map test classes/functions, shared helpers, markers, snapshots,
and import dependencies before moving code.

**Prerequisites:** New branch based on current `main`.

**Expected outcome:** A movement map with no behavior changes and a baseline
test/lint result recorded in the branch.

**Estimated effort:** 0.5 day.

**Confidence:** High.

- [x] **Task:** Inventory large test-module boundaries
  - **Description:** Group tests by behavior and identify helpers that are local
    versus genuinely shared. Record any tests relying on module-level state,
    ordering, snapshots, or private symbols before moving them.
  - **Files:** `tests/test_tui_mode.py`, `tests/test_tool_calling.py`, new test
    module map/documentation only if needed.
  - **Dependencies:** None.
  - **Acceptance Criteria:** Every existing test is assigned to exactly one
    destination group; shared helper candidates and snapshot dependencies are
    identified.
  - **Verification:** `pytest --collect-only -q`; compare collected node IDs and
    baseline count before and after the inventory.

#### Phase 2: Split TUI tests

**Objective:** Make TUI tests independently runnable by behavior without
changing assertions or UI behavior.

**Prerequisites:** Phase 1 inventory.

**Expected outcome:** The 4.6k-line TUI module is replaced by focused modules
with shared setup extracted only where necessary.

**Estimated effort:** 1–2 days.

**Confidence:** Medium.

- [x] **Task:** Split `test_tui_mode.py` by behavior
  - **Description:** Move tests and their local helpers into focused modules,
    recommended: `tests/test_tui_input.py`, `tests/test_tui_streaming.py`,
    `tests/test_tui_rendering.py`, `tests/test_tui_commands.py`,
    `tests/test_tui_cooperation.py`, `tests/test_tui_retry.py`, and
    `tests/test_tui_navigation.py`. Preserve async markers, Textual pilot setup,
    snapshot assumptions, and test semantics. Keep only a thin compatibility
    module if external tooling depends on existing node paths.
  - **Files:** `tests/test_tui_mode.py`, the new `tests/test_tui_*.py` modules,
    `tests/support/tui.py` only if shared setup is proven necessary.
  - **Dependencies:** Phase 1.
  - **Acceptance Criteria:** Each moved test passes unchanged or with only
    import/path adjustments; no test is deleted or weakened; TUI snapshot tests
    remain separate and unchanged unless collection requires an import update.
  - **Verification:** Run each new TUI module independently, then
    `pytest -q tests/test_tui_mode.py tests/test_tui_*.py` (avoiding duplicate
    collection if a compatibility module remains), `tests/test_tui_snapshots.py`,
    and `ruff check one tests`.

#### Phase 3: Split tool-calling tests

**Objective:** Separate agent tool-loop behavior into focused, maintainable test
    modules.

**Prerequisites:** Phase 1; Phase 2 may proceed first for simpler review, but is
    not a semantic dependency.

**Expected outcome:** The 1.1k-line tool-calling module is split without
changing provider fakes, event contracts, or tool-result assertions.

**Estimated effort:** 0.5–1 day.

**Confidence:** High.

- [x] **Task:** Split `test_tool_calling.py` by agent-loop behavior
  - **Description:** Move tests into recommended modules
    `tests/test_agent_tool_calls.py`, `tests/test_agent_retry_abort.py`,
    `tests/test_agent_timeouts_limits.py`, `tests/test_agent_steering.py`, and
    `tests/test_agent_tool_parsing.py`. Extract common fake providers/session
    factories into `tests/support/agents.py` only when used by at least two
    modules. Preserve exact event payload assertions and async timing controls.
  - **Files:** `tests/test_tool_calling.py`, new `tests/test_agent_*.py` modules,
    `tests/support/agents.py` if needed.
  - **Dependencies:** Phase 1.
  - **Acceptance Criteria:** Tool-call, retry, abort, timeout, steering, and
    parser coverage remains present with equivalent assertions; no production
    files change.
  - **Verification:** Run each new module, `tests/test_event_snapshots.py`,
    `tests/test_provider_timeout_regression.py`, steering tests, and
    `ruff check one tests`.

#### Phase 4: Consolidate helpers and validate the suite

**Objective:** Remove accidental duplication introduced by the split and prove
the refactor did not alter coverage or collection.

**Prerequisites:** Phases 2 and 3.

**Expected outcome:** Focused test ownership is clear, helper imports are
explicit, and the complete suite remains green.

**Estimated effort:** 0.5–1 day.

**Confidence:** Medium.

- [x] **Task:** Finalize support helpers and collection parity
  - **Description:** Consolidate only shared fakes/factories, remove obsolete
    source modules or compatibility shims after checking tooling references,
    and compare collected test names/counts against the pre-refactor baseline.
    Do not alter production code or silently drop tests.
  - **Files:** `tests/support/*.py`, affected split test modules, obsolete test
    modules only when safe.
  - **Dependencies:** Phases 2 and 3.
  - **Acceptance Criteria:** No hidden global mutable state; each helper has a
    clear owner; collection parity is documented; focused node selection works.
  - **Verification:** `pytest --collect-only -q`, full `.venv/bin/python -m
    pytest -q`, `.venv/bin/ruff check one tests`, and `git diff --check`.

### Project Acceptance Criteria

- [x] `test_tui_mode.py` and `test_tool_calling.py` are no longer oversized
  catch-all modules.
- [x] Every pre-refactor test remains collected and meaningfully asserted.
- [x] TUI snapshots, event snapshots, timeout tests, and steering tests remain
  green.
- [x] No production files, version, or changelog entries change.
- [x] Full pytest suite and ruff pass on `refactor/test-suite-organization`.

## Follow-up: factual evidence after compaction

Tool/MCP evidence is now preserved in provider requests by default; legacy stale
result pruning is explicit opt-in. Compaction remains lossy, so validate its
factual boundary before any new context-reduction work.

- [ ] Add fixtures with old unique facts, units, dates, and conflicting records;
  verify exact facts remain in outbound requests before compaction, after reload,
  and on follow-up. Stub provider payloads rather than asserting model intelligence.
- [ ] Measure factual loss across compaction separately from latency. Any future
  retrieval/pinned-evidence design needs stable references, bounded access, and
  session-scoped authorization; do not automatically rerun potentially side-effecting
  tools or claim unlimited historical recall.

## Project: One-hour Docker diagnostic session and AI analysis

### Goal

Add a non-interactive diagnostic runner that executes an approximately one-hour
agent workload in an isolated Docker container, exercises all built-in tools,
detects runtime and protocol bugs, and asks the configured local
`llama.cpp/local` model to analyze the diagnostic artifacts. The runner must be
safe for another agent to invoke and must retain only the final Markdown and JSON
reports by default.

### Context

**Delivered telemetry follow-up (0.1.24):** the workload now schedules through
the complete configured duration. Docker reports retain bounded host-side stats
and disposable mounted-workspace/session-size samples at a configurable cadence;
unavailable Docker stats are explicit and never read `~/.config/one`.

`scripts/profile_long_session.py` already drives the real RPC runtime and records
basic event, timing, session, and context metrics. The new runner should extend
that approach rather than alter the production runtime. The repository has no
dedicated Docker diagnostic image yet. The local analysis endpoint is expected at
`http://192.168.200.19:8089` and the model is `llama.cpp/local`.

### Scope

#### In Scope

- New non-interactive `scripts/diagnose_long_session.py` CLI with a default
  `--duration 3600` and configurable workload, model endpoint, report directory,
  artifact retention, and analysis timeout.
- A Docker-based disposable workload environment capable of exercising all
  built-in tools, including mutating tools, inside a fixture workspace.
- Collection of RPC events, stderr/stdout diagnostics, lifecycle timings,
  provider/context metrics, session JSONL, durable evidence sidecar metadata,
  container/resource status, and tool outcomes.
- Deterministic heuristic bug detection for malformed protocol output, missing or
  duplicated lifecycle events, stalls, timeouts, retries, provider failures,
  context/compaction anomalies, evidence/session inconsistencies, and resource
  exhaustion.
- Local model analysis with all built-in tools enabled, while diagnostic inputs
  and the disposable analysis workspace are isolated from the real repository.
- Final `report.md` and `report.json` output, stable exit codes, `--help`, and
  machine-readable completion/failure output suitable for another agent.
- Default cleanup of raw artifacts, containers, temporary workspaces, and logs;
  `--keep-artifacts` remains available for forensic follow-up.

#### Non-Goals

- No changes to production agent behavior or tool contracts.
- No automatic execution of arbitrary host commands, MCP servers, or extensions.
- No mounting of the real repository or host credential directories by default.
- No claim that heuristic findings or local-model analysis prove a bug without
  evidence and confidence metadata.
- No permanent storage of raw prompts, outputs, secrets, or diagnostic logs unless
  the caller explicitly requests artifact retention.

### Assumptions

- Docker is installed and usable by the invoking agent.
- The configured `llama.cpp` endpoint is reachable from the workload/analysis
  environment at `http://192.168.200.19:8089`; the endpoint is configurable for
  other networks.
- The workload may install/use additional safe utilities such as Python, bash,
  git, curl, and image fixtures inside the diagnostic image.
- The final reports may contain sanitized excerpts and paths, but raw artifacts
  are removed after analysis by default.
- `ask_user` can be exercised through deterministic automated answers and
  `spawn_subagent` through bounded child-session settings.

### Open Questions

- Confirm the supported Docker runtime/resource flags on Linux and Docker Desktop;
  provide clear preflight diagnostics when a requested limit is unavailable.
- Decide whether the default workload should use only synthetic fixtures or also
  permit an explicitly supplied read-only project snapshot.
- Define the exact workload cadence and whether a shorter `--smoke-duration` is
  needed for CI and local verification.

### Architecture

The runner has four isolated stages:

1. **Preflight:** validate Docker, endpoint/model settings, duration, output
   paths, available disk, and safe environment-variable handling.
2. **Workload container:** build/use a pinned diagnostic image, create a fixture
   workspace, launch the real RPC agent with `--no-extensions --no-mcp`, execute
   read/write/patch/bash/image/evidence/retry/timeout/steering/compaction and
   bounded subagent scenarios, and stream structured RPC output to the host.
3. **Finding and artifact collection:** normalize and redact events, preserve
   bounded evidence references, calculate timings/resource metrics, run heuristic
   checks, and write a temporary artifact manifest with schema/version metadata.
4. **Model analysis:** invoke `llama.cpp/local` through the existing CLI with
   all built-in tools enabled, no MCP/extensions, and the diagnostic artifact
   directory mounted/readable only in a disposable analysis workspace. Treat all
   log content as untrusted data. Validate the returned Markdown/JSON report,
   write the two final reports, and remove temporary artifacts unless retention
   was requested.

### Architecture Decisions

#### ADR-001: Use Docker for mutating diagnostic workloads

**Decision:** Execute the one-hour workload in a disposable, resource-limited
Docker container containing synthetic fixtures and a dedicated workspace.

**Alternatives:** Run directly on the host; test only read-only tools; rely on
cooperation mode as a safety boundary.

**Rationale:** `bash`, `write`, `edit`, and `apply_patch` must be exercised, while
the existing cooperation mode is explicitly not a sandbox. Docker limits damage
to the disposable fixture environment.

**Trade-offs:** Docker availability, image build time, network reachability, and
platform-specific resource flags become prerequisites. Container isolation is
not treated as a universal security boundary against privileged Docker access.

#### ADR-002: Keep raw diagnostics temporary and reports durable

**Decision:** Store raw event/session/log artifacts in a temporary directory and
delete them after report validation by default. Preserve only `report.md` and
`report.json`; `--keep-artifacts` opts into forensic retention.

**Alternatives:** Always retain raw logs; stream only a final summary; store raw
artifacts in the normal user session directory.

**Rationale:** Diagnostics can contain prompts, paths, command output, or secrets.
The default should minimize retention while still producing actionable reports.

**Trade-offs:** Post-run reproduction requires explicit retention, and a failed
cleanup must be reported rather than silently ignored.

#### ADR-003: Use the existing CLI for local-model analysis

**Decision:** Invoke the existing `one` CLI with `--provider llama.cpp --model
local --llama-cpp-url http://192.168.200.19:8089`, rather than calling a provider
adapter directly from the script.

**Alternatives:** Direct HTTP calls to the OpenAI-compatible endpoint; import and
invoke provider/session internals; embed a second analysis implementation.

**Rationale:** Reuses authentication/model/tool/resource behavior and ensures the
analysis path exercises the supported CLI contract. The endpoint remains a CLI
option for Docker/network differences.

**Trade-offs:** Analysis inherits CLI/session startup overhead and must isolate
its working directory and tools carefully.

### Phases

#### Phase 1: Define diagnostic schemas and safe preflight

**Objective:** Establish versioned artifact/report schemas, redaction rules,
exit-code contract, endpoint handling, and Docker/resource preflight checks.

**Prerequisites:** None.

**Expected outcome:** Another agent can invoke the runner and receive predictable
validation errors before any container or model request starts.

**Estimated effort:** 0.5–1 day.

**Confidence:** High.

- [ ] **Task:** Add diagnostic CLI contract and artifact schemas.

  - **Description:** Define `--duration`, `--model`, `--llama-cpp-url`,
    `--report-dir`, `--keep-artifacts`, `--workload`, `--analysis-timeout`,
    optional `--prompt-file`, JSON output, stable exit codes, schema versions,
    redaction limits, and report validation rules.
  - **Files:** `scripts/diagnose_long_session.py`, new diagnostic schema/helper
    module if required, `tests/test_diagnostic_runner.py`, `README.md`.
  - **Dependencies:** None.
  - **Acceptance Criteria:** `--help` is complete; invalid duration/endpoint/path
    values fail before execution; success and failure output is machine-readable;
    reports have stable required fields and never contain unredacted credentials.
  - **Verification:** Unit-test argument validation, redaction, schema validation,
    exit codes, and cleanup behavior without Docker or a real model.

- [ ] **Task:** Add Docker preflight and resource-policy checks.

  - **Description:** Detect Docker availability, build/runtime capability,
    writable report space, and endpoint reachability. Apply configurable CPU,
    memory, PID, disk, network, and timeout limits; report unsupported limits
    explicitly instead of implying they were enforced.
  - **Files:** `scripts/diagnose_long_session.py`, diagnostic Dockerfile/config,
    `tests/test_diagnostic_runner.py`, `docs/RELEASE_CHECKLIST.md` only if
    operational verification commands are added.
  - **Dependencies:** CLI contract task.
  - **Acceptance Criteria:** Preflight failures are actionable; no host
    workspace/credential mount occurs by default; container cleanup is attempted
    on normal exit, timeout, signal, and analysis failure.
  - **Verification:** Mock Docker command tests and an opt-in integration smoke
    test that starts/stops a minimal disposable container.

#### Phase 2: Build the isolated one-hour workload

**Objective:** Exercise built-in tools and long-session behavior in Docker while
capturing complete, bounded, redacted diagnostics.

**Prerequisites:** Phase 1.

**Expected outcome:** A retained artifact bundle can explain what happened during
the run without exposing the host repository or credentials.

**Estimated effort:** 1–2 days.

**Confidence:** Medium.

- [ ] **Task:** Create the diagnostic image and synthetic fixture workspace.

  - **Description:** Add a pinned Docker image definition with Python, bash, git,
    curl, image/fixture support, and only required utilities. Generate fixture
    files for text, large output, malformed output, images, patches, edits,
    permissions, timeouts, and evidence reload. Keep the image reproducible and
    avoid copying host secrets or the real repository.
  - **Files:** `docker/diagnostic.Dockerfile`, optional
    `docker/diagnostic-requirements.txt`, `scripts/diagnostic_workload.py`,
    `tests/test_diagnostic_workload.py`.
  - **Dependencies:** Docker preflight task.
  - **Acceptance Criteria:** Fixture creation is deterministic and idempotent;
    all required built-in tool scenarios have explicit expected outcomes; image
    build does not depend on private host files.
  - **Verification:** Docker build smoke test and fixture/workload unit tests.

- [ ] **Task:** Implement the RPC workload driver and event collector.

  - **Description:** Adapt the existing RPC subprocess protocol to run for the
    requested duration with bounded cycles and scenario rotation. Exercise all
    built-in tools, automated `ask_user`, bounded subagents, retries, timeouts,
    steering/follow-up, compaction, reload, and durable evidence retrieval.
    Record structured events, stderr, process exits, resource samples, session
    JSONL/evidence metadata, and a heartbeat timeline without unbounded memory.
  - **Files:** `scripts/diagnose_long_session.py`,
    `scripts/diagnostic_workload.py`, `tests/test_diagnostic_runner.py`,
    `tests/test_diagnostic_workload.py`.
  - **Dependencies:** Diagnostic image and fixture task.
  - **Acceptance Criteria:** The driver stops cleanly at the deadline, handles
    malformed/non-event stdout, kills hung descendants, records all scenario
    outcomes, bounds event/log memory, and never runs MCP/extensions by default.
  - **Verification:** Fake RPC process tests for event ordering, malformed lines,
    timeout/signal cleanup, bounded buffers, and scenario coverage; opt-in short
    Docker smoke run.

#### Phase 3: Detect bugs and analyze with llama.cpp

**Objective:** Turn collected evidence into deterministic findings and a validated
AI-assisted report using the configured local model.

**Prerequisites:** Phase 2.

**Expected outcome:** `report.md` and `report.json` identify reproducible issues,
evidence locations, severity, probable causes, recommendations, and uncertainty.

**Estimated effort:** 1–2 days.

**Confidence:** Medium.

- [ ] **Task:** Implement heuristic diagnostic finding detection.

  - **Description:** Check lifecycle pairing/order, stalls, duplicate events,
    malformed RPC, provider/tool failures, timeout/retry anomalies, compaction
    and context inconsistencies, evidence/session mismatches, resource limits,
    and cleanup failures. Emit finding IDs, severity, category, evidence refs,
    probable cause, and confidence without asserting unverified root causes.
  - **Files:** `scripts/diagnostic_findings.py`,
    `tests/test_diagnostic_findings.py`.
  - **Dependencies:** Event collector and artifact schema.
  - **Acceptance Criteria:** Synthetic fixtures trigger known findings and clean
    runs do not produce false critical/high findings; checks are deterministic,
    bounded, redacted, and reference exact artifact records where possible.
  - **Verification:** Table-driven tests for each detector, malformed artifacts,
    missing events, duplicate events, and partial/crashed runs.

- [ ] **Task:** Add isolated llama.cpp/local report analysis.

  - **Description:** Invoke the existing CLI against the configured endpoint with
    all built-in tools available, no MCP/extensions, and a disposable analysis
    workspace. Mount diagnostic inputs read-only or copy only sanitized artifacts;
    instruct the model to treat logs as untrusted data. Require Markdown and JSON
    outputs, validate them, merge model findings with heuristic findings, and
    preserve uncertainty when the model cannot establish a cause.
   - **Files:** `scripts/diagnose_long_session.py`,
     `scripts/diagnostic_analysis_prompt.md`, `tests/test_diagnostic_runner.py`,
     `README.md`.
  - **Dependencies:** Heuristic findings task; local model endpoint available for
    opt-in integration verification.
  - **Acceptance Criteria:** Analysis timeout/failure produces a valid partial
    report; the model cannot modify the real repository; reports cite artifact
    IDs/paths; malformed model output is handled without losing heuristic findings;
    the endpoint and model are configurable with the requested defaults.
  - **Verification:** Stub CLI/model tests for success, timeout, malformed JSON,
    prompt-injection-like log content, and tool mutation isolation; opt-in local
    llama.cpp integration test.

#### Phase 4: Cleanup, reporting, and agent-facing documentation

**Objective:** Make the tool reliable for repeated autonomous invocation and prove
that default cleanup does not remove the final reports.

**Prerequisites:** Phases 1–3.

**Expected outcome:** Other agents can run the command, consume its JSON result,
read the two final reports, and rely on cleanup/exit-code behavior.

**Estimated effort:** 0.5–1 day.

**Confidence:** High.

- [ ] **Task:** Finalize cleanup, report retention, and documentation.

  - **Description:** Atomically write `report.md`/`report.json` to `--report-dir`,
    remove temporary artifacts by default, retain them with `--keep-artifacts`,
    handle cleanup failures visibly, document Docker prerequisites and the
    `192.168.200.19:8089` default, and provide examples for another agent.
  - **Files:** `scripts/diagnose_long_session.py`, `README.md`,
    `docs/RELEASE_CHECKLIST.md`, `tests/test_diagnostic_report.py`.
  - **Dependencies:** Analysis and report validation tasks.
  - **Acceptance Criteria:** Final reports remain readable after cleanup; JSON is
    machine-consumable; repeated runs do not collide; cleanup is idempotent;
    retained artifacts are clearly marked as sensitive diagnostic data.
  - **Verification:** End-to-end short-duration run with a stub model, cleanup
    assertions, retained-artifact assertions, repeated-run collision tests, full
    pytest, Ruff, and `git diff --check`.

### Project Acceptance Criteria

- [ ] Another agent can run a non-interactive one-hour diagnostic session with
  one documented command.
- [ ] Built-in read, write, edit, patch, bash, image, evidence, retry, timeout,
  steering, subagent, and lifecycle paths are exercised in an isolated Docker
  workspace.
- [ ] The run collects bounded, redacted, versioned diagnostics and detects
  deterministic protocol/runtime anomalies.
- [ ] `llama.cpp/local` at the configurable default endpoint produces validated
  Markdown and JSON reports from the diagnostics without modifying the real repo.
- [ ] Default cleanup removes raw artifacts while preserving only `report.md` and
  `report.json`; `--keep-artifacts` provides an explicit forensic override.
- [ ] Unit, short integration, and full-suite verification pass; no production
  runtime behavior changes are introduced.

### Rollout & Rollback

The diagnostic runner is opt-in and does not change normal agent startup or tool
execution. Roll back by removing the new script/image/test/documentation files;
no user-session migration is required. Never mount a real repository or enable
MCP/extensions by default as part of this feature.

### Observability

The runner itself must report phase, elapsed time, container ID, scenario counts,
event counts, findings counts, analysis status, cleanup status, and report paths
without printing secret values. Retained artifacts must include a manifest with
schema version, command configuration, image digest, model endpoint (without
credentials), and timestamps.

### Security Considerations

Docker is a containment aid, not an absolute security boundary. Do not run the
diagnostic container privileged, do not mount the Docker socket, do not pass host
credentials, and do not mount the real repository by default. Sanitize secrets in
event/log/report pipelines. Treat model-visible diagnostic content as untrusted
data and isolate analysis writes from the source checkout.

### Risks & Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Container can reach or damage unintended resources | High | Medium | Disposable workspace, no privileged mode/socket/host mounts, resource limits, explicit network policy |
| One-hour run grows logs or memory without bound | High | Medium | Ring buffers, byte/line caps, periodic flushes, artifact size limits, watchdog |
| Local model follows instructions embedded in logs | High | Medium | Delimit logs as untrusted data, read-only artifact mount, disposable analysis workspace, heuristic findings retained independently |
| llama.cpp endpoint is unreachable from Docker/host | Medium | Medium | Preflight probe, configurable URL, actionable failure report, analysis can be skipped while retaining findings |
| Workload misses a real bug | Medium | High | Scenario rotation, deterministic expected outcomes, explicit coverage report, preserve `--prompt-file`/custom workload extension |
| Cleanup removes evidence needed for debugging | Medium | Medium | Validate reports first, atomic writes, `--keep-artifacts` override, explicit cleanup status |

### Estimated Timeline

Approximately 3–5 engineering days: preflight/schemas, Docker workload, finding
detectors, local-model analysis, cleanup/reporting, and verification. The main
uncertainty is Docker networking/resource behavior across Linux and Docker
Desktop.

## Durable tool evidence with model retrieval after reload

### Goal

Preserve complete tool and MCP results independently of the bounded model-facing
`toolResult` preview, then let the model retrieve the complete result after a
session reload by an evidence ID.

### Context

`AgentSession._TOOL_RESULT_MAX_CHARS` currently limits successful tool-result text
to 12,000 characters before it is placed in `self.messages` and persisted to the
session JSONL. `toolOutputPruning` is a separate, optional provider-context
optimization and does not remove this limit. Raw results are available only
ephemerally in live events, so the remainder is lost after reload or process
termination.

### Architecture

- Keep the existing bounded `toolResult` preview for ordinary provider context;
  do not make full outputs automatically consume the model's context window.
- Persist a separate durable evidence record for every relevant tool/MCP call,
  keyed by a unique, session-scoped evidence ID. The record must retain the
  complete raw result, tool name, sanitized arguments, success/error metadata,
  timestamp, and linkage to the originating tool-result message/call.
- Add a session-manager evidence API for writing, listing metadata, and reading a
  record by evidence ID after reload. Evidence records must not be reconstructed
  as ordinary provider messages during `build_session_context()`.
- Include the evidence ID and a concise retrieval instruction in the bounded
  `toolResult` preview so the model can request the full result when needed.
- Add a read-only model-facing retrieval mechanism that accepts only a valid
  evidence ID from the current session, returns the complete stored evidence in a
  bounded/chunkable form, and cannot execute or replay the original tool.
- Preserve existing event contracts where possible; expose the evidence ID in
  tool lifecycle metadata rather than duplicating the complete raw result.
- Apply existing path/secret sanitization before durable storage and retrieval.
  Define explicit size, retention, and failure behavior before implementation;
  oversized evidence must not silently corrupt the session or provider context.

### Implementation tasks

- [x] **Add durable evidence storage and session reload support.**

  - **Description:** Extend the session persistence layer with a versioned
    evidence record format and APIs to append and retrieve evidence by ID. Keep
    evidence out of the normal conversation list and make old sessions without
    evidence records continue to load unchanged. Choose a sidecar or dedicated
    JSONL representation that supports append-only writes and safe reload.
  - **Files:** `one/core/session_manager.py`, related session persistence tests
    under `tests/test_session_manager.py`, plus any newly required evidence
    module/test file.
  - **Dependencies:** None.
  - **Acceptance Criteria:** Full raw results survive process restart and session
    reload; evidence IDs are unique within the session; malformed or missing
    evidence records do not prevent ordinary session loading; evidence is not
    inserted into provider messages automatically.
  - **Verification:** Add focused persistence/reload tests, including a large
    Unicode result and a legacy session; run `pytest -q tests/test_session_manager.py`
    and `git diff --check`.

- [x] **Link tool execution results to durable evidence.**

  - **Description:** Generate and persist an evidence record for successful,
    failed, and MCP tool calls at the raw-result boundary. Keep the existing
    12,000-character preview behavior for ordinary `toolResult` messages, add
    the evidence ID/reference to that preview, and preserve read-image/path
    sanitization and existing lifecycle event ordering.
  - **Files:** `one/core/agent_session.py`, `one/mcp/client.py` only if needed
    for raw-result normalization, and relevant tests in
    `tests/test_agent_tool_calls.py`, `tests/test_mcp.py`, and
    `tests/test_event_snapshots.py`.
  - **Dependencies:** Durable evidence storage task.
  - **Acceptance Criteria:** Every persisted eligible tool result has a stable
    evidence ID; results over 12,000 characters remain bounded in the normal
    provider message while the complete raw result is retrievable; existing
    event sequences and error contracts remain compatible except for additive
    evidence metadata.
  - **Verification:** Test built-in and MCP results before/after reload,
    success/error paths, and exact 12,000-character preview truncation; run
    focused tool/MCP/event tests.

- [x] **Expose secure evidence retrieval to the model.**

  - **Description:** Add a read-only tool or equivalent model-facing dispatch
    path that retrieves evidence by ID for the active session. Return complete
    content without rerunning the original tool, support bounded chunks or
    continuation for very large records, and reject unknown, malformed, or
    cross-session IDs. Document the retrieval contract and ensure the model sees
    how to use it from the initial preview.
  - **Files:** `one/tools/index.py`, the new evidence tool/schema/dispatch path,
    `one/core/agent_session.py`, and focused tool/RPC tests; update relevant
    `README.md` or `docs/` documentation if the user-facing tool contract is
    exposed.
  - **Dependencies:** Durable evidence storage and tool-result linking tasks.
  - **Acceptance Criteria:** A reloaded session can retrieve the exact complete
    evidence by ID; retrieval is read-only, session-scoped, chunk-safe, and does
    not invoke the original tool; unauthorized/unknown IDs fail clearly without
    leaking data; provider context remains bounded unless the model explicitly
    requests evidence.
  - **Verification:** Add tests for reload retrieval, chunk boundaries, Unicode,
    invalid IDs, cross-session access, and no tool re-execution; run the focused
    tool, session, MCP, and RPC test suites.

- [x] **Define evidence retention, size, and privacy behavior.**

  - **Description:** Specify configurable maximum evidence size, storage failure
    handling, cleanup/retention behavior, and redaction rules for secrets and
    sensitive paths. Ensure a failed evidence write is observable and cannot
    falsely claim that complete evidence is available.
  - **Files:** `one/core/settings_manager.py`, configuration documentation,
    persistence/tool tests, and any storage implementation selected above.
  - **Dependencies:** Durable evidence storage task; coordinate with retrieval
    API before finalizing metadata and error states.
  - **Acceptance Criteria:** Limits and retention are explicit; write/read
    failures produce actionable metadata; sensitive values follow existing
    sanitization policy; no unbounded disk or model-context growth is possible.
  - **Verification:** Test size limits, disk/write failure handling, cleanup,
    secret/path sanitization, and configuration defaults; run full pytest and
    Ruff after implementation.

### Project Acceptance Criteria

- [x] Complete tool and MCP results survive process termination and session reload
  in durable evidence storage.
- [x] The model can retrieve complete evidence after reload using only the
  evidence ID, without rerunning the original tool.
- [x] Ordinary provider context retains a bounded 12,000-character preview and
  optional `toolOutputPruning` behavior remains unchanged.
- [x] Evidence retrieval is read-only, session-scoped, chunk-safe, and covered by
  tests for invalid IDs, cross-session access, large Unicode content, and errors.
- [x] Existing event ordering, JSONL compatibility, sanitization, and CLI/RPC
  contracts remain backward compatible except for additive evidence metadata.

## Follow-up: Diagnostic findings and RPC event correlation

### Goal

Create a follow-up branch, recommended name `fix/diagnostic-event-correlation`,
to resolve the findings from the Docker diagnostic run without weakening the
diagnostic checks. Preserve backward compatibility for existing event consumers
where possible; any new event fields must be additive.

### Evidence from the diagnostic report

- `tool_call_end` events were observed without `toolCallId`.
- Multiple `agent_end` events were observed in one persistent RPC session.
- One malformed/non-object stdout record was collected, but the final report did
  not expose enough redacted raw evidence to identify its source.
- `llama.cpp/local` analysis returned `unavailable` with an empty detail string.
- The 60-second workload itself completed successfully: no missing expected
  coverage, `wait_for_idle` completed 4/4, no runtime failure, and cleanup passed.

### Scope

#### In Scope

- Propagate stable tool-call correlation IDs through tool lifecycle events.
- Make lifecycle detection aware of persistent multi-turn RPC sessions.
- Preserve actionable, redacted evidence for malformed stdout and model-analysis
  failures.
- Add deterministic regression tests and rerun the Docker diagnostic with retained
  artifacts.

#### Non-Goals

- Do not change tool behavior, provider semantics, or production timeouts unless
  a regression test proves they are required for lifecycle correctness.
- Do not treat every repeated `agent_end` in a persistent session as a defect.
- Do not suppress real malformed RPC output or lifecycle mismatches merely to make
  the report green.

### Architecture and decisions

1. Use the provider/tool-call identifier when available; otherwise generate a
   per-session/per-turn fallback ID that is unique and explicitly marked as
   runtime-generated.
2. Include the same `toolCallId` in `tool_call_start` and `tool_call_end`,
   including approval rejection, extension denial, timeout, abort, and error
   paths. Keep the field additive for existing consumers.
3. Correlate `agent_start`/`agent_end` by turn/request/session context. A
   persistent RPC session may legitimately contain multiple completed turns;
   only duplicate terminal events for the same turn should be reported.
4. Keep malformed stdout as a finding, but include bounded and redacted raw data,
   source classification, and event index in retained artifacts and the final
   machine-readable report.
5. Preserve model-analysis failure status while reporting non-secret return code,
   stderr, stdout envelope status, and endpoint/model context sufficient to
   diagnose reachability or protocol failures.

### Phases

#### Phase 1: Correlate tool lifecycle events

**Objective:** Ensure every tool start/end pair can be matched reliably.

**Prerequisites:** None.

**Expected outcome:** Diagnostic reports no longer flag valid tool completions as
missing IDs, while genuine mismatches remain detectable.

**Estimated effort:** 0.5–1 day.

**Confidence:** High.

- [x] **Task:** Preserve and emit tool-call correlation IDs.

  - **Description:** Trace the parsed provider tool-call ID into
    `AgentSession._run_tool_call()` and add it to every `tool_call_start` and
    `tool_call_end` event. Define a deterministic fallback only for providers
    that genuinely omit IDs; document the fallback and avoid collisions across
    concurrent or retried calls. Cover approval rejection, extension denial,
    normal success, tool failure, timeout, abort, and `finish` paths.
  - **Files:** `one/core/agent_session.py`, provider/tool-call parsing modules
    identified during implementation, relevant event/RPC type definitions, and
    focused tests under `tests/test_agent_tool_calls.py`,
    `tests/test_event_snapshots.py`, and `tests/test_rpc_mode.py`.
  - **Dependencies:** None.
  - **Acceptance Criteria:** Every emitted tool lifecycle pair has the same
    non-empty `toolCallId`; existing consumers remain compatible; IDs are not
    derived from secret arguments or unbounded prompt text.
  - **Verification:** Add tests for provider-supplied IDs, fallback IDs,
    retries, rejection/error/timeout paths, and exact event ordering; run the
    focused agent/event/RPC tests and `ruff check .`.

#### Phase 2: Correct persistent-session lifecycle detection

**Objective:** Distinguish valid multi-turn `agent_end` events from duplicate
terminal events within one turn.

**Prerequisites:** Phase 1 is preferred but not strictly required.

**Expected outcome:** A multi-prompt persistent RPC workload does not generate a
false `duplicate-agent-end` finding.

**Estimated effort:** 0.5 day.

**Confidence:** High.

- [x] **Task:** Add turn-aware lifecycle correlation to diagnostics.

  - **Description:** Identify the existing event/request/turn boundary exposed by
    the RPC stream. Update `diagnostic_findings.detect()` to group terminal
    lifecycle events by that boundary and report only repeated terminal events
    within the same turn. If the runtime does not expose sufficient metadata,
    add a bounded additive turn identifier at the event emission boundary.
  - **Files:** `scripts/diagnostic_findings.py`,
    `scripts/diagnostic_workload.py`, `tests/test_diagnostic_findings.py`,
    `tests/test_diagnostic_runner.py`, and runtime event tests if a new field is
    needed.
  - **Dependencies:** Understand the event contract from Phase 1 and existing
    RPC snapshots.
  - **Acceptance Criteria:** Multiple valid turns produce no duplicate finding;
    two terminal events for one turn still produce a finding; reports retain
    exact evidence references.
  - **Verification:** Table-driven detector tests for one turn, multiple turns,
    duplicate terminal events, aborted turns, and partial sessions; run the
    short Docker workload and inspect `report.json`.

#### Phase 3: Improve malformed-output evidence

**Objective:** Make the malformed RPC finding actionable rather than merely
pointing to an event index.

**Prerequisites:** None.

**Expected outcome:** A maintainer can identify whether malformed output came
from the RPC protocol, startup logging, provider output, or the workload driver.

**Estimated effort:** 0.5 day.

**Confidence:** Medium.

- [x] **Task:** Preserve bounded redacted malformed-output context.

  - **Description:** Retain the source stream, raw preview, truncation marker,
    sequence index, and nearby event context in temporary artifacts and expose a
    sanitized bounded excerpt in `report.json`. Ensure normal reports never leak
    credentials or unbounded model output. Run once with `--keep-artifacts` to
    classify the current `events[0]` finding before deciding whether runtime
    stdout or the collector is at fault.
  - **Files:** `scripts/diagnose_long_session.py`,
    `scripts/diagnostic_workload.py`, `scripts/diagnostic_findings.py`,
    `tests/test_diagnostic_runner.py`, `tests/test_diagnostic_findings.py`.
  - **Dependencies:** None.
  - **Acceptance Criteria:** Malformed findings include actionable redacted raw
    context; credentials and oversized output remain protected; valid JSON RPC
    events are not misclassified.
  - **Verification:** Unit-test malformed JSON, non-object output, startup text,
    secret redaction, truncation, and event ordering; run a retained-artifact
    Docker smoke and inspect the exact first malformed record.

#### Phase 4: Diagnose local-model analysis failures

**Objective:** Make `llama.cpp/local` analysis failures diagnosable and verify the
analysis path independently from the workload path.

**Prerequisites:** Phase 3 artifacts and a reachable local model endpoint.

**Expected outcome:** `modelAnalysis.detail` identifies connection, process,
timeout, or malformed-response failures without leaking secrets.

**Estimated effort:** 0.5–1 day.

**Confidence:** Medium.

- [x] **Task:** Add structured model-analysis failure diagnostics.

  - **Description:** Capture sanitized subprocess return code, stderr, bounded
    stdout preview, timeout state, selected model, and endpoint reachability
    status. Confirm whether analysis is expected to run on the host or inside a
    networked container, and document the required endpoint/network setup.
  - **Files:** `scripts/diagnose_long_session.py`,
    `scripts/diagnostic_analysis_prompt.md`, `tests/test_diagnostic_runner.py`,
    and `README.md` if invocation/network documentation changes.
  - **Dependencies:** Local `llama.cpp` endpoint or a deterministic stub CLI for
    tests.
  - **Acceptance Criteria:** Analysis success, timeout, non-zero exit,
    unreachable endpoint, and malformed envelope each produce valid reports with
    distinct actionable status/detail values; heuristic findings are preserved.
  - **Verification:** Stub subprocess tests plus one opt-in real endpoint run;
    inspect `report.json` and verify no credentials appear in output.

#### Phase 5: End-to-end regression verification

**Objective:** Prove the corrected runtime events and diagnostic interpretations
work together on the real Docker workload.

**Prerequisites:** Phases 1–4.

**Expected outcome:** The diagnostic report distinguishes real defects from
expected multi-turn behavior and records any unresolved issue with evidence.

**Estimated effort:** 0.5 day.

**Confidence:** Medium.

- [x] **Task:** Run retained-artifact and normal cleanup diagnostics.

  - **Description:** Execute short and one-hour-compatible commands on a dedicated
    follow-up branch, first with `--keep-artifacts` for investigation and then
    without it for cleanup verification. Compare coverage, lifecycle findings,
    malformed records, analysis status, report validity, and container cleanup.
  - **Files:** `README.md`, `TODO.md`, and diagnostic tests/docs only if command
    or report contracts change.
  - **Dependencies:** Phases 1–4.
  - **Acceptance Criteria:** No false duplicate-agent-end finding for valid
    multi-turn runs; tool lifecycle IDs correlate; malformed output has an exact
    redacted explanation; model-analysis status is actionable; default cleanup
    leaves final reports readable and removes raw artifacts.
  - **Verification:** Focused tests, full `pytest`, Ruff, `git diff --check`, an
    opt-in retained-artifact Docker run, and a normal cleanup Docker run.

### Follow-up Acceptance Criteria

- [x] Every tool lifecycle event pair contains a stable matching `toolCallId`.
- [x] Persistent RPC sessions with multiple valid turns do not trigger a false
  duplicate-terminal-event finding.
- [x] Genuine same-turn lifecycle duplication remains detectable.
- [x] Malformed RPC output includes bounded, redacted, source-identifying
  evidence.
- [x] Model-analysis failures include actionable non-secret diagnostics.
- [x] Full local tests and CI pass on supported platforms.

## Follow-up: TUI viewport performance

### Goal

Improve TUI responsiveness and mouse-copy reliability during long conversations
while preserving the complete logical/session history. The TUI should render only
the latest 500 display lines.

### Confirmed decisions

- Keep full history in `AgentSession`/session persistence, but render only the
  latest 500 lines in the TUI viewport.
- Preserve mouse selection/copy of visible transcript text while streaming.

### Scope

#### In Scope

- Separate logical conversation/session history from the bounded rendered TUI
  viewport.
- Keep `_render_stream()` bounded to the latest 500 lines and avoid unnecessary
  full-widget rebuilds during high-frequency deltas where practical.
- Snapshot mouse selection immediately on mouse release, with a safe fallback,
  so stream refreshes cannot erase the selected text before copying.
- Add TUI performance, viewport, and selection regression tests.

#### Non-Goals

- Do not delete or truncate persisted session history.
- Do not introduce a full virtualized widget framework unless bounded rendering
  proves insufficient after measurement.

### Architecture and decisions

1. `AgentSession.messages` and session JSONL remain the source of truth for full
   history. TUI rendering is a presentation cache and may discard old visible
   lines.
2. Maintain a bounded rendered tail with `MAX_RENDERED_LINES = 500`. All index
   bookkeeping for live assistant/tool blocks must be rebased or invalidated
   whenever the front of the rendered tail is trimmed.
3. Query and snapshot selected text synchronously in `on_mouse_up` before queued
   stream refreshes can replace the `Static` content. Retain a deferred fallback
   only when the immediate selection API returns no text.
4. Coalesce frequent assistant deltas and render at most once per UI flush/frame;
   preserve event ordering and visible final output.
### Phases

#### Phase 1: Bounded TUI rendered viewport

**Objective:** Keep TUI rendering fast for long sessions without losing logical
history.

**Prerequisites:** None.

**Expected outcome:** The TUI always renders at most the latest 500 lines while
`AgentSession` and persisted session history remain complete.

**Estimated effort:** 1–2 days.

**Confidence:** Medium.

- [x] **Task:** Separate full logical history from the rendered tail.

  - **Description:** Introduce a clearly named rendered-line cap of 500 and make
    `_render_stream()` consume only the bounded tail. Keep session messages and
    persistence untouched. Update `_write`, assistant streaming, thinking,
    tool-block updates, `/clear`, and trim/rebase helpers so the rendered tail
    remains internally consistent.
  - **Files:** `one/modes/tui_mode.py`, `tests/test_tui_rendering.py`,
    `tests/test_tui_streaming.py`, `tests/test_tui_retry.py`, and TUI snapshots
    only if the visible tail contract changes.
  - **Dependencies:** None.
  - **Acceptance Criteria:** The rendered list never exceeds 500 lines; the
    latest 500 lines are visible; old session messages remain available after
    rendering trim; active assistant/tool/thinking indexes never point to the
    wrong line after front-trim.
  - **Verification:** Tests append substantially more than 500 lines and assert
    the exact visible tail, logical session preservation, live-block updates,
    tool status updates, thinking spinner behavior, and `/clear` reset.

- [x] **Task:** Coalesce expensive stream renders.

  - **Description:** Ensure contiguous assistant/thinking deltas update the data
    model in order but trigger no more than one full `Static` update per UI flush
    or frame. Avoid rebuilding equivalent `Text` content when no visible line
    changed. Preserve scroll-to-end, retry indicators, tool output, and final
    message rendering.
  - **Files:** `one/modes/tui_mode.py`,
    `tests/test_tui_streaming.py`, `tests/test_tui_rendering.py`.
  - **Dependencies:** Rendered viewport task.
  - **Acceptance Criteria:** High-frequency deltas do not cause one widget update
    per token; visible text remains complete and ordered; spinner and lifecycle
    updates still invalidate the widget when required.
  - **Verification:** Instrument/count render calls during a burst of deltas;
    assert coalescing and run TUI streaming/retry regression tests.

#### Phase 2: Reliable mouse selection and copy

**Objective:** Preserve copy behavior while the stream is being refreshed.

**Prerequisites:** Phase 2 rendered viewport behavior.

**Expected outcome:** Drag-selecting visible transcript text copies the selected
text exactly once, even during active streaming or after viewport trimming.

**Estimated effort:** 0.5–1 day.

**Confidence:** Medium.

- [x] **Task:** Snapshot selection before stream refresh invalidation.

  - **Description:** Capture `screen.get_selected_text()` synchronously inside
    `on_mouse_up`, normalize only the copy payload, and use the existing clipboard
    backend chain. Keep a deferred fallback for terminals/Textual versions where
    the immediate selection is unavailable. Prevent duplicate copies/toasts and
    preserve empty-selection behavior.
  - **Files:** `one/modes/tui_mode.py`, `tests/test_tui_rendering.py`,
    `tests/test_clipboard_image.py` only if shared clipboard behavior is touched,
    and a focused mouse-selection regression test.
  - **Dependencies:** Rendered viewport task.
  - **Acceptance Criteria:** A mouse drag copies exactly the selected visible
    text; rapid stream updates do not turn a valid selection into an empty copy;
    plain clicks do not copy; repeated identical selections do not duplicate the
    clipboard action or toast.
  - **Verification:** Textual pilot mouse-drag tests with a static transcript,
    active streaming updates, a 500-line trim boundary, and mocked clipboard
    backends. Do not assert ambient system clipboard state.

#### Phase 3: Integrated verification

**Objective:** Validate long-session responsiveness and copy reliability together.

**Prerequisites:** Phases 1–3.

**Expected outcome:** Long histories remain usable and visible transcript
selection remains copyable.

**Estimated effort:** 0.5 day.

**Confidence:** Medium.

- [x] **Task:** Run focused and full TUI verification.

  - **Description:** Exercise a long fake session with more than 500 rendered
    lines, retry/abort controls, mouse selection/copy, streaming deltas, thinking,
    tool output, and `/clear`. Review snapshots for intentional changes only.
  - **Files:** Tests and snapshots identified by prior phases; `TODO.md` only for
    verification evidence.
  - **Dependencies:** Phases 1–3.
  - **Acceptance Criteria:** Full logical history remains intact; only the latest
    500 display lines are rendered; TUI render cost stays bounded; selection copy
    works; retry semantics are unchanged.
  - **Verification:** Focused TUI tests, full pytest, Ruff, `git diff --check`,
    and a manual Textual smoke test with long history and mouse selection.

### Acceptance Criteria

- [x] The TUI renders no more than 500 transcript lines at once.
- [x] Full logical/session history is preserved independently of the rendered cap.
- [x] Rendering remains coalesced and responsive during high-frequency streaming.
- [x] Mouse-selected visible transcript text copies reliably and exactly once.
- [x] Existing clipboard, lifecycle, snapshot, and full-suite tests pass.

## Follow-up: Complexity-aware planning policy

### Goal

Ensure the agent invokes the `plan` tool for explicitly planning-oriented or
genuinely complex tasks, without forcing a planning phase for every simple
prompt.

### Confirmed decisions

- Use `plan` for explicit planning requests and tasks with multiple dependent
  phases, multiple components, security/infrastructure risk, validation gates,
  rollback planning, or required user approval.
- Do not create plans for simple questions, single-step edits, one-file fixes
  with clear requirements, or straightforward test/formatting changes.
- Treat user-provided phases as draft scope; for complex tasks, convert them
  into a structured plan through the `plan` tool.
- Do not claim that a plan exists unless the `plan` tool completed successfully.
- After creating a plan in explicitly activated Plan Mode, present it and wait
  for approval before execution only when cooperation mode is enabled. Without
  cooperation mode, continue according to the user's original execution
  request after the plan is created.
- Do not infer that every user request requires read-only planning; Plan Mode
  must be explicitly activated or required by task policy.

### Implementation tasks

- [ ] **Task:** Update the planning system prompt and activation policy.

  - **Description:** Separate complexity-based Plan Mode activation from the
    active read-only Plan Mode reminder. Require the `plan` tool for complex
    tasks, preserve direct execution for simple tasks, and remove the false
    assumption that every user has requested no execution. State that a
    user-supplied phase list is not itself a persisted plan.
  - **Files:** System prompt/runtime prompt source and its tests; exact paths
    to be identified during implementation.
  - **Dependencies:** None.
  - **Acceptance Criteria:** Complex multi-phase/security prompts invoke
    `plan` before execution; simple prompts do not invoke `plan`; Plan Mode
    remains read-only; plans are not reported as created without a successful
    `plan` tool result; approval is required before execution when Plan Mode is
    explicitly active; in cooperation mode execution pauses for approval after
    plan creation, while normal mode continues according to the user's request.
  - **Verification:** Prompt/runtime tests covering simple, complex,
    security-assessment, explicit-plan, and user-provided-phase prompts, plus
    a manual smoke test for both execution paths.

## Follow-up: SSH-safe TUI clipboard shortcuts

### Goal

Allow text pasted through the terminal to work reliably when `one` runs on a
remote host over SSH, while moving image paste to a separate shortcut that does
not conflict with terminal text paste.

### Confirmed decisions

- Keep `Ctrl+V` for text from the host/system clipboard when available.
- Reserve `Ctrl+Shift+V` for terminal/bracketed text paste so local terminal
  paste continues to work over SSH without requiring clipboard access on the
  remote host.
- Move image paste to `Ctrl+Alt+V`.
- Keep `/paste-image` as a discoverable fallback for terminals that intercept
  `Ctrl+Alt+V`.
- Do not use `Ctrl+X` (standard cut) or `Ctrl+D` (delete/EOF behavior).
- Do not change interactive-mode `Ctrl+B` behavior.

### Scope

#### In Scope

- Change the TUI image-paste binding from `Ctrl+Shift+V` to `Ctrl+Alt+V`.
- Ensure `Ctrl+Shift+V` remains the text paste path, including Textual
  bracketed-paste event handling and duplicate-insertion protection.
- Add the `/paste-image` command or equivalent existing command path as a
  fallback if no such command currently exists.
- Update TUI shortcut/help text, README documentation, and relevant changelog
  entry.
- Add regression coverage for shortcut dispatch, text paste over a paste event,
  image-paste dispatch, fallback command behavior, and duplicate insertion.

#### Non-Goals

- Do not add SSH clipboard forwarding or require a clipboard daemon on the
  remote host.
- Do not change clipboard image acquisition, image attachment validation, or
  upload behavior beyond shortcut/command routing.
- Do not change interactive-mode keybindings.

### Intended branch

`fix/tui-ssh-safe-paste-shortcuts`

### Implementation tasks

- [x] **Task:** Reassign image paste and preserve terminal text paste.

  - **Description:** Update the TUI binding and shortcut table so `Ctrl+Alt+V`
    invokes image paste and `Ctrl+Shift+V` remains text paste. Verify Textual
    receives and consumes terminal `events.Paste` exactly once. Preserve the
    existing `Ctrl+V` host-clipboard fallback.
  - **Files:** `one/modes/tui_mode.py`, `tests/test_tui_input.py`,
    `tests/test_tui_image_input.py`, and any shared TUI shortcut test files.
  - **Dependencies:** None.
  - **Acceptance Criteria:** `Ctrl+Shift+V` inserts text exactly once from a
    terminal paste event; `Ctrl+Alt+V` dispatches image acquisition; `Ctrl+V`
    still reads the host clipboard; existing editing keys remain unchanged.
  - **Verification:** Focused Textual pilot tests with mocked clipboard/image
    backends; assert no duplicate text insertion.

- [x] **Task:** Add and document the image-paste fallback command.

  - **Description:** Add `/paste-image` if absent, route it through the same
    image acquisition path as the shortcut, and expose it in `/help` and the
    TUI shortcut/help presentation. Update all user-facing keyboard shortcut
    documentation to describe the final mapping explicitly: `Ctrl+V` for host
    clipboard text, `Ctrl+Shift+V` for terminal/SSH text paste, and
    `Ctrl+Alt+V` for image paste. Remove stale claims that `Ctrl+Shift+V`
    pastes an image.
  - **Files:** `one/modes/tui_mode.py`, `tests/test_tui_commands.py`,
    `tests/test_tui_image_input.py`, `README.md`, `CHANGELOG.md`, and any
    shortcut/help documentation discovered during implementation.
  - **Dependencies:** Reassigned shortcut task.
  - **Acceptance Criteria:** The command queues/imports the same clipboard image
    as `Ctrl+Alt+V`, reports the existing no-backend/no-image errors, and is
    documented as the fallback for terminals intercepting the key chord. README,
    `/help`, TUI shortcut overlay, and changelog all show the same mapping.
  - **Verification:** Command tests with mocked image acquisition, repository
    search for stale shortcut descriptions, and README/shortcut/help text review.

- [x] **Task:** Run integrated verification and refresh snapshots if needed.

  - **Description:** Verify local and SSH-like terminal paste paths, image
    shortcut routing, command fallback, and unchanged interactive-mode keys.
    Update only intentional TUI snapshots.
  - **Files:** `tests/test_tui_snapshots.py`, `tests/snapshots/tui/*`, and
    relevant test files identified by the preceding tasks.
  - **Dependencies:** Tasks above.
  - **Acceptance Criteria:** Existing paste/image behavior remains compatible;
    text terminal paste works without remote clipboard binaries; image paste is
    reachable through both `Ctrl+Alt+V` and `/paste-image`.
  - **Verification:** `pytest -q`, `ruff check .`, `git diff --check`, focused
    TUI tests, and a manual SSH terminal smoke test where available.
