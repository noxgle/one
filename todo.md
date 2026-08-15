# Project: one — TUI info panel (MCP list, full height) + tool timeout semantics

## Goal

1. **Bash timeout:** the model decides the per-command timeout. If the model does not pass `timeout`, the configured default (`tools.timeoutSec`, 30 s) applies. A timed-out command must surface a clear `Command timed out` result to the model — not the ambiguous `(cancelled)` — and the model's explicit `timeout` must actually be honored (today it is silently overridden by the 30 s outer `asyncio.wait_for`).
2. **MCP tools:** same rule — the model decides (optional `timeout` in args); otherwise the MCP client's own default (120 s) applies. No 30 s outer cap.
3. **TUI info panel (sidebar):** stretched to the full available height (currently content-height ≈ half screen) and shows the list of enabled MCP clients.

## Context

- `one/tools/bash.py::bash_tool` has its own `timeout` param (from model args) producing a clear `"Command timed out"` result (`cancelled: False`).
- `one/core/agent_session.py::_execute_tool_by_name` additionally wraps every tool call in `asyncio.wait_for(task, timeout=tool_timeout_sec)` (settings `tools.timeoutSec`, default 30). When that outer timeout fires, `wait_for` cancels the task; `bash_tool` catches `CancelledError` and returns `{"cancelled": True, "content": "(cancelled)"}`; because the task suppresses the cancellation and returns a dict, `wait_for` returns it instead of raising `TimeoutError`. The model sees only `(cancelled)` and its explicit `timeout` arg (e.g. 120) is ignored. Reproduced in `/tmp/opencode/repro_timeout.py`.
- The system prompt hardcodes `"Default timeout 30s if not specified."` (`one/resources/resource_loader.py`) and does not reflect the actual `tools.timeoutSec`; the bash schema `{command, timeout?}` has no semantics hint.
- MCP calls go through the same outer `wait_for` (30 s cap) even though `McpClient.call_tool` already has its own 120 s timeout — same bug class.
- TUI sidebar (`one/modes/tui_mode.py`): `#sidebar` CSS has `width: 42` but **no `height`** → Textual sizes it to content (≈ half screen). `build_sidebar_snapshot()` + `_refresh_sidebar()` render the info panel; `session._mcp_manager.server_status()` already exposes per-server `name/enabled/running/tools/transport/error`.

## Scope

### In Scope
- Bash timeout semantics: model's explicit `timeout` honored; default = `tools.timeoutSec`; outer `wait_for` demoted to a backstop (effective + 5 s grace).
- MCP tool timeout: optional `timeout` in args honored; otherwise MCP client default (120 s); no outer 30 s cap.
- System prompt: dynamic default-timeout text + bash schema hint; MCP tools section notes the optional `timeout` arg.
- TUI sidebar: full-height layout; enabled MCP clients listed (name, tool count, transport, error marker); `MCP: off` / `MCP: none` states.
- Tests: new timeout-semantics tests, sidebar snapshot tests, golden SVG regeneration.

### Non-Goals
- No changes to the agent event contract (`tool_call_start/end`, `turn_*`, snapshots in `test_event_snapshots.py`).
- No changes to `ask_user` / `spawn_subagent` timeout handling (already excluded from the outer cap).
- No changes to `/mcp list|enable|disable` command behavior.
- No new settings keys; `tools.timeoutSec` remains the default, not a hard cap.
- No changes to `execute_bash` (the `/bash` command path) — it already passes the settings timeout directly.

## Assumptions

- The model's explicit `timeout` may exceed `tools.timeoutSec` (user decision: "model decyduje o wielkosci timeout, jezeli nie poda to jest wstawiane 30s"). User abort (Ctrl+C) remains the safety valve.
- For MCP tools the model may pass `timeout` in `args` even though it is not part of the server's input schema; it is consumed by `one` and not forwarded to the MCP server.
- `tools.timeoutSec` default stays 30 s; the prompt text is generated from the actual setting.
- Golden TUI snapshots will change (sidebar content/height) and must be regenerated with `ONE_UPDATE_SNAPSHOTS=1`.

## Open Questions

- None blocking. (Optional future: hard cap on model-provided timeout — explicitly out of scope.)

## Tech Stack

