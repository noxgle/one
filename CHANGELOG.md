# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
pre-1.0 (breaking changes may occur in 0.x releases).

## Unreleased

### Changed

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
