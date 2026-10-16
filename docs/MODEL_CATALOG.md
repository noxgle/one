# Model capacity metadata

`ModelInfo.context_window` and API `contextWindow` remain backward-compatible
total-context values.  They are not input budgets.  Additional diagnostics may
include `inputLimit`, `outputLimit`, `effectiveInputBudget`,
`maxContextWindow`, `serverContextWindow`, and metadata source/time.

`context.policy` defaults to `endpoint-max`: the selected total is the endpoint
capacity, constrained by an observed server cap and any smaller manual total.
It reserves up to 16,384 output tokens (and no more than one eighth of a small
window), then compacts/gauges against the remainder. Set
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
immutable public source URL, revision, SHA-256, and UTC fetch timestamp identify
the exact upstream input used for the limited supported entries; it is intentionally
incomplete and unknown models remain unknown. Regenerate it with
`python scripts/generate_model_catalog.py --source <pinned-snapshot>` (or omit
`--source` to fetch the recorded pinned URL). It is not a claim that the live
models.dev API was contacted. An
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
