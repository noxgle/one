---
title: Environment reference
---

| Variable | Purpose |
| --- | --- |
| `ONE_CODING_AGENT_DIR` | Overrides the global state directory. |
| `XDG_CONFIG_HOME` | Base for the default `one` state directory when no override is set. |
| `LLAMA_CPP_BASE_URL` | llama.cpp endpoint; default `http://127.0.0.1:8080`. |
| `OLLAMA_BASE_URL` | Ollama endpoint; default `http://localhost:11434/v1`. |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` | Provider credentials, used after runtime and stored credentials. |
| `OPENAI_BASE_URL`, `AZURE_OPENAI_BASE_URL`, `DEEPSEEK_BASE_URL`, `GROQ_BASE_URL`, `MISTRAL_BASE_URL`, `OPENROUTER_BASE_URL`, `XAI_BASE_URL`, `OLLAMA_CLOUD_BASE_URL` | Provider endpoint overrides. |

Provider-specific model configuration can override endpoint defaults. Additional
provider keys are mapped in `one/core/auth_storage.py`; endpoints and defaults
are in `one/providers/registry.py`. This table is a practical subset, not an
exhaustive inventory of environment variables used by dependencies, UI detection,
or diagnostics. See [configuration](../guides/configuration-security.md).
