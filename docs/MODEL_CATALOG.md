# Model capacity metadata

`ModelInfo.context_window` and API `contextWindow` remain backward-compatible
total-context values.  They are not input budgets.  Additional diagnostics may
include `inputLimit`, `outputLimit`, `effectiveInputBudget`,
`maxContextWindow`, `serverContextWindow`, and metadata source/time.

Local catalogue metadata is applied synchronously at session creation, before
background discovery. ChatGPT/Codex can use the OpenAI catalogue's `outputLimit`
for an exact model ID when its own catalogue omits that field. This is an
output-only capability fallback: API input/context limits are never copied onto
Codex. Explicit `models.json` output limits take precedence over the catalogue;
unknown IDs use the shared settings fallback. Endpoint discovery does not
supersede a manual or catalogue output limit.

`context.policy` defaults to `endpoint-max`: the selected total is the endpoint
capacity, constrained by an observed server cap and any smaller manual total.
It reserves up to 16,384 output tokens, at most one eighth of the total window
and no more than the effective output limit, then compacts/gauges against the remainder. Set
`context.policy` to `provider-default` for a safe rollback to the provider's
advertised default input budget. Explicit smaller manual context/input/effective
input budgets and known input limits always remain hard constraints.

For example, an endpoint total of 872,000 with a 16,384 reserve has an 855,616
input budget; 11,169 request tokens are about 1.31% of that budget. Larger
windows can increase latency and cost. Unknown capacity uses an internal 128k
estimate for compaction only and remains unknown in `contextWindow` output.
Diagnostics expose both budget `percent` and `contextPercent` (actual selected
total), plus policy, reserve, reason, and constraints. Catalogue maxima are
labelled as catalogue metadata, not proof of a live server capacity.

A compact, sanitized capability snapshot ships with the package. Its embedded
public source URL (`https://models.dev/api.json`), SHA-256, and UTC fetch timestamp
identify the exact upstream input used for the limited supported entries; it is
intentionally incomplete and unknown models remain unknown. Regenerate it with
`python scripts/generate_model_catalog.py` (fetches the live `models.dev` API) or
`python scripts/generate_model_catalog.py --source <pinned-snapshot>` to use a
local raw snapshot file. An
optional background refresh uses a five-minute cache in the agent state
directory, a finite timeout, schema/size/entry validation, and atomic private
writes.  It never sends prompts or credentials.  Runtime bootstrap enables it
for normal CLI/TUI startup; embedding/tests can leave `catalogRefresh` false
(the default) or inject a fetcher, so no network access is forced.  Cache,
snapshot, and provider facts never overwrite explicit `models.json` limits.
Each catalog-applied field records its original baseline, so an authoritative
refresh can remove a field or an entire model without retaining a stale overlay.
The last successful cache is authoritative (including removals); the bundled
snapshot is used only before a cache exists or when refresh fails.

## Generation limit and truncated responses

`generation.maxOutputTokens` is an optional positive integer. For example:

```sh
one config generation.maxOutputTokens 65536
```

This setting is only a fallback. For every model the outgoing output budget is
selected in this order, using positive integers only:

1. `models.json` `outputLimit` for the exact provider/model.
2. Model catalogue `outputLimit` for that identity (cache or bundled snapshot).
3. `settings.json` `generation.maxOutputTokens` (default 16384).

For example, a catalogue limit of 128000 wins over a settings fallback of 8192.
A manual limit of 64000 wins over both. To override a known model, use its entry
in `models.json`, not the global generation fallback:

```json
{"providers":{"chatgpt":[{"id":"gpt-6.1-sol","outputLimit":64000}]}}
```

The loader and model persistence support `outputLimit`; existing explicit values
are preserved. No private settings or historical sessions are migrated. Global
and project settings retain their existing precedence for the fallback.
The compaction reserve is independent of the outgoing limit; it does not cap
generation. Outgoing generation is clamped to available total context and any
independently verified server output hard cap. An endpoint's advertised output
capability is not by itself proof of such a hard cap, and does not override the
selection order above. No current adapter supplies a verified output hard cap.

`/stats` → `contextUsage` exposes `selectedOutputLimit`, `outputLimitSource`
(`models.json`, `catalog`, or `settings`), `outgoingMaxTokens`, and
`outputLimitReason`. `lastRequest` separately records the last limit passed to
the adapter for the currently selected provider/model, not a recalculation from
current settings or the reply's token usage. It is runtime-only and is unknown
until a new request is made after resume. These diagnostics do not prove server
acceptance or a backend's actual capacity.

Reasoning tokens can consume the output budget even when visible text is short.
Larger limits may increase latency and subscription/API consumption. Lower thinking
or smaller tasks can also help. Changes apply to subsequent requests; no private
settings are modified by the implementation.

ChatGPT `max_output_tokens` completion preserves partial text and usage. Durable
assistant `outputTruncation` metadata records the reason, actual requested limit
and reported output tokens. An additive `warning` event with
`warning: output_truncated` carries the same diagnostics. TUI displays the warning;
the saved response also includes an actionable notice. `turn_end.ok` is false with
reason `max_output_tokens`, not a successful complete answer. No automatic retry,
format repair, nudge or tools from that response run. Even finalized native calls
are discarded conservatively. Other incomplete reasons still raise errors.
