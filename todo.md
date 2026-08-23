# Project: one — TUI info panel (MCP list, full height) + tool timeout semantics + follow-ups

> Status: Phases 1-10 **implemented and committed** (396 tests green; Phase 4 in `0265a89`, Phase 5 in `36b72d6`, Phase 6 in `6df9897`, Phase 7 in `2f07d06`, Phase 8 in `22c4652`, Phase 9 in `0ee1cd5`, Phase 10 in `3393173`). Phases 11-12 **implemented and committed** (426 tests green; Phase 11 in `d8307a1`, Phase 12 in `613ae0e`). Phase 13 **implemented and committed** (435 tests green; `/providers` command, Phase 13 in `793ea32`). Phases 14-15 **implemented and committed** (444 tests green; `/providers` completion/help fixes + `/login refresh <provider>`, in `9dac4d2`). Phase 16 **implemented and committed** (445 tests green; `# Current Date` in the system prompt, in `b4777a6`). Phase 17 **implemented and committed** (451 tests green; ctx gauge fallback + real context windows from providers, in `1b59855`).

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

**Phase 9 goal (approved, planned):**
8. **`apply_patch` tool:** like opencode's `apply_patch` — apply a unified-diff patch (envelope `*** Begin Patch` / `*** End Patch`, operations `*** Add File:` / `*** Update File:` / `*** Delete File:` / `*** Move to:`) to one or more files in a single call, with full validation before any write.

**Phase 10 goal (approved, planned):**
9. **`edit` tool robustness (real-usage bug report):** the model frequently calls `edit` with `path` nested INSIDE `edits[0]` instead of as a top-level argument; the tool then fails with a misleading `FileNotFoundError: File not found: ` (empty path) and the model repeats the same mistake. Fix: tolerate the nested `path`, give a clear error naming the actual problem, and make the prompt schema unambiguous.

**Phase 11 goal (approved, planned):**
10. **`/login` validation + remote model fetch:** today `/login` only stores the API key (`set_stored_api_key`) and sets defaults — it never validates the key against the provider and never fetches the provider's model list (`--list-models` lists only the local registry). Fix: `/login <provider> [apiKey] [model]` validates the key BEFORE storing (401/403 → error, key not stored), then fetches the model list from the provider (`GET {base}/models` for OpenAI-compatible providers, Gemini models endpoint, minimal-chat validation for Anthropic which has no list endpoint), registers the fetched models in the registry and persists them to `models.json`.

**Phase 12 goal (approved, planned):**
11. **`one run <task>` value-flag bug (discovered during provider verification):** the re-injection loop in `one/cli/args.py:76-94` pairs values only for `_VALUE_FLAGS = {"--answer-file", "--steer-file", "--param"}`; every other value-taking flag (`--provider`, `--model`, `--api-key`, `--thinking`, `--models`, `--tools`, `--theme`, ...) loses its value → `error: argument --provider: expected one argument` (verified live). Fix: pair values for ALL value-taking flags.

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
- `apply_patch` tool: `{patchText}` unified-diff patch (opencode format: `*** Begin Patch` / `*** End Patch` envelope, `*** Add File:` / `*** Update File:` / `*** Delete File:` headers, optional `*** Move to:`, `@@` hunks with `-`/`+`/context lines); multi-file; validation of the whole patch before any write; paths resolved via `resolve_to_cwd`; added to `DEFAULT_TOOL_NAMES`, `all_tools`, `coding_tools`, `TOOL_ARG_SCHEMAS`, and the default `approvalTools` (cooperation mode).
- `edit` robustness: accept `path` from `edits[0]` when the top-level `path` is missing (model-generated shape); clear `ValueError` naming the real problem when `path` is missing everywhere or `edits` is not a list; prompt schema for `edit` states `path` is TOP-LEVEL.
- `/login` validation: `ProviderAdapter.list_models(api_key)` (new async method) — OpenAICompatible: `GET {base_url}/models` (works for openrouter/ollama-cloud/ollama/llama.cpp/deepseek/mistral/groq/xai — endpoints verified live), Gemini: `GET https://generativelanguage.googleapis.com/v1beta/models?key=...` filtered to `generateContent`-capable models, Anthropic: no list endpoint → `list_models` returns `None` and validation falls back to a minimal chat call (max_tokens=1) with the first registered model.
- `/login` flow (interactive + TUI): validate BEFORE storing (401/403/network error → `Authorization failed for <provider>: <error>`, key NOT stored, defaults NOT changed); on success: store key, fetch models, register in-memory (`ModelRegistry.register_models`), persist to `models.json` (`providers.<provider>` merge, keeping existing `url`/`toolParser`/`contextWindow`), print `Authorized. Fetched N models:` + list, set default provider; optional `[model]` arg resolved against the fetched list.
- NO_AUTH providers (`llama.cpp`, `ollama`): `/login` without a key still fetches and registers the local model list.
- `one run` value-flag fix: pair values for ALL value-taking flags in the re-injection loop (`--provider`, `--model`, `--api-key`, `--thinking`, `--llama-cpp-url`, `--ollama-url`, `--system-prompt`, `--append-system-prompt`, `--mode`, `--session`, `--session-dir`, `--models`, `--tools`, `--export`, `--export-format`, `--theme`, `--prompt-template`, `--skill`, `--extension`, `--list-models`, plus the existing `--answer-file`/`--steer-file`/`--param`).

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
- No LSP diagnostics, no auto-formatting, no filesystem watcher events (opencode's apply_patch does these; `one` has none of these subsystems). No BOM handling. No new dependencies (own diff parser). No changes to the event contract.
- No changes to `_execute_tool_by_name` arg plumbing (normalization lives in `edit_tool` itself, directly testable); no changes to other tools (`write`/`bash` have no reported shape errors); no changes to the event contract.
- No changes to the `--list-models` CLI flag semantics (still lists the local registry; fetched models appear there after `/login` persists them to `models.json`).
- No auto-login on startup, no key rotation, no provider account/balance checks (the openrouter 402 credit issue is a user-account matter, not a code fix).
- No changes to the `run` subcommand's task-text semantics (free-form text after flags still works); no new flags.

## Assumptions

- The model's explicit `timeout` may exceed `tools.timeoutSec` (user decision: "model decyduje o wielkosci timeout, jezeli nie poda to jest wstawiane 30s"). User abort (Ctrl+C) remains the safety valve.
- For MCP tools the model may pass `timeout` in `args` even though it is not part of the server's input schema; it is consumed by `one` and not forwarded to the MCP server.
- `tools.timeoutSec` default stays 30 s; the prompt text is generated from the actual setting.
- Golden TUI snapshots will change (sidebar content/height) and must be regenerated with `ONE_UPDATE_SNAPSHOTS=1`.
- The plan is per-task: created by the model, overwritable (the model adapts it), cleared only at `finish` (user decision) — follow-up prompts within the task keep the plan.
- The plan is visible to the user in the TUI (stream block + sidebar section) — user decision.
- Persistence via jsonl `customType: "plan"` messages (user asked how opencode does it; recommendation accepted).
- In cooperation mode the `plan` call requires approval (user decision); rejection follows the existing `tool_approval_rejected` path.
- `apply_patch` semantics follow opencode's implementation (verified against `anomalyco/opencode` `packages/opencode/src/tool/apply_patch.ts` + `apply_patch.txt`, branch `dev`): envelope format, headers, hunks, validation-before-write, `Success. Updated the following files:` summary with `A`/`M`/`D` lines. In cooperation mode `apply_patch` requires approval (added to default `approvalTools`).
- The reported failure mode is a model-side JSON shape error (observed with local models via llama.cpp): `path` nested in `edits[0]`. The fix makes `one` tolerant of this shape AND gives a clear error when the shape is unrecoverable — both are needed because weaker models repeat the same mistake when the error does not name the cause.
- `/login` validation uses the provider's real API (live-verified: `GET https://openrouter.ai/api/v1/models` and `GET https://ollama.com/v1/models` both return 200 with a `data[].id` list using the stored key). Anthropic has no public models-list endpoint → minimal-chat validation with the first registered model; if none is registered, validation is skipped with a note.
- Fetched models are persisted to `models.json` automatically (consistent with existing behavior: `set_stored_api_key` writes `auth.json`, `set_default_provider` writes `settings.json`). Existing entries for the provider keep their `url`/`toolParser`/`contextWindow`; new ids get defaults (`reasoning=True`, `context_window=None`).
- The `one run` flag bug is a pure parsing defect (verified live: `one run "task" --provider openrouter --model openai/gpt-4.1` → `error: argument --provider: expected one argument`); the fix is local to `args.py` and covered by unit tests on `parse_args`.

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

### Phase 7: CLI default tool list missing "plan" — "Tool 'plan' is disabled" — DONE

**Bug report (user, TUI):** model called `plan` with a valid plan payload; session answered `Tool 'plan' is disabled` (tool err: plan), then the user aborted.

**Root cause (verified by code reading):** `one/cli/main.py:364` builds the default tool list WITHOUT `"plan"`:
`tool_names = ["read", "bash", "edit", "write", "grep", "find", "ls", "finish", "spawn_subagent", "ask_user"]`
The CLI always passes `bootstrap["tools"]` (main.py:401), so the runtime fallback in `one/core/agent_session_runtime.py:77` (which DOES include `"plan"`) never applies. `AgentSession._active_tools` therefore lacks `plan` → `_execute_tool_by_name` raises `RuntimeError("Tool 'plan' is disabled")`. Phase 5 updated the runtime fallback but missed the CLI default — two hardcoded lists drifted. `one/cli/args.py:263` (`--tools` help text) also lacks `plan`.

**Planned fix (exact):**
- Define the default tool names ONCE as a module-level constant, e.g. `DEFAULT_TOOL_NAMES` in `one/tools/index.py` (next to `all_tools`), containing: read, bash, edit, write, grep, find, ls, finish, plan, spawn_subagent, ask_user.
- Use it in `one/cli/main.py:364` (replace the inline list) and in `one/core/agent_session_runtime.py:77` (replace the inline fallback list).
- Update the `--tools` help text in `one/cli/args.py:263` to include `plan`.
- Regression test: unit test asserting `"plan" in DEFAULT_TOOL_NAMES` and that every name in `DEFAULT_TOOL_NAMES` exists in `all_tools`; subprocess test (tests/test_auth_and_cli.py pattern) running `python -m one.cli.main --help` asserting `plan` appears in the `--tools` line.

**Files:** `one/tools/index.py`, `one/cli/main.py`, `one/core/agent_session_runtime.py`, `one/cli/args.py`, `tests/test_tool_calling.py` or `tests/test_auth_and_cli.py`

**Acceptance Criteria:**
- Default CLI/TUI sessions have `plan` in `_active_tools` (no more "Tool 'plan' is disabled").
- `--tools` help text lists `plan`.
- Full suite green.

**Estimated effort:** ~0.5 h

**Confidence:** High (root cause fully identified)

- [x] **Task 7.1: single source of truth for default tool names**
  - **Description:** Add `DEFAULT_TOOL_NAMES` constant to `one/tools/index.py` (read, bash, edit, write, grep, find, ls, finish, plan, spawn_subagent, ask_user). Replace the inline list in `one/cli/main.py:364` and the fallback list in `one/core/agent_session_runtime.py:77` with the constant. Update `--tools` help text in `one/cli/args.py:263` to include `plan`.
  - **Files:** `one/tools/index.py`, `one/cli/main.py`, `one/core/agent_session_runtime.py`, `one/cli/args.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** No hardcoded tool-name lists remain duplicated; plan is in the default list; help text shows plan.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_tool_calling.py tests/test_auth_and_cli.py`

- [x] **Task 7.2: regression tests + full suite**
  - **Description:** Add a unit test asserting `"plan" in DEFAULT_TOOL_NAMES` and all names resolve in `all_tools`; add a subprocess test (tests/test_auth_and_cli.py pattern) running `python -m one.cli.main --help` and asserting `plan` in the `--tools` line. Run the full suite.
  - **Files:** `tests/test_tool_calling.py`, `tests/test_auth_and_cli.py`
  - **Dependencies:** Task 7.1
  - **Acceptance Criteria:** New tests pass; full suite green (342 + new).
  - **Verification:** `.venv/bin/python -m pytest -q`

### Phase 8: TUI crash — Textual's own markup parser rejects unescaped `[` in rendered content (MarkupError) — DONE

**Bug report (user):** after setting a plan, the TUI crashed with `MarkupError: Expected markup value (found '>",\n').` in `_render_stream` (`one/modes/tui_mode.py:521`, `stream_widget.update("\n".join(rendered))`), triggered from `_write_tool_block` (line 665) ← `on_session_event` (line 1782) while rendering a tool result (read) after the plan was set.

**Root cause (verified by web research):**
1. Textual 8.x has its OWN markup parser (`textual/markup.py`, `MarkupTokenizer`, `expect_markup_expression = Expect("markup value", ...)`) which treats ANY `[` not preceded by a backslash as a tag start and requires `key=value` inside the tag. Content like `[1,2,3]`, `[ABC]`, `[type='CNAME']`, `[{"plan": ">", "x": 1}]` → `MarkupError: Expected markup value (found ...)`.
2. `rich.markup.escape` (regex `(\\*)(\[[a-z#/@][^[]*?])`) escapes ONLY `[`-prefixed lowercase/`#`/`/`/`@` tags — it does NOT escape `[1,2,3]`, `[ABC]`, `[type='CNAME']`, `[{"plan": ">", "x": 1}]`. Phase 6's fix (`rich_escape` in `_refresh_sidebar` line 792 and in `_render_stream` line 517) is therefore insufficient for Textual's stricter parser. Rich 15.0.0's own markup.py has no "Expected markup value" error — confirmed the error comes from Textual, not Rich.
3. Sources: dev.to "type='CNAME' crashed my Textual TUI" (fix: build a `rich.text.Text` object instead of a markup string — `Static.update()` accepts a Text renderable and prints it as-is; "escaping is parser-specific"); GitHub anthropics/claude-code#55583 (same error with rich 15.0.0 + textual 8.2.4).

**Planned fix (exact):**
- `_render_stream` (`one/modes/tui_mode.py` ~510-521): build a `rich.text.Text` object instead of a markup string. For each line: `__MK__:` spinner lines → `text.append_text(Text.from_markup(line))` (spinner markup is intentional); all other lines → `text.append(line + "\n")` (literal, never parsed). `stream_widget.update(text)`.
- `_refresh_sidebar` (~784-821): same approach — build `Text`; lines that intentionally carry markup (section headers `[b info]...[/]`, `[b]MCP[/]`, `[b]Plan[/]`, Keys line) → `Text.from_markup`; all data lines (plan text, MCP bullets, info values) → `Text.append` literal. Remove the `rich_escape(display)` workaround from Phase 6 (literal append supersedes it).
- `_show_extension_panel` (same bug class — extension payload JSON can contain `[`/`]`): title via `Text.from_markup`, payload appended literally via `Text.append`.
- Regression tests: TUI test rendering tool output containing `[type='CNAME']`, `[1,2,3]`, `[ABC]`, `[{"plan": ">", "x": 1}]` → no MarkupError; sidebar with plan containing such text → no MarkupError; existing Phase 6 tests still pass.

**Files:** `one/modes/tui_mode.py`, `tests/test_tui_mode.py`

**Acceptance Criteria:**
- Rendering a tool result containing `[type='CNAME']` / `[1,2,3]` / `[ABC]` / `[{"plan": ">", "x": 1}]` does not raise MarkupError.
- Sidebar renders plan text with such chars without MarkupError.
- Intentional markup (headers, spinner) still renders styled.
- Golden snapshots regenerated (Text-object rendering changes the SVG representation; visible output verified pixel-identical to pre-Phase-8 goldens — 0 text/bg cell diffs). Full suite green.

**Estimated effort:** ~1 h

**Confidence:** High (root cause fully identified via web research; fix approach proven in the referenced article)

- [x] **Task 8.1: Text-object rendering in `_render_stream` + `_refresh_sidebar`**
  - **Description:** In `_render_stream` (`one/modes/tui_mode.py` ~510-521): replace the markup-string join with a `rich.text.Text` object — `text = Text()`; `__MK__:` lines → `text.append_text(Text.from_markup(line))`; other lines → `text.append(line + "\n")`; `stream_widget.update(text)`. In `_refresh_sidebar` (~784-821): build the sidebar as a `Text` object — section headers/Keys line via `Text.from_markup`, all data lines (plan text, MCP bullets, info values) via `Text.append` literal; remove the `rich_escape(display)` workaround from Phase 6 (literal append supersedes it).
  - **Files:** `one/modes/tui_mode.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** No raw content line is ever passed through Textual's markup parser; intentional markup (headers, spinner) still renders styled; Phase 6 tests still pass.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_tui_mode.py tests/test_tui_snapshots.py`

- [x] **Task 8.2: regression tests + full suite**
  - **Description:** Add TUI tests exercising the REAL Textual render path (app-level, `stream_widget.update` through the actual widget): tool result / plan text containing `[type='CNAME']`, `[1,2,3]`, `[ABC]`, `[{"plan": ">", "x": 1}]` → no MarkupError; sidebar with plan containing such text → no MarkupError. Note: Phase 6 tests have a gap — they don't exercise the real Textual parser (the user's crash content passed those tests but crashed in the real TUI). Run the full suite.
  - **Files:** `tests/test_tui_mode.py`
  - **Dependencies:** Task 8.1
  - **Acceptance Criteria:** New tests pass; full suite green (344 + new).
  - **Verification:** `.venv/bin/python -m pytest -q`

