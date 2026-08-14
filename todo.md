# Project: one — batch fix: system prompt schemas, edit tool, MCP commands, auto-compact check

## Goal
Answer 4 user reports: (1) add missing `spawn_subagent`/`ask_user` arg schemas to the system prompt tool list, (2) fix the `edit` tool (schema mismatch + multi-edit corruption) and add `/mcp list|enable|disable` commands + MCP docs, (3) verify auto-compaction default.

## Context
User feedback after TUI work:
- The system prompt lists `spawn_subagent {}` (no schema/description) — `TOOL_ARG_SCHEMAS` in `one/resources/resource_loader.py` (lines 43-52) lacks `spawn_subagent` and `ask_user`.
- MCP client is implemented (`one/mcp/client.py`, config `mcpServers` in settings.json) and MCP tools are appended to the system prompt, but there are no `/mcp` commands and no docs on how to add servers.
- `edit` tool: prompt documents `{oldString, newString}` but the implementation reads `oldText`/`newText` (KeyError); sequential `str.replace` corrupts multi-edits when an earlier replacement introduces text matching a later oldText.
- Auto-compaction: `DEFAULT_SETTINGS["compaction"]["enabled"] = True` already (settings_manager.py:17) + fallback `True` in `AgentSession.auto_compaction_enabled` — nothing to change; report only.

## Non-Goals
- No changes to MCP protocol transport (stdio only stays).
- No rename of `/bash-show` or settings keys.
- No new settings UI beyond the three `/mcp` commands.

## Tech Stack
- Python 3.12, pytest (284 tests), Textual TUI, asyncio.

## Constraints
- Do not touch real `~/.config/one` / `.one/` data.
- Keep event contract stable (tests/test_event_snapshots.py).
- Full suite must pass: `.venv/bin/python -m pytest -q`.

## Architecture
- System prompt is rebuilt per provider call (`_flatten_messages_for_provider` → `_build_runtime_system_prompt`), so live MCP enable/disable is visible to the model on the next turn step.
- MCP tools resolve before native tools (`agent_session.py:477`), so `_active_tools` must keep native tools separate from MCP tools (`_base_tools` + mcp names).
- `/mcp` commands persist the `enabled` flag in settings.json AND live-start/stop the server so the change applies immediately.

## Phases

### Phase 1: system prompt — spawn_subagent + ask_user schemas
**Objective:** model knows the args of `spawn_subagent` and `ask_user`.
**Estimated effort:** ~30 min

- [ ] **Task: add TOOL_ARG_SCHEMAS entries**
  - **Description:** In `one/resources/resource_loader.py` add:
    - `"spawn_subagent": "{task, tasks?, model?, tools?}  # delegate a subtask to an isolated subagent (returns summary)"`
    - `"ask_user": "{question, timeoutSec?}  # ask the human a question and wait for their answer"`
    - Also fix `edit` entry to `{path, edits: [{oldString, newString}]}` (already correct in prompt; keep).
  - **Files:** `one/resources/resource_loader.py`
  - **Acceptance Criteria:** `get_system_prompt(selected_tools=[...all tools...])` contains `- spawn_subagent {task, tasks?, model?, tools?}` and `- ask_user {question, timeoutSec?}`.
  - **Verification Commands:** `.venv/bin/python -m pytest -q`
  - **Dependencies:** none

### Phase 2: fix edit tool
**Objective:** `edit` stops erroring on documented keys and applies multi-edits correctly.
**Estimated effort:** ~1h

- [ ] **Task: normalize keys + span-splice application**
  - **Description:** In `one/tools/edit.py`:
    1. Accept both `oldString`/`newString` (documented) and `oldText`/`newText` (legacy). Missing both → clear ValueError.
    2. Apply edits by splicing the ORIGINAL text at validated (non-overlapping, unique) spans sorted by start — never sequential `str.replace`.
    3. Keep diff/firstChangedLine output contract.
  - **Files:** `one/tools/edit.py`, `tests/test_tools.py`
  - **Acceptance Criteria:** new unit tests: oldString keys work; multi-edit where an earlier newText contains a later oldText applies at the original location; unknown-key edit raises a clear error.
  - **Verification Commands:** `.venv/bin/python -m pytest -q tests/test_tools.py && .venv/bin/python -m pytest -q`
  - **Dependencies:** none

### Phase 3: MCP — /mcp list|enable|disable + docs
**Objective:** user can inspect and toggle MCP servers from TUI/interactive; README documents the config.
**Estimated effort:** ~3h

