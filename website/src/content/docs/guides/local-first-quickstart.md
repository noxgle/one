---
title: Local-first quickstart
---

Install ONE with Python 3.12 or newer:

```sh
python3 -m pip install one-agent
one --version
```

Start an Ollama server separately, with a model available, then choose its actual
model name in place of `MODEL_NAME`:

```sh
one --provider ollama --model MODEL_NAME --ollama-url http://localhost:11434/v1
```

Ask it to explain a small repository. Alternatively, use a local llama.cpp
server with `--provider llama.cpp --model MODEL_NAME --llama-cpp-url http://127.0.0.1:8080`.
These local providers do not require an API key; they still require a running,
compatible server and model. ONE can execute tools with your permissions: use
`--preset readonly --no-extensions --no-mcp` for a more restricted inspection,
not as an OS or network sandbox.

Local state is stored under `~/.config/one` by default, not necessarily inside
the current repository. See [configuration](configuration-security.md),
[CLI reference](../reference/cli.md), and [presets](../library/presets.md).