- **Python 3.12+ / asyncio:** `one/core/agent_session.py` (tool loop, `wait_for`), `one/tools/bash.py` (subprocess timeout), `one/mcp/client.py` (MCP clients).
- **Textual:** `one/modes/tui_mode.py` (sidebar widget, CSS).
- **pytest:** only gate (`testpaths = tests`, no linter/typechecker).

## Constraints

- Keep the event contract stable (`test_event_snapshots.py`, `test_tool_calling.py` assert exact sequences/payloads).
- `McpManager.call_tool` has exactly one caller (`agent_session.py:480`) — signature change is safe with a default param.
- Tests must keep using `SettingsManager.in_memory(...)` / scratch `ONE_CODING_AGENT_DIR`; never touch real `~/.config/one` or `.one/`.
- No new dependencies.

## Architecture

### Timeout flow (after fix)

```
model tool call: {"tool":"bash","args":{"command":..., "timeout": N?}}
  -> _execute_tool_by_name("bash", args, timeout_sec=settings.timeoutSec)
       effective = args.timeout  or  timeout_sec  (settings default)
       bash_tool(cwd, command, effective, prefix)      # inner timeout, clear "Command timed out"
       outer wait_for = effective + 5 s grace           # pure backstop
  -> MCP tool: args.timeout ? client.call_tool(..., timeout=args.timeout)
                            : client.call_tool(...)     # client default 120 s
       outer wait_for = args.timeout + 5 s grace  or  None
```

- Timeout → model sees `"Command timed out"` (adaptable: retry with longer timeout).
- User abort (Ctrl+C) → `(cancelled)` — now distinguishable from timeout.
- Other tools (read/write/edit/grep/find/ls): unchanged outer cap (settings timeout).

### Sidebar (after fix)

```
#sidebar { width: 42; height: 1fr; overflow-y: auto; ... }   # full height
Info section gains:
  MCP: demo(3,stdio) web(5,http)      # enabled servers, tool count, transport, '!' on error
  MCP: off                            # started with --no-mcp (no manager)
  MCP: none                           # no enabled servers configured
```

## Architecture Decisions

### ADR-001: Model decides the bash timeout; settings value is the default
**Decision:** `effective_timeout = args.get("timeout") or tool_timeout_sec` (settings, default 30 s). The effective value is passed into `bash_tool` (inner timeout, clear message) and the outer `asyncio.wait_for` becomes a backstop at `effective + 5 s` (`_TOOL_TIMEOUT_GRACE_SEC`).

**Alternatives:**
- Keep the outer 30 s cap (status quo) — model's `timeout` stays useless above 30 s; user explicitly rejected this.
- Cap model timeout at `tools.timeoutSec` — defeats the parameter's purpose; rejected by user.
- Remove the outer `wait_for` entirely for bash — no protection against a hang in `bash_tool` cleanup; backstop is safer.

**Rationale:** matches the documented schema (`{command, timeout?}`) and the user's rule; makes timeout results actionable for the model; keeps a safety net.

**Tradeoffs:** a model can request a very long timeout (session appears hung) — mitigated by Ctrl+C abort; no hard cap (out of scope).

