# Task-level file checkpoints

Status: **opt-in MVP**, available through TUI `/checkpoints` commands. A checkpoint
is an undo record for direct ONE file mutations, not a workspace snapshot or a
security sandbox. Normal sessions keep their existing file-tool behavior unless
checkpoint recording is enabled.

## Usage

Use a persistent session on a POSIX system with descriptor-relative no-follow
filesystem support. In the TUI:

```text
/checkpoints enable
# Submit the task normally, then wait for completion.
/checkpoints list
/checkpoints undo TASK_ID
/checkpoints confirm TOKEN
```

`undo` only prepares a proposal: inspect the paths and restore/delete actions
before explicitly confirming the returned short-lived token. To select a subset,
use `/checkpoints undo TASK_ID path/to/file "path with spaces"`. Move endpoints
must be selected together. `/checkpoints cancel` invalidates outstanding tokens;
`/checkpoints delete TASK_ID` deletes a retained record without changing workspace
files. There is no force undo. List output is bounded and reports truncation.

Settings JSON `{"checkpoints": {"enabled": true}}` opts persistent sessions in
automatically. A session with an existing checkpoint sidecar resumes recording
even without that setting. Removing the setting does not discard retained data.
Checkpoint recording is not supported for in-memory sessions. If activation or
safe capture fails, covered mutations fail closed rather than run unrecorded.

## Task boundary and coverage

Each non-streaming `AgentSession.prompt()` invocation receives a fresh UUID.
Its model/tool iterations, retries and compaction share one checkpoint. Calls made
while streaming enqueue input instead of starting another checkpoint. Steering
consumed within the loop belongs to the current task; input drained into a new
prompt starts another task after the previous record closes. Finish, error,
abort and cancellation all close the current record before any queue drain.
Failed or aborted tasks can still contain successfully recorded, undoable changes.
Empty complete tasks do not shadow the previous undoable task.

Covered operations are direct `write`, `edit` and `apply_patch` dispatched by the
owning session. Patch Add/Update/Delete/Move operations are preflighted together;
both move endpoints are captured. The first preimage is the exact current file
bytes, existence and supported permission bits, including dirty or untracked
files. Git is not consulted. Later writes update the expected postimage, never
the original preimage. Undo can remove a created file or restore a deleted one.

Shell/bash commands (including manual shell), MCP tools, extension hooks,
subagent changes, external programs and direct user edits are **not covered**.
Children have separate sessions and are not aggregated into parent checkpoints.
Do not interpret an undoable record as coverage of all task effects.

## Safety and conflicts

Paths must be inside the workspace. Traversal, symlinks in any component,
hardlinks, special files, unsupported metadata, duplicate/ambiguous patch targets
and missing parent directories are rejected. Recording does not create or remove
directories. Checkpoint edits require valid UTF-8 rather than lossy decoding.
The engine uses descriptor-relative no-follow traversal and staged replacements,
checks workspace/parent identity, file identity, bytes, existence and mode, and
rejects unsupported platforms. These restrictions are stricter than ordinary
file tools. Existing policy/approval checks still precede tool dispatch.
Existing files require Python descriptor-based xattr enumeration; where `os.listxattr`
is unavailable (including standard macOS Python), they are rejected before
mutation rather than assumed metadata-free. Checkpoint transactions on macOS are
therefore limited; ordinary non-checkpoint file tools are unaffected.

Before later mutations and before undo, the engine checks against the expected
postimage. Intervening writes, mode changes, replacement identities and unsafe
paths block the operation; no force overwrite is offered. Identical-state ABA
(a write that restores the same observable state) cannot always be detected.

Undo is restricted to the latest eligible completed, complete, not-yet-fully-undone
task; a newer incomplete task blocks older undo. The session must be idle, without
active tools, manual shell, pending questions or running children, and must have
file-write capability. Readonly blocks undo. Prepare/confirm run on the session
owner loop through the host/facade using bounded detached JSON, not live objects
or saved bytes. Tokens are expiring, one-use and bound to session/runtime epoch;
new tasks, cancellation and stale session requests invalidate them. Confirmation
checks idle state, policy and file conflicts again.

All restore files are staged before restoration. Multi-file operations are **not
atomic against concurrent external writers**: a race or I/O failure can leave a
partial restore. The engine reports restored paths and marks the record
incomplete, disabling automatic retry. A failed mutation is likewise not advertised
as safely undoable, even if rollback succeeded. There is no automatic replay,
restore or crash recovery on startup.

## Storage, retention and limits

Private per-session `<session>.jsonl.checkpoints/` sidecars live under managed
session storage, outside the workspace. Directories use mode 0700 and manifests
and locks mode 0600, with ownership/type checks. Version-2 checksummed manifests
include session/workspace identity, task status, relative paths and private
base64 pre/postimages. File bytes are not added to JSONL history, provider context,
evidence or UI metadata. Normal file-tool diffs remain normal tool output.

The engine durably writes intent before mutation, uses atomic manifest publication
and fsync, checks storage revisions and uses a nonblocking lock. Missing/corrupt
records are rejected; interrupted active/intended/undoing records become
incomplete, never automatically undoable. The manifest is checksummed, not signed:
it does not protect against an attacker with the user's permissions.

Fixed limits are 100 paths per task, 1 MiB per preimage or postimage, 10 MiB total
preimage bytes per task and 50 MiB serialized retained data per session. Move
endpoints count separately and base64/manifest overhead counts toward storage.
Reservations precede mutation. Limit/disk errors reject the operation; there is no
silent eviction. Explicitly delete old records when retention reaches the ceiling.

Resume loads only the same session and workspace identity. Forks do not copy
checkpoint sidecars and record only their own future tasks if enabled. Session
switches isolate managers; session deletion removes the sidecar using guarded
directory deletion. Saved bytes may contain secrets and deleted content: retention
is local but **not encrypted**, and deletion is **not secure erasure**. Backups and
filesystem snapshots can retain deleted data.

## Implementation and verification

`one/core/checkpoints.py` owns validation, persistence, transactions and undo.
`AgentSession` owns task lifetime and file-tool dispatch; `SessionManager` owns
sidecar location and deletion. `TuiRuntimeHost` and `agent_runtime_facade` expose
list/prepare/confirm plus enable/cancel/delete commands. The TUI never restores
files directly. No new model-facing undo tool is advertised.

Coverage includes engine filesystem safety, dirty/untracked baselines, selective
undo and move pairing, limits/corruption/restart, injected transaction failures,
prompt lifecycle, session resume/fork/delete, detached runtime epoch/policy gates,
and TUI command confirmation. Run the `tests/test_checkpoint*.py`,
`tests/test_agent_checkpoint_integration.py` and `tests/test_tui_checkpoints.py`
modules explicitly, alongside the runtime/session/event suites and full CI gates.
