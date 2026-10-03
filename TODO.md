# Active roadmap for `one`

This file contains only current, verified work. Completed implementation history
is available in [GitHub commit history](https://github.com/noxgle/one/commits/main/);
user-facing changes are recorded in [`CHANGELOG.md`](CHANGELOG.md). Do not keep
completed task logs or unprioritized feature proposals here. Record accepted
future work as GitHub issues.

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
