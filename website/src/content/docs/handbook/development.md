---
title: "Development"
---

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
export ONE_CODING_AGENT_DIR="$(mktemp -d)"
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
git diff --check
```

The temporary state directory keeps tests away from real credentials and
sessions. Export it again in each new shell used for tests or manual CLI checks.

See [CONTRIBUTING.md](https://github.com/noxgle/one/blob/main/CONTRIBUTING.md) for contributor conventions and focused
test commands. Linux and macOS are supported in CI. Windows is currently
best-effort and is not an officially supported platform yet; shell-oriented
behavior remains experimental pending Windows CI. Windows 11 is the planned
future target, while Windows 10 support is deferred.
