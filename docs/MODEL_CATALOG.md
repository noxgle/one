# Model capacity metadata

`ModelInfo.context_window` and API `contextWindow` remain backward-compatible
total-context values.  They are not input budgets.  Additional diagnostics may
include `inputLimit`, `outputLimit`, `effectiveInputBudget`,
`maxContextWindow`, `serverContextWindow`, and metadata source/time.

Local catalogue metadata is applied synchronously at session creation, before
background discovery. ChatGPT/Codex can use the OpenAI catalogue's `outputLimit`
for an exact model ID when its own catalogue omits that field. This is an
output-only capability fallback: API input/context limits are never copied onto
Codex. Manual limits and advertised backend output limits take precedence;
unknown IDs retain the conservative generation fallback.

`context.policy` defaults to `endpoint-max`: the selected total is the endpoint
capacity, constrained by an observed server cap and any smaller manual total.
It reserves the known model output limit, or up to 16,384 output tokens when
unknown (at most one eighth of a small window), then compacts/gauges against the remainder. Set
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

This requests a larger per-response generation budget; it is not the model's
advertised `outputLimit`. The shared context resolver clamps it to that limit
and available total context. The reserved output and effective input budget used
by the gauge and compaction reflect the selected generation budget. Without an
explicit setting the known output limit is used; otherwise the fallback is 16384
and retains the conservative small-context reserve (at most one eighth of total).
Known or explicitly configured budgets can reserve more, up to the total context. No total-context
estimate can guarantee backend acceptance when capacity metadata is unknown.

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
