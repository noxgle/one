# Runs, budgets and diagnostics

A run is one accepted root prompt and its entire agent tree. Tool iterations,
retries, repairs, automatic compaction, steering and automatically drained
follow-ups share the run and its ledger. A question and its answer do not start
a new run. A subsequent independent prompt starts a new run; an explicit manual
compaction is a separate run. A terminal finish, error or cancellation does not
automatically drain remaining queued messages.

Compaction provider attempts, including V2 validation repairs and cache-aware
fallbacks, are admitted and accounted under `byPurpose.compaction` in that run;
do not count them again. Missing provider usage remains unknown. Admission and
cancellation still apply, and V2 keeps the active history on a failed commit.
See [compaction](COMPACTION.md) for the legacy failure behavior and offline-only
native gate.

## Shared limits

`runBudget` is opt-in, shared by the root and all descendants, and frozen at run
start. Changes apply to the next run. In global or project `settings.json`:

```json
{
  "runBudget": {
    "maxTokens": 2000000,
    "maxTimeSec": 300,
    "maxCostUsd": 10,
    "maxActiveSubagents": 4
  },
  "generation": {"maxOutputTokens": 4096}
}
```

An absent field or zero means unlimited for that dimension. Tokens, seconds and
active subagents must be non-negative integers; cost must be a finite,
non-negative number. Booleans, nulls and unknown fields are rejected. The
existing `budget.maxTokens` and `budget.maxTimeSec` remain separate legacy
session-based limits, checked between steps using session statistics and time;
they are not the shared run ledger. Local tool and subagent timeouts also still
apply.

Before a model request, ONE reserves capacity against the shared limits; it can
refuse a request if input capacity, an enforceable output bound, or suitable
pricing is unknown. Subagent admission is also bounded across the whole tree;
a spawn or batch that cannot fit is refused rather than waiting for slots held
by ancestors and deadlocking. Deadline, budget exhaustion and whole-run
cancellation stop admission and signal the tree; subtree cancellation leaves
siblings alone. Cleanup is bounded, and an incomplete cleanup can block new
work because external state is unknown.

### Pricing for a cost limit

Cost admission requires explicit pricing bound to the registered provider,
model and endpoint. `models.json` in the agent configuration directory can
contain the following **synthetic schema example, not production prices**:

```json
{
  "providers": {
    "openai": [{
      "id": "gpt-4.1",
      "pricing": {
        "schemaVersion": 1,
        "version": "example-offline-v1",
        "source": "synthetic-example-not-for-production",
        "currency": "USD",
        "unit": "perMillionTokens",
        "cacheMode": "read_write",
        "input": "2",
        "output": "4",
        "cacheRead": "1",
        "cacheWrite": "3"
      }
    }]
  }
}
```

Replace the example with verified rates, source and version for the actual
endpoint and model, including applicable context tiers and cache storage times.
There is no automatic pricing update or built-in fallback. Rates are non-negative
finite decimal strings or integers, not floating-point numbers. `cacheMode` is
`none`, `read` or `read_write`; `none` forbids cache rates and `read` forbids
`cacheWrite`. Missing rates for enabled cache categories mean unknown, not
zero, and prevent safe cost-limited admission. Unknown pricing fields are
rejected. Prices are snapshotted for requests; changing endpoint identity does
not silently reuse another endpoint's price.

Usage and USD amounts are application estimates, **not a provider billing
limit or guarantee**. Partial or missing usage, interrupted requests, provider
semantics, token estimation and incorrect prices can leave usage unknown or
unbounded. The UI distinguishes used, reserved and uncertain bounds; zero
reported usage is not proof of zero cost. The provider login authentication
probe is a separate operation outside prompt runs and `runBudget`; model
catalog/discovery and token-counting operations are separate too and may incur
provider charges.

Cancellation and budgets are application controls, not an OS sandbox. They do
not guarantee remote operations have stopped, undo writes or roll back external
effects. Inspect external state before retrying.

## Resume and inspection

Saved sessions have run-history sidecars. Resume or fork does not restart live
work or restore an active budget pool: interrupted historical work remains
historical, and the next accepted prompt gets a new run. Forks have their own
history. A crash can leave historical nodes marked interrupted; their recorded
usage and uncertain reservations are not proof of final provider charges.

- In the TUI, `/agents` opens the direct-child monitor; press **T** there for
  the current or most recently completed run tree. Use R to refresh, N/P for
  pages, C for the selected subtree or W for the whole run, then Y to confirm.
  The tree is a diagnostic view, not a historical-run selector.
- `one run` prints a run summary; `one run --json` includes a `run` object.
  JSON print mode also includes `run` alongside messages. These are bounded
  root pages, not a full archive dump.
- RPC offers `run_tree_page` (`offset`, `limit`), `cancel_run` (`runId`) and
  `cancel_run_subtree` (`runId`, `nodeId`); `get_state` includes a root run
  page. Cancellation reports whether the request was accepted, not that
  external effects were reversed.
- Python SDK helpers in `one.core.sdk` are `run_tree_page(session, offset=0,
  limit=100)`, `run_history_page(session, run_id, offset=0, limit=100)` and
  `cancel_run(session, run_id, node_id=None)`. History paging reads persisted
  snapshots without reviving execution; the live tree helper returns the current
  or most recently completed run. Pages are limited to 100 nodes.