### Phase 9: `apply_patch` tool (opencode-style unified-diff patches) — DONE

**User request:** "moze wprowadzic tool takiego tez jak w opencode: apply_patch" — a tool that applies unified-diff patches to files, like opencode's `apply_patch`.

**Reference semantics (verified against opencode source, branch `dev`):**
- `packages/opencode/src/tool/apply_patch.ts` + `apply_patch.txt`: envelope `*** Begin Patch` / `*** End Patch`; per-file sections with headers `*** Add File: <path>` (every line prefixed `+`), `*** Delete File: <path>`, `*** Update File: <path>` (optionally followed by `*** Move to: <path>`); hunks `@@ ...` with `-`/`+`/context (space) lines; parameter `patchText` (required).
- Validation before any write: parse errors → `apply_patch verification failed: <detail>`; empty patch (`*** Begin Patch\n*** End Patch`) → `patch rejected: empty patch`; no hunks → `apply_patch verification failed: no hunks found`; update of a missing file → verification failure.
- New files: trailing `\n` appended if missing. Summary: `Success. Updated the following files:` + one line per file (`A <path>` / `M <path>` / `D <path>`).
- opencode extras NOT applicable to `one`: LSP diagnostics, auto-formatting, filesystem watcher events, BOM handling, permission metadata UI.

**Design for `one` (no new dependencies — hard project constraint):**
- New `one/tools/apply_patch.py`: `apply_patch_tool(cwd, patchText)` following the `edit_tool` pattern (`resolve_to_cwd`, `{"content": [...], "details": {...}}` return). Own minimal unified-diff parser: split envelope, parse headers, parse hunks (`@@ -l,c +l,c @@`), apply hunks to file content with exact context matching (no fuzz), compute per-file diff via `difflib.unified_diff` for `details.diff`.
- Operations: add (create file, `+` lines, ensure trailing `\n`), update (apply hunks to existing file), delete (remove file), move (update + `*** Move to:` → write new path, remove old). Multi-file patches supported; ALL files validated before ANY write (all-or-nothing on validation errors).
- Registration: `one/tools/index.py` — import + `all_tools["apply_patch"]` + `DEFAULT_TOOL_NAMES` + `coding_tools` (mutating tool); `one/resources/resource_loader.py` — `TOOL_ARG_SCHEMAS["apply_patch"]`; `one/core/settings_manager.py:34` — default `approvalTools` becomes `["bash", "write", "edit", "plan", "apply_patch"]`; AGENTS.md 11 → 12 tools.
- No event-contract changes (normal tool; `tool_call_start/end` unchanged).

**Files:** `one/tools/apply_patch.py` (new), `one/tools/index.py`, `one/resources/resource_loader.py`, `one/core/settings_manager.py`, `tests/test_apply_patch.py` (new), `AGENTS.md`

