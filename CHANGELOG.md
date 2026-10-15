# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
pre-1.0 (breaking changes may occur in 0.x releases).

## Unreleased

### Fixed

- **Retry recovery** — enabled retries now use a finite default, unlimited mode
  continues past that cap, observes live retry-mode changes, and keeps backoff
  arithmetic bounded.
- **Provider recovery and TUI feedback** — repair-request timeouts can recover
  without replaying completed tools; the TUI keeps its footer spinner active
  while hidden/native tool arguments stream or tools execute.
- **TUI completed tool turns** — native tool-call carrier messages no longer
  leave `[empty]` / `[empty response]` panels before a successful finish summary.

## [0.1.81] - 2026-10-14

### Added

- **Theme-aware CommandPalette (Ctrl+P) and model overlay (Ctrl+K)** — every built-in
  TUI palette is now registered as a Textual ``Theme`` so native widgets (CommandPalette,
  Input, OptionList, etc.) pick up ``$foreground``, ``$background``, ``$surface``,
  ``$panel``, ``$border``, and ``$block-cursor-*`` colours immediately via
  ``app.set_theme()``. The model picker overlay is styled dynamically in
  ``_apply_theme`` and model/provider strings are sanitised with ``rich_escape``
  to prevent markup injection.

- **Detached TUI plan projection** — active structured plans now cross the
  runtime facade with explicit presence and clear state, so seven-step plans
  survive streaming, reloads, and the sidebar without treating omitted data as
  a clear.

- **TUI footer inside conversation border** — the spinner/abort footer (`#waiting_status`)
  now lives inside a shared `#conversation` wrapper with the scrollable transcript.
  The wrapper carries the panel border, background, and theme; the inner scroll body
  has no border. The footer row is reserved even when idle to avoid relayout, stays
  fixed while browsing the transcript, and displays an animated block-shade glyph
  followed by `Ctrl+C — abort`. Theme changes apply to both wrapper and footer.

- **Bounded TUI result viewer** — durable tool results expose a trusted `[view]`
  action. Pages are fetched on the runtime worker, show only sanitized result
  data, and are rejected after a session change; very large results are read
  with PageUp/PageDown rather than copied into the transcript.

- **Live subagent monitor** — the TUI now provides `/agents`, a read-only view
  of the current session's direct child agents with a selected bounded live
   response and single-row tool lifecycle preview, with safe redacted operation summaries
   and safe result/error text only while
   the existing details toggle is enabled. Generated inline **[view]** links on
   `spawn_subagent` tool calls open the corresponding preview; `/subagents on|off` remains the enable/disable control
  for subagent tools.

### Changed

- **TUI transcript renderer** — the bounded stream now uses a virtual,
  line-at-a-time Textual renderer with stable line IDs and demand-cached wrapped
  rows instead of rebuilding an aggregate `Content` visual for each update.

- **Sidebar subagent status** — `Subagents: on` and `Agents: N running` are
  consolidated into a single `Subagents: on (N running)` line; the `/agents`
  command is unchanged.

- **TUI responsiveness validation** — the offline profiler now exercises the
   detached `TuiSessionView`/runtime-facade route under bounded delta floods,
   child invalidations, slow worker I/O/Git stubs, and CPU contention. It reports
   measured UI latency and explicit gate status without recording content,
   including paint/render p99 and count plus the maximum measured event-loop
   block across input, paint, drain, and sidebar paths.
- **TUI runtime ownership** — explicit `--mode tui` startup now constructs
  settings, providers, resources, MCP, and session state on the dedicated
  runtime loop instead of the Textual loop; CLI image paths are imported by
  that owner when sent with the first TUI prompt.
- **Bash prompt guidance** — system instructions now use the actual `exitCode`
  result field, distinguish successful process exit from task completion, and
  advise diagnostic retries only for changed commands that can address a
  recoverable cause.
- **Subagent delegation guidance** — prompts and native tool metadata tell
  smaller/less capable models to prefer one `task`, reserving parallel `tasks`
  for explicitly requested independent subtasks.
