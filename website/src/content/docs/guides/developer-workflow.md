---
title: Developer workflow and runtime boundary
---

From a source checkout, create a virtual environment and install development
dependencies:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
export ONE_CODING_AGENT_DIR="$(mktemp -d)"
scripts/test.sh tools -x
```

Use a fresh isolated state directory in every shell that runs tests or ONE. Tests
involving providers and MCP use fakes, not live services. Select focused tests for
your change; `scripts/test.sh --help` lists the suites and
[Contributing to ONE](../handbook/development.md) describes what each one covers.

Before committing or opening a pull request, run the full gate. Focused suites are
local feedback, not a substitute for it:

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
git diff --check
```

When a change touches packaging, dependencies, entry points, or the container
image, also run `python -m build`, `twine check dist/*`, and
`scripts/test-docker.sh`; see the [release checklist](../library/release_checklist.md).
When it touches documentation, regenerate the inventories with
`scripts/generate_docs_reference.py` from the repository root instead of editing
them by hand.

In the pull request, report validation as you observed it: exact commands, the
revision and environment, and separate **passed**, **failed**, and **not run**
lists. Project conventions and release instructions live in `CONTRIBUTING.md` and
`AGENTS.md` in the checkout.

## Runtime boundary

`one/cli/main.py` wires services and modes. `one/core/agent_session.py` owns the
agent loop and `session_manager.py` owns sessions and evidence sidecars. The TUI
uses a worker runtime: live session access stays on its owner loop, while the
Textual UI communicates through the facade and detached JSON DTOs. Do not bypass
that boundary; test changes with `tests/test_tui_runtime_boundary.py` and
`tests/test_agent_runtime_facade.py`. See [architecture](../library/architecture.md)
and [tools](../reference/tools.md).