**Acceptance Criteria:**
- `apply_patch` callable by the model; schema `{patchText}` in the prompt.
- Add/Update/Delete/Move + multi-file patches work; validation errors abort before any write.
- Cooperation mode asks for approval before executing `apply_patch` (default `approvalTools`).
- Full suite green (355 + new).

**Estimated effort:** ~2.5 h

**Confidence:** High (reference semantics verified from opencode source; tool pattern and registration points already known from Phases 5-7)

- [x] **Task 9.1: `apply_patch` tool implementation**
  - **Description:** Create `one/tools/apply_patch.py` with `apply_patch_tool(cwd, patchText)` (pattern: `one/tools/edit.py`). Parse the opencode envelope format: `*** Begin Patch` / `*** End Patch`; sections `*** Add File: <path>` (lines prefixed `+`), `*** Delete File: <path>`, `*** Update File: <path>` with optional `*** Move to: <path>`; hunks `@@ -l,c +l,c @@` with `-`/`+`/context lines. Resolve paths via `resolve_to_cwd(path, cwd)`. Validate the ENTIRE patch before writing anything: unknown header → error; update/delete of a missing file → error; hunk context mismatch → error; empty patch → `patch rejected: empty patch`; no hunks → `no hunks found`; parse errors → `apply_patch verification failed: <detail>`. Apply: add (create with trailing `\n` if missing), update (exact context matching, no fuzz), delete, move (write new path + remove old). Return `{"content": [{"type": "text", "text": "Success. Updated the following files:\nA <path>\nM <path>\nD <path>"}], "details": {"diff": <combined unified diff>}}` (diff via `difflib.unified_diff`, like `edit_tool`).
  - **Files:** `one/tools/apply_patch.py` (new)
  - **Dependencies:** None
  - **Acceptance Criteria:** All four operations + move work; multi-file patches work; any validation error aborts before any write; return shape matches the `edit_tool` pattern.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_apply_patch.py`

- [x] **Task 9.2: registration (index, schema, approval, AGENTS.md)**
  - **Description:** `one/tools/index.py`: import `apply_patch_tool`, add `all_tools["apply_patch"] = ToolDef("apply_patch", "Apply a unified-diff patch to files (opencode format: *** Begin Patch / *** End Patch; Add/Update/Delete/Move)", apply_patch_tool)`, add `"apply_patch"` to `DEFAULT_TOOL_NAMES` (lines 27-29) and to `coding_tools` (line 53). `one/resources/resource_loader.py`: add `"apply_patch": "{patchText}  # unified diff patch (*** Begin Patch / *** End Patch envelope; Add/Update/Delete/Move)"` to `TOOL_ARG_SCHEMAS` (lines 43-55). `one/core/settings_manager.py:34`: default `approvalTools` → `["bash", "write", "edit", "plan", "apply_patch"]`. Update `AGENTS.md` (11 → 12 tools; mention `apply_patch` in the tools list).
  - **Files:** `one/tools/index.py`, `one/resources/resource_loader.py`, `one/core/settings_manager.py`, `AGENTS.md`
  - **Dependencies:** Task 9.1
  - **Acceptance Criteria:** `apply_patch` in `DEFAULT_TOOL_NAMES`/`all_tools`/`coding_tools`; schema shown in the prompt; approval prompt in cooperation mode; AGENTS.md lists 12 tools.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_tool_calling.py tests/test_auth_and_cli.py`; manual: `python -m one.cli.main --help` shows `apply_patch` in `--tools`.

- [x] **Task 9.3: tests + full suite**
  - **Description:** New `tests/test_apply_patch.py` (tmp_path-based, no real FS): add file (with/without trailing `\n`), update (context match, `-`/`+`), delete, move, multi-file patch, empty patch error, no-hunks error, unknown header error, update/delete missing file error, context mismatch error, all-or-nothing (one bad hunk → nothing written), `..`/absolute path behavior consistent with `resolve_to_cwd`, return shape (`content` text + `details.diff`). Run the full suite.
  - **Files:** `tests/test_apply_patch.py` (new)
  - **Dependencies:** Task 9.1, Task 9.2
  - **Acceptance Criteria:** New tests pass; full suite green (355 + new).
  - **Verification:** `.venv/bin/python -m pytest -q`

### Phase 10: `edit` tool robustness — model puts `path` inside `edits[0]` (real-usage bug report) — DONE

**Bug report (user, real usage of `one`):** "podczas korzystania z one jest duzo błedów z edit" — repeated failures. Example observed call:
```json
{"tool":"edit","args":{"edits":[{"oldString":"    def test_invalid_room_id_zero(self):\n        ...", "newString":"    def test_invalid_room_id_negative(self):\n        ...", "path":"/tmp/room_reservation/tests/test_reservation_system.py"}]}}
```
The model nests `path` INSIDE the edit dict instead of passing it top-level. The session log shows `tool start: edit` → `tool err: edit` → the agent retries with `find` — wasted steps, repeated mistakes.

**Root cause (verified in code):**
1. `one/core/agent_session.py:525,533-534` — `path_arg = args.get("path") or args.get("file")`; for `edit` it calls `fn(cwd, path_arg or "", args.get("edits", []))`. With the malformed shape, `path_arg` is `""`.
2. `one/tools/edit.py:24-26` — `resolve_to_cwd("", cwd)` resolves to the CWD DIRECTORY; `p.exists()` is True but `p.is_file()` is False → `raise FileNotFoundError(f"File not found: {path}")` with an EMPTY path. The model sees a misleading "File not found: " that does not name the real problem (wrong JSON shape), so it repeats the same mistake.
3. The prompt shows only `- edit {path, edits: [{oldString, newString}]}` (`one/resources/resource_loader.py:278`, from `TOOL_ARG_SCHEMAS`); the `ToolDef` description ("Edit files with exact replacement") is NOT in the prompt. The schema line is the model's only hint and is evidently not unambiguous enough for local models.
4. Secondary failure mode: if the model passes `edits` as a dict (not a list), `edit_tool` iterates over dict keys (strings) → `e.get(...)` → `AttributeError` (unhandled, confusing).

**Planned fix (exact):**
- `one/tools/edit.py`:
  - If `path` is empty/missing AND `edits` is a non-empty list whose first element is a dict, recover `path` from `edits[0].get("path")` or `edits[0].get("file")` (tolerate the observed model shape).
  - If `path` is still empty → `raise ValueError("Edit tool input is invalid. 'path' is required as a top-level argument (not inside edits).")` — names the real cause.
  - If `edits` is not a list → `raise ValueError("Edit tool input is invalid. edits must be a list of {oldString, newString} objects.")`.
  - Keep all existing behavior (uniqueness, overlap checks, diff details) unchanged.
- `one/resources/resource_loader.py` `TOOL_ARG_SCHEMAS["edit"]` → `"{path, edits: [{oldString, newString}]}  # path is TOP-LEVEL (never inside edits); oldString must be unique in the file"` — the prompt then states the rule explicitly.
- `tests/test_resource_loader.py:55,91` — update the two assertions that check the exact `- edit {path, edits: [{oldString, newString}]}` string to the new schema text.
- Tests in `tests/test_tools.py`: `edit_tool` with `path` nested in `edits[0]` works (file edited, summary correct); `edit_tool` with no `path` anywhere → `ValueError` matching `top-level`; `edits` as a dict → `ValueError` matching `list`; existing edit tests unchanged and green.

**Files:** `one/tools/edit.py`, `one/resources/resource_loader.py`, `tests/test_tools.py`, `tests/test_resource_loader.py`

**Acceptance Criteria:**
- The observed malformed call shape (path inside `edits[0]`) succeeds instead of failing.
- Missing `path` everywhere → clear error naming the top-level requirement.
- `edits` as dict → clear error.
- Prompt schema for `edit` states `path` is TOP-LEVEL.
- Full suite green (393 + new).

**Estimated effort:** ~1 h

**Confidence:** High (root cause fully identified from the reported call + code paths; fix is local to one tool + one schema line)

- [x] **Task 10.1: `edit_tool` robustness (nested path recovery + clear errors)**
  - **Description:** In `one/tools/edit.py::edit_tool`: (a) after the existing `if not edits` check, if `path` is empty and `edits` is a non-empty list with a dict first element, set `path = edits[0].get("path") or edits[0].get("file") or ""`; (b) if `path` is still empty → `ValueError("Edit tool input is invalid. 'path' is required as a top-level argument (not inside edits).")`; (c) if `edits` is not a list → `ValueError("Edit tool input is invalid. edits must be a list of {oldString, newString} objects.")` (place this check before the `if not edits` check so a dict does not slip through). Keep uniqueness/overlap/diff behavior unchanged.
  - **Files:** `one/tools/edit.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** Malformed shape (path in `edits[0]`) succeeds; missing path everywhere → clear ValueError; `edits` as dict → clear ValueError; existing edit behavior unchanged.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_tools.py`

- [x] **Task 10.2: prompt schema + resource-loader test updates**
  - **Description:** In `one/resources/resource_loader.py` `TOOL_ARG_SCHEMAS` change `"edit": "{path, edits: [{oldString, newString}]}"` to `"edit": "{path, edits: [{oldString, newString}]}  # path is TOP-LEVEL (never inside edits); oldString must be unique in the file"`. Update `tests/test_resource_loader.py` lines 55 and 91 to assert the new string.
  - **Files:** `one/resources/resource_loader.py`, `tests/test_resource_loader.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** Prompt shows the TOP-LEVEL hint for `edit`; resource-loader tests assert the new schema text.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_resource_loader.py`

- [x] **Task 10.3: regression tests + full suite**
  - **Description:** Add tests to `tests/test_tools.py`: (a) `edit_tool(cwd, "", [{"oldString": ..., "newString": ..., "path": "a.txt"}])` edits the file and returns the success dict; (b) `edit_tool(cwd, "", [{"oldString": ..., "newString": ...}])` → `ValueError` matching `top-level`; (c) `edit_tool(cwd, "a.txt", {"oldString": "x"})` → `ValueError` matching `list`. Run the full suite.
  - **Files:** `tests/test_tools.py`
  - **Dependencies:** Task 10.1, Task 10.2
  - **Acceptance Criteria:** New tests pass; full suite green (393 + new).
  - **Verification:** `.venv/bin/python -m pytest -q`

### Phase 11: `/login` validation + remote model fetch — DONE (in `d8307a1`)