- **Read tool guidance** — prompts and native tool metadata now explain its path,
  line-offset, and line-limit arguments, bounded output, and image handling.

### Fixed

- **TUI transcript selection** — virtual cached rows now apply Textual selection
  at paint time with physical-row character offsets, preserving CJK, emoji,
  combining characters, and whitespace without leaking a selection into shared
  row cache entries.
- **TUI completed responses** — the detached runtime carries the canonical
  completed assistant text separately from the bounded live preview, so a
  completed answer replaces a streaming preview rather than retaining its
  truncation marker.
- **TUI context projection** — provider and tool completion coalesce an
  owner-loop context/stat refresh during a turn; the sidebar no longer waits
  solely for the final turn event to update.

- **TUI runtime replacement** — new, switched, and forked sessions retain the
  worker-owned MCP, auth, tool, and resource configuration.

- **Sustained TUI tool runs** — waiting animation now updates a dedicated status
  widget instead of rebuilding the transcript. Transcript line fragments are
  bounded by retained stable IDs and evidence actions use constant-time line
  lookup, avoiding repeated sequence matching during normal paints.

- **Invalid tool-call recovery** — explicit malformed text and rejected native
  tool batches now receive one bounded repair attempt. Persistent invalid calls
  end as a visible failed turn rather than a successful raw-JSON answer; detached
  TUI diagnostics retain their safe source/category/reason metadata.

- **TUI evidence overlay theming** — evidence viewer (header, body, content,
  actions, and Previous/Next/Close buttons) now follows the selected theme
  via `_apply_theme`; changes apply immediately while the viewer is open
  without raw content leakage.

- **TUI approval summary displayArgs** — detached runtime `tui_approval_request`
  events now propagate sanitized `displayArgs` (bounded to 512 characters) into
  the pending approval dict so `_approval_summary` shows the actual tool
  arguments instead of `{}`.

- **TUI evidence and model paging** — production runtime command allowlisting now
  permits bounded evidence and model pages. The viewer distinguishes a rejected
  runtime request, stale session view, and genuinely unavailable durable result
  without exposing runtime error details.
- **Flat JSON tool calls** — conservative normalization now accepts unambiguous
  flat `bash`, `spawn_subagent`, and `finish` arguments while rejecting conflicts
  with nested arguments and preserving bounded malformed-call repair.

- **TUI tool results and evidence actions** — result previews now default to 100
  logical lines, preserve safe command/task summaries, and generated full-result
  links remain valid across completion, later turns, reflow, and transcript trim.
- **TUI MCP sidebar** — detached runtime snapshots now carry normalized MCP
  connection state so configured, failed, retrying, and running servers render
  correctly.

- **TUI assistant response retained** — when the next message starts before a
  scheduled final paint, the assistant response is preserved and runtime events
  are bridged so no output is dropped.

- **Detached TUI streaming** — the runtime host now bridges its current
  session's events into the facade and safely rebinds them after session
  changes, so streamed assistant, thinking, and tool activity remains visible.
- **Default TUI mode startup** — bare `one` now honors the configured
  `defaultMode` when starting through the detached TUI runtime.
- **Large TUI tool results** — tool-result previews no longer exceed the 64 KiB
  UI ingress limit because duplicated output fields are removed.
- **MCP TUI rendering** — sidebar and shutdown status rendering is now
  normalized and safe.
- **TUI main transcript scrolling** — incoming assistant, tool, and subagent
  activity now follows output only when the viewer is already at the tail. Tail
  follow is coalesced and respects later wheel/scrollbar input; retained history
   stays anchored across viewport trimming where possible.
- **Live assistant paint stalls** — active assistant presentation is now capped
  at 8 KiB before wrapping/formatting (while full content remains in session
  history and evidence), preventing a large in-progress response from causing
  an ordinary UI stall.
- **TUI responsiveness during filesystem inspection** — synchronous built-in
  `read`, `grep`, `find`, and `ls` bodies now run in a worker thread while the
  shared asyncio loop continues to process UI input, approvals, and events.
