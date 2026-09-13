# one

[![CI](https://github.com/noxgle/one/actions/workflows/ci.yml/badge.svg)](https://github.com/noxgle/one/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)

```text
 ██████╗ ███╗   ██╗███████╗
██╔═══██╗████╗  ██║██╔════╝
██║   ██║██╔██╗ ██║█████╗
██║   ██║██║╚██╗██║██╔══╝
╚██████╔╝██║ ╚████║███████╗
 ╚═════╝ ╚═╝  ╚═══╝╚══════╝

███████╗ ██████╗ ██████╗
██╔════╝██╔═══██╗██╔══██╗
█████╗  ██║   ██║██████╔╝
██╔══╝  ██║   ██║██╔══██╗
██║     ╚██████╔╝██║  ██║
╚═╝      ╚═════╝ ╚═╝  ╚═╝

███████╗██╗   ██╗███████╗██████╗ ██╗   ██╗ ██████╗ ███╗   ██╗███████╗
██╔════╝██║   ██║██╔════╝██╔══██╗╚██╗ ██╔╝██╔═══██╗████╗  ██║██╔════╝
█████╗  ██║   ██║█████╗  ██████╔╝ ╚████╔╝ ██║   ██║██╔██╗ ██║█████╗
██╔══╝  ╚██╗ ██╔╝██╔══╝  ██╔══██╗  ╚██╔╝  ██║   ██║██║╚██╗██║██╔══╝
███████╗ ╚████╔╝ ███████╗██║  ██║   ██║   ╚██████╔╝██║ ╚████║███████╗
╚══════╝  ╚═══╝  ╚══════╝╚═╝  ╚═╝   ╚═╝    ╚═════╝ ╚═╝  ╚═══╝╚══════╝
```

Autonomous terminal agent written in Python: it executes assigned tasks on its own
(shell / files / code) — plan, run tools, verify, report — and can optionally work
in a **cooperation mode** where a human approves mutating tools, steers or aborts
mid-task, and answers agent questions.

The project started as a re-implementation of the `pi` coding agent and is now an
independent project with its own roadmap — see `TODO.md`. Completed work is
recorded in `DONE.md`.

`one` — *one for everyone* — builds on the experience gained while creating
[`term_agent`](https://github.com/noxgle/term_agent).

> **Maturity:** Alpha (`0.1.x`). The public API, tool contract, and storage formats may
> change in `0.1` releases. See `CHANGELOG.md` and `TODO.md` for the roadmap.

## ⚠️ Workspace trust warning

Running `one` inside a repository can execute arbitrary Python code from
`.one/extensions/*.py` and can launch shell commands from MCP server
configurations in `.one/settings.json`. **Only start `one` in repositories you
trust.** If you need to inspect an untrusted repository, use the safe startup:

```bash
one --no-extensions --no-mcp
```

Extensions and MCP servers are **not sandboxed** — they run with the same
permissions as `one` itself. Cooperation mode is **not** a security boundary or
sandbox; it is an approval gate only.

## Installation

### From source (recommended for development)

```bash
git clone https://github.com/noxgle/one.git
cd one
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
one --help
# also works as a module:
python -m one --help
```

### From PyPI (when published)

```bash
pip install one-agent
one --help
```

> The distribution name is `one-agent` (PyPI), the import name and console script
> remain `one`. The single version source is `one/config.py:VERSION`.

### Verify installation

```bash
one --version
one --help
python -m one --help
python -c "import one; print(one.VERSION)"
```

## Configure a provider

`one` supports OpenAI-compatible providers, Anthropic, Gemini, and the local
`llama.cpp`/`Ollama` backends. Three ways to provide credentials (highest
precedence first):

1. **Runtime** — CLI flag (`--api-key`), set programmatically, or via the provider
   adapter at session start.
2. **Stored** — key persisted by `/login` in `auth.json`.
3. **Environment variable** — `<PROVIDER>_API_KEY` (e.g. `OPENAI_API_KEY`,
   `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`,
   `XAI_API_KEY`, `DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`,
   `OLLAMA_CLOUD_API_KEY`). Environment keys cannot be removed by `/logout`
   (they are runtime-supplied, not one-managed).

**Quick start with an API key:**

```bash
# via environment (recommended for CI/headless)
export OPENAI_API_KEY=sk-...
one run "summarize README.md" --provider openai --model gpt-4o-mini

# via /login (interactive or TUI) — validates and fetches the model list:
one
# then in the REPL/TUI:
/login openai sk-... gpt-4o-mini
/login status
```

`/login` validates the key before storing it (`401/403` → `Authorization failed`,
key is **not** stored). On success it fetches the provider's model list
(`GET {base}/v1/models` for OpenAI-compatible, `GET /v1beta/models?key=…` for
Gemini, probe chat for Anthropic which has no public list endpoint), registers
models in-memory and persists them to `models.json`.

For local models no key is needed:

```bash
# llama.cpp (OpenAI-compatible, default http://127.0.0.1:8080)
LLAMA_CPP_BASE_URL=http://127.0.0.1:8080 one --provider llama.cpp --model local
# or per-model url in models.json — see "Local llama.cpp provider" below
```

Subscription login (OAuth) for Anthropic and ChatGPT/Codex is documented in
"Subscription login (OAuth)" below. It is a supported production feature, but
the underlying provider endpoints are externally controlled and may change.

## Run a task

### Headless one-shot (`one run`)

```bash
one run "fix the failing tests and summarize the changes"
one run "refactor the auth module" --provider openai --model gpt-4o --json
one run "implement feature X" --answer-file /tmp/answer.txt --steer-file /tmp/steer.txt
```

- `one run "<task>"` runs the full autonomy loop to `finish` (result summary +
  exit code `0` success / `1` failure, token/time/budget limits respected).
- `--json` prints the structured result `{summary, goalSuccess, finished}`.
- `--answer-file` / `--steer-file` enable headless `ask_user` and steering while
  the run is active (polled files).
- Other useful flags: `--thinking`, `--models`, `--tools`, `--cooperation`,
  `--no-subagents`, `--continue`/`--resume`/`--fork`.

In headless mode the task can also come from a file: `one run @task.txt` or
`@file` arguments are expanded and templated via `--param name=value`.

### Interactive REPL

```bash
one
# or explicitly:
one --mode text
```

### TUI (Textual)

```bash
one --mode tui
```

Built-in TUI themes: `default`, `light`, `hacker`, `solarized`, `fallout`.
Switch in-app: `/theme solarized`.

TUI highlights:

- live response streaming (provider-dependent; OpenAI-compatible/Anthropic/Gemini/Codex supported)
- scrollable main stream with scrollbar, simplified view (`> ...` for user messages)
- tool lifecycle visible in stream (`tool start (timeout Ns)`, `tool ok/err`)
- app version in the sidebar (`Version: …`, from the single source `one/config.py:VERSION`; CLI prints `one v…` at startup)

TUI keyboard shortcuts (also listed in the in-app shortcuts overlay):

| Shortcut | Action |
| --- | --- |
| `Ctrl+P` | Command palette |
| `Ctrl+C` | Abort turn / reject pending approval |
| `Ctrl+L` | Clear stream |
| `Ctrl+Q` | Quit |
| `Ctrl+Z` | Toggle cooperation mode |
| `Ctrl+S` | Toggle subagents |
| `Ctrl+O` | Toggle bash output |
| `Ctrl+V` | Paste text from the system clipboard |
| `Ctrl+Shift+V` | Paste image from the system clipboard |
| `Ctrl+R` | Cycle retry mode (`off` → `on` → `unlimited`) |
| `Ctrl+F1` | Show slash-command help |
- subscription login via `/login chatgpt subscription` / `/login anthropic subscription` (OAuth loopback/paste)
- cooperation approval / `ask_user` pauses the spinner and shows a waiting indicator plus a toast notification

### JSON-RPC (`--mode rpc`)

JSON-RPC over stdin/stdout for external orchestration (sessions, events,
extension UI). See `one/modes/rpc_mode.py` and `tests/snapshots/rpc/*.jsonl`.

## Vision / images (multimodal)

`one` supports image input via the `read_image` tool or the `--image` CLI flag.

### How it works

- **`read_image {path}`** — loads a local PNG/JPEG/WebP file, imports it into the
  session's private blob store, and returns a transient reference. The image is sent
  to the vision model on the next step.
- **`--image <path>`** — equivalent convenience flag (repeatable); same limits apply.

### Supported formats & limits

| format | limit |
|--------|-------|
| PNG, JPEG, WebP | 4 images per prompt |
| Source file | 10 MB (decoded Base64 ≤ 5 MiB) |

### Capability behavior

- `input.image` on the model definition signals vision support.
- **Cooperation mode** — if the provider lacks image support, the tool call is denied
  *before* the HTTP request; the reason is fed back to the model.
- **Autonomous mode** — the session catches the rejection and returns controlled
  feedback without crashing.
- **Retention** — image blobs are transient per-turn: they are never persisted to
  JSONL, never emit a blob-hash reference, and are auto-collected when the turn ends.

### Privacy

Error messages and event payloads log only the **basename** of the source image — the
full filesystem path is never written to JSONL or emitted in events.

### Supported modes & limitations

- `one run`, `--mode text`, `--mode tui`, interactive mode: `read_image` / `--image` work.
- `--mode rpc`: images are supported when the JSON-RPC request includes an `images` array.
- **Steer / follow-up messages** are text-only (the steer queue does not forward images).
- **Codex provider** supports image input via the Responses API (`input_image`
  parts); invalid or missing attachments are rejected before any HTTP request.
- **Print mode (`--print`)** with image-only input (no task text) is unsupported — a text
  message is required even when images are supplied.

## Cooperation mode

By default `one` works autonomously. With `--cooperation` (or `/cooperation` —
Ctrl+Z in the TUI, Ctrl+A in interactive mode) it asks before running mutating tools
(`bash`, `write`, `edit`, `plan`, `apply_patch`); rejections require a reason that is
fed back to the model.
Mid-task steering (`/steer`, `/follow`) and abort (Ctrl+C) work in every interactive mode.

`--cooperation` is an approval gate, not a sandbox. See the trust warning above.

## Apply patch safety

`apply_patch` applies unified-diff hunks via a staged, transactional workflow:

- **Collision policy:** Add and Delete/Update target the same path is a cross-role conflict
  (rejected). Duplicate sources, duplicate targets, and hardlink aliases are rejected.
- **Symlink policy:** Any existing symlink component (leaf or ancestor) for a source or
  target causes rejection.
- **Staged backup/rollback:** All outputs are computed and staged to temp files before any
  mutation. Original files are evacuated to same-directory backups. On commit failure,
  installed outputs are unlinked (if identity unchanged), backups are restored only when
  the original path is absent, and stages/dirs are cleaned. Retained backups are reported
  for manual recovery.
- **Path support:** Absolute and `..` paths are supported. Cross-filesystem Move is staged
  as copy+delete (not inode-preserving).
- **Explicit non-guarantees:** `apply_patch` provides no guarantee against crash/power-loss,
  concurrent hostile filesystem modification, or rollback I/O failure. In such cases,
  backups may remain and must be handled manually.

## Slash commands

Available in the TUI and interactive mode (type `/help` in the app):

| Command | Description |
| --- | --- |
| `/help` | List all commands |
| `/stats` | Session statistics (tokens, cost) |
| `/state`, `/status` | Current session state |
| `/queue [clear [all|steering|follow]]` | Show or clear the steering/follow-up queues |
| `/tools` | List active tools |
| `/clear` | Clear the stream |
| `/abort` | Abort the current turn |
| `/model [provider/model]` | Show or switch the model |
| `/model-cycle` | Cycle to the next available model |
| `/thinking [level]` | Show or set the thinking level (`off`, `minimal`, `low`, `medium`, `high`, `xhigh`) |
| `/thinking-cycle` | Cycle the thinking level |
| `/theme [name]` | Show or switch the TUI theme |
| `/steer <text>` | Send a steering message to the agent |
| `/follow <text>` | Send a follow-up message |
| `/compact [instructions]` | Compact the session context |
| `/tree` | Show the session tree |
| `/navigate <id> [--summary <text>]` | Navigate to a session entry |
| `/fork <id>` | Fork the session at an entry |
| `/new` | Start a new session |
| `/providers [name|#] [model|#]` | List logged-in providers, models, and switch |
| `/login [status|refresh <provider>|provider [apiKey] [model] [subscription]]` | Show or configure provider credentials (subscription = OAuth login flow) |
| `/logout <provider>` | Remove runtime API key, stored API key, and OAuth token for a provider locally |
| `/retry <on|off|unlimited>` | Set auto-retry mode (`unlimited` retries provider calls without a cap) |
| `/retry-cycle` | Cycle retry mode `off` → `on` → `unlimited` (Ctrl+R in TUI) |
| `/config [key] [value]` | Show or set a config value (e.g. `tools.maxSteps`; `0` = unlimited tool steps) |
| `/extui <list|request|respond|cancel|clear>` | Extension UI control |
| `/cooperation [on|off]` | Toggle cooperation mode (approval gates) |
| `/subagents [on|off]` | Enable/disable subagents (Ctrl+S in TUI) |
| `/bash-show [on|off]` | Show/hide tool output in the main window (bash, ls, read, grep, find, edit, write; only tool status when off) (Ctrl+O) |
| `/history [n]` | List recent slash commands (last 50); `/history N` prints `-> <cmd>` (interactive) or fills input (TUI) |
| `/mcp [list|enable <name>|disable <name>]` | List, enable, or disable MCP servers (tools are added/removed live) |
| `/bash <command>` | Run a shell command directly |
| `/exit`, `/quit` | Quit the app |

CLI flags: `--no-subagents` disables subagents, `--no-bash-output` hides bash output (exit code only).

## Subscription login (OAuth)

`one` supports subscription-based login for Anthropic and ChatGPT/Codex providers via
OAuth. No API keys required — just your account credentials.

**Anthropic subscription login:**

1. Run `/login anthropic subscription` (TUI/interactive).
2. The app opens your browser to `console.anthropic.com` for authorization (loopback flow)
   or shows a paste code for manual authorization (paste flow).
3. On success the access token and refresh token are stored in `auth.json` under the
   `oauth.anthropic` key with `type: "oauth"`.
   For plain API keys (`/login anthropic sk-ant-...`) the key is stored under
   `apiKeys.anthropic`. Anthropic API keys have **no public `GET /v1/models` endpoint** —
   validation uses a minimal chat probe (`max_tokens=1`) instead.
4. Models are fetched from `api.anthropic.com/v1/models` **only for OAuth tokens**
   and registered in the local registry + persisted to `models.json`.
5. Use `/logout anthropic` to remove the stored OAuth token locally.

**ChatGPT/Codex subscription login:**

1. Run `/login chatgpt subscription` — the app opens your browser to `chatgpt.com` for
   OAuth authorization (loopback flow).
2. The access token is stored in `auth.json` under the `oauth.chatgpt` key; the `accountId`
   (required for the Codex backend) is extracted from the JWT claims.
3. Models are fetched from `chatgpt.com/backend-api/codex/models` (Responses API) and
   registered in the local registry. If the endpoint returns zero models the server may
   be gating the list behind a newer `client_version` header.
4. Run `/login refresh chatgpt` to re-fetch the live model list without re-entering
   credentials.
5. Fallback seed models (`gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`) are used when
   no models have been fetched yet.
6. Use `/logout chatgpt` to remove the stored OAuth token locally.

`/logout <provider>` removes one-managed runtime API key, stored API key, and stored
OAuth credentials from memory and `auth.json`. It does **not** call any provider
revocation endpoint and cannot remove credentials that were supplied externally
(e.g. via environment variables).

OAuth tokens are automatically refreshed before expiry. The provider name in `auth.json`
maps to the Responses API backend for ChatGPT and the Anthropic Messages API for
Anthropic.

> **Provider-controlled endpoints:** the OAuth flows for Anthropic and ChatGPT/Codex
> use reverse-engineered endpoints that are not part of a public API. They may change
> without notice and third-party use may be subject to the provider's Terms of Service.
> See `SECURITY.md` for details.

## MCP servers

`one` can connect to Model Context Protocol (MCP) servers and expose their tools to the agent.
Configure servers in `settings.json`:

- **Agent dir** — global: `~/.config/one/settings.json` (or `ONE_CODING_AGENT_DIR`)
- **Project dir** — per-project: `.one/settings.json` (checked after the agent dir)

The agent-dir file is the canonical location shown in examples; the project file
is useful for per-repository MCP setup and is merged with the same `mcpServers`
shape. Use `one --no-mcp` to disable all MCP servers for a run.

**stdio transport** (spawns the server per session):

```json
"mcpServers": {
  "filesystem": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    "enabled": true
  }
}
```

**streamable HTTP transport** (single long-lived HTTP connection):

```json
"mcpServers": {
  "web-deepsearch": {
    "url": "http://127.0.0.1:8000/mcp",
    "enabled": true
  }
}
```

- `url` — streamable HTTP transport; the server must be reachable (e.g. `docker compose up -d` for `web-deepsearch`).
- `command` — stdio transport; spawns the server as a child process per session.
- `enabled: false` keeps the config but skips the server at startup.
- `--no-mcp` disables MCP entirely for a run.
- Manage servers at runtime: `/mcp list`, `/mcp enable <name>`, `/mcp disable <name>` (TUI and interactive mode). Changes take effect immediately and persist to settings.json.
- MCP tools are documented automatically in the agent's system prompt.

## Extensions

`one` discovers extensions, skills, prompts, and themes via `one/resources/resource_loader.py`.
See [`docs/EXTENSIONS.md`](docs/EXTENSIONS.md) for the full extension contract:

- discovery paths (`<agent_dir>/extensions/`, `<cwd>/.one/extensions/`, `--extension <path>`)
- package manager (`one install/remove/update/list`)
- hook contract (`tool.execute.before/after`, `chat.message`, `experimental.session.compacting`, `dispose`)

Project extensions in `.one/extensions/` are not sandboxed — see the trust warning.

## Global install (run `one` from any directory)

The installer is Unix-specific (Linux, macOS) and requires a source checkout
(it installs the current tree editable). On Windows use `pip install one-agent`
or `pipx install one-agent`.

```bash
chmod +x scripts/install.sh
./scripts/install.sh
```

Installer creates:

- virtualenv in `~/.one/venv`
- launcher in `~/.local/bin/one`

The config dir (`~/.config/one`, or legacy `~/.one/agent` if already present) is
created lazily on first run, not by the installer.

After that, `one` works from any directory (if `~/.local/bin` is on your `PATH`).

You can override config location with:

```bash
export ONE_CODING_AGENT_DIR=/custom/path
```

### Configuration files

- **Agent dir** (default `~/.config/one`, override via `ONE_CODING_AGENT_DIR`):
  `auth.json`, `models.json`, `settings.json`, `sessions/*.jsonl`, `reports.jsonl`,
  `extensions/`. One-time auto-migration from legacy `~/.one/agent`.
- **Project dir** (`.one/` in the current working directory): per-project MCP
  and extension configuration. Ignored by git (`.gitignore` covers `.one/`).
- If the global agent dir is not writable (e.g. read-only home), `one` falls back
  to `<cwd>/.one/agent` for that run and sets `ONE_CODING_AGENT_DIR` accordingly.
- All sensitive files are written atomically with restrictive permissions (`0700` for
  directories, `0600` for `auth.json`/`settings.json`/`models.json`/`sessions/*.jsonl`
  on POSIX).

## Local llama.cpp provider

`one` supports local `llama.cpp` server mode via OpenAI-compatible API.

1. Start `llama.cpp` server with OpenAI endpoint enabled.
2. Optionally set base URL (default is `http://127.0.0.1:8080`):

```bash
export LLAMA_CPP_BASE_URL=http://127.0.0.1:8080
```

3. Use model/provider in CLI or interactive mode:

```bash
one --provider llama.cpp --model local
# custom endpoint:
one --provider llama.cpp --model local --llama-cpp-url http://127.0.0.1:8089
# or in interactive mode:
# /model llama.cpp/local
```

`llama.cpp` local provider does not require an API key by default.

You can also set endpoint per model in `models.json`:

```json
{
  "providers": {
    "llama.cpp": [
      {
        "id": "local",
        "reasoning": false,
        "contextWindow": 32768,
        "url": "http://127.0.0.1:8089",
        "toolParser": [
          { "type": "raw-function-call" },
          { "type": "json" }
        ]
      }
    ]
  }
}
```

### Ollama

Same pattern as `llama.cpp`, default `http://localhost:11434/v1`, override via
`OLLAMA_BASE_URL` or `--ollama-url`. No API key required for local Ollama.

## Upgrade and uninstall

```bash
# upgrade from source
cd one && git pull && .venv/bin/pip install -e .[dev]

# upgrade from PyPI
pip install --upgrade one-agent

# upgrade global install
./scripts/install.sh   # re-creates ~/.one/venv from the current checkout

# uninstall (pip)
pip uninstall one-agent

# uninstall global install
rm -rf ~/.one/venv ~/.local/bin/one
# config/data remain in ~/.config/one — remove manually if desired:
# rm -rf ~/.config/one
```

The version is defined once in `one/config.py:VERSION` and drives both the
package metadata (`pyproject.toml` dynamic version) and `one --version`.

## Platform support

- **Linux and macOS** — fully supported (CI runs on `ubuntu-latest` and `macos-latest`
  for Python 3.12 and 3.13).
- **Windows** — best-effort: core agent, TUI, and file tools work; shell-dependent
  features (`bash` quoting, pipelines, `command_prefix`, process groups) are POSIX-oriented
  and some tests skip on Windows. Windows is declared in package classifiers but treated as
  experimental until shell behavior is fully validated on Windows CI.

## Documentation

- `TODO.md` — open roadmap items
- `DONE.md` — delivered functionality and release-preparation record
- `docs/EXTENSIONS.md` — extension hook contract
- `docs/RELEASE_CHECKLIST.md` — reproducible release verification steps
- `CONTRIBUTING.md` — development setup, testing, conventions
- `SECURITY.md` — supported versions, reporting, trust boundaries
- `CHANGELOG.md` — release notes
- `CODE_OF_CONDUCT.md` — community guidelines