> Implementation note: live checks showed openrouter AND ollama-cloud expose a PUBLIC `GET /models` (bad key → 200 + full list), so a 200 list does NOT prove a key. Final design: `validate_and_fetch` always proves a given key with a minimal chat call (`max_tokens=1`) against `model_id` or the first fetched model; 401/403 → hard failure (`Authorization failed`, nothing stored); 402 → soft pass with a credit note; 400 (probe model rejects e.g. `reasoning_effort`) → soft pass; other errors → hard failure. `max_tokens` added to all adapter `chat()` signatures (base default `None`).

**Context (verified in code + live):**
- `/login` (interactive: `one/modes/interactive_mode.py:714-764`; TUI: `one/modes/tui_mode.py:1132-1163` + `_complete_login` 1567-1594) stores the key via `set_stored_api_key` and sets defaults — NO network validation, NO model fetch.
- `--list-models` (`one/cli/main.py:263-271`) lists only the local registry (builtins + `models.json`).
- `ProviderAdapter` (`one/providers/base.py`) has only `chat()` — no `list_models`.
- Live-verified endpoints with the stored keys: `GET https://openrouter.ai/api/v1/models` → 200 `data[].id` list; `GET https://ollama.com/v1/models` → 200 list (ollama-cloud). Anthropic has no public models-list endpoint. Gemini: `GET https://generativelanguage.googleapis.com/v1beta/models?key=...` (filter `supportedGenerationMethods` contains `generateContent`).

**Planned fix (exact):**
- `one/providers/base.py`: add `async def list_models(self, api_key: str, headers: dict | None = None) -> list[str] | None` (base raises `NotImplementedError`; `None` = "no list available").
- `one/providers/openai_compatible.py`: `GET {base_url}/models` with Bearer key; parse `data[].id`; on error raise `RuntimeError(f"{name} API error {status}: {body}")` (401/403 surfaces as `Authorization failed`).
- `one/providers/gemini.py`: `GET https://generativelanguage.googleapis.com/v1beta/models?key={key}`; return ids whose `supportedGenerationMethods` includes `generateContent`.
- `one/providers/anthropic.py`: `list_models` returns `None`; validation falls back to a minimal chat call (`max_tokens=1`, message "ping") with the first registered model for the provider.
- `one/core/model_registry.py`: `register_models(provider, ids) -> int` (adds `ModelInfo(provider, id, reasoning=True, context_window=None)` for ids not already present, dedupe) and `persist_models(provider, ids)` (merge into `models.json` `providers.<provider>`, keep existing `url`/`toolParser`/`contextWindow`, write via `get_models_path()`).
- Login flow helper (new `one/core/provider_login.py` or equivalent): `async def validate_and_fetch(adapter, api_key, provider) -> tuple[bool, str | None, list[str] | None]` — validates BEFORE storing; used by both interactive `/login` and TUI `_complete_login`:
  - key given → validate first; failure → `Authorization failed for <provider>: <error>`, key NOT stored, defaults NOT changed.
  - success → store key, fetch models (if list available), `register_models` + `persist_models`, print `Authorized. Fetched N models:` + list, set default provider; optional `[model]` resolved against the fetched list (then default model + session model switch).
  - NO_AUTH providers (`llama.cpp`, `ollama`): no key → skip validation, still fetch + register the local model list.
- `AGENTS.md`: document the new `/login` semantics (validation + model fetch).

**Files:** `one/providers/base.py`, `one/providers/openai_compatible.py`, `one/providers/anthropic.py`, `one/providers/gemini.py`, `one/core/model_registry.py`, `one/core/provider_login.py` (new), `one/modes/interactive_mode.py`, `one/modes/tui_mode.py`, `tests/test_login_validation.py` (new), `tests/test_provider_payloads.py` (or `tests/test_extra_providers.py`), `AGENTS.md`

**Acceptance Criteria:**
- `/login <provider> <key>` with a valid key: key stored, models fetched + registered + persisted to `models.json`, list printed, default provider set.
- `/login <provider> <bad-key>`: `Authorization failed`, key NOT stored, defaults unchanged.
- `/login llama.cpp` (no key): local model list fetched + registered.
- `--list-models` shows fetched models after `/login` (persisted).
- Full suite green (396 + new).

**Estimated effort:** ~3 h

**Confidence:** Medium (endpoints verified live; Anthropic validation path is the least certain — no list endpoint, minimal-chat fallback)

- [x] **Task 11.1: `list_models` on provider adapters**
  - **Description:** Add `async list_models(api_key, headers=None) -> list[str] | None` to `ProviderAdapter` (base: `NotImplementedError`). Implement in `OpenAICompatibleAdapter` (`GET {base_url}/models`, Bearer, parse `data[].id`, error → `RuntimeError` with status+body), `GeminiAdapter` (models endpoint, filter `generateContent`), `AnthropicAdapter` (return `None`). Add a minimal-chat validation helper for Anthropic (chat with `max_tokens=1`, first registered model).
  - **Files:** `one/providers/base.py`, `one/providers/openai_compatible.py`, `one/providers/anthropic.py`, `one/providers/gemini.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** Each adapter exposes `list_models`; OpenAI-compatible parses `data[].id`; Gemini filters by `generateContent`; Anthropic returns `None`; errors raise `RuntimeError` with status.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_provider_payloads.py tests/test_extra_providers.py` + new unit tests with stub HTTP (httpx MockTransport)

- [x] **Task 11.2: registry registration + persistence**
  - **Description:** In `one/core/model_registry.py`: `register_models(provider, ids) -> int` (in-memory `ModelInfo` additions with dedupe) and `persist_models(provider, ids)` (merge into `models.json` `providers.<provider>` via `get_models_path()`, keep existing `url`/`toolParser`/`contextWindow`, create file if missing).
  - **Files:** `one/core/model_registry.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** New ids registered and deduped; `models.json` merged without losing existing per-model fields; file created when missing.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_login_validation.py` (temp-dir models.json)

