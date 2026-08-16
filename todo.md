# Project: one — TUI info panel (MCP list, full height) + tool timeout semantics + follow-ups

> Status: Phases 1-6 **implemented and committed** (342 tests green; Phase 4 in `0265a89`, Phase 5 in `36b72d6`).

## Goal

1. **Bash timeout:** the model decides the per-command timeout. If the model does not pass `timeout`, the configured default (`tools.timeoutSec`, 30 s) applies. A timed-out command must surface a clear `Command timed out` result to the model — not the ambiguous `(cancelled)` — and the model's explicit `timeout` must actually be honored (today it is silently overridden by the 30 s outer `asyncio.wait_for`).
2. **MCP tools:** same rule — the model decides (optional `timeout` in args); otherwise the MCP client's own default (120 s) applies. No 30 s outer cap.
3. **TUI info panel (sidebar):** stretched to the full available height (currently content-height ≈ half screen) and shows the list of enabled MCP clients.

**Follow-up goals (approved later, also done):**
4. MCP is rendered as its **own sidebar section** (header like `Info`/`Keys`) with one `- <name>` bullet per enabled client — no tool counts/transport (user decision), section always visible (`off`/`none` lines).
5. Keyboard shortcuts `Ctrl+A` / `Ctrl+S` moved from the Info section into the **Keys section** (Info shows no shortcut hints).
6. A plain user prompt typed **while the agent is streaming** is queued as a follow-up instead of failing with `[error] streamingBehavior is required while streaming` (TUI and CLI).

**Phase 5 goal (approved, planned):**
7. **`plan` tool:** the model can persist an execution plan for the current task (the prompt already contains PLANNING RULES but no tool to store a plan). The plan is injected into the system prompt on every step, survives compaction and session restarts (jsonl), is cleared on `finish`, requires user approval in cooperation mode, and is visible in the TUI (stream block + sidebar section).

## Context

- `one/tools/bash.py::bash_tool` has its own `timeout` param (from model args) producing a clear `"Command timed out"` result (`cancelled: False`).
- `one/core/agent_session.py::_execute_tool_by_name` additionally wraps every tool call in `asyncio.wait_for(task, timeout=tool_timeout_sec)` (settings `tools.timeoutSec`, default 30). When that outer timeout fires, `wait_for` cancels the task; `bash_tool` catches `CancelledError` and returns `{"cancelled": True, "content": "(cancelled)"}`; because the task suppresses the cancellation and returns a dict, `wait_for` returns it instead of raising `TimeoutError`. The model sees only `(cancelled)` and its explicit `timeout` arg (e.g. 120) is ignored. Reproduced in `/tmp/opencode/repro_timeout.py`.
- The system prompt hardcodes `"Default timeout 30s if not specified."` (`one/resources/resource_loader.py`) and does not reflect the actual `tools.timeoutSec`; the bash schema `{command, timeout?}` has no semantics hint.
- MCP calls go through the same outer `wait_for` (30 s cap) even though `McpClient.call_tool` already has its own 120 s timeout — same bug class.
- TUI sidebar (`one/modes/tui_mode.py`): `#sidebar` CSS has `width: 42` but **no `height`** → Textual sizes it to content (≈ half screen). `build_sidebar_snapshot()` + `_refresh_sidebar()` render the info panel; `session._mcp_manager.server_status()` already exposes per-server `name/enabled/running/tools/transport/error`.
- The system prompt (`one/resources/resource_loader.py`, `_BASE_PROMPT` PLANNING RULES, lines 67-73) instructs the model to create at most one plan per task and adapt it, but **no `plan` tool exists** — the rules are dead text. `TOOL_ARG_SCHEMAS` (~46-54) lists 10 tools, no `plan`.
- `_build_runtime_system_prompt()` (`one/core/agent_session.py:104-126`) is rebuilt on every provider call (`_flatten_messages_for_provider`) — the natural injection point for an active plan (survives compaction).
- Sessions are `.jsonl` files (`session_manager`); `compaction_summary` messages already use `customType` — the pattern for persisting the plan.
- `_run_tool_call` (`agent_session.py:541-667`) gates tools listed in `_approval_tools` (from `settings_manager.get_tool_approval_tools()`, default `["bash", "write", "edit"]`) when an approval callback is set (`--cooperation`); `finish` is excluded. Rejection path (`tool_approval_rejected`) already exists.
- opencode (core) does not persist plans — plan mode is a read-only agent mode, the plan lives in the conversation; plugins (opencode-planner, BRHP) persist via markdown/JSONL. `one` can persist cheaply because sessions are already jsonl.

## Scope

