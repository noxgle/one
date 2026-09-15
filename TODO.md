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
- [x] **P1-16:** TUI shortcut table/pointer (`Ctrl+R`, `Ctrl+Shift+V`, `Ctrl+V`,
  `Ctrl+Z` from `TUI_SHORTCUTS`, `tui_mode.py:129-142`).
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
  - **Verification:** New fake-approval regression tests; `.venv/bin/python -m pytest -q tests/test_tui_mode.py tests/test_approval.py`.
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
  - **Description:** The existing paste-image binding (`action_paste_image`) is missing from the shortcuts overlay/help; add it.
  - **Files:** `one/modes/tui_mode.py` (shortcuts overlay), TUI snapshot files if changed.
  - **Acceptance:** Overlay lists Ctrl+Shift+V with image-paste description.
  - **Verification:** Snapshot/command-list test update.
  - **Details:** `TUI_SHORTCUTS` tuple extended with `("Ctrl+Shift+V", "paste image from the system clipboard")` and `("Ctrl+Shift+R", "cycle retry mode")`; bindings registered in the keymap.

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
  - **Details:** Login help string updated in TUI slash-help (tui_mode.py:1318), TUI action_help (tui_mode.py:2490), and interactive_mode (interactive_mode.py:528) — all now include `[subscription]`; tests assert the string in `test_tui_mode.py:632`, `test_tui_mode.py:1095`, and `test_interactive_mode.py:392`.

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
  - **Files:** `one/modes/tui_mode.py`, `tests/test_tui_mode.py`, `tests/snapshots/tui/base.txt`, `tests/snapshots/tui/overlay.txt`, `tests/snapshots/tui/widget_panel.txt`.
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
  - **Files:** `one/modes/tui_mode.py`, `tests/test_tui_mode.py`, TUI snapshots if the rendered fixtures change.
  - **Dependencies:** Existing `effectiveTimeout` event field.
  - **Acceptance Criteria:** A TUI event containing `effectiveTimeout` visibly renders the timeout; timeout overrides show the override value; tools with no timeout do not show a misleading value.
   - **Verification:** Direct TUI event/render test plus targeted snapshot tests.
  - **Details:** `tool start (timeout Ns)` rendering, plain format when timeout None.

- [x] **Task 12:** Prevent duplicated multiline assistant/tool responses.
  - **Description:** Trace and fix duplicate rendering in both paths: suppress repeated final assistant content after streamed/thinking deltas, and make active tool-block tracking resilient when the 500-line TUI history is trimmed so `tool_call_end` cannot append a second status block. Preserve legitimate separate tool output blocks.
  - **Files:** `one/modes/tui_mode.py`, possibly `one/core/agent_session.py` for event semantics, `tests/test_tui_mode.py`, `tests/test_event_snapshots.py` only if the event contract must change.
  - **Dependencies:** None; coordinate with Task 11 when updating the same TUI event renderer.
  - **Acceptance Criteria:** A multiline streamed response appears once; reasoning/final content is not duplicated; after more than 500 rendered lines, a tool has exactly one status block; genuine tool output still appears once.
   - **Verification:** TUI regression with streamed multiline events, reasoning plus final text, and a tool start/end sequence crossing the trim boundary; targeted and full test suites.
  - **Details:** message_end live-delta suppression + `_trim_stream` invalidating stale tool-block indexes, 4 new tests, snapshots regenerated.

- [x] **Task 13:** Prevent repeated assistant responses across failed attempts/retries.
  - **Description:** Fix the remaining TUI duplication where the same multiline assistant response is displayed two or three times after a provider/MCP-related attempt fails and retry starts. A streamed partial assistant block remains visible because the failed attempt has no terminating `message_end`; the next `message_start` currently resets bookkeeping without removing the old live block. Add bounded cleanup of the currently tracked live assistant block before starting a new assistant message and defensively at `auto_retry_start`, without deleting legitimate tool output, retry status, or separate assistant messages. Preserve the existing event contract unless a terminating event is strictly necessary.
  - **Files:** `one/modes/tui_mode.py`, possibly `one/core/agent_session.py` only if explicit failed-stream termination is required, `tests/test_tui_mode.py`, `tests/test_event_snapshots.py` only if event semantics change.
  - **Dependencies:** Task 12's live-block and trim-safe rendering helpers.
  - **Acceptance Criteria:** When a provider streams a multiline response, fails, and retries, the visible response appears exactly once after the successful retry. The same holds for two failed attempts followed by success. Distinctive lines from the Docker/Playwright MCP example are not repeated; legitimate retry indicators, tool output, and the final answer remain visible.
  - **Verification:** Add a fake-provider TUI regression that emits the multiline text in chunks, raises after streaming on the first (and then second) attempt, and succeeds on the following attempt. Assert each distinctive line occurs once in `"\n".join(app._stream_lines)`; run `tests/test_tui_mode.py`, `tests/test_event_snapshots.py`, and the full suite.
   - **Details:** Investigation found no duplicate TUI subscription. The likely root cause is stale `_assistant_live_start_idx`/live buffer state across an unterminated failed stream: `message_start` resets indexes but leaves the already-rendered block, so every retry appends another copy.
   - **Details:** Stale live block discarded at `message_start`/`auto_retry_start` via tracked start index + line count; `_trim_stream` rebases live/tool-block indexes on front-trim; 4 new regression tests (one-fail, two-fail, trim-rebase, trim-eats-block); full suite 1027 passed.