- [x] **Task 11.3: login flow helper + interactive/TUI wiring**
  - **Description:** New `one/core/provider_login.py` with `async def validate_and_fetch(adapter, api_key, provider) -> tuple[bool, str | None, list[str] | None]` (validate BEFORE storing; 401/403/network → failure with message). Rewire interactive `/login` (`interactive_mode.py:714-764`) and TUI `_complete_login` (`tui_mode.py:1567-1594`): on success store key, fetch + register + persist models, print `Authorized. Fetched N models:` + list, set default provider, resolve optional `[model]` against fetched list; NO_AUTH providers skip validation but still fetch. Update `AGENTS.md`.
  - **Files:** `one/core/provider_login.py` (new), `one/modes/interactive_mode.py`, `one/modes/tui_mode.py`, `AGENTS.md`
  - **Dependencies:** Task 11.1, Task 11.2
  - **Acceptance Criteria:** Valid key → stored + models fetched/registered/persisted + list printed; invalid key → `Authorization failed`, nothing stored; no-key NO_AUTH provider → models fetched; `[model]` arg resolved from fetched list.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_login_validation.py tests/test_interactive_mode.py` + manual live check: `/login openrouter <key>` and `/login ollama-cloud <key>` in a scratch agent dir

- [x] **Task 11.4: regression tests + full suite**
  - **Description:** New `tests/test_login_validation.py`: stub adapters (no network) — success path (key stored, models registered + persisted to temp `models.json`), failure path (401 → key NOT stored), Anthropic minimal-chat fallback, NO_AUTH fetch without key, dedupe/merge behavior. Run the full suite.
  - **Files:** `tests/test_login_validation.py` (new)
  - **Dependencies:** Task 11.3
  - **Acceptance Criteria:** New tests pass; full suite green (396 + new).
  - **Verification:** `.venv/bin/python -m pytest -q`

### Phase 12: `one run <task>` value-flag fix — DONE (in `613ae0e`)

**Context (verified live):** `one/cli/args.py:65-96` — `parse_args` detects the `run` subcommand only when `argv[0] == "run"`; the re-injection loop (76-94) collects flags but pairs values ONLY for `_VALUE_FLAGS = {"--answer-file", "--steer-file", "--param"}`. All other value-taking flags lose their values: `one run "task" --provider openrouter --model openai/gpt-4.1` → `error: argument --provider: expected one argument` (reproduced). Consequence: headless runs cannot select provider/model via flags (only settings/env).

**Planned fix (exact):**
- `one/cli/args.py`: replace the hardcoded `_VALUE_FLAGS` with the full set of value-taking flags mirroring the `parser.add_argument` calls (lines 99-147): `--provider`, `--model`, `--api-key`, `--llama-cpp-url`, `--ollama-url`, `--system-prompt`, `--append-system-prompt`, `--thinking`, `--mode`, `--session`, `--session-dir`, `--models`, `--tools`, `--export`, `--export-format`, `--theme`, `--prompt-template`, `--skill`, `--extension`, `--list-models`, `--answer-file`, `--steer-file`, `--param`. In the loop: flag in the set → append flag + next arg (if present) and skip both; other `-`-prefixed → append alone; else → task text. Repeatable flags (`--param`, `--skill`, `--extension`, `--prompt-template`, `--theme`) pair per occurrence.
- Keep `run_task` free-form semantics unchanged (task text = all non-flag tokens).

**Files:** `one/cli/args.py`, `tests/test_auth_and_cli.py`

**Acceptance Criteria:**
- `parse_args(["run", "task", "--provider", "openrouter", "--model", "openai/gpt-4.1"])` → `provider == "openrouter"`, `model == "openai/gpt-4.1"`, `run_task == "task"`.
- Flags before the task text work the same: `parse_args(["run", "--provider", "openrouter", "task"])`.
- `--list-models` (optional-value flag) still works: `run "task" --list-models` and `run "task" --list-models gpt`.
- Existing behavior unchanged: `one run <task>` without flags, `--json`, `--answer-file`/`--steer-file` pairing.
- Full suite green (396 + new).

**Estimated effort:** ~1 h

**Confidence:** High (pure parsing defect; unit-testable via `parse_args`)

- [x] **Task 12.1: value-flag pairing in the `run` re-injection loop**
  - **Description:** In `one/cli/args.py:76-94`, generalize `_VALUE_FLAGS` to the full set of value-taking flags (list above); pair each occurrence with its value; keep task-text collection for everything else.
  - **Files:** `one/cli/args.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** `--provider`/`--model`/`--api-key`/`--thinking`/`--models`/`--tools`/`--theme`/`--list-models` etc. keep their values with `run`; task text intact; no argparse errors.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_auth_and_cli.py` (new `parse_args` unit tests)

- [x] **Task 12.2: regression tests + full suite**
  - **Description:** Add `parse_args` unit tests to `tests/test_auth_and_cli.py` (flags after task, flags before task, optional-value `--list-models`, repeatable `--param`, existing `--answer-file` pairing) + one subprocess smoke test (`python -m one.cli.main run "task" --provider openrouter --model openai/gpt-4.1 --json` with scratch `ONE_CODING_AGENT_DIR` asserting no argparse error). Run the full suite.
  - **Files:** `tests/test_auth_and_cli.py`
  - **Dependencies:** Task 12.1
  - **Acceptance Criteria:** New tests pass; full suite green (396 + new).
  - **Verification:** `.venv/bin/python -m pytest -q`

### Phase 13: `/providers` — display & select logged-in providers — DONE (in `793ea32`)

**Context:** `/model` dumps raw JSON of ALL registry providers (no key info); `/login status` shows auth but no models; switching requires typing `/model <p>/<id>` by hand. Missing: one command listing **logged-in** providers (key present or NO_AUTH) with quick selection.

**Approved design:**
- `/providers` → numbered list of logged-in providers only (`ModelRegistry.get_available()`), `*` marks current default, model count per provider, usage hint. Not-logged-in providers are NOT shown (user decision).
- `/providers <number|name>` → second step: numbered list of that provider's registered models + usage hint (no switch yet).
- `/providers <number|name> <number|model-id>` → switch: reuse the `/model <p>/<m>` path (`set_model` + `set_default_provider`/`set_default_model`; TUI additionally `_refresh_sidebar()`). Selection is registry-only (no dynamic resolve — use `/model` for that).
- Both modes: `one/modes/interactive_mode.py` (print) and `one/modes/tui_mode.py` (`_write` + sidebar), help text updated in both.

**Files:** `one/modes/interactive_mode.py`, `one/modes/tui_mode.py`, `tests/test_interactive_mode.py`, `tests/test_tui_mode.py`

**Acceptance Criteria:**
- `/providers` lists only providers with configured auth (or NO_AUTH), numbered, `*` on current, counts.
- `/providers 1` (or name) lists that provider's models numbered without switching.
- `/providers 1 2` / `/providers openrouter glm-5.1` switches model + persists defaults (TUI refreshes sidebar).
- Unknown/not-logged-in provider or bad model ref → clear error, nothing changed.
- Full suite green (426 + new).

**Estimated effort:** ~2 h

**Confidence:** High (pure command-layer logic over existing registry APIs)

- [x] **Task 13.1: `/providers` in interactive mode**
  - **Description:** Add `/providers` handling (list / second-step model list / switch) near the `/model` block; update both help texts.
  - **Files:** `one/modes/interactive_mode.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** All three forms work; errors as specified; output via `print`.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_interactive_mode.py`
- [x] **Task 13.2: `/providers` in TUI mode**
  - **Description:** Same three forms via `self._write(...)`; `_refresh_sidebar()` after a successful switch; update help text.
  - **Files:** `one/modes/tui_mode.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** As 13.1 plus sidebar refresh; no golden-snapshot changes (new command not exercised there).
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_tui_mode.py`
- [x] **Task 13.3: tests + full suite**
  - **Description:** Interactive tests (list filters unauthenticated, second step, switch by number/id, error paths) + TUI tests (same core paths via `_mk_app_session` + runtime key). Run full suite.
  - **Files:** `tests/test_interactive_mode.py`, `tests/test_tui_mode.py`
  - **Dependencies:** Task 13.1, 13.2
  - **Acceptance Criteria:** New tests pass; full suite green (426 + new).
  - **Verification:** `.venv/bin/python -m pytest -q`

### Phase 14: `/providers` follow-up fixes — DONE (in `9dac4d2`)

**User-reported issues after Phase 13:**
1. `/providers` missing from the TUI **keybinding** help (`action_help`, `one/modes/tui_mode.py:1736`). The `/help` *command* in both modes already lists it.
2. Autocompletion does not offer `/providers`: `_SLASH_COMMANDS` (`one/modes/tui_mode.py:193-200`) lacks it (the completer at line 311 filters exactly this tuple).
3. ollama-cloud shows only 1 model — NOT a bug: `/providers` reads the local registry (`models.json`), which currently holds only the stale builtin `glm-5:cloud`. Fix is operational: run `/login ollama-cloud <key>` once — Phase 11 validation fetches all ~19 live models and persists them to `models.json`; `/providers` then lists them. Optional UX follow-up (out of scope here): a hint in `/providers` output when a provider's model list looks stale, or a `/providers --refresh <provider>` action reusing `validate_and_fetch`.

**Planned fix (exact):**
- `one/modes/tui_mode.py:193`: add `"/providers"` to `_SLASH_COMMANDS` (after `"/model-cycle"`).
- `one/modes/tui_mode.py:1736` (`action_help`): insert `/providers` after `/model-cycle` in the one-line help string.
- `tests/test_tui_mode.py`: add assertions — `"/providers" in _SLASH_COMMANDS`; `action_help` output contains `/providers` (via `app.action_help()` + `_stream_lines`).

**Files:** `one/modes/tui_mode.py`, `tests/test_tui_mode.py`

**Acceptance Criteria:**
- Typing `/prov` in the TUI input autocompletes to `/providers`.
- The keybinding help line includes `/providers`.
- Full suite green (435 + new).

**Estimated effort:** ~0.5 h

**Confidence:** High (two one-line edits + assertions)

- [x] **Task 14.1: completion + keybinding help**
  - **Description:** Add `"/providers"` to `_SLASH_COMMANDS` and to `action_help`'s command list.
  - **Files:** `one/modes/tui_mode.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** As above.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_tui_mode.py`
- [x] **Task 14.2: tests + full suite**
  - **Description:** Assertions for completion list membership and help output; run full suite.
  - **Files:** `tests/test_tui_mode.py`
  - **Dependencies:** Task 14.1
  - **Acceptance Criteria:** New tests pass; full suite green (435 + new).
  - **Verification:** `.venv/bin/python -m pytest -q`

### Phase 15: `/login refresh <provider>` — re-fetch live model list — DONE (in `9dac4d2`)

**Motivation:** `/providers` reads the LOCAL registry (`models.json`); stale lists (e.g. ollama-cloud showing only the builtin `glm-5:cloud`) can only be fixed today by re-running full `/login <provider> <key>` with the key typed again. User request: pick from the provider's CURRENT list. Placement decided by user: under `/login` (auth+fetch lifecycle), not as a `/providers` flag.

**Approved design:**
- `/login refresh <provider>` (interactive + TUI):
  1. Resolve adapter from `session.providers` (missing → error).
  2. Key via `model_registry.get_api_key_and_headers(ModelInfo(provider=..., id=""))` — handles NO_AUTH (empty key OK), stored keys, env fallback; not-ok → print its error/hint (`set <ENV> or use /login`).
  3. `validate_and_fetch(adapter, api_key, provider)` — revalidates the key (minimal chat probe; 401/403 → clear failure, nothing changed; 402/400 → soft pass with note).
  4. On success: `register_models` + `persist_models` (merge into `models.json`), print `Authorized. Fetched N models (added M new).` + numbered list + hint `pick with /providers <number|name> <model-number|id>` or `/model <p>/<id>`.
  5. On failure: print error; registry untouched.
- Provider argument is the NAME (consistent with `/login <provider>`); numbering stays a `/providers` concern.
- Help text updated in both modes: `/login [status|refresh <provider>|provider [apiKey] [model]]`.
- Non-goals: refreshing ALL providers at once; auto-refresh inside `/providers` listing.

**Files:** `one/modes/interactive_mode.py` (login handler ~731+), `one/modes/tui_mode.py` (`_complete_login` area / command handling), `tests/test_interactive_mode.py`, `tests/test_tui_mode.py`

**Acceptance Criteria:**
- `/login refresh ollama-cloud` with a stored valid key: models re-fetched, registered + persisted, count + list printed; new ids selectable via `/providers`.
- NO_AUTH provider (`/login refresh llama.cpp`): fetches without key.
- Missing key → hint error; invalid key (401/403) → `Authorization failed`, registry unchanged; unknown provider/adapter → clear error.
- Full suite green (435 + new).

**Estimated effort:** ~1.5 h

**Confidence:** High (reuses tested `validate_and_fetch`/`register_models`/`persist_models` paths from Phase 11)

- [x] **Task 15.1: `/login refresh` in interactive mode**
  - **Description:** Extend the `/login` handler: `refresh <provider>` branch (key resolution → validate_and_fetch → register/persist → numbered list output); update help line.
  - **Files:** `one/modes/interactive_mode.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** As above.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_interactive_mode.py`
- [x] **Task 15.2: `/login refresh` in TUI mode**
  - **Description:** Same branch in TUI command handling (`self._write` outputs); update help lines (both `/help` command and `action_help`).
  - **Files:** `one/modes/tui_mode.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** As above.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_tui_mode.py`
- [x] **Task 15.3: tests + full suite**
  - **Description:** Interactive tests (stub adapter + stored key success incl. `added N new` count; NO_AUTH; missing-key hint; 401 keeps registry intact) + TUI tests (`_LoginStub` pattern). Run full suite.
  - **Files:** `tests/test_interactive_mode.py`, `tests/test_tui_mode.py`
  - **Dependencies:** Task 15.1, 15.2
  - **Acceptance Criteria:** New tests pass; full suite green (435 + new).
  - **Verification:** `.venv/bin/python -m pytest -q`

### Phase 16: current date & time in the system prompt — DONE (in `b4777a6`)

**Motivation:** the model has no notion of "today" — questions like "what did I do yesterday", log analysis, cron/scheduling tasks need the current date/time.

**Approved design (user confirmed):**
- Injected in `AgentSession._build_runtime_system_prompt()` (`one/core/agent_session.py:134`) as a trailing section — rebuilt every step, so it stays fresh in long sessions and covers ALL modes (run/print/rpc/tui/interactive).
- Format (local timezone):
  ```
  # Current Date
  Today is 2026-08-21 (Friday), 14:33 local time (CEST, UTC+02:00).
  ```
- Section goes LAST (after `# Active Plan`); no config option — always on.
- No changes to resource_loader / adapters / prompt templates.

