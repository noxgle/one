# Release Checklist

Reproducible commands for verifying the `one-agent` package before publication.
Run from the repository root.

## Prerequisites

```bash
# Activate the virtual environment (already created during setup)
source .venv/bin/activate
```

## 1. Full test suite

```bash
python -m pytest -q
```

## 2. Lint

```bash
python -m ruff check .
```

## 3. Build packages

```bash
python -m build
```

Produces `dist/one-agent-*.tar.gz` and `dist/one-agent-*.whl`.

## 4. Twine check (wheel + sdist)

```bash
twine check dist/*
```

## 5. Dependency consistency

```bash
pip check
```

## 6. Fresh install outside checkout

```bash
# Install into a temporary venv from the built wheel
TMPDIR=$(mktemp -d)
python3 -m venv "$TMPDIR/test-env"
# PYTHONPATH must NOT be set so the test resolves the installed package,
# not the source checkout.
env -u PYTHONPATH "$TMPDIR/test-env/bin/python" -c "import one; print(one.config.VERSION)"
env -u PYTHONPATH "$TMPDIR/test-env/bin/python" -c "from one.modes.print_mode import run_print_mode; print('import ok')"

# Same for the sdist (rebuild first, then install)
python -m build --sdist
env -u PYTHONPATH "$TMPDIR/test-env/bin/python" -m pip install --no-deps "$(ls dist/one-agent-*.tar.gz)"
env -u PYTHONPATH "$TMPDIR/test-env/bin/python" -c "import one; print(one.config.VERSION)"

rm -rf "$TMPDIR"
```

## 7. Archive allowlist inspection

```bash
# List all files in the wheel
unzip -l dist/one-agent-*.whl

# List all files in the sdist
tar tzf dist/one-agent-*.tar.gz
```

Verify archives exclude: `tests/`, `.one/`, `*.egg-info/`, `__pycache__/`,
`.git/`, credentials, and any private blobs.

## 8. Gitleaks — full-history scan

```bash
# Check the installed version's flag syntax first
gitleaks --help | grep -i redact || gitleaks version

# Full-history redacted scan (output redacted; secrets must never be printed)
gitleaks detect --source . --report-format json --report-path /tmp/gitleaks-report.json
# Inspect only finding types (not raw secrets):
jq '[.[].RuleID] | unique' /tmp/gitleaks-report.json
```

**Note:** Syntax (`--report-format`, `--report-path`) varies by gitleaks version.
Always check `gitleaks --help` for the exact flags on the installed version.

## 9. pip-audit — resolved-environment scan

```bash
pip-audit --requirement /dev/null  # scan the current resolved environment
# Or for specific packages:
pip-audit -r requirements.txt  # if a requirements file is maintained
```

## 10. License & fixture provenance

- Review all third-party licenses in `.venv/lib/python*/site-packages/*/LICENSE*`
- Verify test image fixtures (PNG/JPEG/WebP) are self-generated (see `_make_png`,
  `_make_jpeg` in test files) or have permissive licenses

## Notes

- **Version rule:** user-visible changes bump `VERSION` per `AGENTS.md → Versioning`
  (pinned tests, TUI goldens, and `CHANGELOG → Unreleased` move with it).
- **Secret/history scanning and GitHub security controls remain manual
  maintainer steps.** These are not automated in CI and must be run by an
  authorized maintainer with the necessary permissions.
- The `ONE_CODING_AGENT_DIR` environment variable must point to a scratch dir
  during tests (every CLI subprocess test uses a `tmp_path`-backed agent dir).
- CLI subprocess tests strip all inherited provider API keys via
  `_PROVIDER_KEY_NAMES` in `tests/test_image_cli_rpc.py`.