### In Scope
- Bash timeout semantics: model's explicit `timeout` honored; default = `tools.timeoutSec`; outer `wait_for` demoted to a backstop (effective + 5 s grace).
- MCP tool timeout: optional `timeout` in args honored; otherwise MCP client default (120 s); no outer 30 s cap.
- System prompt: dynamic default-timeout text + bash schema hint; MCP tools section notes the optional `timeout` arg.
- TUI sidebar: full-height layout; enabled MCP clients listed (name, tool count, transport, error marker); `MCP: off` / `MCP: none` states.
- Tests: new timeout-semantics tests, sidebar snapshot tests, golden SVG regeneration.
- `plan` tool: `{plan: str}` free-form text; stored as `AgentSession._plan`; injected into the system prompt every step; cleared on `finish`.
- Persistence: `customType: "plan"` message in the session jsonl (set/update/clear), restored on session load, excluded from compaction and from provider messages.
- Cooperation: `plan` added to the default `approvalTools` → approval prompt in cooperation mode.
- TUI: "Plan:" block in the stream (`plan_update` event) + "Plan" sidebar section (like MCP/Keys).
- Tests + AGENTS.md (10 → 11 tools).

### Non-Goals
- No changes to the agent event contract (`tool_call_start/end`, `turn_*`, snapshots in `test_event_snapshots.py`).
- No changes to `ask_user` / `spawn_subagent` timeout handling (already excluded from the outer cap).
- No changes to `/mcp list|enable|disable` command behavior.
- No new settings keys; `tools.timeoutSec` remains the default, not a hard cap.
- No changes to `execute_bash` (the `/bash` command path) — it already passes the settings timeout directly.
- No plan propagation to subagents (subagents keep their own context).
- No structured plan format (steps list) — free text only; no plan diffing/editing UI.
- No changes to the event contract of existing events; `plan_update` is a new additive event.
- No new settings keys (the `approvalTools` default list changes only).

## Assumptions

- The model's explicit `timeout` may exceed `tools.timeoutSec` (user decision: "model decyduje o wielkosci timeout, jezeli nie poda to jest wstawiane 30s"). User abort (Ctrl+C) remains the safety valve.
- For MCP tools the model may pass `timeout` in `args` even though it is not part of the server's input schema; it is consumed by `one` and not forwarded to the MCP server.
- `tools.timeoutSec` default stays 30 s; the prompt text is generated from the actual setting.
- Golden TUI snapshots will change (sidebar content/height) and must be regenerated with `ONE_UPDATE_SNAPSHOTS=1`.
- The plan is per-task: created by the model, overwritable (the model adapts it), cleared only at `finish` (user decision) — follow-up prompts within the task keep the plan.
- The plan is visible to the user in the TUI (stream block + sidebar section) — user decision.
- Persistence via jsonl `customType: "plan"` messages (user asked how opencode does it; recommendation accepted).
- In cooperation mode the `plan` call requires approval (user decision); rejection follows the existing `tool_approval_rejected` path.

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

### Sidebar (final state)

```
#sidebar { width: 42; height: 1fr; overflow-y: auto; ... }   # full height
Info block:
  Model/Theme/Thinking/Ctx/Retry/Status/Coop/Subagents/Bash/CWD/Session
  (Coop/Subagents without shortcut hints)
MCP section (own header, between Info and Keys):
  [b]MCP[/]
  off                                 # started with --no-mcp (no manager)
  none                                # no enabled servers configured
  - web-deepsearch                    # one bullet per enabled client (name only)
Keys section:
  Ctrl+C abort | Ctrl+L clear | Ctrl+Q quit | Ctrl+A coop | Ctrl+S subagents | Ctrl+V paste
```

### Plan tool (final state)