**Files:** `one/core/agent_session.py`, plus a unit test in the existing agent-session test file.

**Acceptance Criteria:**
- Every provider request's system message ends with the `# Current Date` section containing today's date, weekday, HH:MM, tz abbreviation and UTC offset.
- Existing behavior unchanged otherwise; full suite green (444 + new).

**Estimated effort:** ~0.5 h

**Confidence:** High (single injection point, pure formatting)

- [x] **Task 16.1: inject date section**
  - **Description:** Append the `# Current Date` section in `_build_runtime_system_prompt()` after the plan block; compute via `datetime.now().astimezone()`.
  - **Files:** `one/core/agent_session.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** As above.
  - **Verification:** `.venv/bin/python -m pytest -q <agent-session tests>`
- [x] **Task 16.2: tests + full suite**
  - **Description:** Unit test asserting the section exists and contains today's date; run full suite to catch any exact-prompt assertions.
  - **Files:** existing agent-session test file
  - **Dependencies:** Task 16.1
  - **Acceptance Criteria:** New test passes; full suite green (444 + new).
  - **Verification:** `.venv/bin/python -m pytest -q`

### Phase 17: ctx gauge fix + real context windows from providers — DONE (in `1b59855`)

**Motivation:** `ollama-cloud/nemotron-3-ultra` (fetched via `/login`, registered with `context_window=None`) shows no ctx usage. Root cause: `AgentSession.__init__` assigns `self.model` WITHOUT the `FALLBACK_CONTEXT_WINDOW` normalization that only `set_model()` applies (`agent_session.py:64` vs `:981`). Additionally, fetched models never learn their REAL context length even though provider list endpoints expose it (OpenRouter `context_length`, Gemini `inputTokenLimit`).

**Approved design (user picked full scope):**
- **17.1** helper `_with_fallback_context(model)` used by BOTH `__init__` and `set_model` → every model without a known window gets 128k; gauge + compaction always work.
- **17.2** pipeline carries `{"id": str, "contextWindow": int | None}`:
  - `ProviderAdapter.list_models_detailed()` (base default delegates to `list_models()` → `contextWindow: None`; keeps stubs working); overrides: openai_compatible parses `data[].context_length`, gemini parses `inputTokenLimit`; anthropic inherits default (no list endpoint).
  - `validate_and_fetch` prefers `list_models_detailed` (getattr fallback to `list_models` for exotic adapters/stubs) and returns model entries as dicts.
  - `register_models` / `persist_models` accept `str | dict` entries; new entries carry `contextWindow`; existing entries get it filled when missing (refresh enriches). models.json load already reads `contextWindow`.
  - Display loops in interactive/TUI login+refresh handle dicts.

**Files:** `one/core/agent_session.py`, `one/providers/base.py`, `one/providers/openai_compatible.py`, `one/providers/gemini.py`, `one/core/provider_login.py`, `one/core/model_registry.py`, `one/modes/interactive_mode.py`, `one/modes/tui_mode.py`, tests.

**Acceptance Criteria:**
- Session started with a `context_window=None` model has a working ctx gauge (128k fallback).
- After `/login` on OpenRouter, fetched models persist real `context_length` into models.json and the registry; Gemini persists `inputTokenLimit`; providers without the field fall back to 128k at runtime.
- Full suite green (445 + new).

**Estimated effort:** ~2 h

**Confidence:** High

- [x] **Task 17.1: fallback normalization in __init__**
  - **Description:** Extract `_with_fallback_context()`; apply in `AgentSession.__init__` and `set_model`.
  - **Files:** `one/core/agent_session.py`
  - **Dependencies:** None
  - **Acceptance Criteria:** `get_context_usage()` non-None for a None-window model passed to the constructor.
  - **Verification:** `.venv/bin/python -m pytest -q tests/test_compaction.py`
- [x] **Task 17.2: context lengths end-to-end**
  - **Description:** As designed above (adapters → provider_login → registry → persistence → mode callers).
  - **Files:** see file list
  - **Dependencies:** Task 17.1
  - **Acceptance Criteria:** As above.
  - **Verification:** `.venv/bin/python -m pytest -q`

### Phase 18: subscription OAuth login (Claude Pro, ChatGPT/Codex) — DONE

**Motivation:** today only API-key auth exists (`auth.json` / env / runtime). Subscription logins (Claude Pro/Max, ChatGPT Plus/Pro) use OAuth — user asked for Codex-style "log in with account".

**Research findings (Aug 2026, reverse-engineered flows — UNDOCUMENTED APIs, may change):**

*Anthropic Claude Pro/Max (simple — copy/paste flow, no local server):*
- client_id `9d1c250a-e61b-44d9-88ed-5944d1962f5e`; authorize `https://claude.ai/oauth/authorize?code=true&client_id=...&response_type=code&redirect_uri=https://console.anthropic.com/oauth/code/callback&scope=org:create_api_key user:profile user:inference&code_challenge=<S256>&code_challenge_method=S256&state=<verifier>`
- Browser shows `CODE#STATE`; user pastes it back (no callback server!)
- Exchange/refresh: POST `https://console.anthropic.com/v1/oauth/token` (JSON): `authorization_code` / `refresh_token` grants
- Access token = `sk-ant-oat01-...`; usage: `Authorization: Bearer` against `api.anthropic.com/v1/messages` + header `anthropic-beta: oauth-2025-04-20` (+ `claude-code-20250219`), UA `claude-cli/<ver>`; `GET /v1/models` works with the token
- ⚠️ Anthropic (Feb 2026 docs): Pro/Max OAuth is for official clients only — third-party use at own risk

*OpenAI ChatGPT/Codex (harder — local callback server + Responses API backend):*
- client_id `app_EMoamEEZ73f0CkXaXp7hrann`; authorize `https://auth.openai.com/oauth/authorize`, redirect `http://localhost:1455/auth/callback` (local HTTP server REQUIRED), scope `openid profile email offline_access`, extra params `id_token_add_organizations=true`, `codex_cli_simplified_flow=true`, `originator`
- Exchange/refresh: POST `https://auth.openai.com/oauth/token` (form-encoded); proactive refresh ~5 min before expiry + reactive retry once on 401
- accountId from JWT claims (id_token → access_token fallback chain: top-level `chatgpt_account_id` → `https://api.openai.com/auth`.chatgpt_account_id → `organizations[0].id`)
- Storage shape: `{"type": "oauth", "access", "refresh", "expires" (ms), "accountId"}`
- Runtime requests go to `https://chatgpt.com/backend-api/codex/responses` — the **Responses API**, NOT chat/completions: input items use content type `input_text`, `store=false` mandatory, `instructions` required, stateless (full history every request); headers `Authorization: Bearer`, `ChatGPT-Account-Id`, `originator`, `session_id` (UUIDv7, also as `prompt_cache_key`)
- Models endpoint: `GET /models?client_version=...` → `{"models": [{"slug": ...}]}` (non-standard schema)

**Design:**
- New `one/core/oauth.py`: PKCE (S256), per-provider flow runners, `ensure_fresh_token()` (margin-based proactive + single reactive retry), token record persistence in `auth.json` (generic `get_oauth_record`/`set_oauth_record` in AuthStorage; file stays 0600-equivalent plaintext like today).
- `AuthStorage.get_api_key()` extended: OAuth record's live access token counts as the provider key (after `ensure_fresh_token`) so `get_api_key_and_headers` keeps working unchanged.
- Anthropic adapter: token starting with `sk-ant-oat01` → Bearer auth + OAuth beta headers instead of `x-api-key`.
- NEW provider `chatgpt` + adapter `one/providers/codex_responses.py`: Responses-API wire (messages→input items, instructions, store=false, SSE delta parsing), non-standard models listing, required headers.
- `/login <provider>` without a key → interactive choice: `[1] paste API key  [2] log in with browser (subscription)` for oauth-capable providers; help texts updated in both modes.
- Builtins: seed `chatgpt` provider models (gpt-5.x-codex slugs) so `/providers` works pre-login.

**Sub-tasks (each independently shippable):**
- [x] **Task 18.1:** OAuth core — PKCE, token records in AuthStorage, `ensure_fresh_token` with refresh grants; unit tests with mocked httpx.
- [x] **Task 18.2:** Claude Pro login (paste flow) + Anthropic adapter OAuth mode + model fetch; e2e test with stubbed endpoints.
- [x] **Task 18.3:** ChatGPT login — localhost:1455 callback server (threaded http.server), JWT claim extraction; tests with fake server.
- [x] **Task 18.4:** `codex_responses.py` adapter (Responses wire, SSE, models-by-slug) + provider registration + builtins.
- [x] **Task 18.5:** `/login` UX (key-vs-browser choice) in interactive+TUI, help texts, full suite green.

### Phase 18 HOTFIX: chatgpt token exchange fails ("token response missing access_token") — DONE

**Symptom (live test):** `/login chatgpt subscription` — browser flow OK (callback + state validated), token exchange returns 200 JSON without `access_token`.

**Root-cause research (opencode source, Aug 2026):** compared our exchange with opencode's working implementation (`anomalyco/opencode`, `packages/core/src/plugin/provider/openai.ts`; older reference in issue #3281). Differences found:

| aspect | opencode (works) | ours (fails) |
|---|---|---|
| `state` in token body | NOT sent | sent ← prime suspect |
| User-Agent | `opencode/<version>` | default `python-httpx/...` |
| error diagnostics | status surfaced | no body in message |
| authorize `originator` | `originator=opencode` | `originator=codex_cli_rs` (authorize worked anyway; leave) |
| expires | `expires_in ?? 3600` | same semantics ✓ |
| accountId chain | id_token→access: top-level → nested auth claim → orgs[0].id | identical ✓ |
| refresh body | grant_type/refresh_token/client_id only | identical ✓ |

**Fix plan (exact edits):**