- **TUI cooperation approvals** — main and child tool approvals now share one
  fail-closed FIFO overlay with a labeled origin, safe operation summary, and
  dedicated rejection-reason field. Explicit buttons or Alt+A/Alt+R resolve
  approvals; ordinary prompt input never does.
- **Live subagent monitor layout** — `/agents` now renders the selected child
   header and chronological activity timeline inside one frame.
- **TUI overlays** — `/agents` and cooperation approval surfaces, including their
  action buttons, now follow the selected TUI theme.
- **Rejected tool-call diagnostics** — invalid text tool candidates now identify
  their response stage and safe parse category without exposing model content;
  the TUI explicitly confirms that no tool was executed.
- **Default skill startup** — silently preserve existing lower-precedence skills
  instead of warning on every startup.

- **TUI snapshot render-settlement** — snapshot capture now waits for pending
  stream/sidebar frames, revisions, viewport updates, and compositor flushes
  before capturing, eliminating flaky test diffs.

- **TUI completed-assistant scrollbar/reflow geometry race** — a committed
  wrap-width fix prevents the completed response provisional 1-row / scrollbar
  width oscillation and ensures geometry stabilises before copy/selection
  (up to 1800 rows).

## [0.1.64] - 2026-10-04

### Changed

- **Project roadmap** — `TODO.md` now lists only current, verified work;
  completed implementation details are available in Git history rather than a
  separate delivered-work ledger.

### Added

- **First-run default skills** — fresh installations copy the packaged `explore`
  and `one` skills into the active user agent directory without overwriting user
  files or lower-precedence existing skills.

- **Live subagent controls** — `/subagents on|off` now updates tool visibility
  in the active session. `--no-subagents` remains a hard disable that cannot be
  overridden by the runtime toggle.
- **Restricted TUI manual commands** — `/bash` now uses a fail-closed,
  shell-free strict/dev command profile. It is separate from model bash and
  RPC/interactive commands, which retain their existing behavior. Manual TUI
  commands and output remain local session history (and may persist sensitive
  output on disk); they are never sent to the model. Strict/dev is not an OS
  sandbox, while model bash results still enter model context.

- **Lifecycle elapsed timing** — durable, monotonic `timing` entries record whole
  turns, attempts, provider requests, and tool handling without entering the
  provider conversation context. `/stats` and RPC summarize current-branch
  timing by scope and outcome; the TUI displays completed tool durations and a
  single completed-turn timing summary.

- **Beginner documentation** — added a five-minute Quickstart, copy-and-paste
  prompt examples, and an FAQ/troubleshooting guide, linked from the README and
  included in source distributions.
- **Session statistics** — `/stats`, interactive mode, and RPC now report
  durable compaction counts/reasons and timings, session age, normal response
  timings, and measured provider output tokens per second when available.

- **Model temperature modes** — sessions default to balanced (`0.5`) and support
  coder (`0.2`), creative (`0.8`), and experimental (`1.2`) presets, plus
  numeric values from `0.0..1.2`. Temperature is available in the TUI, RPC,
  CLI, and isolated subagent overrides.

- **Subagent lifecycle timeouts** — subagents now use an activity-aware
  `subagents.idleTimeoutSec` watchdog (default 1800 seconds) plus optional
  `subagents.maxDurationSec` hard cap (default 0, disabled). Timed-out children
  retain their session and return bounded partial diagnostics rather than success.
- **Structured plans** — the `plan` tool now accepts validated step/status
  lists, persists them across sessions, and renders status-aware checklists.
- **TUI logo asset** — startup artwork now loads from the packaged
  `one/assets/logo.txt`, which is also linked from the main documentation.
- **Ollama Cloud image input** — `glm-5.3-flash:cloud` is registered as a
  vision-capable model and sends native Ollama raw-Base64 image payloads.

### Changed

- **TUI plan display** — the sidebar now keeps a focused three-step window
  around the active or next unfinished structured-plan item, while standalone
  plan updates render their full bounded plan panel without a `Tool:` label.
