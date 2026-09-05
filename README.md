# one

[![CI](https://github.com/picon/one/actions/workflows/ci.yml/badge.svg)](https://github.com/picon/one/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)

Autonomous terminal agent written in Python: it executes assigned tasks on its own
(shell / files / code) — plan, run tools, verify, report — and can optionally work
in a **cooperation mode** where a human approves mutating tools, steers or aborts
mid-task, and answers agent questions.

The project started as a re-implementation of the `pi` coding agent and is now an
independent project with its own roadmap — see `TODO.md`. Completed work is
recorded in `DONE.md`.

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
git clone https://github.com/picon/one.git
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
> remain `one`. The single version source is `one/config.py:VERSION` (`0.1.0`).

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

1. **CLI flag** — `--api-key` / `--provider` / `--model`
2. **Environment variable** — `<PROVIDER>_API_KEY` (e.g. `OPENAI_API_KEY`,
   `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`,
   `XAI_API_KEY`, `DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`,
   `OLLAMA_CLOUD_API_KEY`)
3. **Stored key via `/login`** — interactive/TUI or RPC login that validates the
   key before persisting it

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
- tool lifecycle visible in stream (`tool start`, `tool ok/err`)
- subscription login via `/login chatgpt subscription` / `/login anthropic subscription` (OAuth loopback/paste)
- cooperation approval / `ask_user` pauses the spinner and shows a waiting indicator plus a toast notification

### JSON-RPC (`--mode rpc`)

JSON-RPC over stdin/stdout for external orchestration (sessions, events,
extension UI). See `one/modes/rpc_mode.py` and `tests/snapshots/rpc/*.jsonl`.

## Modes summary

- `--mode text|json` / `-p` — one-shot task execution (prints the stream, JSON for automation)
- interactive — REPL with slash commands, cooperation toggle (Ctrl+A), steer/abort
- `--mode tui` — Textual TUI with live streaming, themes, sidebar
- `--mode rpc` — JSON-RPC over stdin/stdout

## Cooperation mode

By default `one` works autonomously. With `--cooperation` (or `/cooperation`, Ctrl+A
in the TUI/interactive mode) it asks before running mutating tools (`bash`, `write`,
`edit`, `plan`, `apply_patch`); rejections require a reason that is fed back to the model.
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
| `/login [status|refresh <provider>|provider subscription|provider [apiKey] [model]]` | Show or configure provider credentials (subscription = OAuth login flow) |
| `/logout <provider>` | Remove runtime API key, stored API key, and OAuth token for a provider locally |
| `/retry <on|off>` | Enable/disable auto-retry |
| `/config [key] [value]` | Show or set a config value (e.g. `tools.maxSteps`) |
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

- discovery paths (`<agent_dir>/extensions/`, `<cwd>/.one/extensions/`, `--extensions <path>`)
- package manager (`one install/remove/update/list`)
- hook contract (`tool.execute.before/after`, `chat.message`, `experimental.session.compacting`, `dispose`)

Project extensions in `.one/extensions/` are not sandboxed — see the trust warning.

## Global install (run `one` from any directory)

The installer is Unix-specific (Linux, macOS). On Windows use `pip install one-agent`
or `pipx install one-agent`.

```bash
chmod +x scripts/install.sh
./scripts/install.sh
```

Installer creates:

- virtualenv in `~/.one/venv`
- launcher in `~/.local/bin/one`
- config in `~/.config/one` (or legacy `~/.one/agent` if already present)

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
- `CONTRIBUTING.md` — development setup, testing, conventions
- `SECURITY.md` — supported versions, reporting, trust boundaries
- `CHANGELOG.md` — release notes
- `CODE_OF_CONDUCT.md` — community guidelines
