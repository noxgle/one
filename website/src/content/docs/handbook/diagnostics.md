---
title: "Diagnostics"
---

### ONE Doctor

`one doctor` checks installation and settings offline, without starting a model,
provider, MCP server, or network check. It does **not** verify an endpoint,
model availability, credentials, or provider connectivity. After an update,
inspect and preview before approving a repair:

The check assesses local model selection and authentication, including runtime
fallbacks. Missing settings files alone do not imply a setup is needed; a local
model may be selectable by default even when its server is not running. An
unusable selection, uncertain assessment or fallback from configured defaults
requires review and produces exit code 1.

```bash
one doctor --check
one doctor --fix --dry-run --json
one doctor --fix
one doctor --setup
```

`--check` is the default; it, `--fix`, and `--setup` are mutually exclusive.
`--fix` plans safe settings migrations and asks for confirmation on a terminal;
`--yes` approves them without prompting and is valid only with `--fix`.
`--dry-run` requires `--fix` or `--setup` and writes nothing; setup's preview
describes the process rather than collecting choices. `--setup` requires an
interactive terminal, asks for scope, provider and model, then shows a redacted
diff and asks for confirmation. It does not collect credentials; use `/login`
separately in the interactive UI when needed (OAuth requires network access).
`-h`/`--help` shows all options. `--json` emits one report for check or fix,
but cannot be combined with setup. Invalid combinations return exit code 2;
0 means the report is OK (or setup succeeded), and 1 means action is required,
setup was cancelled, or a repair failed. A preview can be OK even when it
contains planned safe actions; it has not applied them.

JSON has `formatVersion: 1`, `issues`, `actions`, and `result` (`ok` or
`action_required`). Issues include ID, level, scope, description and status;
actions add classification, file, redacted diff and preconditions. Reports
avoid printing configuration values and paths. See
[Doctor details](../library/doctor.md) for migration rules, configuration sources,
write limitations, backups and manual recovery. Writes are Linux-only and
per-file, not a transaction: stop other editors before repair. Existing files
get a private backup; a newly created settings file has no previous backup.

The opt-in Docker diagnostic runner drives a persistent RPC session in an
isolated disposable workspace and produces bounded telemetry and reports:

```bash
.venv/bin/python scripts/diagnose_long_session.py --json
.venv/bin/python scripts/diagnose_long_session.py --duration 10 --skip-analysis --json
```

It requires Docker and a reachable configured model. Its container is a
containment aid, not a security boundary. Reports may contain sensitive
diagnostic data; artifacts are removed after a successful report unless
`--keep-artifacts` is used. See `--help` for workload, network, endpoint, and
telemetry options.

`--duration` is the scenario-admission window. The final admitted scenario gets
up to 60 seconds to drain after that deadline; reports distinguish a truncated
final scenario, clean shutdown, and runtime failure.

The offline write-tool regression harness uses a loopback fake OpenAI-compatible
provider inside an isolated `--network none` container; it never uses provider
credentials or outbound network access:

```bash
.venv/bin/python scripts/diagnose_write_tool.py --json
```

It writes only hashes and per-case assertions to `write-tool-reports/`; use
`--keep-artifacts` only when inspecting its disposable workspace. The Docker
smoke test is opt-in: `WRITE_TOOL_DOCKER_SMOKE=1 .venv/bin/python -m pytest -q tests/test_write_tool_diagnostic.py`.
