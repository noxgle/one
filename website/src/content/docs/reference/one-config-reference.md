# ONE — documentation of configuration files: `settings.json` and `models.json`

Sources: the [`noxgle/one`](https://github.com/noxgle/one) repository (`main` branch),
the `one.noxgle.com/docs` documentation portal, and direct inspection of the
source code (`one/core/settings_manager.py`, `one/core/model_registry.py`,
`one/core/types.py`).

---

## 0. Where these files live and how they merge

* **Agent state (global):** `~/.config/one` (or `$XDG_CONFIG_HOME/one`).
  Full override: `ONE_CODING_AGENT_DIR` overrides both locations.
  The state directory contains: `auth.json`, `models.json`, `settings.json`,
  sessions, and reports.
* **Project:** the `.one/` directory in the repo → `.one/settings.json`.

**Merge rule (recursive, from lowest to highest priority):**

```
built-in DEFAULT_SETTINGS  →  global ~/.config/one/settings.json  →  project .one/settings.json
```

* Objects (dicts) merge recursively — sub-keys are merged, not replaced.
* A value that is not an object (e.g. a list, string, number) **replaces** the
  previous value.
* CLI options and runtime choices may override settings for specific operations.
* `settings.json` is **not a complete validation schema** — additional keys
  (normalized/optional) are accepted. Ranges and runtime-derived values are
  validated by the SettingsManager together with its consumers.
* The global file is locked to `0600`, its directory to `0700` (POSIX).
* **Locked file:** if the global `settings.json` is malformed or not an object,
  the manager "locks" it and **refuses to write** until it is repaired.
  The project file is not modified while loading (it is repo content).

**Authentication precedence (credentials):** runtime input → stored `auth.json`
→ provider environment variable. Never commit `.env`/keys.

---

## 1. `settings.json` — the settings inventory

Generated from `DEFAULT_SETTINGS` in `one/core/settings_manager.py`.
`null` does not specify the type of an accepted non-null value. Objects may
accept additional keys beyond this inventory.

```json
{
  "defaultProvider": null,
  "defaultModel": null,
  "defaultThinkingLevel": "medium",
  "context": { "policy": "endpoint-max" },
  "generation": { "maxOutputTokens": 16384 },
  "defaultTemperature": 0.5,
  "enabledModels": [],
  "compaction": {
    "enabled": true,
    "thresholdPercent": 80,
    "recentTokens": 8192,
    "minKeptMessages": 20,
    "summarizeWithModel": true,
    "maxSummaryInputTokens": 20000,
    "experimentalV2": false,
    "strategy": "auto",
    "cacheAware": true,
    "buffer": { "percent": 15, "minTokens": 16000 },
    "summary": { "schema": "v2", "maxOutputTokens": 4000, "toolOutputs": "clipped", "clipChars": 2000, "retryOnInvalid": 1 },
    "constraints": { "enabled": true, "maxTokens": 2000 },
    "failure": { "mode": "keep_history", "latchAfter": 2 },
    "native": { "chatgpt": "auto", "anthropic": "off" },
    "rehydrate": { "enabled": false, "maxFiles": 5, "maxTokens": 12000 },
    "precompute": { "enabled": false }
  },
  "toolOutputPruning": {
    "enabled": false,
    "recentTokens": 8192,
    "minResultTokens": 2048,
    "marker": "Stale tool output pruned from active context. Re-run the tool if details are required."
  },
  "retry": { "enabled": true, "mode": "on", "maxRetries": 3, "baseDelayMs": 1500, "maxDelayMs": 20000 },
  "image": { "autoResize": true, "blockImages": false },
  "sessionDir": null,
  "theme": "hacker",
  "tui": { "infoPanel": "top", "manualBashMode": "strict", "toolResultPreviewLines": 100 },
  "defaultMode": "tui",
  "quietStartup": false,
  "steeringMode": "interrupt",
  "followUpMode": "queue",
  "transport": "http",
  "thinkingBudgets": {},
  "providerProfiles": {},
  "shellCommandPrefix": null,
  "tools": {
    "maxSteps": 100,
    "timeoutSec": 30,
    "approval": false,
    "approvalTools": ["bash", "write", "edit", "plan", "apply_patch"]
  },
  "bash": { "showOutput": false },
  "webConnect": {
    "timeoutSec": 15,
    "maxResponseBytes": 2000000,
    "maxOutputChars": 12000,
    "maxRedirects": 5,
    "extractMainContent": true,
    "preserveLinks": true,
    "preserveCodeBlocks": true
  },
  "subagents": {
    "enabled": true,
    "maxConcurrent": 2,
    "maxDepth": 3,
    "timeoutSec": 1800,
    "idleTimeoutSec": 1800,
    "maxDurationSec": 0
  },
  "askUser": { "timeoutSec": 0 },
  "providers": { "timeoutSec": 300 },
  "packages": [],
  "budget": { "maxTokens": 0, "maxTimeSec": 0 }
}
```

### Field meanings

**Default provider/model**
* `defaultProvider` (string|null) — default provider (e.g. `openai`, `anthropic`,
  `ollama`, `llama.cpp`, `gemini`, `chatgpt`, `openrouter`, `xai`, `deepseek`,
  `mistral`, `groq`, `ollama-cloud`).
* `defaultModel` (string|null) — default model.
* `defaultThinkingLevel` — `off|on|minimal|low|medium|high|xhigh` (default `medium`).
* `defaultTemperature` — float, range `0.0..1.2`, rounded to tenths.
  Presets: `coder` 0.2, `balanced` 0.5, `creative` 0.8, `experimental` 1.2.

**Context and generation**
* `context.policy` — `endpoint-max` (default, uses endpoint capacity) or
  `provider-default` (safe rollback to the provider's advertised default input
  budget).
* `generation.maxOutputTokens` — positive integer; **fallback only** for models
  without a known limit. Default **16384**. It is not the context-window size
  nor a guarantee of backend capacity.

**Compaction**
* `enabled` — enable; `thresholdPercent` (80) — the trigger threshold;
  `recentTokens`, `minKeptMessages`, `summarizeWithModel`, `maxSummaryInputTokens`.
* `strategy` `auto`, `cacheAware`, `buffer.{percent,minTokens}`,
  `summary.{schema,maxOutputTokens,toolOutputs,clipChars,retryOnInvalid}`,
  `constraints.{enabled,maxTokens}`, `native.{chatgpt,anthropic}`,
  `rehydrate.{enabled,maxFiles,maxTokens}` (file reread, opt-in),
  `precompute.enabled`, `failure.{mode,latchAfter}`, `experimentalV2` (opt-in).

**Tool output pruning** — `toolOutputPruning.{enabled,recentTokens,minResultTokens,marker}`.

**Retry** — `retry.{enabled,mode,maxRetries,baseDelayMs,maxDelayMs}`.
An explicit `enabled: 0` disables generic retries.

**Images** — `image.{autoResize,blockImages}`.

**Sessions** — `sessionDir` (string|null) — session directory.

**UI / TUI** — `theme` (default `hacker`), `tui.{infoPanel: top|sidebar,
manualBashMode, toolResultPreviewLines}`, `defaultMode` (`tui`),
`quietStartup`, `steeringMode` (`interrupt`), `followUpMode` (`queue`),
`transport` (`http`).

**thinkingBudgets / providerProfiles** — `thinkingBudgets` (dict, empty),
`providerProfiles` (dict, open-ended by design — per-provider overrides;
e.g. `chatgpt.experimentalStableSessionId`, `chatgpt.promptCache.*`).

**Tools**
* `tools.maxSteps` — `0` = unlimited tool steps.
* `tools.timeoutSec` — tool timeout (30).
* `tools.approval` — require approval (human-in-the-loop).
* `tools.approvalTools` — list of tools requiring approval
  (`bash`, `write`, `edit`, `plan`, `apply_patch`).

**bash** — `bash.showOutput` (default false).

**webConnect** — `webConnect.{timeoutSec,maxResponseBytes,maxOutputChars,
maxRedirects,extractMainContent,preserveLinks,preserveCodeBlocks}`.
`web_connect` works in Default and Readonly without `bash`. Proxy env ignored,
TLS enabled, cookies not forwarded between redirects.

**Subagents**
* `subagents.enabled`, `maxConcurrent`, `maxDepth`, `timeoutSec` (idle alias),
  `idleTimeoutSec` (1800; `0` disables the watchdog), `maxDurationSec` (0 =
  disabled; a positive value stops even an active stream at that limit).

**askUser** — `askUser.timeoutSec` (0).

**providers** — `providers.timeoutSec` (300).

**packages / budget** — `packages` (list, empty), `budget.{maxTokens,maxTimeSec}`
(a shared budget across a prompt and its subagents, opt-in; `0` = disabled).

### How to edit from the UI
* TUI: `/config <path> <value>` (e.g. `/config generation.maxOutputTokens 32768`),
  `/config context.policy provider-default`.
* CLI: `one config generation.maxOutputTokens 65536`.

---

## 2. `models.json` — the model registry

State file: `~/.config/one/models.json` (private file `0600`). Structure:

```json
{
  "providers": {
    "<provider>": [
      {
        "id": "<model-id>",
        "reasoning": true,
        "contextWindow": 128000,
        "url": null,
        "baseUrl": null,
        "toolParser": null,
        "inputImage": false,
        "inputLimit": null,
        "outputLimit": null,
        "effectiveInputBudget": null,
        "maxContextWindow": null,
        "serverContextWindow": null,
        "defaultContextWindow": null,
        "defaultInputBudget": null,
        "pricing": {
          "provider": "<provider>",
          "model": "<model-id>",
          "input": 0.0,
          "output": 0.0,
          "cacheRead": 0.0,
          "cacheWrite": 0.0
        }
      }
    ]
  }
}
```

### Model keys (mapping to `ModelInfo`)

| Key in models.json | ModelInfo field | Type / notes |
|---|---|---|
| `id` | `id` | **required** (if an object has `id`, it is registered). The model identifier. |
| `reasoning` | `reasoning` | bool, default `true`. |
| `contextWindow` | `context_window` | int; only `0 < x <= 10_000_000` (else `None`). Legacy total-context; **not** an input budget. |
| `url` / `baseUrl` | `base_url` | string. `url` takes precedence over `baseUrl`. Overrides the registry URL. |
| `toolParser` | `tool_parser` | list of `{"type": ...}` (e.g. `raw-function-call`, `json`). |
| `inputImage` / `input_image` | `input_image` | bool. **Strict bool** — only `True`/`False`; `1`→True, `0`/string/other→False. |
| `inputLimit` | `input_limit` | int > 0. |
| `outputLimit` | `output_limit` | int > 0. **Highest priority** in the generation-limit selection. |
| `effectiveInputBudget` | `effective_input_budget` | int > 0. |
| `maxContextWindow` | `max_context_window` | int > 0. |
| `serverContextWindow` | `server_context_window` | int > 0. |
| `defaultContextWindow` | `default_context_window` | int > 0. Part of `manual_fields`. |
| `defaultInputBudget` | `default_input_budget` | int > 0. Part of `manual_fields`. |
| `pricing` | `pricing` | `PriceSnapshot` object (see below). |

### `manual_fields` (provenance)
Keys for which a positive number is given go into `manual_fields` (a frozenset of
manually-set fields): `contextWindow`, `inputLimit`, `outputLimit`,
`effectiveInputBudget`, `maxContextWindow`, `serverContextWindow`,
`defaultContextWindow`, `defaultInputBudget`. This lets a catalog refresh remove
a field or an entire model without retaining stale overlays.

### `pricing` (PriceSnapshot)
```json
"pricing": {
  "provider": "<provider>",
  "model": "<model-id>",
  "input": 0.0,
  "output": 0.0,
  "cacheRead": 0.0,
  "cacheWrite": 0.0
}
```
Invalid `pricing` → a registry error (`Invalid pricing for ...`).

### Loading rules and precedence
* **Duplicate** `(provider, id)` pairs are deduplicated; later definitions
  (models.json) override built-in entries, BUT known built-in capabilities win
  over a stale persisted `false` (e.g. input_image).
* **Built-in models** (`BUILTIN_MODELS`) are always available: openai (gpt-4.1,
  gpt-4o), anthropic (claude-3-7/3-5-haiku), gemini (2.5-pro/flash), openrouter,
  ollama-cloud (glm-5, glm-5.3-flash), llama.cpp/ollama (`local`), xai (grok-4),
  deepseek (chat/reasoner), mistral, groq, chatgpt (gpt-5.6/6.1/6.2 family).
* **NO_AUTH_PROVIDERS** = `{"llama.cpp", "ollama"}` — no key required.
* **Placeholder keys** (`your_...`, `changeme`, `sk-xxx`, `none`, `null`,
  `placeholder`, `enter`, `insert`, `xxx`, `<...>`) do **not** count as
  configured auth → providers like openai will not become "available" before
  working local models.
* **File validation:**
  * malformed JSON → error + file **locked** (write refused);
  * `providers` not a dict → error;
  * `providers.<p>` not a list → error;
  * valid JSON but not a dict → error + lock.
* `outputLimit` from models.json **wins** over the catalogue and over
  `generation.maxOutputTokens`.

### Generation-limit selection order (output budget)
1. `models.json` `outputLimit` for the exact provider/model.
2. Model catalogue `outputLimit` (cache or bundled snapshot).
3. `settings.json` `generation.maxOutputTokens` (default 16384).

### Capacity catalog (model_catalog)
* A compact, sanitized capacity snapshot ships in `one/assets/model_catalog.json`.
  Source: `https://models.dev/api.json` (SHA-256 + timestamp).
* Regenerate: `python scripts/generate_model_catalog.py`
  (or `--source <pinned-snapshot>`).
* Optional background refresh uses a 5-minute cache in the state directory; it
  does **not** send prompts or keys. Cache/snapshot/provider facts never
  overwrite explicit `models.json` limits.
* `context.policy=endpoint-max` reserves up to 16384 output tokens; unknown
  capacities use a 128k estimate for compaction only.

### ChatGPT/Codex (gpt-6.2-sol/astra)
* Advertise a ~1.05M window (1_050_000); accept `xhigh`; reject
  `temperature`/`top_p` (ONE omits those for that family).

---

## 3. Example configurations

### A. Local Ollama model (no key)
`settings.json`:
```json
{
  "defaultProvider": "ollama",
  "defaultModel": "local",
  "defaultThinkingLevel": "medium"
}
```
`models.json`:
```json
{
  "providers": {
    "ollama": [
      { "id": "local", "reasoning": false, "contextWindow": 32768 }
    ]
  }
}
```
Run:
```bash
one --provider ollama --model local --ollama-url http://localhost:11434/v1
```

### B. Local llama.cpp model (no key)
```json
// settings.json
{ "defaultProvider": "llama.cpp", "defaultModel": "local" }

// models.json
{ "providers": { "llama.cpp": [ { "id": "local", "reasoning": false, "contextWindow": 32768 } ] } }
```
```bash
LLAMA_CPP_BASE_URL=http://127.0.0.1:8080 one --provider llama.cpp --model local
```

### C. Keyed provider (OpenAI-compatible) + limit override
```json
// models.json
{
  "providers": {
    "openai": [
      {
        "id": "gpt-4.1",
        "reasoning": true,
        "contextWindow": 1000000,
        "inputImage": true,
        "outputLimit": 64000,
        "pricing": {
          "provider": "openai",
          "model": "gpt-4.1",
          "input": 0.004,
          "output": 0.016,
          "cacheRead": 0.005,
          "cacheWrite": 0.02
        }
      }
    ]
  }
}
```
Key via `auth.json` or `OPENAI_API_KEY`.

### D. Override a limit for a known model
```json
{ "providers": { "chatgpt": [ { "id": "gpt-6.1-sol", "outputLimit": 64000 } ] } }
```

---

## 4. Caveats and safety

* `settings.json` is **not a complete schema** — additional keys are accepted;
  ranges/runtime values are validated by the SettingsManager.
* A malformed global file **blocks writes** until repaired.
* `contextWindow` is legacy total-context, **not** an input budget.
* `generation.maxOutputTokens` is only a fallback; it does not guarantee
  capacity.
* `inputImage` is a strict bool — `"true"` (string) is treated as `false`.
* `models.json` `outputLimit` has the highest priority in generation.
* Never commit `auth.json`/`.env`/keys; state is private, not encrypted.
* Presets and MCP run with your permissions; for an untrusted repo use
  `one --no-extensions --no-mcp`; `--preset readonly` restricts ONE-mediated
  actions but is **not** an OS/network sandbox.
