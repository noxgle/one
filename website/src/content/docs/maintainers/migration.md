---
title: Content migration and audit status
---

## Migration map (2026-11-15)

| Previous source | Canonical portal location | Review source |
| --- | --- | --- |
| README installation, update, modes, TUI, slash commands | `handbook/` chapters with matching titles | `one/cli/args.py`, `one/modes/tui_commands.py` |
| README providers, configuration, sessions, context, images, safety, integrations, Doctor | `handbook/` chapters with matching titles | provider registry, settings manager, session runtime |
| `docs/QUICKSTART.md`, `EXAMPLES.md`, `FAQ.md`, `DOCKER.md` | `library/quickstart`, `examples`, `faq`, `docker` | existing guides retained |
| `docs/PRESETS.md`, `SKILLS.md`, `EXTENSIONS.md` | matching lowercase `library/` articles | policy/presets and resource loader |
| `docs/RUNS.md`, `COMPACTION.md`, `CHECKPOINTS.md` | matching lowercase `library/` articles | run context, compaction, checkpoint implementation |
| `docs/DOCTOR.md`, `MODEL_CATALOG.md` | matching lowercase `library/` articles | diagnostics and model registry |
| `docs/architecture.md`, `RELEASE_CHECKLIST.md` | `library/architecture`, `release_checklist` | runtime and contributor workflow |
| `CONTRIBUTING.md` (repository root) | `handbook/development` mirrors the join, test and validation workflow | `CONTRIBUTING.md`, `AGENTS.md`, `scripts/test.sh --help`, CI workflows |
| New task guides | `guides/` | configuration, auth storage, RPC and runtime code |
| New generated inventories | `reference/cli`, `settings`, `tools` | offline generator and Python source |

Old Markdown paths remain as short pointers. No second independently maintained
copy is kept. The README is an entry point. Phase reports, benchmarks and
implementation status have been retained in the in-depth material, not discarded.
They are engineering context, not promises that experimental features are stable.

## What was audited

The new reference generator reads CLI parser declarations, declared settings
defaults, tool registration and prompt schemas. The new guides describe state
precedence, authentication, readonly boundaries and JSON-lines RPC. All inventories
can be regenerated without calling a provider or reading actual user state.

## Explicit publication gaps

This initial portal does **not** claim the proposal's exhaustive semantic audit:

- CLI inventory shows declarations; subcommand handlers and validation still need
  a human-readable option-by-option contract (including package/config commands).
- Settings defaults are complete for `DEFAULT_SETTINGS`, not all optional,
  normalized, provider-profile and run-budget keys. Allowed ranges, scope,
  precedence exceptions and application timing need per-field review.
- Tool schemas cover all built-ins; result shapes, operational limits, failure
  cases and policy interactions still need per-tool contract tables.
- RPC includes a minimal conversation, not the full event/error/cancellation
  reference. The EOF behavior is documented as an implementation limitation.
- Existing articles were migrated, not reverified line by line. Live provider,
  local inference, external links and production server behavior are not validated
  by the offline build. Do not represent these as verified production examples.

These gaps are release gates for declaring full completion of the original
proposal, not hidden claims of coverage. Repository `TODO.md` tracks them.
