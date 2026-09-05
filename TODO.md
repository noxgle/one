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
