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