- **TUI tool panels** — lifecycle status remains under `Tool:`, while command
  and tool output now render under a distinct `Result:` heading; the sidebar
  also shows the attached Git branch beside its CWD when available.
- **Subagent timeout compatibility** — legacy `subagents.timeoutSec` remains an
  idle-timeout alias when `idleTimeoutSec` is not configured.
- **MCP disable persistence** — `/mcp disable <name>` now saves `enabled: false`,
  including unavailable servers with no live tools, until `/mcp enable <name>`.
- **TUI MCP availability markers** — enabled MCP servers in the sidebar now
  display `[ok]` when running normally and `[!]` when unavailable or retrying.
- **MCP recovery** — configured stdio servers can opt into delayed automatic
  restart after an unexpected exit or stdout EOF, while streamable HTTP servers
  now recover failed transport/protocol connections by default (`restart: false`
  opts out). Failed tools are removed from the live agent catalog immediately;
  retries, exhaustion policy, and runtime status are configurable without
  persisting a runtime disable. Interrupted tool calls are not replayed.
- **Unified TUI and interactive details rendering** — completed reasoning is
  styled as italic `Thought:` content, tool and MCP status lines report their
  actual effective timeout, and `/details-show` consistently controls detailed
  result bodies while preserving visible status lines.
- **README mode documentation** — reorganized onboarding and added an accurate
  mode/automation reference distinguishing TUI, one-shot text/JSON, JSON-RPC,
  and `one run`.
- **Complexity-aware planning policy** — plan-tool usage is limited to explicit
  or genuinely complex tasks; simple tasks skip planning, and approval prompts
  occur only when cooperation is enabled.

### Fixed

- **Subagent timeout isolation** — the parent's `tools.timeoutSec` no longer
  shortens a spawned child's idle timeout. Subagents use their own configured
  idle timeout, with an optional per-call `timeout` override.
- **Cooperative questions** — runtime prompts now accurately advertise `ask_user`
  only when cooperation and the tool are active, and distinguish a resumable
  question from terminal `finish` results.
- **Plan completion guard** — status-only plan updates no longer re-arm the
  post-plan finish guard after a tool step has executed.
- **Write argument safety** — write calls now require explicit string content,
  reject omission metadata, and keep source bodies out of ordinary model-context
  previews while retaining durable evidence.
- **Codex native calls** — Responses stream fragments now normalize each completed
  function-call item exactly once, preventing duplicate-call diagnostics.
- **Native tool batches** — distinct provider-native calls now execute sequentially
  with ordered replay results; mixed control-tool batches are safely rejected.
- **Native tool-call diagnostics** — malformed native calls are rejected without
  execution and shown as a concise safe TUI diagnostic while the agent performs
  its bounded repair path.
- **Cooperation-off human-input invariant** — model `ask_user` calls now proceed
  deterministically without creating a prompt or blocking when cooperation is
  disabled; pending model questions and TUI approvals are released on disable.
- **TUI lifecycle separators** — horizontal rules now start after a blank line,
  preventing assistant output from visually joining the separator.

- **Write tool parsing** — write-content bodies now preserve exact text safely;
  escaped bodies omitted from feedback no longer leak or corrupt parser state.
- **TUI session selection** — numbered `/sessions <n>` selections now resolve
  against the most recently displayed list, rather than a newly mtime-sorted
  scan; stale listed sessions direct users to rerun `/sessions`.
- **TUI session loading** — persisted aborted requests are now labeled as
  historical status after `/sessions` reload, while their durable session
  history is retained.
- **TUI session switching** — queued output from a replaced session is discarded
  before the loaded transcript is rendered.
- **ChatGPT/Codex compaction** — Responses API calls always use required SSE
  streaming, including non-live summarization, without emitting live TUI deltas.
- **Cooperation persistence** — TUI and interactive cooperation commands and
  shortcuts now save their approval setting for subsequent sessions.
