# ONE Doctor

Doctor is an offline diagnostic, migration and setup command. See `one doctor
--help` for the CLI and the [README](../README.md#one-doctor) for examples and
exit codes. Run it from the project whose settings you want to inspect. Check
and dry-run do not write; neither tests filesystem writability with a write
probe. Doctor does not call a provider, test credentials or check network,
endpoint or model availability. A locally configured authentication method is
not proof that it works. If Python is too old to import ONE, Doctor itself may
not start and cannot report the problem.

Read-only diagnostics are not Linux-specific. The secure settings writer used
by repair and setup is Linux-only; on macOS and other unsupported platforms,
write planning or application fails closed rather than changing settings.

## Sources and setup

Doctor reads global `settings.json` from `ONE_CODING_AGENT_DIR` when set;
otherwise it uses `$XDG_CONFIG_HOME/one` (or `~/.config/one`). If the XDG
location is absent and legacy `~/.one/agent` exists, Doctor inspects the legacy
location without migrating it. It also reads the current project's
`.one/settings.json`; project settings generally override global settings,
which override defaults. Some settings have special rules, such as global-only
`tui.manualBashMode`. Coexisting legacy and current state warrants manual review;
Doctor does not copy private state between them.

Check assesses effective settings, locally registered models and available auth
without contacting a provider. An absent settings file alone is not a readiness
failure: runtime may select a local model without credentials. A missing or
unknown usable selection, or a fallback from configured defaults, requires review
and yields `action_required` (exit 1). Even a locally selectable model does not
establish that its server, model or credentials work.

`--setup` reads local settings and model/auth metadata, asks for a global or
project scope, a supported provider and a model ID, and displays a redacted
diff before confirmation. It preserves credentials and existing unrelated
settings. Login, including OAuth, is a separate interactive `/login` operation;
it may require network access. Setup's local readiness result does not verify
the provider. Its dry-run only describes the steps and runs checks; it does not
simulate chosen values. Setup may create the selected parent directory and a
new settings file after confirmation.
Custom Ollama and llama.cpp model IDs are selected directly at runtime without
writing `models.json`; their availability is still unverified. A global setup
that conflicts with project provider or model overrides is rejected before any
write; choose project scope or update the project settings instead.

## Reports and repair

`--check` is the default; `--fix` plans migrations for existing settings files.
A terminal prompt approves safe changes; in noninteractive use, pass `--yes`
to approve them. `--fix --dry-run` only previews. `--json` is supported for
check and fix, not setup. JSON format version 1 contains `issues` and `actions`
arrays and a `result` of `ok` or `action_required`. Issues have `id`, `level`,
`scope`, `description`, and `status`; actions additionally have
`classification`, `file`, `diff`, and `preconditions`. Action statuses include
`planned`, `applied`, `declined`, `conflict`, `failed`, `rolled_back` and
`unsafe_rollback`. Values and actual paths are not included in the report's
redacted diffs. Preview success does not mean a planned repair was applied.
Unknown settings are reported for review, not automatically removed; invalid,
unreadable, future-version or ambiguous layers can block migration. Fix does
not modify authentication or sessions.

After upgrading, run `one doctor --check`, then `one doctor --fix --dry-run
--json`, review the report and use `one doctor --fix` only when appropriate.
Resolve manual-review issues yourself and rerun check. Do not assume a
successful migration resolves unrelated warnings.

## Schema and future migrations

`configVersion` is per settings file and independent of the ONE release and
JSON report format versions. Missing means version 0; the current schema is 1.
It must be a non-negative integer (not a boolean); invalid or future versions
are not migrated. Version 0 to 1 copies `subagents.timeoutSec` to
`idleTimeoutSec` only when the latter is absent, retaining the old key. The
migration checks both raw layers and refuses ambiguous changes to the selected
source or value. Unknown data is preserved. No file is created by fix merely
to add a version. Setup updates provider/model fields but is not a schema
migration.

For a future schema, add a pure one-version step to the registry in
`one/core/doctor/migrations.py`, increment `CURRENT_CONFIG_VERSION`, and keep
both layers in mind when preserving effective behavior. Missing steps, invalid
context and conflicts must leave the original unchanged. Extend
`tests/test_doctor_migrations.py` and relevant repair tests for old and current
versions, sequential steps, idempotence, unknown-field preservation, invalid
and future versions, and cross-layer conflicts; also review report redaction
and dry-run output for the new version. Do not silently reinterpret ambiguous
data.

## Writes, rollback and recovery

Writes use the Linux-only settings writer, in a trusted directory, and are
per-file rather than a multi-file transaction. Stop ONE and other editors before
repair. The writer rechecks snapshots of both settings layers and target
metadata, uses an advisory lock and replaces a single file; a non-cooperating
writer can still race between the final check and rename or rollback. Locks do
not provide a compare-and-swap guarantee. A failure after one file is applied
does not undo earlier files. Symlinks in the path, hard-linked or non-regular
targets, special mode bits and extended attributes including ACLs are rejected;
the filesystem must support directory fsync and xattr enumeration. Parent
permissions are not tightened by the writer.

Before changing an existing file, the writer retains a private mode-0600
`.settings.json.backup-*` in the same directory. A newly created file has no
previous file to back up. The `.settings.json.lock` file is intentionally
retained. A crash can leave a backup or temporary file. On a post-replacement
failure the writer attempts rollback only if it can safely identify its own
write; `rolled_back` means that attempt succeeded, while `unsafe_rollback`
means manual inspection is necessary. Setup may show only a generic failure;
reports do not expose backup paths. A moved directory can make a backup's path
unknown.

An interruption before replacement is reported as `interrupted`. If replacement
may have happened but the writer cannot confirm its own version, it reports
`unsafe_rollback` and retains the backup instead of overwriting the file. An
interruption after a confirmed replacement can report `rolled_back` if recovery
is verified. Earlier successful actions remain `applied`; later actions are
not attempted. Abrupt termination or repeated interruption can still prevent a
report or leave temporary files.

For recovery, stop writers, inspect both settings files and private backups in
their respective directories, and compare them securely before restoring the
appropriate backup manually. Preserve any newer changes rather than blindly
overwriting them. For a file newly created by setup there is no old backup;
if appropriate remove it manually after checking for subsequent changes. Check
ownership and permissions, then rerun `one doctor --check` and review remaining
issues. Backups can contain sensitive configuration; protect and dispose of
them appropriately.