1. `one/core/oauth.py::exchange_authorization_code` — remove `state` param from signature AND body (send only grant_type/code/client_id/redirect_uri/code_verifier); comment why (matches opencode/codex-rs).
2. `one/core/oauth.py::_token_request` — add `headers={"User-Agent": _user_agent()}` on the POST (`_user_agent()` = `one/{importlib.metadata.version('one')}` fallback `one/0.0.0`); wrap `resp.json()` in try/except (non-dict/None → treat as missing access_token); enrich BOTH errors with `_describe_response(resp)` = `"HTTP {status}: {body[:400] or '<empty body>'}"`; when JSON dict has `error_description`/`error`, append it to the missing-access_token message.
3. Callers of `exchange_authorization_code` (`run_paste_flow`, `run_loopback_flow`) — drop `state=` kwarg.
4. Tests (`tests/test_oauth.py`):
   - `test_anthropic_token_request_uses_json_body`: remove `state="S"` kwarg + `body["state"] == "S"` assertion.
   - `test_run_paste_flow_code_state` / `..._bare_code_...`: fake_exchange drops `state` param; assert code+verifier only (no state assertions).
   - NEW `test_token_request_missing_access_token_includes_diagnostics`: 200 response `{"error":"invalid_grant","error_description":"code expired"}` → OAuthError message contains "invalid_grant", "code expired", "HTTP 200".
   - NEW: assert chatgpt exchange body has exactly {grant_type, code, client_id, redirect_uri, code_verifier}.
5. Run `.venv/bin/python -m pytest tests/test_oauth.py -q` then full suite; commit as `fix(oauth): align chatgpt token exchange with opencode (drop state, UA header, diagnostics)`.

**Follow-up note (chat runtime, not login):** opencode issue #3281 states the Codex backend requires a specific system prompt starting "You are Codex, based on GPT-5..." for OAuth requests to be accepted — if `/model chatgpt/...` requests get rejected post-login, prepend that instruction prefix in `CodexResponsesAdapter._build_payload`.

**As fixed (487 tests):** state dropped from the token body (exact opencode shape: grant_type/code/client_id/redirect_uri/code_verifier); UA header `one/<version>` on token requests; both token-endpoint errors now carry `HTTP <status>: <body[:400]>` plus `error_description`/`error` when present; non-JSON 2xx bodies handled cleanly; 4 test updates + 3 new tests (`missing_access_token_includes_diagnostics`, `non_json_body_is_handled`, `chatgpt_exchange_body_exact_shape`).

### Phase 18 HOTFIX-6: real 400 reason visible — wrong model slug + refresh drops account headers — FIXED

**Live evidence (HOTFIX-5 diagnostics worked):**
`chatgpt API error 400: {"detail":"The 'gpt-5.3-codex' model is not supported when using Codex with a ChatGPT account."}`
→ wire format now ACCEPTED; only the builtin seed slug is not in the account's allow-list. Also `/login refresh chatgpt` prints "no models endpoint" because headers are dropped on that path.

**Diagnosis:**
1. Builtin seed `gpt-5.3-codex` (model_registry.py:47) is an invented slug; real ChatGPT-Codex slugs are `gpt-5.1-codex-max`, `gpt-5.1-codex`, `gpt-5.1-codex-mini`.
2. `/login refresh <provider>` gets `key_info["headers"]` from `get_api_key_and_headers` but calls `validate_and_fetch(adapter, api_key, provider)` WITHOUT them → `_fetch_detailed` hits the models endpoint without `ChatGPT-Account-Id` → empty/shape-mismatched response silently becomes "no models endpoint".
3. `list_models_detailed` returns `[]` silently when the JSON lacks the `"models"` key — shape mismatch must be a loud error.

**Applied fix:**
1. `one/core/provider_login.py`: `validate_and_fetch` gains `headers: dict[str, str] | None = None`; passes it into BOTH `_fetch_detailed(adapter, key, headers)` calls and the probe `adapter.chat(api_key, chat_model, [...], "off", max_tokens=1, headers=headers)`.
2. `one/modes/tui_mode.py` (~1218) and `one/modes/interactive_mode.py` (~803): `validate_and_fetch(adapter, api_key, provider, headers=key_info.get("headers") or None)`.
3. `one/providers/codex_responses.py` `list_models_detailed`: after `data = resp.json()` raises `RuntimeError(f"chatgpt models endpoint returned unexpected shape: {str(data)[:400]}")` when `not isinstance(data, dict) or "models" not in data`.
4. `one/core/model_registry.py` line ~47: replaced single `gpt-5.3-codex` with `gpt-5.1-codex-max` + `gpt-5.1-codex` (both `reasoning=True, context_window=400_000`).
5. `tests/test_oauth.py`: 5 new tests — `test_validate_and_fetch_forwards_headers`, `test_validate_and_fetch_headers_none_by_default`, `test_validate_and_fetch_codex_validation_error_with_headers`, `test_codex_list_models_detailed_raises_on_shape_mismatch`, `test_model_registry_codex_seed_slugs`.
6. Full test suite: **496 passed** (491 + 5 new).

**Then:** `/login refresh chatgpt` should either list real slugs or print the exact HTTP reason; chat with `gpt-5.1-codex-max` expected to work.

### Phase 18 HOTFIX-5: chat 400 + missing Codex models — FIXED

**Live status before fix:** login stored OK; only builtin seed model listed; chat → bare `400 Bad Request` ×3 retries.

