# one

Python implementation of the `one` coding agent, based on `pi` coding-agent, focused on CLI and JSON-RPC.

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
