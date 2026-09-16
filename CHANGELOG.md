# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
pre-1.0 (breaking changes may occur in 0.x releases).

## Unreleased

### Fixed

- **Responsive TUI streaming input** — rapid provider deltas are coalesced
  into bounded UI renders, so keyboard input remains editable and submittable
  while output streams without an unbounded Textual message backlog.
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

- **Subagent timeout diagnostics** — `inspect_subagent_timeout()` returns the last
  subagent timeout diagnostic (cause, duration, timeoutSec, task summary); exposed
  via `/inspect-timeout` in TUI and RPC, and logged in interactive mode.
- **Streaming delivery modes** — TUI dispatches queued steer/follow-up messages
  using `deliveryMode` (`followUp` vs `steer` vs `idle`), matching the
  `deliveryMode` event field emitted by the session.
- **13 tools** (was 12): added `read_image` for vision / image input alongside the existing tool set.
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
  shortcuts overlay entries for Ctrl+Shift+V (paste image) and Ctrl+R;
  cooperation toggle moved from Ctrl+A to Ctrl+Z in the TUI.
- **TUI reliability fixes** — no more duplicated assistant/tool output across
  streamed retries and history trimming; single-insert paste; working waiting
  spinner; backspace/delete/arrows fire once.
- **`/login` help** — all help outputs show the `[subscription]` argument.
- **Skills system** — modular, discoverable instruction sets via `.SKILL.md` files
  with YAML frontmatter (name, description, optional license/compatibility/metadata/allowed-tools/disable-model-invocation).
  Skills are listed in the system prompt (metadata only) and loaded on demand via
  `/skill:<name>` (TUI/interactive) or `invoke_skill` ctype (RPC). Supports progressive
  disclosure, trust warnings, cooperation approval gates, and `/reload` for resource
  refresh. Includes `docs/SKILLS.md` with full specification.

### Changed

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