```
model tool call: {"tool":"plan","args":{"plan": "<text>"}}
  -> _execute_tool_by_name("plan", args)          # new branch, like ask_user
       plan_tool(plan)                            # validates non-empty
       self._plan = plan                          # session state
       emit plan_update {plan}                    # TUI stream block
       session_manager.append_message(customType="plan")   # jsonl persistence (NOT self.messages)
  -> system prompt (every step): "# Active Plan\n<plan>"   # via _build_runtime_system_prompt
  -> finish (terminal): self._plan = None; emit plan_update {plan: ""}; append cleared marker
  -> session load: restore _plan from last customType="plan" message; strip plan messages from provider list
  -> compaction: never drop customType="plan" messages
  -> cooperation: "plan" in approvalTools -> approval callback before execution
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

### ADR-005: MCP as a separate sidebar section; shortcuts move to Keys
**Decision:** The MCP line inside the Info block was replaced by a dedicated section (bold header `MCP`, like `Info`/`Keys`) placed between Info and Keys: one `- <name>` bullet per enabled server (name only — user decision, details dropped), `off` when there is no manager, `none` when nothing is enabled. The section is always visible. `Ctrl+A`/`Ctrl+S` hints were removed from the `Coop:`/`Subagents:` Info lines and both shortcuts are listed in the Keys section.

**Alternatives:**
- Keep the compact `MCP: demo(3,stdio) web(5,http)` line — user explicitly asked for a separate section with bullets (`- web-deepsearch`).
- Hide the section when MCP is off — user chose "section always visible" (consistency with the rest of the panel).
- Keep shortcuts inside Info lines — user explicitly asked Keys-only for shortcuts.

**Rationale:** matches the user's sidebar layout request; each client on its own line removes the >38-char wrap logic; `build_sidebar_snapshot` data (incl. toolCount/transport) is kept for the snapshot API.

**Tradeoffs:** tool counts/transport no longer visible in the sidebar (still in `/mcp list`); golden snapshots regenerate again.

### ADR-006: User prompts during streaming are queued as follow-ups
**Decision:** `AgentSession.prompt()` raises `RuntimeError("streamingBehavior is required while streaming")` when called without options while a turn is running. TUI `on_input_submitted` and CLI `interactive_mode` now check `session.is_streaming` and call `prompt(text, {"streamingBehavior": "followUp"})` (message lands in the follow-up queue, processed after the current turn; steering queue is still available via `/steer`). Both show a "Queued follow-up message." confirmation; the TUI refreshes the sidebar (queue counters) after queueing.

**Alternatives:**
- Surface the runtime error to the user (status quo) — the user's bug report explicitly calls the missing queueing a problem.
- Use `steer` as the default queue — user chose follow-up ("1 A") so a plain prompt never interrupts the current turn.
- Fix only the TUI — user approved fixing `interactive_mode` too ("2 tak"); same latent bug.

**Rationale:** matches user decisions; one consistent rule across TUI and CLI; follow-up messages are automatically popped and processed at the end of the current turn (`agent_session.py` turn loop).

**Tradeoffs:** a queued prompt is processed only after the current turn ends (visible via the sidebar queue counters); no API changes.

### ADR-007: `plan` tool — session state + system-prompt injection + jsonl persistence
**Decision:** A `plan` tool (`{plan: str}`) stores the plan in `AgentSession._plan`. The active plan is injected into `_build_runtime_system_prompt()` on every provider call (section `# Active Plan`), so it is always visible to the model and survives compaction. The plan is persisted as a `customType: "plan"` message in the session jsonl (set/update/clear), restored on session load, excluded from compaction, and stripped from provider messages. Cleared only on `finish`. `plan` is added to the default `approvalTools` (cooperation mode asks the user). TUI renders a "Plan:" stream block (`plan_update` event) and a sidebar section.

**Alternatives:**
- Plan as a plain conversation message — pollutes provider context, may be compacted away, no enforcement of "max 1 plan", the model may not treat it as authoritative.
- Plan as a markdown file via the existing `write` tool — no visibility guarantee, pollutes the workspace, no lifecycle.
- State-only (no jsonl persistence) — plan lost on session restart; opencode core does exactly this (plan lives in conversation), but `one` sessions are already jsonl so persistence is cheap.
- Structured steps format — more rigid, harder for the model to adapt; free text chosen.

**Rationale:** the prompt already mandates planning; the tool makes the plan authoritative (system prompt) and durable (jsonl). Matches user decisions: clear only at finish, TUI visibility, cooperation approval, persistence.

