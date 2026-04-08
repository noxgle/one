# one

Python implementation of the `one` coding agent, based on `pi` coding-agent, focused on CLI and JSON-RPC.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
one --help
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
        "url": "http://192.168.200.38:8089"
      }
    ]
  }
}
```