- **Streaming provider timeouts** — `providers.timeoutSec` now measures idle
  time between meaningful content or reasoning tokens, allowing active streams
  to continue past the configured interval while retaining finite transport
  safeguards. Non-streaming requests retain their absolute deadline.
- **Reasoning-only first responses** — tool-enabled turns now make one bounded
  strict JSON-tool-call recovery request when a provider emits display-only
  reasoning with empty assistant content, without treating reasoning as actions.
- **Post-tool format repair** — each completed non-terminal tool now restores
  one bounded JSON-repair opportunity, allowing a later malformed response to
  recover into `finish` without bypassing tool-step, abort, or budget limits.
- **Malformed tool-output recovery** — strict JSON repair after tool outputs
  preserves untrusted provider-view boundaries, while OpenAI-compatible
  reasoning remains separate from parseable content.
- **TUI compaction output** — `/compact` now displays a readable status,
  summary, and counters instead of exposing the raw JSON result object.
- **llama.cpp thinking off** — `/thinking off` now passes llama.cpp's
  `chat_template_kwargs.enable_thinking=false` request control for compatible
  chat templates, preventing unwanted reasoning output where the server and
  model support that option.
- **Manual context compaction** — `/compact` now forces a summary below the
  automatic threshold; automatic and recovery compaction remain threshold-driven.
- **TUI slash completion focus** — Tab no longer moves focus out of the command
  input when slash completion has no match; slash completion remains functional.
- **Thinking levels and persistence** — `/thinking` and `/thinking-cycle` now
  save the selected level for new sessions while retaining each session's own
  recorded choice. OpenAI-compatible, Anthropic, Gemini, and Codex adapters
  now map supported levels to their native reasoning controls; `off` avoids
  enabled reasoning configuration and provider thought streams stay separate
  from visible answers.
- **Provider-native TUI thinking** — ChatGPT/Codex Responses summary deltas,
  OpenRouter reasoning traces, and Ollama Cloud native thinking now render
  separately from visible answers; OpenRouter and Ollama Cloud use their native
  reasoning request controls.

- **Docker diagnostic coverage** — special diagnostic scenarios now explicitly
  request their safe tools, disabled capabilities are reported separately, and
  a final idle wait truncated only by the outer deadline no longer fails an
  otherwise completed workload.
- **Diagnostic workload duration** — long Docker diagnostic runs now continue
  scheduling scenarios through the configured duration instead of stopping at a
  premature half-duration reserve.
- **SSH terminal paste after focus loss** — terminal text paste now restores
  TUI input focus after a terminal security confirmation clears it, while
  preserving exactly-once insertion and other widget focus ownership.
- **Bounded TUI transcript viewport** — long conversations now render only the
  latest 500 display lines while retaining complete session history. Stream
  refreshes coalesce equivalent output, and mouse-selection copying remains
  reliable during streaming and viewport trimming.
- **Tool/MCP evidence preservation** — provider requests retain bounded tool
  previews. Complete sanitized results are kept in durable session evidence and
  can be read by the model after reload through `evidence_read`; optional stale
  preview pruning remains available for smaller requests.
- **Responsive TUI streaming input** — one ordered UI accumulator now batches
  both ordinary and thinking deltas at a bounded cadence, flushing before
  lifecycle boundaries. This avoids token-rate transcript/sidebar rebuilds so
  keyboard input remains visibly editable and submittable while providers wait,
  reason, stream, or retry.
- **Steering checkpoints** — steering received while a tool is running is now
  delivered FIFO to the next provider request after its result, without waiting
  for the entire turn; terminal finish, abort, timeout, and error paths retain
  queued input for an explicit follow-up.
- **Skill invocation syntax** — the system prompt now explicitly states that
  `/skill:<name>` is a TUI/interactive UI command, never a `read` tool path.
  When the model passes `/skill:<name>` or `skill:<name>` to `read`, the agent
  returns a structured diagnostic with the matching skill's `filePath` instead
  of attempting filesystem access. Skills in the prompt now include their
  `filePath` in metadata for unambiguous autonomous loading.