- [ ] **Task: settings — server enabled flag**
  - **Description:** Add `set_mcp_server_enabled(name, enabled)` to `one/core/settings_manager.py` (persists `mcpServers.<name>.enabled` in global settings). `get_mcp_servers()` already exists.
  - **Files:** `one/core/settings_manager.py`
  - **Acceptance Criteria:** after `set_mcp_server_enabled("x", False)`, `get_mcp_servers()["x"]["enabled"] is False`.
  - **Dependencies:** none

- [ ] **Task: McpManager — enable/disable/status + skip disabled at start**
  - **Description:** In `one/mcp/client.py`:
    - `create()`: skip configs with `enabled is False`.
    - `enable_server(name, command, args, env)` → starts client, registers tools, returns tool names.
    - `disable_server(name)` → closes client, removes its tools, returns removed names.
    - `server_status()` → per configured server: name, enabled, command, status (running/error/not-started), tool names, error.
  - **Files:** `one/mcp/client.py`, `tests/test_mcp.py`
  - **Acceptance Criteria:** unit tests using the existing FAKE_SERVER_SRC: enable registers tools, disable removes them, create skips disabled, status reflects state.
  - **Dependencies:** settings task

- [ ] **Task: session — sync MCP tools**
  - **Description:** In `one/core/agent_session.py`: remember `_base_tools` (native tools) at init; add `sync_mcp_tools()` that sets `_active_tools = _base_tools + [t.name for t in mcp manager tools]`.
  - **Files:** `one/core/agent_session.py`
  - **Acceptance Criteria:** after sync, `_active_tools` contains mcp names; after disable+sync, they are gone.
  - **Dependencies:** McpManager task

- [ ] **Task: /mcp commands in TUI + interactive**
  - **Description:** Add to both modes: `/mcp` (usage), `/mcp list` (server_status lines), `/mcp enable <name>` (read config from settings, persist enabled=true, live enable, report tools), `/mcp disable <name>` (persist enabled=false, live disable, report removed tools). Unknown server → error with hint to add `mcpServers` in settings.json. Manager None (e.g. `--no-mcp`) → info message.
  - **Files:** `one/modes/tui_mode.py`, `one/modes/interactive_mode.py`, `tests/test_tui_mode.py`, `tests/test_interactive_mode.py`
  - **Acceptance Criteria:** tests drive the commands with a fake McpManager double; `/help` lists `/mcp`.
  - **Dependencies:** session sync task

- [ ] **Task: README MCP docs**
  - **Description:** Add an MCP section: settings.json config example (`mcpServers: {name: {command, args, env, enabled}}`), `--no-mcp` flag, `/mcp list|enable|disable` commands, note that MCP tools appear in the system prompt.
  - **Files:** `README.md`
  - **Acceptance Criteria:** section exists with a copy-pasteable example.
  - **Dependencies:** none

### Phase 4: auto-compaction default (report only)
**Objective:** confirm/communicate that the default is already `true`.
**Estimated effort:** 0

- [ ] **Task: no code change — verify and report**
  - **Description:** `DEFAULT_SETTINGS["compaction"]["enabled"] = True` and session fallback `True`. If a user sees it off, their settings.json has a persisted `false`. Report in summary; no change.
  - **Files:** none
  - **Acceptance Criteria:** summary explains where to check (`/config compaction.enabled` or settings.json).
  - **Dependencies:** none

## Risks & Mitigations
| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| MCP live enable touches session internals | medium | low | keep `_base_tools` snapshot; unit tests for sync |
| edit splice rewrite breaks existing tool tests | high | medium | preserve output contract; run full suite |
| Two parallel coders touch overlapping test files | low | low | split by file ownership (Phase 1+2 vs Phase 3) |
| Fake MCP server tests flaky | low | low | reuse existing FAKE_SERVER_SRC pattern |

## Acceptance Criteria
- [ ] System prompt lists `spawn_subagent` and `ask_user` with full arg schemas.
- [ ] `edit` works with `oldString`/`newString` and multi-edit collisions; legacy `oldText`/`newText` still works.
- [ ] `/mcp list`, `/mcp enable <name>`, `/mcp disable <name>` work in TUI and interactive; changes take effect immediately.
- [ ] README documents MCP config and commands.
- [ ] `.venv/bin/python -m pytest -q` — 284+ tests pass.

## Estimated Timeline
~4-5h total: Phase 1 (30m), Phase 2 (1h), Phase 3 (3h), Phase 4 (0). Parallelizable: Phases 1+2 vs Phase 3.