- [x] **Task 14:** Prevent duplicated text when pasting into the TUI.
   - **Details:** ctrl+v consumed at widget level (stop+prevent) plus _on_paste override with shared truncation; 5 real-dispatch regression tests; image paste untouched.
  - **Description:** Trace and fix the interaction between the custom `Ctrl+V`/clipboard action and Textual's native `events.Paste`/`TextArea._on_paste()` path. A single terminal paste must insert the clipboard contents exactly once, without breaking multiline paste, truncation, keyboard typing, or submit behavior. Preserve image-paste handling on `Ctrl+Shift+V`.
  - **Files:** `one/modes/tui_mode.py`, `tests/test_tui_mode.py`, possibly Textual-version compatibility code only if required.
  - **Dependencies:** None.
  - **Acceptance Criteria:** One user paste produces one text insertion; a paste followed by Enter submits one prompt containing the text once; multiline and 10,240-character truncation behavior remain correct; image paste remains separate and functional.
  - **Verification:** Add real Textual pilot/event tests for `ctrl+v`, `events.Paste`, both paths emitted for one paste, and end-to-end paste-then-submit. Run tests against the supported Textual version range where practical.
  - **Details:** Likely duplication boundary is `ctrl+v` → custom `_CommandTextArea.action_paste()` plus native bracketed-paste `TextArea._on_paste()`; current tests call `action_paste()` directly and do not exercise dispatch.

- [x] **Task 15:** Restore visible waiting-icon animation in the TUI.
  - **Description:** Ensure every waiting-animation frame mutation refreshes the `#stream` widget. `_tick_waiting()` currently updates an existing spinner line in `_stream_lines` but can omit `_render_stream()`, leaving the visible icon frozen or absent after recent stream-trimming changes. Preserve waiting predicates, cleanup on `turn_end`, and retry behavior.
  - **Files:** `one/modes/tui_mode.py`, `tests/test_tui_mode.py`, TUI snapshots only if rendering output intentionally changes.
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
  - **Files:** `one/modes/tui_mode.py`, `tests/test_tui_mode.py`.
  - **Dependencies:** None.
  - **Acceptance Criteria:** One `backspace` press deletes exactly one char; `delete` and arrows also single-step; printable typing, selection deletion, multiline join, paste paths, spinner, and shortcuts unchanged.
  - **Verification:** Real `pilot.press` regression tests (backspace/delete/arrows/printable/multiline/selection/repeat + no-regression for Task 14/15 and shortcuts); full suite and ruff.
  - **Details:** Reproduced pre-fix via Pilot: `hello@(0,5)+backspace → hel`, `delete → llo`, `left → 2 steps`. Task 14 masked this only for stopped keys.
  - **Details:** Fixed with `event.prevent_default()` after explicit `super()._on_key(event)`; 8 real-dispatch regression tests; full suite 1042 passed, ruff clean.

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
  - **Details:** TUI pilot/action + count validation + schema tests in `tests/test_tui_mode.py`, `tests/test_tui_image_input.py` (Textual paste, submit with refs, image-count rejection, `_extract_image_paths` multi-path handling).
  - **Files:** `one/modes/tui_mode.py`, `one/modes/print_mode.py`, `one/modes/rpc_mode.py`, `one/cli/main.py`, `one/core/agent_session.py`, `one/core/clipboard_image.py`, `one/resources/resource_loader.py`, `tests/test_tui_mode.py`, `tests/test_tui_image_input.py`, `tests/test_image_cli_rpc.py`, `tests/test_system_prompt.py`.
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
    `tests/test_tool_calling.py`, `tests/test_tui_mode.py`,
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