- **Plan completion guard** — a `finish` immediately after creating a plan is
  rejected until the agent executes another tool step.
- **Reporting integrity** — the system prompt now prohibits claims of changes
  or verification without a successful observed tool result.
- **MCP context estimation compatibility** — context usage calculation now
  tolerates lightweight MCP test/client tool objects without an input schema.
- **Context compaction** — auto-compaction now uses the complete provider
  request estimate (runtime prompt and tool schemas included) and runs at the
  default 80% threshold before sending a request.
- **Terminal `finish`** — queued steering/follow-up messages are no longer
  executed automatically after the agent reports a completed task; they remain
  pending for an explicit next turn.
- **Provider stream hangs** — provider calls now have an outer deadline, SSE
  streams stop on terminal events, and stalled streams fail closed instead of
  leaving the TUI stuck on `Thinking:`. Spawned subagents also respect tool
  timeouts.
- **Clean TUI shutdown** — quitting with Ctrl+Q no longer prints
  `BaseSubprocessTransport ... Event loop is closed`: MCP stdio servers are
  fully closed (stdin/pipes/drain task), the session aborts first, and bash
  processes are reaped on cancellation.

### Added

- **TUI input history** — local history now combines prompts and slash commands,
  retains the latest 50 entries, restores multiline input, and supports
  Ctrl+Up/Ctrl+Down navigation.
- **TUI session browser** — `/sessions` lists current-project sessions and loads
  by number or unique full exact name; it supports validated rename and
  confirmation-protected deletion of a session JSONL plus its evidence sidecar.
  New sessions receive a locally derived, normalized first-prompt title (64
  Unicode characters maximum).

- **Docker diagnostic soak telemetry** — bounded container CPU, memory, PID,
  network/block-I/O and disposable workspace/session-size samples are included
  in diagnostic reports, with unavailable stats reported explicitly.

- **llama.cpp configuration guidance** — document remote/custom
  `models.json` entries, context-window alignment, tool parser order, endpoint
  verification, and safe host configuration.

- **Subagent timeout diagnostics** — `inspect_subagent_timeout()` returns the last
  subagent timeout diagnostic (cause, duration, timeoutSec, task summary); exposed
  via `/inspect-timeout` in TUI and RPC, and logged in interactive mode.
- **Streaming delivery modes** — TUI dispatches queued steer/follow-up messages
  using `deliveryMode` (`followUp` vs `steer` vs `idle`), matching the
  `deliveryMode` event field emitted by the session.
- **14 tools** (was 12): added `read_image` for vision/image input and
  `evidence_read` for bounded retrieval of durable tool evidence.
- **Multimodal image input** — `--image <path>` CLI flag and `read_image` tool support PNG, JPEG, WebP
  (up to 4 images per prompt, 10 MB source / 5 MiB Base64). Images are transient per-turn.
- **Codex image input** — the ChatGPT/Codex Responses adapter sends `input_image`
  parts for vision-capable models (`gpt-5.6-sol/terra/luna`); invalid attachments
  are rejected before any HTTP request.
- **Retry mode `unlimited`** — third mode next to `off`/`on` (`/retry <on|off|unlimited>`,
  `/retry-cycle`, Ctrl+R in TUI cycles `off` → `on` → `unlimited`).
- **`maxSteps=0`** — `tools.maxSteps` set to `0` means unlimited tool steps per turn
  (abort, budget limits, `finish`, and normal completion still end the turn).
- **TUI additions** — version line in the sidebar and `one v<version>` CLI banner;
  effective timeout in tool lines (`tool start (timeout Ns): bash {...}`);
  shortcuts overlay entries for SSH-safe text/image paste shortcuts and Ctrl+R;
  cooperation toggle moved from Ctrl+A to Ctrl+Z in the TUI.
- **TUI reliability fixes** — no more duplicated assistant/tool output across
  streamed retries and history trimming; single-insert paste; working waiting
  spinner; backspace/delete/arrows fire once.
