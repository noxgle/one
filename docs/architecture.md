# Architecture of `one`

```
 ██████╗ ███╗   ██╗███████╗
██╔═══██╗████╗  ██║██╔════╝
██║   ██║██╔██╗ ██║█████╗
██║   ██║██║╚██╗██║██╔══╝
╚██████╔╝██║ ╚████║███████╗
 ╚═════╝ ╚═╝  ╚═══╝╚══════╝
```

[Full TUI logo artwork](https://github.com/noxgle/one/blob/main/one/assets/logo.txt)

Internal architecture of the autonomous terminal agent `one` — components,
data flows, contracts, and extension points.

> **Audience:** contributors, reviewers, and agents inspecting the codebase.
> This document is derived from the source tree at commit `HEAD`; verify
> against the code if any detail has drifted.

## 1. Overview

`one` is a Python 3.12+ application distributed as `one-agent` (import and
console-script name are `one`). It provides four user-visible interfaces (TUI,
text, JSON, RPC) plus a headless `one run` subcommand, all driven by a single
synchronous agent loop.

```
┌──────────────────────────────────────────────────────────┐
│                      one/cli/main.py                      │
│  entry point · arg parsing · provider registry            │
│  · session/runtime wiring · mode dispatch                 │
└──────────┬──────────────────────────────┬────────────────┘
           │                              │
    ┌──────▼──────┐              ┌────────▼────────┐
    │ one/modes/  │              │ one/modes/run_  │
    │  tui_mode.py│              │   mode.py       │
    │  text mode  │              │  (headless)     │
    │  json mode  │              │                 │
    │  rpc mode   │              │                 │
    └──────┬──────┘              └────────┬────────┘
           │                              │
    ┌──────▼──────────────────────────────▼────────┐
    │         one/core/agent_session.py             │
    │  synchronous event-emitting loop · tool       │
    │  dispatch · compaction · steering queue       │
    └──────┬──────────────────────────────┬────────┘
           │                              │
    ┌──────▼──────┐              ┌────────▼────────┐
    │ one/tools/  │              │ one/providers/  │
    │  14 tools   │              │  adapters       │
    └──────┬──────┘              └────────┬────────┘
           │                              │
    ┌──────▼──────────────────────────────▼────────┐
    │   one/core/session_manager.py · evidence      │
    │   sidecars · JSONL persistence                │
    └──────────────────────────────────────────────┘
```

## 2. Entry point and CLI routing

**File:** `one/cli/main.py`

The CLI entry point:

1. Parses arguments with `typer` (`one/cli/args.py`), including `--mode`
   (`tui`, `text`, `json`, `rpc`), `--provider`, `--model`, `--image`,
   `--json`, `--no-extensions`, `--no-mcp`, and positional task messages.
2. Builds the provider registry via `build_provider_registry()` which
   constructs adapters for every known backend.
3. Creates `AgentSessionRuntime` (session factory, switching, forking,
   model restoration).
4. Dispatches to the selected mode:

| Mode | File | Characteristics |
| --- | --- | --- |
| `tui` | `one/modes/tui_mode.py` | Textual-based TUI with streaming, slash commands, keyboard shortcuts, session history |
| `text` | `one/modes/print_mode.py` | One-shot positional messages → final assistant text |
| `json` | `one/modes/jsonl.py` | One-shot positional messages → `{"messages": [...]}` session dump |
| `rpc` | `one/modes/rpc_mode.py` | Long-lived JSON-lines stdin/stdout protocol; no startup banner |
| `one run` | `one/modes/run_mode.py` | Headless autonomy loop; `--json` emits `{"summary":"...","goalSuccess":true,"finished":true}` |

The default mode (when none is specified) is `tui`.

## 3. Core agent loop

**File:** `one/core/agent_session.py`

The `AgentSession` class is the central component. It implements a synchronous
event-emitting loop that:

1. **Builds context** — compacts conversation history if the token budget is
   exceeded, loads skill metadata, system prompt fragments, and tool definitions.
2. **Invokes the provider** — serializes messages into the provider's expected
   format and receives streaming text, tool calls, or structured output.
3. **Dispatches tool calls** — parses provider output (JSON-in-text format or
   provider-native structured calls), validates arguments against registered
   schemas, and executes the matching tool function.
4. **Emits events** — `tool_call_start`, `tool_call_end`, `message_start`,
   `message_end`, `turn_start`, `turn_end`, `agent_start`, `agent_end`, etc.
5. **Handles steering** — processes queued steering messages at provider
   boundaries.
6. **Manages compaction** — generates rolling summaries when history exceeds
   the configured threshold.

Key configuration:
- `maxSteps` — maximum number of tool steps (0 = unlimited).
- `tools.timeoutSec` — default per-tool timeout (30s).
- `compaction` settings — threshold for automatic summary generation.

### 3.1 Tool dispatch

Tools are registered in `one/tools/index.py`. Each tool has a name, description,
and function. Dispatch happens in `AgentSession._run_tool_call()`:

1. Match the tool name against `all_tools`.
2. Validate arguments against the schema in `resource_loader.py`.
3. Call `tool.execute.before` extension hooks (can deny the call).
4. Execute the tool function with validated arguments.
5. Call `tool.execute.after` extension hooks.
6. Emit `tool_call_start` and `tool_call_end` events.
7. Store results in `self.messages` (bounded to 12,000 chars) and write
   complete evidence to the session sidecar.

### 3.2 Event contract

The event system (`one/core/event_bus.py`) emits typed events that external
consumers (TUI, RPC, diagnostics) rely on. The contract is stable:
- Events are additive; new events must not change existing payloads.
- Event sequences are tested via `tests/test_event_snapshots.py`.
- Every tool call produces a `tool_call_start` followed by `tool_call_end`.
- Every turn produces `turn_start` and `turn_end`.
- Every agent session produces `agent_start` and `agent_end`.

## 4. Providers

**Directory:** `one/providers/`

Provider adapters serialize messages into the expected format and deserialize
responses. The registry maps provider names to adapter instances.

### 4.1 Supported providers

| Name | Adapter | Notes |
| --- | --- | --- |
| `openai` | `OpenAICompatibleAdapter` | OpenAI-compatible REST API |
| `anthropic` | `AnthropicAdapter` | Anthropic Messages API; supports `cache_control` breakpoints |
| `gemini` | `GeminiAdapter` | Google Gemini API |
| `chatgpt` | `CodexResponsesAdapter` | ChatGPT/Codex subscription (Responses API) |
| `openrouter` | `OpenAICompatibleAdapter` | OpenRouter gateway (`reasoning_mode="openrouter"`) |
| `ollama-cloud` | `OllamaCloudAdapter` | Ollama Cloud API |
| `llama.cpp` | `OpenAICompatibleAdapter` | Local llama.cpp server (no API key, `supports_reasoning_effort=False`) |
| `ollama` | `OpenAICompatibleAdapter` | Local Ollama server (no API key) |
| `xai`, `deepseek`, `mistral`, `groq` | `OpenAICompatibleAdapter` | Various OpenAI-compatible backends |
| `azure-openai` | `OpenAICompatibleAdapter` | Azure OpenAI (only when `AZURE_OPENAI_BASE_URL` is set) |

### 4.2 Provider contract

All adapters inherit from `ProviderAdapter` (`one/providers/base.py`) and
implement:
- `invoke(messages, tools, ...)` — send a request and return structured output.
- `is_vision_capable(model)` — check if the model supports image input.
- `get_capabilities(model)` — return capability metadata (vision, tool calls, etc.).

Native structured tool calls (OpenAI function calling, Anthropic tool use,
Gemini tool calls) are absorbed into a normalized `{id, name, arguments}` shape
that the session loop treats identically to JSON-in-text parsed calls.

### 4.3 Image support

Image attachments are stored in the blob store (`one/core/attachments.py`)
using content-addressed hashing. Supported formats: PNG, JPEG, WebP.
Each adapter serializes images differently:
- **OpenAI-compatible:** `image_url` parts in message content.
- **Anthropic:** `image` content blocks with `source` (base64).
- **Gemini:** `inline_data` parts with MIME type.
- **Codex Responses:** `input_image` parts with base64 data URI.

Images are rejected before any HTTP request if:
- The model lacks vision capability.
- The blob is missing, corrupt, or exceeds size limits.
- The mode does not support images (e.g., steer/follow-up are text-only).

## 5. Tools

**Directory:** `one/tools/`

There are 14 registered tools:

| Tool | Description | Category |
| --- | --- | --- |
| `read` | Read file contents | Read-only |
| `read_image` | Load image for vision inspection | Read-only |
| `bash` | Execute shell commands | Mutating (with timeout) |
| `edit` | Exact-string file replacement | Mutating |
| `write` | Write/overwrite files | Mutating |
| `grep` | Search file contents | Read-only |
| `find` | Find files by pattern | Read-only |
| `ls` | List directory contents | Read-only |
| `evidence_read` | Read durable evidence by ID | Read-only (session-scoped) |
| `finish` | End the task with summary | Terminal |
| `plan` | Store an execution plan | Utility |
| `ask_user` | Ask the human a question | Interactive |
| `spawn_subagent` | Delegate subtask to subagent | Concurrent |
| `apply_patch` | Apply OpenCode patch format | Mutating |

### 5.1 Tool categories

- **Coding tools:** `read`, `read_image`, `bash`, `edit`, `write`, `apply_patch`.
- **Read-only tools:** `read`, `read_image`, `grep`, `find`, `ls`.
- **Mutating tools:** subject to cooperation approval when enabled.

### 5.2 Evidence storage

Every tool result is stored as a durable evidence record:
- The bounded preview (≤12,000 chars) goes into `self.messages` for provider context.
- The complete raw result is written to a sidecar JSONL file keyed by a session-scoped evidence ID.
- The model can retrieve the full evidence via `evidence_read` using the evidence ID from the preview.

## 6. MCP (Model Context Protocol)

**File:** `one/mcp/client.py`

MCP clients connect to external tool servers via:
- **stdio transport:** spawn a subprocess and communicate via stdin/stdout.
- **Streamable HTTP transport:** connect to an HTTP endpoint.

Servers are configured in `.one/settings.json` under `mcpServers`. Features:
- Automatic tool discovery and registration.
- Server recovery after unexpected exit (stdio) or failed connections (HTTP).
- `restart`, `restartDelaySec`, `maxRestartAttempts`, `restartExhaustion`
  configuration per server.
- Server tools are removed immediately on failure and restored on reconnect.

## 7. Extensions and resources

**Directory:** `one/resources/`

### 7.1 Extensions

Extensions are Python files that export a `register(ctx) -> hooks` function.
They are discovered from:
1. `<agent_dir>/extensions/` — global extensions (default: `~/.config/one/extensions/`).
2. `<cwd>/.one/extensions/` — project-local extensions.
3. `--extension <path>` — explicit paths (repeatable).

Hooks include:
- `tool.execute.before` — can modify arguments or deny the call.
- `tool.execute.after` — observe outcomes.
- `chat.message` — intercept user messages.
- `experimental.session.compacting` — influence compaction summaries.
- `dispose` — cleanup at session end.

Extensions run with full permissions (not sandboxed). Use `--no-extensions` to disable.

### 7.2 Skills

Skills are `SKILL.md` files with YAML frontmatter. Discovered from:
1. `~/.config/one/skills/` — global skills.
2. `~/.agents/skills/` — platform-wide skills.
3. `.one/skills/` — project-local skills.
4. `--skill <path>` — explicit path.

Skills use progressive disclosure: only metadata (name, description) appears in
the system prompt; full content is loaded on demand via `/skill:<name>` or RPC.

### 7.3 Resource loader

`one/resources/resource_loader.py` handles discovery, parsing, validation, and
reload of all resource types (extensions, skills, prompts, themes).

## 8. Persistence

**File:** `one/core/session_manager.py`

Session management:
- JSONL format for conversation history.
- Evidence sidecars for complete tool/MCP results.
- Atomic writes via `tempfile + os.replace`.
- Session metadata (id, name, model, tool usage) stored separately.

### 8.1 Data directories

| Path | Contents | Permissions |
| --- | --- | --- |
| `~/.config/one/auth.json` | API keys and OAuth tokens | 0600 |
| `~/.config/one/settings.json` | Global settings | atomic write |
| `~/.config/one/models.json` | Model registry and capabilities | atomic write |
| `~/.config/one/sessions/*.jsonl` | Session conversation history | 0600 |
| `~/.config/one/blobs/` | Image attachment storage | 0700 dir, 0600 files |

The agent directory can be overridden via `ONE_CODING_AGENT_DIR`.

### 8.2 Legacy migration

Old state at `~/.one/agent/` is migrated to `~/.config/one/` on first run.
Migration tightens permissions but never fails the startup if it encounters errors.

## 9. Configuration

**File:** `one/core/settings_manager.py`

Settings are stored in `settings.json` and include:
- `tools.timeoutSec` (default: 30) — per-tool timeout.
- `compaction.threshold` — automatic compaction threshold.
- `compaction.maxTokens` — maximum tokens after compaction.
- `cooperation` — enable/disable approval gates.
- `mcp.enabled` — enable/disable MCP servers.
- `extensions` — enable/disable extensions.
- `toolOutputPruning` — optional provider-context optimization.
- `subagents.timeoutSec` (default: 1800) — subagent task timeout.
- `retry.mode` — `off`, `on`, or `unlimited`.
- `maxSteps` — maximum tool steps (0 = unlimited).

## 10. Authentication

**File:** `one/core/auth_storage.py`

Auth precedence: **runtime input > stored `auth.json` > environment variable**.

Supported providers:
- API key authentication (OpenAI, Anthropic, Gemini, etc.).
- OAuth/subscription login (Anthropic, ChatGPT/Codex).
- Local servers (llama.cpp, Ollama) need no API key.

Tokens are stored in `auth.json` with mode 0600 and automatically refreshed
before expiry for OAuth providers.

## 11. TUI (Textual UI)

**File:** `one/modes/tui_mode.py`

The TUI is built on Textual `>=0.74.0` and provides:
- Streaming response rendering.
- Tool lifecycle status display.
- Session history and navigation.
- Slash commands: `/help`, `/steer`, `/follow`, `/abort`, `/login`, `/mcp`, `/skill`, `/compact`, `/reload`.
- Keyboard shortcuts (Ctrl+P, Ctrl+C, Ctrl+L, Ctrl+Q, Ctrl+Z, Ctrl+R, etc.).
- Image input via `--image`, paste, or `read_image` tool.
- Cooperation approval prompts (Ctrl+Z to toggle).
- Subagent management (Ctrl+S to toggle).
- Version display in sidebar.

The stream buffer is capped at 500 entries; absolute indexes are rebased after
trimming. Golden-rendering snapshots are tested via `tests/test_tui_snapshots.py`.

## 12. RPC mode

**File:** `one/modes/rpc_mode.py`

RPC mode implements a long-lived JSON-lines protocol:
- **Input:** one JSON command per stdin line.
- **Output:** one JSON event/response per stdout line.
- No startup banner (stdout is pure protocol data).

Supported commands:
- `prompt` — submit a message.
- `wait_for_idle` — wait for work completion.
- `steer` — send steering text.
- `follow_up` — send follow-up message.
- `abort` — abort current turn.
- `answer_question` — answer an `ask_user` question.
- `get_pending_questions` — list pending questions.
- `invoke_skill` — load and invoke a skill.
- `get_skills` — list skill metadata.
- `reload_resources` — reload skills, extensions, prompts.

Use an `id` field to correlate requests and responses.

## 13. Data flow: a single turn

```
1. User input (TUI/text/RPC) or queued steering
        ↓
2. AgentSession._run_turn():
   a. Build context (compaction, skill metadata, tool definitions)
   b. Check for pending steering → inject into conversation
   c. Invoke provider (streaming response)
   d. Parse assistant output (text, tool calls, or both)
        ↓
3. For each tool call:
   a. Emit tool_call_start event
   b. Validate arguments against schema
   c. Call tool.execute.before hooks (can deny)
   d. Execute tool function
   e. Call tool.execute.after hooks
   f. Store bounded preview in self.messages
   g. Write complete result to evidence sidecar
   h. Emit tool_call_end event
        ↓
4. Accumulate assistant message and tool results
5. Repeat until: no tool calls, maxSteps reached, or finish called
6. Emit turn_end, agent_end
        ↓
7. Persist session (JSONL + evidence sidecars)
```

## 14. Compaction

Compaction maintains a rolling summary plus recent messages:
- Triggered when history exceeds the configured threshold.
- The model generates a summary of older messages.
- The summary is inserted as a system message at the beginning of the context.
- Older messages are discarded from the provider context.
- `/compact` forces manual compaction for non-empty history.
- Compaction is lossy; factual evidence remains in the sidecar for retrieval.

## 15. Steering and follow-up

- Steering messages (`/steer` or RPC `steer`) are queued and consumed at the
  next provider boundary (after the current provider response, before the next).
- Follow-up messages (`/follow_up` or RPC `follow_up`) are queued for the next
  turn after the current one completes.
- Both maintain FIFO ordering.
- Terminal events (`finish`, `abort`, `timeout`) do not automatically drain queues.

## 16. Subagents

`spawn_subagent` delegates work to an isolated agent session:
- Each subagent has its own session ID, model, and tool configuration.
- Default timeout: `subagents.timeoutSec` (1800s / 30 min).
- On timeout: abort, collect diagnostics, return typed result with session ID.
- Parent can inspect/resume the subagent session via the session manager.
- Nested spawning is forbidden (recursion guard).

## 17. Testing

**Directory:** `tests/`

- ~1000+ tests across 50+ files.
- Full suite: `.venv/bin/python -m pytest -q`.
- Lint: `.venv/bin/ruff check .`.
- TUI snapshots: `ONE_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest -q tests/test_tui_snapshots.py`.
- Focused suites: `scripts/test.sh <suite>` (quick, mcp, tui, providers, tools, auth, extensions, cli, rpc, core, sessions, failed/last, full/all).
- Async tests require explicit `@pytest.mark.asyncio`.
- All tests use `ONE_CODING_AGENT_DIR` pointing to a scratch directory.
- Tests never contact real API servers or MCP servers.

### Test organization

| File | Focus |
| --- | --- |
| `tests/test_tui_*.py` | TUI input, streaming, rendering, commands, cooperation, retry, navigation |
| `tests/test_tui_snapshots.py` | Golden-rendering regression tests |
| `tests/test_agent_*.py` | Tool calls, retry/abort, timeouts, steering, parsing |
| `tests/test_event_snapshots.py` | Event sequence and payload verification |
| `tests/test_provider_*.py` | Provider adapter payloads and capabilities |
| `tests/test_extension_*.py` | Extension hooks and UI |
| `tests/test_mcp.py` | MCP client connectivity |
| `tests/test_session_*.py` | Session persistence and reload |
| `tests/test_image_*.py` | Image attachment storage and processing |
| `tests/test_cli_*.py` | CLI subprocess and argument parsing |

## 18. Versioning

- Single source of truth: `one/config.py:VERSION` (currently `0.1.45`).
- Drives `pyproject.toml` dynamic version and `one --version` output.
- Bump before tags; tags must equal the VERSION string.
- User-visible changes require a version bump, `CHANGELOG.md` Unreleased entry,
  and regenerated TUI snapshots.
- Tests-only, refactor-only, and comment-only changes do not bump version.

## 19. Known limitations

- **No sandboxing:** Extensions, MCP servers, and tools run with full permissions.
- **JSON-in-text tool calls:** The primary contract; native function-calling is
  absorbed but not yet normalized into a common adapter layer.
- **Windows:** Best-effort support; no Windows CI. POSIX shell behavior may differ.
- **Compaction is lossy:** Factual evidence survives in sidecars but the
  provider context loses detail after compaction.
- **Provider-native tool calls:** Malformed/unknown calls are handled with
  best-effort recovery; a formal `invalid` catch-all tool is planned.

## 20. Future directions

See `TODO.md` for the complete roadmap. Active work includes:
- Normalized provider-native tool call adapter layer.
- Windows shell support and CI.
- Prompt-cache performance optimization (partially complete).
- Autonomous provider-timeout recovery.
- Diagnostic runner for long-session testing.
- Test-suite organization refactor (split large modules).
- Plan-mode agent and phase system (explicitly deferred).
- Dependency security audit automation.
