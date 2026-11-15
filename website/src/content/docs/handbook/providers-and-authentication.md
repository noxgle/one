---
title: "Providers and authentication"
---

Supported backends include OpenAI-compatible providers, Anthropic, Gemini,
llama.cpp, Ollama, and ChatGPT/Codex. Credential precedence is runtime input,
then stored `auth.json`, then the provider environment variable. Common
variables include `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and `GEMINI_API_KEY`.

### Thinking levels

`/thinking <off|on|minimal|low|medium|high|xhigh>` and `/thinking-cycle` save the
selected level as the default for future sessions; a loaded session continues
to use its recorded level. Models without reasoning capability remain at
`off`. `off` omits enabled reasoning where supported. OpenAI-compatible and
Codex APIs use their available effort enums (their highest/lowest available
effort may be used for `xhigh`/`minimal`); Anthropic and Gemini 2.5 use
deterministic thinking-token budgets. Provider and model API restrictions still
determine which controls a request accepts.

### Temperature

Temperature defaults to **balanced (0.5)**. Presets are `coder` (0.2),
`balanced` (0.5), `creative` (0.8), and `experimental` (1.2); numeric values
are rounded to tenths and bounded to `0.0..1.2`. The TUI shows the nearest mode
and exact value and changes it with **Ctrl+Left/Right** or
`/temperature <value|mode>`. Startup accepts `--temperature` and
`--temperature-mode`; numeric wins when both are supplied. `get_state` exposes
temperature through RPC, and `set_temperature` accepts a numeric `temperature`
or preset `mode`. Subagents inherit their parent's effective value unless their
`spawn_subagent` call supplies `temperature` (which wins) or `temperatureMode`.
Anthropic and Codex omit temperature and emit a warning; requests still run.

OpenRouter uses its native `reasoning: {"effort": ...}` request dialect and
streams reasoning separately. Ollama Cloud uses native `think`; `minimal` and
`low` map to `"low"`, `medium` to `"medium"`, `high` and `xhigh` to `"high"`,
and `off` to `false`. ChatGPT/Codex Responses reasoning summaries
also render separately from answer text. Local Ollama and llama.cpp retain their
OpenAI-compatible/local behavior.

For local models, no key is required:

In the TUI, press **Ctrl+K**, then choose **Configure Ollama** or
**Configure llama.cpp**. Selecting an unconfigured `local` placeholder also opens
setup. Enter the server URL, optionally click **Fetch server models** to
list models already available on that server, then select a model or enter its
exact ID manually. **Save & use** writes the model URL to global `models.json`,
saves provider/model defaults in global `settings.json`, and activates the model.
The dialog previews both paths and respects `ONE_CODING_AGENT_DIR` and XDG.
To skip discovery, press Enter in the URL field to focus **Manual model ID**,
type the exact model ID, and press Enter again to save. The form scrolls in
compact terminals while Save/Cancel and progress/error feedback remain visible.

Run Ollama or `llama-server` with a loaded model separately; ONE does not install
servers or download model weights. Opening the dialog makes no network request,
and saving a manually entered ID does not verify server/model availability.
From a container, use an address that reaches the host rather than container
localhost; see [Docker](../library/docker.md). Existing unrelated configuration is
preserved; repair malformed files or conflicting project defaults before saving.
The two files are written individually, not as a cross-file transaction; a
partial save or activation failure is reported so you can review and retry.
`one doctor --setup` remains an offline alternative for selecting defaults.

```bash
LLAMA_CPP_BASE_URL=http://127.0.0.1:8080 one --provider llama.cpp --model local
one --provider ollama --model local --ollama-url http://localhost:11434/v1
```

`/login` validates credentials before storing them and refreshes available
models. Anthropic and ChatGPT/Codex also support subscription OAuth login; see
[Subscription login](providers-and-authentication.md#subscription-login-oauth). Provider-controlled OAuth
endpoints can change without notice.

### Subscription login (OAuth)

- `/login anthropic subscription` starts Anthropic OAuth. `/login refresh anthropic`
  refreshes models; `/logout anthropic` removes local stored credentials.
- `/login chatgpt subscription` starts ChatGPT/Codex OAuth. Seed Codex models
  are available before a live model list is fetched; `/login refresh chatgpt`
  refreshes it.

`/logout <provider>` removes one-managed runtime/stored credentials locally. It
does not revoke a provider account token or remove environment credentials.