**Tradeoffs:** plan text consumes system-prompt tokens every step (bounded by the model's plan size); the plan is not propagated to subagents; `plan_update` is a new event (additive, no contract break).

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

### Phase 3: Follow-ups (sidebar MCP section, Keys shortcuts, prompt queueing) — DONE

**Objective:** finalize the sidebar layout per user feedback (MCP as own section, shortcuts in Keys) and fix prompt queueing during streaming (TUI + CLI).

**Prerequisites:** Phases 1-2.

**Expected outcome:** sidebar renders `Info → MCP (bullets) → Keys`; plain prompts while streaming are queued as follow-ups; all tests green; `AGENTS.md` kept in sync.

**Estimated effort:** ~1.5 h

**Confidence:** High

- [x] **Task 3.1: MCP as a separate sidebar section (bullets)**
  - **Description:** In `_refresh_sidebar` (`one/modes/tui_mode.py`): replace the `mcp_text` logic (`MCP: off|none|name(...)...` with >38-char wrap) with `mcp_lines = ["off"]` / `["none"]` / `[f"- {sv['name']}" for sv in s["mcpServers"]]`; render a `[b info]MCP[/]` section between the Info block and the Keys block. `build_sidebar_snapshot` unchanged.
  - **Files:** `one/modes/tui_mode.py`
  - **Dependencies:** Phase 2
  - **Acceptance Criteria:** Sidebar order `Info … Session → MCP → Keys`; bullets `- demo`, `- web`; disabled servers excluded; `off`/`none` states.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_tui_mode.py`
  - **Committed in:** `7684705`

- [x] **Task 3.2: Move Ctrl+A/Ctrl+S to the Keys section**
  - **Description:** `Coop: {s['coop']} (Ctrl+A)` → `Coop: {s['coop']}`; `Subagents: … (Ctrl+S)` → `Subagents: …`; Keys line gains `Ctrl+S subagents` (Ctrl+A coop already listed).
  - **Files:** `one/modes/tui_mode.py`
  - **Dependencies:** Task 3.1
  - **Acceptance Criteria:** Info lines carry no shortcut hints; Keys lists Ctrl+A coop and Ctrl+S subagents; goldens regenerated.
  - **Verification:** `ONE_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest -q tests/test_tui_snapshots.py`
  - **Committed in:** `52ca069` (with 3.3)

- [x] **Task 3.3: Queue user prompts during streaming (TUI + CLI)**
  - **Description:** In `on_input_submitted` (`one/modes/tui_mode.py`): if `self.session.is_streaming` → `await self.session.prompt(text, {"streamingBehavior": "followUp"})` + `self._write("Queued follow-up message.", "info")`, else plain `prompt(text)`. In `interactive_mode.py`: same check + `print("Queued follow-up message.")`. New test `test_tui_prompt_queued_while_streaming` (slow provider; assert no `[error]` in stream, message lands in `get_pending_queues()["followUp"]`, consumed after the turn).
  - **Files:** `one/modes/tui_mode.py`, `one/modes/interactive_mode.py`, `tests/test_tui_mode.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** Second prompt during a running turn queues (follow-up) instead of `[error] streamingBehavior is required while streaming`; queued message processed after the turn.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_tui_mode.py tests/test_interactive_mode.py`
  - **Committed in:** `52ca069`

- [x] **Task 3.4: AGENTS.md refresh**
  - **Description:** Update `AGENTS.md`: 323 tests (~90 s), 10 tools, `one/mcp/client.py` (McpManager, 120 s call_tool default), `settings_manager.py` + `run_mode.py`, MCP config gotchas (`settings.json` `mcpServers`, `--no-mcp`, fake managers in tests), `tools.timeoutSec`/bash `timeout` override, `ask_user` implemented, TUI golden snapshot workflow (`ONE_UPDATE_SNAPSHOTS=1`, adler32 hash note), `todo.md` as the active committed plan.
  - **Files:** `AGENTS.md`
  - **Dependencies:** None
  - **Acceptance Criteria:** No stale claims (test/tool counts, roadmap wording); every statement verifiable in the repo.
  - **Verification:** manual diff review
  - **Committed in:** `c535d7f`

### Phase 4: Restore the TUI waiting spinner (regression after prompt queueing) — DONE

**Bug report (user):** "po ostatnich zmianach zniknęła w TUI animacja oczekiwania na wynik pracy modelu" — the `⠋ Ctrl+C abort` waiting spinner no longer shows.

**Root causes (verified by code reading):**
1. `_run_prompt` in `on_input_submitted` (`one/modes/tui_mode.py` ~line 1415): when the session is streaming, a second prompt is queued (`streamingBehavior: "followUp"`) and the coroutine returns almost immediately — but the `finally` block **unconditionally** runs `self._turn_active = False; self._remove_thinking_line(); self._render_stream()`. The first turn is still in flight, so the spinner (driven by `_tick_waiting` → `evaluate_waiting(self._turn_active, self._last_delta_ts, now)`) is killed for the remainder of that turn.
2. Follow-up turns started by the session itself (turn loop pops `_follow_up` → `prompt()` → emits `turn_start`, `one/core/agent_session.py` ~1415-1417) never arm `_turn_active`: it is set only in `on_input_submitted`. `on_session_event` handles `turn_end` (clears it) but **ignores `turn_start`** — so queued prompts run with no spinner at all (not even between tool calls / before the first token).

**Planned fix (exact — was drafted, then reverted per process rules):**
- `_run_prompt` (tui_mode.py): add `queued = False`; set it True in the streaming branch after queueing; in `finally`, run the turn cleanup (`_turn_active = False`, `_remove_thinking_line()`, `_render_stream()`) **only when `not queued`**; always `_refresh_sidebar()`.
- `on_session_event` (tui_mode.py): add `elif et == "turn_start": self._turn_active = True` (arms the spinner for session-initiated turns — queued follow-ups, retries). Place near the `turn_end` handler; order in the elif chain does not matter.
- Regression test `test_tui_spinner_survives_queued_prompt_and_resumes_for_followup` in `tests/test_tui_mode.py`: slow provider (1 s sleep before first delta), submit prompt 1, poll until `session.is_streaming`, submit prompt 2 (queued), `await pilot.pause()` a few times, assert `app._turn_active is True` (must survive queueing); then poll until `not app._turn_active and not session.is_streaming` while counting spinner windows (any `line.startswith(_THINKING_MARK)` in `app._stream_lines`), assert `>= 2` windows (turn 1 + follow-up turn).

**Files:** `one/modes/tui_mode.py`, `tests/test_tui_mode.py`

**Acceptance Criteria:**
- Spinner appears while the first turn runs, survives a queued second prompt, and reappears for the follow-up turn.
- Existing spinner/queue tests still pass: `test_tui_error_turn_resets_spinner`, `test_tui_ctrl_c_after_finished_error_turn`, `test_tui_prompt_queued_while_streaming`.

**Estimated effort:** ~1 h

**Confidence:** High (root cause fully identified; fix already proven in a working draft)

- [x] **Task 4.1: `queued` flag in `_run_prompt`** — skip turn cleanup when the message was queued
  - **Files:** `one/modes/tui_mode.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** After a queued submission, `app._turn_active` stays True while the first turn still runs.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_tui_mode.py::test_tui_spinner_survives_queued_prompt_and_resumes_for_followup`

- [x] **Task 4.2: arm `_turn_active` on `turn_start`** in `on_session_event`
  - **Files:** `one/modes/tui_mode.py`
  - **Dependencies:** None (independent of 4.1)
  - **Acceptance Criteria:** Follow-up turn (session-initiated) shows the spinner while waiting for the first token / between tool calls.
  - **Verification:** same test as 4.1 (spinner windows >= 2)

- [x] **Task 4.3: regression test + full verification**
  - **Files:** `tests/test_tui_mode.py`
  - **Dependencies:** Tasks 4.1, 4.2
  - **Acceptance Criteria:** New test passes; existing spinner tests pass; full suite green.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_tui_mode.py tests/test_tui_snapshots.py && .venv/bin/python -m pytest -q`
  - **Committed in:** `0265a89`

### Phase 5: `plan` tool — persistent execution plan (TUI + cooperation) — DONE

**Objective:** the model can store an execution plan via the `plan` tool; the plan is injected into the system prompt on every step, persisted in the session jsonl (survives restart/compaction), cleared on `finish`, gated by user approval in cooperation mode, and visible in the TUI (stream block + sidebar section).

**Prerequisites:** Phases 1-4 (all committed).

**Expected outcome:** `plan` callable by the model (schema + prompt rules updated); plan visible to the model every step; plan survives compaction and session reload; `finish` clears it; cooperation mode asks for approval; TUI shows the plan; full suite green (325 + new tests).

**Estimated effort:** ~3 h

**Confidence:** High

- [x] **Task 5.1: `plan` tool definition + registration**
  - **Description:**
    - `one/tools/plan.py`: `plan_tool(plan: str) -> dict` — raises `ValueError` on empty/whitespace plan; returns `{"ok": True, "result": "Plan stored. Follow it; adapt it via the plan tool when the situation changes materially."}` (pattern: `finish.py`).
    - `one/tools/index.py`: register `plan` in `all_tools` (meta tool like `finish` — NOT in `coding_tools`/`read_only_tools`).
    - `one/core/agent_session_runtime.py` line 77: add `"plan"` to the default `tool_names` list.
    - `one/resources/resource_loader.py`: `TOOL_ARG_SCHEMAS["plan"] = "{plan}  # the execution plan for the current task; visible to you on every step"`; PLANNING RULES (lines 67-73) gain a line: "Use the plan tool to store the plan."
  - **Files:** `one/tools/plan.py` (new), `one/tools/index.py`, `one/core/agent_session_runtime.py`, `one/resources/resource_loader.py`
  - **Dependencies:** None
  - **Acceptance Criteria:**
    - `plan_tool("...")` returns ok; `plan_tool("")` / `plan_tool("   ")` raises `ValueError`.
    - System prompt tools list contains `- plan {plan}`; PLANNING RULES mention the plan tool.
    - `plan` is in the default `tool_names` (runtime) and `all_tools`.
  - **Verification:**
    - `.venv/bin/python -m pytest -q tests/test_tools.py tests/test_resource_loader.py`

- [x] **Task 5.2: session integration — state, dispatch, system prompt, finish clearing**
  - **Description:**
    - `AgentSession.__init__`: `self._plan: str | None = None`.
    - `_execute_tool_by_name` (`one/core/agent_session.py` ~481): new branch `elif tool_name == "plan":` → `plan_text = args.get("plan", "")`; `result = plan_tool(plan_text)`; `self._plan = plan_text`; `self._emit({"type": "plan_update", "plan": self._plan})`; return result.
    - `_build_runtime_system_prompt()` (~104-126): when `self._plan` is set, append `\n# Active Plan\n{self._plan}\nFollow this plan; adapt it via the plan tool only when the situation changes materially.` (after the MCP tools section).
    - Turn loop finish branch (~1221-1229, where `finish` breaks): after `_finish_assistant_message`, `if self._plan: self._plan = None; self._emit({"type": "plan_update", "plan": ""})`.
  - **Files:** `one/core/agent_session.py`
  - **Dependencies:** Task 5.1
  - **Acceptance Criteria:**
    - Model calls `plan` → `session._plan` set; `plan_update` event emitted with the plan text.
    - Next provider call's system prompt contains `# Active Plan` + the plan text.
    - `finish` → `session._plan is None`; `plan_update` with `""` emitted.
    - Existing event-contract tests unaffected (plan not used in their scenarios).
  - **Verification:**
    - `.venv/bin/python -m pytest -q tests/test_tool_calling.py tests/test_event_snapshots.py`

- [x] **Task 5.3: jsonl persistence — restore on load, compaction exclusion**
  - **Description:**
    - On plan set/update (Task 5.2 branch): `self.session_manager.append_message({"role": "user", "customType": "plan", "content": plan_text, "timestamp": int(time.time() * 1000)})` — appended to the jsonl ONLY, NOT to `self.messages` (the provider sees the plan via the system prompt, not as a message).
    - On clear (finish): append the same with `content: ""` (cleared marker).
    - Session load/constructor: scan loaded messages for `customType == "plan"`; restore `self._plan` from the last one (`""` → `None`); remove all plan messages from the provider message list.
    - Compaction (`one/core/agent_session.py`): never drop messages with `customType == "plan"` when selecting the dropped window.
  - **Files:** `one/core/agent_session.py` (+ `one/core/session_manager.py` only if append/load helpers are needed)
  - **Dependencies:** Task 5.2
  - **Acceptance Criteria:**
    - After a plan call, the session jsonl contains the `customType: "plan"` message; `self.messages` does not.
    - Reloading the session from jsonl restores `session._plan`; plan messages are absent from provider messages.
    - Compaction keeps the plan message; after compaction the system prompt still contains the plan.
  - **Verification:**
    - `.venv/bin/python -m pytest -q tests/test_compaction.py tests/test_session_manager.py` + new persistence tests (Task 5.6)

- [x] **Task 5.4: cooperation approval for `plan`**
  - **Description:** `one/core/settings_manager.py`: default `approvalTools` (line 34) and the fallback in `get_tool_approval_tools()` (line 176) become `["bash", "write", "edit", "plan"]`. No change in `_run_tool_call` (plan != finish → gated when an approval callback is set and `plan` ∈ `_approval_tools`; rejection path already exists).
  - **Files:** `one/core/settings_manager.py`
  - **Dependencies:** Task 5.2
  - **Acceptance Criteria:** With an approval callback set, a `plan` call invokes the callback; rejection yields `tool_approval_rejected` + `tool_call_end ok: False` and the model sees the rejection.
  - **Verification:** new approval test (Task 5.6); `.venv/bin/python -m pytest -q tests/test_auth_and_cli.py tests/test_tool_calling.py`

- [x] **Task 5.5: TUI — "Plan:" stream block + sidebar section**
  - **Description:**
    - `on_session_event` (`one/modes/tui_mode.py`): handle `plan_update` → render a `[b]Plan[/]` block in the stream with the plan text (bordered block like tool blocks); `plan == ""` → render nothing.
    - `_refresh_sidebar()`: add a `[b]Plan[/]` section between the Info block and the MCP section: `getattr(session, "_plan", None)`; when set, show the text truncated to ~200 chars with `…` (constant `_PLAN_SIDEBAR_MAX = 200`); section hidden when no plan. Use `getattr` so dummy sessions without `_plan` don't crash.
    - Golden snapshots: existing scenarios have no plan → no regeneration expected; if `_DummySession` lacks `_plan`, `getattr` covers it.
  - **Files:** `one/modes/tui_mode.py`
  - **Dependencies:** Task 5.2
  - **Acceptance Criteria:** `plan_update` renders a Plan block in the stream; sidebar shows the Plan section when `session._plan` is set and hides it when cleared; no crash for sessions without `_plan`.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_tui_mode.py tests/test_tui_snapshots.py`

- [x] **Task 5.6: tests — unit, session, persistence, compaction, approval, schema, TUI**
  - **Description:**
    - `tests/test_tools.py`: `test_plan_tool_stores_text`, `test_plan_tool_rejects_empty`.
    - `tests/test_tool_calling.py` (or new `tests/test_plan_tool.py`): fake provider calls `plan` → `session._plan` set, `plan_update` emitted, next system prompt contains the plan; `finish` clears it (`plan_update` with `""`).
    - Persistence: jsonl contains `customType: "plan"`; reload restores `_plan`; plan messages absent from provider messages.
    - Compaction: plan survives compaction (message kept, system prompt still contains plan).
    - Approval: approval callback invoked for `plan`; rejection path (`tool_approval_rejected`).
    - `tests/test_resource_loader.py`: `TOOL_ARG_SCHEMAS["plan"]` present; prompt contains `- plan {plan}` and the PLANNING RULES mention.
    - TUI: `plan_update` renders the Plan block; sidebar section shown/hidden (app-level test with `session._plan` set).
  - **Files:** `tests/test_tools.py`, `tests/test_tool_calling.py` (or `tests/test_plan_tool.py`), `tests/test_compaction.py`, `tests/test_resource_loader.py`, `tests/test_tui_mode.py`
  - **Dependencies:** Tasks 5.1-5.5
  - **Acceptance Criteria:** All new tests pass; full suite green (325 + new).
  - **Verification:**
    - `.venv/bin/python -m pytest -q`

- [x] **Task 5.7: AGENTS.md — 11 tools**
  - **Description:** Update `AGENTS.md`: "10 tools" → "11 tools"; add `plan` to the tool list; one line on plan semantics (persistent per-task plan, system-prompt injection, cleared on finish, approval-gated in cooperation mode).
  - **Files:** `AGENTS.md`
  - **Dependencies:** Task 5.2
  - **Acceptance Criteria:** No stale tool count; statement verifiable in the repo.
  - **Verification:** manual diff review

### Phase 6: TUI crash — plan with markup chars (MarkupError) + render during shutdown (NoMatches) — DONE

**Bug report (user):** during TUI use: `MarkupError: Expected markup value (found '>",\n').` followed by `unhandled exception during asyncio.run() shutdown` with `NoMatches("No nodes match '#stream' on Screen(id='_default')")` in `_render_stream` (`one/modes/tui_mode.py:511`) called from `_run_prompt`'s `finally` (line 1448) after `CancelledError` from the provider.

**Root causes (verified by code reading):**
1. `_refresh_sidebar` (`one/modes/tui_mode.py:784-792`): the Plan section inserts the RAW plan text (`display`) into the Rich markup string (`plan_block = f"[b {self._theme.info}]Plan[/]\n{display}\n"`), then `self.query_one("#sidebar", Static).update(sidebar)` (line 816) renders it as markup. Plan text containing `[`/`]` (e.g. JSON like `[{"plan": ">", ...}]`) → `MarkupError`. The stream path is safe (`_render_stream` escapes every non-spinner line with `rich_escape`, line 517); the sidebar is not.
2. `_render_stream` (lines 510-511) and `_refresh_sidebar` (line 816) call `query_one(...)` unconditionally; when the app is closing (Ctrl+C abort during streaming → `CancelledError` → `_run_prompt` `finally` → `_render_stream`), the DOM is torn down → `NoMatches` → unhandled exception during `asyncio.run()` shutdown.

**Planned fix (exact):**
- `_refresh_sidebar`: escape the plan text before inserting into the markup string: `display = rich_escape(display)` (import already present: `from rich.markup import escape as rich_escape`, line 13).
- `_render_stream`: guard `query_one("#stream")` with try/except (return early when the widget is gone).
- `_refresh_sidebar`: guard `query_one("#sidebar", Static)` with try/except (return early).
- Regression test: TUI test setting `session._plan` to text with markup chars (e.g. `[b]bold[/]` and JSON with brackets) → `_refresh_sidebar` does not raise; sidebar update contains escaped text.

**Files:** `one/modes/tui_mode.py`, `tests/test_tui_mode.py`

**Acceptance Criteria:**
- Plan text with `[`/`]`/`<`/`>` renders in the sidebar without MarkupError.
- Abort during streaming while the app is closing does not raise NoMatches (render guards).
- Existing TUI tests + golden snapshots unchanged; full suite green.

**Estimated effort:** ~0.5 h

**Confidence:** High (root cause fully identified)

- [x] **Task 6.1: escape plan text in sidebar + render guards**
  - **Description:** In `_refresh_sidebar` (`one/modes/tui_mode.py`): `display = rich_escape(display)` before building `plan_block`. In `_render_stream`: wrap `stream_widget = self.query_one("#stream")` in try/except (return early on failure). In `_refresh_sidebar`: wrap `self.query_one("#sidebar", Static).update(sidebar)` in try/except (return early on failure).
  - **Files:** `one/modes/tui_mode.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** Sidebar renders plan text with markup chars without error; render calls are no-ops when the DOM is gone.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_tui_mode.py tests/test_tui_snapshots.py`

- [x] **Task 6.2: regression tests + full suite**
  - **Description:** Add a TUI test: set `session._plan` to text containing `[b]bold[/]` and JSON with brackets (e.g. `[{"plan": ">", "x": 1}]`), call `_refresh_sidebar` (app-level), assert no exception and the sidebar widget content contains the escaped text (no raw `[` markup crash). Run the full suite.
  - **Files:** `tests/test_tui_mode.py`
  - **Dependencies:** Task 6.1
  - **Acceptance Criteria:** New test passes; full suite green (340 + new).
  - **Verification:** `.venv/bin/python -m pytest -q`

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
| `plan_update` event breaks event-contract snapshots | CI failure | Very low | Additive event; existing scenarios never call `plan`; new tests assert the sequence explicitly |
| Plan text inflates system-prompt tokens every step | Cost/latency | Medium | Plan size is model-controlled; prompt rules keep it concise; truncation only in the TUI sidebar, not in the prompt |
| Restore-on-load misses the plan after compaction | Feature gap | Low | Compaction explicitly keeps `customType: "plan"` messages (Task 5.3) |
| Golden TUI snapshots change | CI failure | Low | Existing snapshot scenarios have no plan; `getattr` guards dummy sessions |

## Project Acceptance Criteria

- [x] Model's explicit bash `timeout` is honored above `tools.timeoutSec`; missing timeout uses the settings default (30 s).
- [x] Timed-out bash commands return `Command timed out` (not `(cancelled)`); user abort still returns `(cancelled)`.
- [x] MCP tool calls are not capped at `tools.timeoutSec`; optional model `timeout` is honored; client default 120 s otherwise.
- [x] System prompt states the actual default timeout and documents the bash `timeout` arg semantics.
- [x] TUI sidebar spans the full available height.
- [x] TUI info panel lists enabled MCP clients (name, tool count, transport, error marker) with `MCP: off` / `MCP: none` states.
- [x] MCP is a separate sidebar section (header, `- <name>` bullets, `off`/`none` states) between Info and Keys.
- [x] Keyboard shortcuts live only in the Keys section (Ctrl+A coop, Ctrl+S subagents); Info lines have no shortcut hints.
- [x] Plain user prompts during streaming are queued as follow-ups in TUI and CLI (no `streamingBehavior is required` error).
- [x] Full test suite green: `.venv/bin/python -m pytest -q` (324 tests).
- [x] TUI waiting spinner restored: armed for the running turn, survives queued prompts, reappears for session-initiated follow-up turns (Phase 4).
- [x] `plan` tool callable by the model (`{plan: str}`); schema + PLANNING RULES updated; plan stored in `AgentSession._plan`.
- [x] Active plan injected into the system prompt on every step (`# Active Plan`); cleared on `finish` (with `plan_update` event).
- [x] Plan persisted in the session jsonl (`customType: "plan"`), restored on session load, excluded from compaction and provider messages.
- [x] Cooperation mode asks for approval before executing `plan` (default `approvalTools` includes `plan`); rejection follows the existing path.
- [x] TUI shows the plan: "Plan:" stream block on `plan_update` + "Plan" sidebar section (hidden when no plan).
- [x] Full test suite green: `.venv/bin/python -m pytest -q` (340 tests).
- [x] Sidebar renders plan text with markup chars without MarkupError (rich_escape).
- [x] Render calls are no-ops when the DOM is gone (NoMatches guarded in _render_stream/_refresh_sidebar).
- [x] Full test suite green: `.venv/bin/python -m pytest -q` (342 tests).

## Estimated Timeline

- Phase 1: ~2 h (single implementer).
- Phase 2: ~1.5 h (single implementer).
- Phase 3 (follow-ups): ~1.5 h (sidebar MCP section + Keys shortcuts + prompt queueing + AGENTS.md).
- Total: ~5 h; uncertainty low — all phases were small, well-scoped changes in ~6 source files + tests. All work is committed.
- Phase 5: ~3 h (plan tool: core + session + persistence + approval + TUI + tests + AGENTS.md).
- Total with Phase 5: ~8 h; Phase 5 uncertainty low — all integration points identified (dispatch branch, system-prompt builder, finish branch, settings defaults, TUI event/sidebar).