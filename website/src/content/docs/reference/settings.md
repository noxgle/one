---
title: "Settings reference"
---

Generated from `DEFAULT_SETTINGS` in `one/core/settings_manager.py`.
The JSON values give declared defaults and their JSON types; `null` does not
specify the type of an accepted non-null value. Objects may accept additional
keys not present in this inventory, including normalized/optional settings.
This is not a complete validation schema: accepted ranges, merge exceptions,
and runtime-derived values require the settings manager and their consumers.
See [configuration and security](../guides/configuration-security.md).

```json
{
  "defaultProvider": null,
  "defaultModel": null,
  "defaultThinkingLevel": "medium",
  "context": {
    "policy": "endpoint-max"
  },
  "generation": {
    "maxOutputTokens": 16384
  },
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
    "buffer": {
      "percent": 15,
      "minTokens": 16000
    },
    "summary": {
      "schema": "v2",
      "maxOutputTokens": 4000,
      "toolOutputs": "clipped",
      "clipChars": 2000,
      "retryOnInvalid": 1
    },
    "constraints": {
      "enabled": true,
      "maxTokens": 2000
    },
    "failure": {
      "mode": "keep_history",
      "latchAfter": 2
    },
    "native": {
      "chatgpt": "auto",
      "anthropic": "off"
    },
    "rehydrate": {
      "enabled": false,
      "maxFiles": 5,
      "maxTokens": 12000
    },
    "precompute": {
      "enabled": false
    }
  },
  "toolOutputPruning": {
    "enabled": false,
    "recentTokens": 8192,
    "minResultTokens": 2048,
    "marker": "Stale tool output pruned from active context. Re-run the tool if details are required."
  },
  "retry": {
    "enabled": true,
    "mode": "on",
    "maxRetries": 3,
    "baseDelayMs": 1500,
    "maxDelayMs": 20000
  },
  "image": {
    "autoResize": true,
    "blockImages": false
  },
  "sessionDir": null,
  "theme": "hacker",
  "tui": {
    "infoPanel": "top",
    "manualBashMode": "strict",
    "toolResultPreviewLines": 100
  },
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
    "approvalTools": [
      "bash",
      "write",
      "edit",
      "plan",
      "apply_patch"
    ]
  },
  "bash": {
    "showOutput": false
  },
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
  "askUser": {
    "timeoutSec": 0
  },
  "providers": {
    "timeoutSec": 300
  },
  "packages": [],
  "budget": {
    "maxTokens": 0,
    "maxTimeSec": 0
  }
}
```