### ADR-002: MCP tools get the same rule (model decides; client default 120 s)
**Decision:** `McpManager.call_tool(name, args, timeout: float | None = None)` threads an optional timeout to the client (`client.call_tool(..., timeout=timeout or 120.0)`). The outer `wait_for` for MCP tools uses `args.timeout + 5 s` when provided, else `None` (client's own 120 s rule governs).

**Alternatives:**
- Keep the 30 s outer cap on MCP calls — same "cancelled" bug class for MCP tools; user chose the symmetric rule ("podobnie jak w punkcie 1").
- Hardcode outer 120 s for MCP — duplicates the client's own timeout; no benefit.

**Rationale:** one consistent rule across tool types; MCP client already owns a sane default.

**Tradeoffs:** MCP calls can now run up to 120 s (or model-specified) — intended; abort still works.

### ADR-003: Dynamic system prompt for the default timeout
**Decision:** `_BASE_PROMPT` gains a `__DEFAULT_TOOL_TIMEOUT__` placeholder replaced in `get_system_prompt()` with `getattr(settings_manager, "get_tool_timeout_sec", lambda: 30)()`; `TOOL_ARG_SCHEMAS["bash"]` becomes `"{command, timeout?}  # timeout in seconds; default if omitted"`; the MCP tools section in `_build_runtime_system_prompt` notes the optional `timeout` arg.

**Alternatives:**
- Keep the hardcoded "30s" — stale when `tools.timeoutSec` changes; user's complaint explicitly suspected missing timeout info.
- Add a new settings key for prompt text — unnecessary indirection.

**Rationale:** the model must know the real default to decide correctly; minimal change, existing tests assert only the `"- bash {command, timeout?}"` prefix (still matches).

**Tradeoffs:** prompt changes slightly per settings; no downside.

### ADR-004: Sidebar full height + MCP section
**Decision:** `#sidebar` gets `height: 1fr; overflow-y: auto;`. `build_sidebar_snapshot()` gains `mcpEnabled` (bool) and `mcpServers` (list of enabled servers: `name`, `transport`, `toolCount`, `running`, `error`) sourced from `getattr(session, "_mcp_manager", None).server_status()` filtered by `enabled=True`; `_refresh_sidebar()` renders the compact `MCP:` line(s).

**Alternatives:**
- Keep content-height sidebar — user explicitly wants full height.
- Reuse `/mcp list` output in the stream — not persistent; user asked for the info panel.

**Rationale:** one-line CSS fix; snapshot function stays pure/testable; handles `--no-mcp` and empty config gracefully.

**Tradeoffs:** sidebar content grows; `overflow-y: auto` handles small terminals; golden snapshots must be regenerated.

## Phases

### Phase 1: Tool timeout semantics — model decides

**Objective:** bash and MCP tool calls honor the model's explicit `timeout`; default = `tools.timeoutSec` (bash) / client 120 s (MCP); timeouts surface as `Command timed out`, not `(cancelled)`; the model is told the real default.

**Prerequisites:** None.

**Expected outcome:** `pytest -q` green; new tests prove: model timeout > settings honored; missing timeout → settings default with "timed out" message; prompt contains the actual default; MCP optional timeout threaded through.

**Estimated effort:** ~2 h

**Confidence:** High

- [x] **Task 1.1: Effective bash timeout + outer backstop in `_execute_tool_by_name`**
  - **Description:**
    - Add module constant `_TOOL_TIMEOUT_GRACE_SEC = 5` in `one/core/agent_session.py`.
    - bash branch: `effective = args.get("timeout") or timeout_sec or self.settings_manager.get_tool_timeout_sec()`; call `bash_tool(cwd, command, effective, shell_prefix)`.
    - Outer `wait_for`: for bash use `effective + _TOOL_TIMEOUT_GRACE_SEC`; for MCP tools use `(args.get("timeout") or 0) + grace` when a timeout was passed, else `None` (no outer cap); all other tools unchanged (`timeout_sec`).
    - MCP branch: `return await self._mcp_manager.call_tool(tool_name, args, timeout=args.get("timeout"))` (None → client default).
  - **Files:** `one/core/agent_session.py`, `one/mcp/client.py` (`McpManager.call_tool` gains `timeout: float | None = None`, passes `timeout or 120.0` to the client).
  - **Dependencies:** None
  - **Acceptance Criteria:**
    - Model passes `timeout: 3` with settings `timeoutSec: 1` and `sleep 2 && echo ok` → tool result ok, output contains `ok`, not cancelled.
    - Model passes no timeout with settings `timeoutSec: 1` and `sleep 3` → tool result contains `timed out` (not `(cancelled)`).
    - User abort (Ctrl+C) still yields `(cancelled)` and kills the process group.
    - `McpManager.call_tool(name, args, timeout=X)` reaches the client; `timeout=None` keeps the 120 s default.
    - Existing `test_tool_timeout_surfaces_error` (explicit `timeout: 0.01`) still passes.
  - **Verification:**
    - `.venv/bin/python -m pytest -q tests/test_tool_calling.py tests/test_tools.py tests/test_mcp.py`

- [x] **Task 1.2: Dynamic system prompt (default timeout + schema hints)**
  - **Description:**
    - `_BASE_PROMPT`: replace hardcoded `Default timeout 30s if not specified.` with `Default timeout __DEFAULT_TOOL_TIMEOUT__s if not specified.`
    - `get_system_prompt()`: `.replace("__DEFAULT_TOOL_TIMEOUT__", str(getattr(self.settings_manager, "get_tool_timeout_sec", lambda: 30)()))` (keep the `__TOOLS__` replacement; custom `system_prompt` path untouched).
    - `TOOL_ARG_SCHEMAS["bash"]` → `"{command, timeout?}  # timeout in seconds; default if omitted"` (keep the `- bash {command, timeout?}` prefix so existing substring assertions pass).
    - `_build_runtime_system_prompt()` in `one/core/agent_session.py`: MCP tools section gains one line: optional `timeout` (seconds) in args overrides the per-call timeout.
  - **Files:** `one/resources/resource_loader.py`, `one/core/agent_session.py`
  - **Dependencies:** Task 1.1 (same file, avoid merge conflicts — do 1.1 first)
  - **Acceptance Criteria:**
    - `SettingsManager.in_memory({"tools": {"timeoutSec": 45}})` → prompt contains `Default timeout 45s if not specified.`
    - Default settings → prompt contains `Default timeout 30s if not specified.`
    - Existing assertions `"- bash {command, timeout?}" in prompt` still hold.
  - **Verification:**
    - `.venv/bin/python -m pytest -q tests/test_resource_loader.py tests/test_system_prompt.py`

- [x] **Task 1.3: Regression tests for timeout semantics**
  - **Description:**
    - `tests/test_tool_calling.py` (or `tests/test_tools.py`): `test_bash_model_timeout_honored_above_settings` (settings `timeoutSec: 1`, model `{"tool":"bash","args":{"command":"sleep 2 && echo ok","timeout":3}}` → ok, output `ok`); `test_bash_no_timeout_uses_settings_default` (settings `timeoutSec: 1`, `sleep 3`, no timeout arg → toolResult content contains `timed out`).
    - `tests/test_resource_loader.py`: `test_system_prompt_reflects_actual_timeout` (timeoutSec 45 → `Default timeout 45s`).
    - `tests/test_mcp.py`: `McpManager.call_tool` passes an explicit timeout to the client (fake client records it); `timeout=None` → default 120.
  - **Files:** `tests/test_tool_calling.py`, `tests/test_tools.py`, `tests/test_resource_loader.py`, `tests/test_mcp.py`
  - **Dependencies:** Tasks 1.1, 1.2
  - **Acceptance Criteria:** New tests pass; full suite green.
  - **Verification:**
    - `.venv/bin/python -m pytest -q`

### Phase 2: TUI info panel — full height + enabled MCP clients

**Objective:** sidebar spans the full available height and lists enabled MCP clients with tool counts.

**Prerequisites:** None (independent of Phase 1).

**Expected outcome:** `#sidebar` fills the vertical space; info panel shows `MCP: <name>(<tools>,<transport>) ...` (or `MCP: off` / `MCP: none`); golden snapshots regenerated; full suite green.

**Estimated effort:** ~1.5 h

**Confidence:** High

- [x] **Task 2.1: Sidebar full height**
  - **Description:** In the TUI CSS block, add `height: 1fr;` and `overflow-y: auto;` to `#sidebar` (keep `width: 42`).
  - **Files:** `one/modes/tui_mode.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** In a TUI run, the sidebar visually spans the full terminal height (border reaches the bottom), content scrolls if it overflows.
  - **Verification:** Manual `one --mode tui` run; snapshot tests capture the new layout.

- [x] **Task 2.2: MCP clients section in the info panel**
  - **Description:**
    - `build_sidebar_snapshot()`: add `mcpEnabled` (`getattr(session, "_mcp_manager", None) is not None`) and `mcpServers` — from `server_status()` filtered `enabled=True`, mapped to `{"name", "transport", "toolCount", "running", "error"}`; empty list when manager is None or nothing enabled.
    - `_refresh_sidebar()`: render after the `Bash:` line:
      - manager None → `MCP: off`
      - no enabled servers → `MCP: none`
      - else → `MCP: ` + `name(toolCount,transport)` per server, `!` suffix when `error` present; wrap to extra lines if the joined line exceeds the panel width.
    - Keep `sanitize_display_text` on the whole sidebar text.
  - **Files:** `one/modes/tui_mode.py`
  - **Dependencies:** Task 2.1 (same file/CSS block — do 2.1 first)
  - **Acceptance Criteria:**
    - Snapshot with a fake manager exposing `server_status()` → `mcpServers` entries correct (enabled only, tool counts, transport, error).
    - Session without `_mcp_manager` → `mcpEnabled: False`, `mcpServers: []`, sidebar renders `MCP: off`.
    - No crash when `_mcp_manager` exists but no servers are enabled.
  - **Verification:**
    - `.venv/bin/python -m pytest -q tests/test_tui_mode.py`

- [x] **Task 2.3: Sidebar tests + golden snapshot regeneration**
  - **Description:**
    - `tests/test_tui_mode.py`: extend `test_build_sidebar_snapshot_contains_runtime_details` with `mcpEnabled is False` / `mcpServers == []`; add a test with a fake `_mcp_manager` (class exposing `server_status()`) asserting the snapshot fields and (if feasible) the rendered `MCP:` line via the app.
    - `tests/test_tui_snapshots.py`: `_DummySession` has no `_mcp_manager` → sidebar shows `MCP: off`; regenerate golden files `base.txt`, `widget_panel.txt`, `overlay.txt` with `ONE_UPDATE_SNAPSHOTS=1`; review the diff (sidebar height + MCP line) before committing.
  - **Files:** `tests/test_tui_mode.py`, `tests/test_tui_snapshots.py`, `tests/snapshots/tui/base.txt`, `tests/snapshots/tui/widget_panel.txt`, `tests/snapshots/tui/overlay.txt`
  - **Dependencies:** Tasks 2.1, 2.2
  - **Acceptance Criteria:** All TUI tests pass; golden files regenerated and diffed (only sidebar-related changes).
  - **Verification:**
    - `ONE_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest -q tests/test_tui_snapshots.py && .venv/bin/python -m pytest -q`

## Rollout & Rollback

- No config migration, no schema changes, no new dependencies. Rollout = normal commit.
- Rollback = revert the commit(s); behavior returns to the old (30 s outer cap) semantics.
- Golden snapshot regeneration is a one-time commit; if the SVG renderer differs across environments, regenerate locally and verify the diff is sidebar-only.

## Observability

- TUI stream already shows `tool start/ok/err` lines; after the fix, timed-out commands display `Command timed out` in the tool result block (visible to the user and the model).
- Sidebar shows MCP server state (`!` marker for servers with errors) — refreshed on every session event and `/mcp` command.

## Security Considerations

- Model-provided timeouts are no longer capped by `tools.timeoutSec`; a long timeout can keep the session busy — Ctrl+C abort remains available and kills the process group (`_kill_process_group`).
- No new code execution paths; MCP `timeout` arg is consumed by `one` and not forwarded to the server.

## Risks & Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Golden SVG snapshots differ after sidebar change | CI failure | Certain (expected) | Regenerate with `ONE_UPDATE_SNAPSHOTS=1`, review diff is sidebar-only |
| Event-contract tests break | CI failure | Low | No event payload/sequence changes; only tool-result *text* changes for timeouts |
| `test_tool_timeout_surfaces_error` (explicit 0.01 s timeout) regresses | CI failure | Low | Effective timeout = model arg → inner timeout fires first; covered in Task 1.3 |
| `McpManager.call_tool` signature change misses a caller | Runtime error | Very low | Grep confirms single caller (`agent_session.py:480`); default param keeps compatibility |
| Model requests very long timeout → session appears hung | UX | Medium | Ctrl+C abort; documented in ADR-001; optional hard cap explicitly out of scope |

## Project Acceptance Criteria

- [x] Model's explicit bash `timeout` is honored above `tools.timeoutSec`; missing timeout uses the settings default (30 s).
- [x] Timed-out bash commands return `Command timed out` (not `(cancelled)`); user abort still returns `(cancelled)`.
- [x] MCP tool calls are not capped at `tools.timeoutSec`; optional model `timeout` is honored; client default 120 s otherwise.
- [x] System prompt states the actual default timeout and documents the bash `timeout` arg semantics.
- [x] TUI sidebar spans the full available height.
- [x] TUI info panel lists enabled MCP clients (name, tool count, transport, error marker) with `MCP: off` / `MCP: none` states.
- [x] Full test suite green: `.venv/bin/python -m pytest -q` (68+ tests).

## Estimated Timeline

- Phase 1: ~2 h (single implementer).
- Phase 2: ~1.5 h (single implementer).
- Total: ~3.5 h; uncertainty low — both phases are small, well-scoped changes in 4 source files + tests.