- **SSH-safe TUI paste shortcuts** — Ctrl+V retains host/system clipboard text;
  Ctrl+Shift+V accepts terminal bracketed text paste without remote clipboard
  binaries; image paste moved to Ctrl+Alt+V, with `/paste-image` as a fallback.
- **`/login` help** — all help outputs show the `[subscription]` argument.
- **Skills system** — modular, discoverable instruction sets via `SKILL.md` files
  with YAML frontmatter (name, description, optional license/compatibility/metadata/allowed-tools/disable-model-invocation).
  Skills are listed in the system prompt (metadata only) and loaded on demand via
  `/skill:<name>` (TUI/interactive) or `invoke_skill` ctype (RPC). Supports progressive
  disclosure, trust warnings, cooperation approval gates, and `/reload` for resource
  refresh. Includes `docs/SKILLS.md` with full specification.

### Changed

- **Default settings** — new configurations now use the `hacker` theme, hide
  bash output by default, allow 100 tool steps, and make no automatic retry
  attempts (`retry.maxRetries=0`). Existing explicit global and project
  settings remain unchanged.
- **Compaction threshold** — new configurations now default to 80% context
  usage; existing saved `thresholdPercent` values are preserved.
- **Provider timeout** — increased the default outer provider and HTTP
  transport timeout from 190/180 seconds to 300 seconds; the 120-second SSE
  idle watchdog remains active for stalled streams.
- **README** — added "Resources and skills" section documenting skill discovery,
  `/skill:name` invocation, `/reload`, project vs global skill directories,
  and trust warning; links to `docs/SKILLS.md`.
- Auth precedence wording corrected to **runtime → stored → env** in README and docs.
- SECURITY.md scoped atomic-storage claims to text config/session persistence (blob store
  uses its own 0600/0700 handling).
- Security reporting contact set to s.wielgosz@noxgle.com.

## [0.1.0] — 2026-08-30

### Added

- **Autonomous terminal agent** with 4 modes: headless (`run`), text, JSON-RPC, and Textual TUI.
- **12 tools**: read, bash, edit, write, grep, find, ls, finish, plan, spawn_subagent, ask_user, apply_patch.
- **Cooperation mode** (`--cooperation`, Ctrl+A in TUI) — approval gates for mutating tools.
- **Provider adapters** for OpenAI-compatible, Anthropic, Gemini, and Codex Responses API (ChatGPT subscription).
- **Subscription OAuth login** for Anthropic (Claude Pro/Max) and ChatGPT/Codex (Plus/Pro).
- **MCP server support** — stdio and streamable-HTTP transports; tools exposed live to the agent.
- **Project extensions** (`.one/extensions/*.py`) with hook contract inspired by opencode.
- **Session management** — `.jsonl` format, compaction, branching, navigation.
- **Thinking/reasoning streaming** — raw reasoning deltas rendered chronologically with tool boundaries.
- **Bash timeout semantics** — model-controlled per-command timeout with clear "Command timed out" results.
- **Plan tool** — persistent execution plan injected into the system prompt every step, persisted in session jsonl.
- **apply_patch tool** — unified-diff patches with collision-safe, rollback-aware staging.
- **TUI features** — full-height sidebar, live streaming, themes, MCP panel, thinking visualization, slash command history.
- **SDK** — programmatic agent session creation with `agentDir` isolation.
- **Atomic, private persistence** — `0700`/`0600` file modes, atomic writes via `os.replace`.
- **Complete credential removal** (`/logout`) — removes API keys and OAuth tokens locally.
- **RPC login validation** — validates credentials before storage; consistent with TUI/interactive.

### Changed

- All user-facing text, help, and documentation in English.
- `one run` flag parsing fixed — all value-taking flags retain their values.
- Edit tool robustness — tolerates model-side `path` nesting inside `edits[0]`.

### Security

- Trust boundary documented: extensions and MCP servers are not sandboxed.
- Private LAN IPs, stale claims, and sensitive paths removed from documentation.
- Token redaction in error messages and RPC responses.
