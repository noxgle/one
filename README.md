# one

Autonomous terminal agent written in Python: it executes assigned tasks on its own
(shell / files / code) — plan, run tools, verify, report — and can optionally work
in a **cooperation mode** where a human approves mutating tools, steers or aborts
mid-task, and answers agent questions.

The project started as a re-implementation of the `pi` coding agent and is now an
independent project with its own roadmap — see `TODO.md`.

## Modes

- `--mode text|json` / `-p` — one-shot task execution (prints the stream, JSON for automation)
- interactive — REPL with slash commands, cooperation toggle (Ctrl+A), steer/abort
- `--mode tui` — Textual TUI with live streaming, themes, sidebar
- `--mode rpc` — JSON-RPC over stdin/stdout for external orchestration (sessions, events, extension UI)

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
one --help
```

Run TUI mode (Textual v2):

```bash
one --mode tui
```

Built-in TUI themes:
- `default`
- `light`
- `hacker`
- `solarized`
- `fallout`

Switch theme in TUI:

```text
/theme solarized
```

TUI highlights:
- live response streaming (provider-dependent; OpenAI-compatible/Anthropic/Gemini supported)
- scrollable main stream with scrollbar
- simplified main stream view (`> ...` for user messages)
- tool lifecycle visible in stream (`tool start`, `tool ok/err`)

## Cooperation mode

By default `one` works autonomously. With `--cooperation` (or `/cooperation`, Ctrl+A
in the TUI/interactive mode) it asks before running mutating tools (`bash`, `write`,
`edit`); rejections require a reason that is fed back to the model. Mid-task steering
(`/steer`, `/follow`) and abort (Ctrl+C) work in every interactive mode.

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
| `/login [status|provider [apiKey] [model]]` | Show or configure provider credentials |
| `/logout <provider>` | Remove stored credentials for a provider |
| `/retry <on|off>` | Enable/disable auto-retry |
| `/config [key] [value]` | Show or set a config value (e.g. `tools.maxSteps`) |
| `/extui <list|request|respond|cancel|clear>` | Extension UI control |
| `/cooperation [on|off]` | Toggle cooperation mode (approval gates) |
| `/subagents [on|off]` | Enable/disable subagents (Ctrl+S in TUI) |
| `/bash-show [on|off]` | Show/hide bash command output (only the exit code when off) |
| `/bash <command>` | Run a shell command directly |
| `/exit`, `/quit` | Quit the app |

CLI flags: `--no-subagents` disables subagents, `--no-bash-output` hides bash output (exit code only).

## Global install (run `one` from any directory)

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
one --provider llama.cpp --model local --llama-cpp-url http://192.168.200.38:8089
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
        "url": "http://192.168.200.38:8089",
        "toolParser": [
          { "type": "raw-function-call" },
          { "type": "json" }
        ]
      }
    ]
  }
}
```