**Research findings applied (openai/codex discussion #7296 + LiteLLM-style handlers):**
1. `input` items now carry `"type": "message"` (backend requires it).
2. `instructions` fallback `_BASE_INSTRUCTIONS` ("You are Codex, based on GPT-5...") when the session has no system message — empty instructions rejected.
3. HTTP errors now carry the response body: `_error_with_body()` used in both stream and non-stream paths (`resp.text` / `await resp.aread()`) — next failure shows OpenAI's actual reason.
4. Login-time model fetch passes `ChatGPT-Account-Id` from the stored record (`_fetch_detailed(adapter, key, headers)` passthrough in `provider_login.py`) — root cause of "No models fetched".

**Tests:** payload asserts `type: message`; new `test_codex_payload_instructions_fallback`, `test_run_oauth_login_passes_account_id_headers`; stream fake got `is_error`/`aread`. Suite green: **491 passed**.

**Next:** user retries chat (works, or shows real server reason) and `/login refresh chatgpt` / re-login for the model list.

### Phase 18 HOTFIX-4: `ModelRegistry.set_oauth_record` delegation — FIXED (OAuth login end-to-end working)

**Live evidence (HOTFIX-3 worked — token exchange OK):** traceback showed a fully populated record in `run_oauth_login` locals (`type/access/refresh/expires/accountId` with real JWT + accountId) then:
`AttributeError: 'ModelRegistry' object has no attribute 'set_oauth_record'`

**Root cause:** `set_oauth_record` lives on **AuthStorage**; `run_oauth_login(provider, adapter, model_registry)` calls it on the registry. Sessions expose only `model_registry` (no auth_storage attribute on AgentSession), and ModelRegistry already delegated OAuth *reads* via its private `self._auth` — the write delegation was missed.

**Applied fix:**
1. `one/core/model_registry.py`: added `set_oauth_record(provider, record)` delegating to `self._auth.set_oauth_record`.
2. `tests/test_oauth.py`: regression test — registry write lands in AuthStorage; `has_configured_auth` True; `get_api_key_and_headers` returns access token + `ChatGPT-Account-Id`.
3. Suite green: **489 passed**.

**Status:** full `/login <provider> subscription` path works end-to-end (verified live up to persist). Remaining follow-up unchanged: Codex backend may require the Codex system-prompt prefix for chat requests.

### Phase 18 HOTFIX-3: double conversion in `run_oauth_login` — FIXED & VERIFIED LIVE (token exchange succeeded)

**Live evidence (user reinstall worked — new diagnostics visible):**
`Subscription login failed: chatgpt token response missing access_token (received keys: ['access', 'accountId', 'expires', 'refresh', 'type'])`

The received keys are OUR RECORD shape (`access`/`refresh`/`expires`/`accountId`/`type`), NOT a token response (`access_token`...). Root cause: `run_paste_flow`/`run_loopback_flow` already return a normalized record via `build_oauth_record`, but `provider_login.run_oauth_login()` called `build_oauth_record(spec, token_resp)` a SECOND time on that record → `record.get("access_token")` is None → raises. Tests missed it because the fake `run_login` mock returned a token-response shape instead of the real record shape.

**Applied fix:**
1. `one/core/provider_login.py`: `record = await run_login(...)` directly (double conversion removed; unused `build_oauth_record` import dropped).
2. `tests/test_oauth.py`: `test_run_oauth_login_anthropic_paste` mock now returns the real RECORD shape; added regression test `test_run_oauth_login_does_not_double_convert` (record without `access_token` must be stored verbatim).
3. Suite green: **488 passed**.

**Next:** user retries `/login chatgpt subscription` — this was the last known blocker in the login path itself.

### Phase 18 HOTFIX-2: stale `~/.local` install — reinstall required after code changes — RESOLVED (user reinstalled editable; new diagnostics confirmed live code)

**Observation:** after the fix, the TUI still prints `chatgpt token response missing access_token` WITHOUT the new `(HTTP ...: ...)` suffix. The fixed `_token_request` ALWAYS appends diagnostics, so a process printing the bare message is running PRE-FIX code.

**CONFIRMED root cause:** `which -a one` resolves to `/home/picon/.local/bin/one` — a FROZEN user-site copy (`~/.local/lib/python3.x/site-packages/one`) snapshot from between commits `00a33a4` (feat OAuth) and `82e9019` (hotfix). It has the login feature but not the fix. Earlier changes "worked automatically" because testing went through `.venv/bin/one` / `python -m one.cli.main`, which always use live repo sources. A frozen `~/.local` copy updates ONLY on explicit `pip install --user`.

**⚠️ USER ACTION REQUIRED — reinstall `one` to the latest version after code changes:**

```bash
# 1) remove the stale frozen user-site copy
python3 -m pip uninstall -y one
rm -f ~/.local/bin/one

# 2) reinstall EDITABLY so bare `one` always tracks repo sources
python3 -m pip install --user -e /home/picon/workspace/one
hash -r

# 3) verify
which -a one        # should point at the editable install
grep -c _describe_response ~/.local/lib/python3*/site-packages/one/core/oauth.py   # >=1 means hotfix present
```

**Standing rule:** whenever code changes are pulled/committed and `one` is launched via `~/.local/bin/one`, re-run step 2 (or launch `.venv/bin/one`). Quick sanity check that the running build contains the hotfix: trigger an OAuth error — the message must contain `(HTTP <status>: ...)`.

**Pending code hardening (blocked by edit permissions):**
- `build_oauth_record`: second raiser of the same bare message — enrich with `(received keys: [...])`; test `build_oauth_record` error assertion updated to `match="received keys"`.

**After reinstall, retry `/login chatgpt subscription`.** If it still fails, the message will now contain `(HTTP <status>: <body>)` — paste it verbatim; that body names the real server-side reason.

- [ ] **User:** run the reinstall block above, then retry `/login chatgpt subscription`
- [x] **Dev:** `build_oauth_record` hardening done — error now lists `(received keys: [...])`; test asserts `match="received keys"` (487 tests green)


**Implementation notes (as built):**
- `one/core/oauth.py`: `OAuthFlowSpec` + `ANTHROPIC_OAUTH`/`CHATGPT_OAUTH`; `generate_pkce` (S256), `build_authorize_url`, `_token_request` (json for Anthropic, form for ChatGPT), `exchange_authorization_code`/`refresh_access_token`, `jwt_payload` (no signature check), `extract_chatgpt_account_id` (per-token chain: top-level → nested auth claim → orgs[0]; id_token wins over access_token), `build_oauth_record` (`expires_in` → JWT `exp` → 1h fallback; ms epoch), `ensure_fresh_token(auth, provider, margin_ms=5min)` (no-op without spec/record; raises when expired + no refresh token), `run_paste_flow` (state=verifier fallback for bare CODE), `run_loopback_flow` (threaded HTTPServer on 127.0.0.1:<port>, state validation, provider-error surfacing, timeout 300s), `run_login` dispatcher.
- AuthStorage: generic `get/set/remove_oauth_record`; ModelRegistry: OAuth record counts as configured auth, `get_api_key_and_headers` falls back to access token (+ injects `ChatGPT-Account-Id` header from record; explicit API key still wins), async `ensure_oauth_fresh(provider)` called in `agent_session._request...` before credential resolution (getattr-guarded for fake registries; OAuthError → RuntimeError).
- Anthropic adapter: `sk-ant-oat*` tokens → `Authorization: Bearer` + `anthropic-beta: oauth-2025-04-20` instead of `x-api-key`; `/v1/models` fetch ONLY for OAuth tokens (None for API keys → minimal-chat validation path unchanged).
- `chatgpt` provider = `CodexResponsesAdapter`: Responses wire (`input_text` items, `instructions`, `store=false`, `max_output_tokens`, reasoning effort map xhigh→high/off→omit), SSE delta parsing (`response.output_text.delta` / `response.completed` / `response.failed`), models via non-standard `{models:[{slug}]}` endpoint with `client_version` param; headers originator/session_id/Bearer. Builtin seed: `gpt-5.3-codex`.
- `/login <provider> subscription|oauth` runs the flow in interactive+TUI; interactive additionally offers a `[1] Subscription [2] API key` menu when no key argument given and the provider is oauth-capable. Shared orchestration: `provider_login.run_oauth_login()` (flow → build_oauth_record → persist → best-effort model fetch/register).
- 33 new tests in `tests/test_oauth.py` (mocked httpx everywhere; loopback tests use a real local server hit via urllib).

**Files:** `one/core/oauth.py` (new), `one/core/auth_storage.py`, `one/providers/anthropic.py`, `one/providers/codex_responses.py` (new), `one/providers/registry.py`, `one/core/model_registry.py` (builtins), `one/modes/interactive_mode.py`, `one/modes/tui_mode.py`, tests.

**Estimated effort:** Tasks 18.1-18.2 ≈ one session; 18.3-18.5 ≈ another.

**Confidence:** Medium-High (flows verified against multiple independent implementations; but undocumented → breakage risk)

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
| Textual markup parser stricter than Rich (any `[` = tag start) | TUI crash | Certain (reported) | Phase 8: render via `rich.text.Text` objects (literal append), never raw markup strings |
| Diff parser edge cases (malformed hunks, CRLF, no trailing newline) | Wrong file content | Medium | Strict validation before any write; all-or-nothing semantics; tests cover malformed input |
| Model nests `path` inside `edits[0]` (observed with local models) | Repeated edit failures, wasted steps | High (reported) | Phase 10: recover nested `path`; clear error naming the cause; prompt schema states TOP-LEVEL |
| `/login` validation hits a provider without a models endpoint (Anthropic) | Validation gap | Medium | Minimal-chat fallback (max_tokens=1); if no model registered, skip validation with a note |
| `models.json` merge corrupts existing per-model fields (`url`/`toolParser`) | Broken local config | Low | Merge keeps existing entries untouched; only new ids are added; unit tests assert field preservation |
| Fetched model list is huge (ollama-cloud ~19 models) | Noisy output / big models.json | Low | Print full list (small); registry dedupes; persistence is a merge, not a replace |
| `one run` flag fix mis-pairs a task word that looks like a flag value | Wrong task text | Low | Pairing only for known value-taking flags; unit tests cover flags before/after task |
| Anthropic minimal-chat validation costs tokens | Cost | Very low | max_tokens=1, single message; only on explicit `/login` |

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
- [x] Tool output / plan text with unescaped `[` (e.g. `[type='CNAME']`, `[1,2,3]`, `[ABC]`, `[{"plan": ">", "x": 1}]`) renders in the TUI stream without MarkupError (Phase 8).
- [x] Sidebar renders plan text with such chars without MarkupError (Phase 8).
- [x] Full test suite green: `.venv/bin/python -m pytest -q` (355 tests).
- [x] `apply_patch` tool callable by the model (`{patchText}`); Add/Update/Delete/Move + multi-file patches work; validation aborts before any write (Phase 9).
- [x] Cooperation mode asks for approval before executing `apply_patch` (default `approvalTools` includes it) (Phase 9).
- [x] Full test suite green: `.venv/bin/python -m pytest -q` (393 tests) (Phase 9).
- [x] `edit` succeeds when the model nests `path` inside `edits[0]`; missing `path` everywhere → clear error naming the top-level requirement; `edits` as dict → clear error (Phase 10).
- [x] Prompt schema for `edit` states `path` is TOP-LEVEL (never inside edits) (Phase 10).
- [x] Full test suite green: `.venv/bin/python -m pytest -q` (396 tests) (Phase 10).
- [x] `/login <provider> <key>` validates the key BEFORE storing; invalid key → `Authorization failed`, nothing stored (Phase 11).
- [x] `/login` fetches the provider model list, registers it in-memory and persists it to `models.json`; `--list-models` shows fetched models afterwards (Phase 11).
- [x] NO_AUTH providers (`llama.cpp`, `ollama`) fetch their local model list via `/login` without a key (Phase 11).
- [x] Full test suite green: `.venv/bin/python -m pytest -q` (426 tests) (Phase 11).
- [x] `one run <task> --provider X --model Y` works (no `expected one argument` error); `parse_args` unit tests cover flags before/after task (Phase 12).
- [x] Full test suite green: `.venv/bin/python -m pytest -q` (426 tests) (Phase 12).
- [x] `/providers` lists only logged-in providers (numbered, `*` on current); second step lists a provider's models; third form switches model + persists defaults; NO_AUTH providers always listed (Phase 13).
- [x] Full test suite green: `.venv/bin/python -m pytest -q` (435 tests) (Phase 13).
- [x] `/providers` is offered by TUI autocompletion (`_SLASH_COMMANDS`) and present in the keybinding help line (Phase 14).
- [x] `/login refresh <provider>` re-fetches the live model list with the stored key, registers + persists it; NO_AUTH works without key; missing key → hint; 401/403 → registry unchanged (Phase 15).
- [x] Full test suite green: `.venv/bin/python -m pytest -q` (444 tests) (Phases 14-15).
- [x] Every provider request's system message ends with `# Current Date` (date, weekday, HH:MM, tz abbrev, UTC offset), rebuilt each step (Phase 16).
- [x] Full test suite green: `.venv/bin/python -m pytest -q` (445 tests) (Phase 16).
- [x] Ctx gauge works for every model (128k fallback applied in `__init__` and `set_model`); OpenRouter/Gemini context lengths fetched at `/login`, persisted to models.json, enriched on refresh; providers without the field fall back at runtime (Phase 17).
- [x] Full test suite green: `.venv/bin/python -m pytest -q` (451 tests) (Phase 17).

## Estimated Timeline

- Phase 1: ~2 h (single implementer).
- Phase 2: ~1.5 h (single implementer).
- Phase 3 (follow-ups): ~1.5 h (sidebar MCP section + Keys shortcuts + prompt queueing + AGENTS.md).
- Total: ~5 h; uncertainty low — all phases were small, well-scoped changes in ~6 source files + tests. All work is committed.
- Phase 5: ~3 h (plan tool: core + session + persistence + approval + TUI + tests + AGENTS.md).
- Total with Phase 5: ~8 h; Phase 5 uncertainty low — all integration points identified (dispatch branch, system-prompt builder, finish branch, settings defaults, TUI event/sidebar).
- Phase 9 (apply_patch tool): ~2.5 h; uncertainty low — reference semantics verified from opencode source; registration points known (index.py, resource_loader.py, settings_manager.py, AGENTS.md).
- Total with Phase 9: ~10.5 h; Phase 9 delivered in `0ee1cd5` (10 files, +896/-11; 38 new tests; full suite 393 green).
- Phase 10 (edit tool robustness): ~1 h; uncertainty low — root cause fully identified from the reported call + code paths (agent_session.py:525/533-534, edit.py:24-26, resource_loader.py:278).
- Total with Phase 10: ~11.5 h; Phase 10 delivered in `3393173` (5 files, +114/-5; 3 new tests; full suite 396 green; reported malformed edit shape verified working end-to-end).
- Phase 11 (`/login` validation + remote model fetch): done — took longer than planned (~5 h incl. the public-`/models` discovery: openrouter + ollama-cloud return 200 for bad keys, so chat-based key proof (max_tokens=1) validates all keyed providers; Anthropic has no list endpoint → minimal-chat fallback).
- Phase 12 (`one run` value-flag fix): done — quick, pure parsing fix in args.py; `--provider`/`--model`/etc. now keep their values before/after the task text.
- Total with Phases 11-12: ~15.5 h (phases 1-10) + ~6 h (phases 11-12).