# TODO — Roadmap for the autonomous agent `one`

Completed work is recorded in [`DONE.md`](DONE.md). User-facing release notes
are in [`CHANGELOG.md`](CHANGELOG.md).

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

## Project: TUI/UX fix batch (`fix/tui-ux-batch`)

### Goal

Fix seven reported UX gaps without changing agent behavior otherwise:
Ctrl-C handling of approval prompts, version visibility, unlimited retry mode
with shortcut, Ctrl+Shift+V shortcut listing, Codex image support, visible
bash timeout in TUI, and the `/login` help text. Work happens on branch
`fix/tui-ux-batch` (based on `main`); no merge/push without approval.

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
- [ ] `ruff` clean, full `pytest` green, no unrelated behavior changes.
- [ ] No merge/push without explicit approval.

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
