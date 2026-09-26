# one

[![CI](https://github.com/noxgle/one/actions/workflows/ci.yml/badge.svg)](https://github.com/noxgle/one/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)

`one` is an autonomous Python terminal agent. It can plan, use files and shell
tools, verify work, and report a result. It also supports approval gates,
steering, questions, sessions, MCP tools, extensions, skills, and image input.

> **Maturity:** Alpha (`0.1.x`). Public APIs, tool contracts, and storage
> formats may change in `0.1` releases. See [CHANGELOG.md](CHANGELOG.md) and
> [TODO.md](TODO.md).

## Table of contents

- [Quick start](#quick-start)
- [Installation](#installation)
- [Updating](#updating)
- [Choose a mode](#choose-a-mode)
- [Headless tasks with `one run`](#headless-tasks-with-one-run)
- [Providers and authentication](#providers-and-authentication)
- [Sessions and configuration](#sessions-and-configuration)
- [Images](#images)
- [Cooperation and safety](#cooperation-and-safety)
- [Integrations](#integrations)
- [Diagnostics](#diagnostics)
- [Development](#development)
- [References](#references)

## Quick start

```bash
export OPENAI_API_KEY=sk-...
one run "summarize README.md" --provider openai --model gpt-4o-mini
```

For an interactive terminal UI, start `one` (the default mode is TUI) or use
`one --mode tui`. To authenticate interactively, start the TUI and use:

```text
/login openai sk-... gpt-4o-mini
/login status
```

## Installation

### From source

```bash
git clone https://github.com/noxgle/one.git
cd one
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
one --help
```

### From PyPI

```bash
pip install one-agent
one --help
```

The distribution name is `one-agent`; the import and console-script name are
`one`. The version source is `one/config.py:VERSION`.

Verify an installation with `one --version`, `one --help`, or
`python -m one --help`.

## Updating

### Installation from PyPI

Upgrade the installed distribution with:

```bash
python -m pip install --upgrade one-agent
one --version
```

On Windows PowerShell, use the same command with the Python launcher if needed:

```powershell
py -3.12 -m pip install --upgrade one-agent
one --version
```

### Installation from source

Pull the latest changes and refresh the editable installation:

```bash
git pull
python -m pip install -e '.[dev]'
one --version
```

Updating the package does not remove user configuration, credentials, or saved
sessions. These are stored outside the repository under `~/.config/one` by
default (or under `ONE_CODING_AGENT_DIR` when configured). For a virtual
environment, activate it before running the commands above or use that
environment's Python executable explicitly.

## Choose a mode

`--mode` accepts exactly `tui`, `text`, `json`, or `rpc`. `one run <task>` is a
distinct headless subcommand outside `--mode` routing, not a `--mode` value.
The default settings select the TUI.

| Interface | Interactive | Input | Output | Startup banner | Images | Best for |
| --- | --- | --- | --- | --- | --- | --- |
| `--mode tui` | Yes | Textual editor and slash commands | Rendered stream | Yes | `--image`, pasted/path images, `read_image` | Human-driven work |
| `--mode text` | No | Positional messages | Last assistant text | Yes, unless `quietStartup` is set | Startup `--image` | Simple one-shot text output |
| `--mode json` | No | Positional messages | JSON object containing session `messages` | Yes, unless `quietStartup` is set | Startup `--image` | One-shot session data |
| `--mode rpc` | Yes, protocol-driven | One JSON object per stdin line | JSON event/response object per stdout line | No | Startup `--image`; prompt `attachments` | Orchestrators and UI clients |
| `one run` | No | Required task argument | Summary, or a result JSON object with `--json` | Yes unless `--json` or `quietStartup` suppresses it | Startup `--image` | Autonomous CI/headless tasks |

`--mode text` is **not** an interactive REPL. It sends the supplied positional
message or messages and prints after they finish. Do not use `--mode cli`:
it is not an accepted CLI mode.

### TUI

```bash
one
one --mode tui --provider openai --model gpt-4o-mini
one --mode tui --image screenshot.png
```

The Textual TUI is the human-facing interface. It streams responses, displays
tool lifecycle status, retains sessions, and accepts slash commands such as
`/help`, `/steer`, `/follow`, `/abort`, `/login`, and `/mcp list`. It displays a
startup banner and status information.

Configure the startup information panel in `settings.json`:

```json
{"tui": {"infoPanel": "top"}}
```

`top` is the default minimal layout with no right sidebar; `sidebar` enables the
detailed right sidebar. These startup settings retain their existing semantics.
At runtime, the information layouts are mutually exclusive:

- `/layout compact` shows the top status panel and hides the detailed sidebar.
- `/layout wide` hides the top status panel and shows the detailed right sidebar.
- `/layout focus` hides both information panels.

Use `/sidebar hide` or `/sidebar show` to hide or show the detailed sidebar;
showing it selects the `wide` layout, while hiding it from `wide` returns to
`compact`. The separate visual logo in the conversation window remains visible
in every layout and is not transcript content. The top status panel explicitly
shows `COOP: ON` or `COOP: OFF`.

Current application shortcuts are:

| Shortcut | Action |
| --- | --- |
| `Ctrl+P` | Command palette |
| `Ctrl+K` | Provider/model picker |
| `Ctrl+C` | Abort turn; reject a pending approval |
| `Ctrl+L` | Clear stream |
| `Ctrl+Q` | Quit |
| `Ctrl+Z` | Toggle cooperation |
| `Ctrl+S` | Toggle subagents |
| `Ctrl+O` | Toggle bash output |
| `Ctrl+V` | Paste host/system clipboard text |
| `Ctrl+Shift+V` | Paste terminal text (SSH-safe) |
| `Ctrl+Alt+V` | Paste an image from the system clipboard |
| `Ctrl+R` | Cycle retry mode (`off` → `on` → `unlimited` → `off`) |
| `Ctrl+Up` / `Ctrl+Down` | Navigate the local input history (up to 50 prompts and commands) |
| `Ctrl+F1` | Show slash-command help |
| `Esc` | Close the shortcuts panel |

Use `/paste-image` if the terminal intercepts `Ctrl+Alt+V`.

### One-shot text and JSON

```bash
# Prints the final assistant text after the prompt completes.
one --mode text "summarize README.md"

# --print is the same one-shot print path.
one --print "summarize README.md"

# Serializes the full session message list.
one --mode json "summarize README.md"

```

Text and JSON modes consume positional messages; they do not read prompts from
stdin and do not offer an interactive approval, question, or steering channel.
The one-shot JSON shape is `{"messages": [...]}`. It is not the result schema
of `one run --json` and it is not JSON-RPC. `--mode json` still shows the
startup banner unless `quietStartup` is configured. A print-mode invocation
with images but no text fails rather than silently succeeding.

### RPC

```bash
one --mode rpc
```

RPC is a long-lived JSON-lines protocol: write one JSON command per stdin line;
read JSON event and response objects from stdout, one per line. It emits no
startup banner, so stdout remains protocol data. A `prompt` command is accepted
before its asynchronous work completes; session events follow on stdout. Include
an `id` to correlate responses and request events.

```json
{"id":"p1","type":"prompt","message":"summarize README.md"}
{"id":"idle","type":"wait_for_idle"}
```

The first command receives a successful response, then prompt/tool/message
events; `wait_for_idle` receives its response after work completes. Use `steer`,
`follow_up`, `abort`, `answer_question`, and `get_pending_questions` for the
corresponding control channels. RPC prompt images use an `attachments` array of
paths. `steer` and `follow_up` reject image attachments. See
[`one/modes/rpc_mode.py`](one/modes/rpc_mode.py) and RPC snapshots under
[`tests/snapshots/rpc/`](tests/snapshots/rpc/) for the implemented command set
and event shapes.

## Headless tasks with `one run`

```bash
one run "fix the failing tests and summarize the changes"
one run "refactor the auth module" --provider openai --model gpt-4o --json
one run @task.txt --param component=auth
one run "implement feature X" --answer-file /tmp/answer.txt --steer-file /tmp/steer.txt
```

`one run <task>` runs the autonomy loop and prints its final summary. It is a
subcommand, not a `--mode` route. `@task.txt` expands the task from that file;
`--param name=value` substitutes `{{name}}` in the expanded task file. For this
subcommand, `--json` is a separate clean-output contract: it suppresses the
startup banner and instead prints exactly:

```json
{"summary":"...","goalSuccess":true,"finished":true}
```

The task command can cooperate with a human without an interactive UI:

- `--cooperation` prompts on stdin before mutating tools; EOF rejects.
- `--answer-file PATH` writes an `ask_user` question to the file and polls until
  its content changes to an answer. Without it, questions receive a deterministic
  “proceed with best judgment” answer.
- `--steer-file PATH` polls for text, submits non-empty contents as steering,
  then clears the file. These channels are text-only.

Exit status is `0` only when the agent finishes with `goalSuccess`; `1` means a
failed, unfinished, aborted, or resumed-without-task result; `2` is usage or
validation failure. More generally, CLI validation errors use `2`; normal
subcommand failures use `1`. TUI and RPC processes normally exit `0` when
closed. `one run` writes a report to `reports.jsonl` when it has an agent dir.

## Providers and authentication

Supported backends include OpenAI-compatible providers, Anthropic, Gemini,
llama.cpp, Ollama, and ChatGPT/Codex. Credential precedence is runtime input,
then stored `auth.json`, then the provider environment variable. Common
variables include `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and `GEMINI_API_KEY`.

### Thinking levels

`/thinking <off|minimal|low|medium|high|xhigh>` and `/thinking-cycle` save the
selected level as the default for future sessions; a loaded session continues
to use its recorded level. Models without reasoning capability remain at
`off`. `off` omits enabled reasoning where supported. OpenAI-compatible and
Codex APIs use their available effort enums (their highest/lowest available
effort may be used for `xhigh`/`minimal`); Anthropic and Gemini 2.5 use
deterministic thinking-token budgets. Provider and model API restrictions still
determine which controls a request accepts.

OpenRouter uses its native `reasoning: {"effort": ...}` request dialect and
streams reasoning separately. Ollama Cloud uses native `think`; `minimal` and
`low` map to `"low"`, `medium` to `"medium"`, `high` and `xhigh` to `"high"`,
and `off` to `false`. ChatGPT/Codex Responses reasoning summaries
also render separately from answer text. Local Ollama and llama.cpp retain their
OpenAI-compatible/local behavior.

For local models, no key is required:

```bash
LLAMA_CPP_BASE_URL=http://127.0.0.1:8080 one --provider llama.cpp --model local
one --provider ollama --model local --ollama-url http://localhost:11434/v1
```

`/login` validates credentials before storing them and refreshes available
models. Anthropic and ChatGPT/Codex also support subscription OAuth login; see
[Subscription login](#subscription-login-oauth). Provider-controlled OAuth
endpoints can change without notice.

### Subscription login (OAuth)

- `/login anthropic subscription` starts Anthropic OAuth. `/login refresh anthropic`
  refreshes models; `/logout anthropic` removes local stored credentials.
- `/login chatgpt subscription` starts ChatGPT/Codex OAuth. Seed Codex models
  are available before a live model list is fetched; `/login refresh chatgpt`
  refreshes it.

`/logout <provider>` removes one-managed runtime/stored credentials locally. It
does not revoke a provider account token or remove environment credentials.

## Sessions and configuration

By default state is private under `~/.config/one` (override with
`ONE_CODING_AGENT_DIR`): `auth.json`, `models.json`, `settings.json`, sessions,
and reports. A project `.one/` directory supplies per-project MCP and extension
configuration. Use `--no-session`, `--session`, `--continue`, `--resume`, or
`--fork` to control session use.

Session JSONL, durable evidence, and provider context are separate. Provider
context receives bounded tool/MCP previews; durable sessions can retain complete
sanitized results in an evidence sidecar retrievable by the `evidence_read` tool.
See [Cooperation and safety](#cooperation-and-safety) and the slash-command
help for configuration such as `tools.maxSteps` (`0` means unlimited tool steps)
and optional `toolOutputPruning`.

### TUI session browser

Persisted sessions are scoped to the current project/session directory. In the
TUI, `/sessions` lists them newest first with a temporary number, name, age, and
message count. The first user prompt automatically becomes the local session
name after whitespace/control-character normalization (up to 64 Unicode
characters); rename it when needed.

```text
/sessions
/sessions 2
/sessions Fix the parser error
/sessions rename 2 Parser investigation
/sessions delete Parser investigation
yes
```

Names in load, rename, and delete commands must be the **full exact name**—no
partial or fuzzy matching is performed. `delete` always asks for `yes`; any
other reply cancels it. It deletes the JSONL and its matching durable-evidence
sidecar. Deleting the active session opens a new empty session so the TUI stays
usable. Loading or deleting waits until an active turn has stopped. Use
`--continue` or `--resume` at startup to open the most recently modified session.

## Images

Image input supports PNG, JPEG, and WebP: at most four images per prompt, with
a 10 MB source-file limit and decoded Base64 limited to 5 MiB. Use startup
`--image` (repeatable) or TUI image paste:

```bash
one run "describe this diagram" --image diagram.png
one --mode text "describe this diagram" --image diagram.png
```

Images are transient for the current turn and are not persisted in session JSONL.
Errors and events use only source basenames. Vision-incapable models reject
images before an HTTP request. ChatGPT/Codex vision-capable models send native
Responses API `input_image` parts; invalid or missing blobs fail explicitly.

TUI, text/json, and `one run` accept startup `--image`; RPC accepts startup
images and prompt `attachments`. `read_image` is a runtime tool the agent can
call during its turn to inspect a file; it is not a startup image-input channel
for `--mode text` or `one run`. Steering and follow-up messages are text-only.

## Cooperation and safety

> ## ⚠️ Workspace trust warning
>
> Starting `one` in a repository can execute arbitrary Python from
> `.one/extensions/*.py` and shell commands from MCP configuration in
> `.one/settings.json`. **Use it only in repositories you trust.** To inspect an
> untrusted repository, start with:
>
> ```bash
> one --no-extensions --no-mcp
> ```
>
> Extensions and MCP servers are not sandboxed; they run with `one`’s
> permissions. Cooperation mode is an approval gate, not a sandbox or security
> boundary.

`--cooperation` requests approval before mutating tools (`bash`, `write`,
`edit`, `plan`, and `apply_patch`). In the TUI, toggle it with `Ctrl+Z`; use
`Ctrl+A` in the interactive fallback. Rejections include a reason returned to
the agent. Interactive modes provide `/steer`, `/follow`, and abort controls;
RPC and `one run` provide the channels described above.

`apply_patch` validates collisions and symlink paths and uses staging/rollback,
but it cannot guarantee recovery from power loss, hostile concurrent filesystem
changes, or rollback I/O failure. Review changes and retained backups when an
error is reported.

## Integrations

### MCP servers

MCP servers can be configured in global or project `settings.json` with
`mcpServers`. Stdio servers run as child processes; streamable HTTP servers use
their configured URL. Use `--no-mcp` to disable all of them for a run and
`/mcp list|enable|disable` in the TUI. MCP tools are added to the agent prompt.

Stdio servers can opt into runtime recovery after an unexpected process exit or
stdout EOF; recovery remains off by default for stdio. Streamable HTTP servers
recover failed connections by default because they have no child-process monitor.
Set `"restart": false` on an HTTP server to opt out. The following optional
fields belong under `mcpServers.<name>`:

```json
{
  "restart": true,
  "restartDelaySec": 60,
  "maxRestartAttempts": 3,
  "restartExhaustion": "disable",
  "retryIntervalSec": 300
}
```

After each failure, `one` removes that server's tools immediately and waits
`restartDelaySec` before reconnecting. A successful reconnect restores the
tools. At the attempt limit, the `disable` exhaustion policy stops recovery for
that runtime only (the configured `enabled` value is not changed); `retry`
continues at `retryIntervalSec`. `/mcp disable` cancels pending recovery and
persists `enabled: false` until `/mcp enable` resets recovery and persists
`enabled: true`. Recovery never replays the interrupted tool call.

For web search, the [web-deepsearch MCP server](https://github.com/noxgle/mcp-web-deepsearch)
uses DuckDuckGo and can run over stdio. It needs no API key, but its server is
not sandboxed and may require outbound network access.

### Extensions and skills

Extensions, themes, prompts, and skills are discovered by the resource loader.
See [docs/EXTENSIONS.md](docs/EXTENSIONS.md) for discovery paths, `--extension`,
package commands, and hooks. See [docs/SKILLS.md](docs/SKILLS.md) for skill
frontmatter, discovery, trust prompts, and invocation. Skills are metadata in
the system prompt and load only when invoked with `/skill:<name>` or RPC
`invoke_skill`; they are not auto-executed.

## Diagnostics

The opt-in Docker diagnostic runner drives a persistent RPC session in an
isolated disposable workspace and produces bounded telemetry and reports:

```bash
.venv/bin/python scripts/diagnose_long_session.py --json
.venv/bin/python scripts/diagnose_long_session.py --duration 10 --skip-analysis --json
```

It requires Docker and a reachable configured model. Its container is a
containment aid, not a security boundary. Reports may contain sensitive
diagnostic data; artifacts are removed after a successful report unless
`--keep-artifacts` is used. See `--help` for workload, network, endpoint, and
telemetry options.

`--duration` is the scenario-admission window. The final admitted scenario gets
up to 60 seconds to drain after that deadline; reports distinguish a truncated
final scenario, clean shutdown, and runtime failure.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
git diff --check
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contributor conventions and focused
test commands. Linux and macOS are supported in CI. Windows is currently
best-effort and is not an officially supported platform yet; shell-oriented
behavior remains experimental pending Windows CI. Windows 11 is the planned
future target, while Windows 10 support is deferred.

## References

- [TODO.md](TODO.md) — roadmap
- [DONE.md](DONE.md) — delivered work
- [CHANGELOG.md](CHANGELOG.md) — release notes
- [CONTRIBUTING.md](CONTRIBUTING.md) — development guide
- [SECURITY.md](SECURITY.md) — security policy and reporting
- [docs/architecture.md](docs/architecture.md) — internal architecture
- [docs/EXTENSIONS.md](docs/EXTENSIONS.md) — extension contract
- [docs/SKILLS.md](docs/SKILLS.md) — skills contract
- [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md) — release verification
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — community guidelines
