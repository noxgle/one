# Compaction

The legacy baseline uses the existing local summary strategy. Automatic
compaction can be requested before a provider call
(`auto_preflight`), after a turn (`auto`), or once after a context-limit error
(`context_limit_retry`). Manual compaction is explicit and can summarize even a
short history; in that case the raw history is retained. Otherwise the recent
window is retained and a synthetic summary is placed at index zero. Repeated
compactions include the previous summary in the next summarization request.
Raw session history and evidence are not deleted.

Automatic compaction belongs to the current run; explicit manual compaction
starts a separate run. The summarizer uses `_invoke_provider` with purpose
`compaction`, so provider admission and usage accounting already apply. Do not
charge that usage again as a separate run. Admission errors propagate; task
cancellation is not converted into a successful summary. See [Runs](RUNS.md)
for the run and cancellation contracts. Ordinary summarizer exceptions or an
empty response currently result in a placeholder summary, and the compaction
still completes; this is legacy behavior, not a guarantee of a useful summary.

`firstKeptEntryId` maps the live retention boundary to a message-producing
entry on the session branch. `get_message_entry_ids()` enumerates messages,
custom messages and branch summaries in branch order; compaction entries are
not themselves message-producing entries. The live synthetic summary at index
zero accounts for the offset on subsequent compactions. Resume reconstructs
the latest summary and retained entries from that boundary, followed by newer
entries. Forks preserve the selected branch history and reconstruct from it.

The TUI host forwards the compact operation's optional `instructions` through
the runtime boundary to `session.compact`. In legacy mode this is an explicit
replacement summary; in V2 it is a bounded hint to checkpoint generation.
The legacy `compaction.last.summary` statistic is used by existing consumers;
it contains session content and must not be treated as sanitized telemetry.
Any new metrics should contain only bounded, non-content metadata.

## P0 settings and telemetry

The normalized `compaction` settings expose `experimentalV2` (default false),
`strategy`, `cacheAware`, and the `buffer`, `summary`, `constraints`, `failure`,
`native`, `rehydrate`, and `precompute` sections through explicit getters.
With `experimentalV2` false, legacy behavior remains in force. With it true,
the retention, structured checkpoint and failure policy below is active.
Cache-aware local summary, bounded rehydration and an offline native implementation
are present; precompute remains future work. Production native
capability remains disabled because the ChatGPT backend contract is unverified.

## Rehydration and recovery

V2 preserves the constraint ledger and active plan as bounded derivative data;
if required blocks cannot fit, compaction fails rather than truncating them.
Optional file reread is off by default. When enabled it is capped at five files,
12,000 estimated tokens and the remaining request budget, regardless of larger
settings. Only exact relative paths from a validated checkpoint are considered;
read policy, workspace boundaries, no-follow opens, secret-name exclusions,
regular-file and size checks apply. Unsupported platforms fail closed. Missing,
denied, changed or oversized files are skipped. Reread text is untrusted data,
not instructions. It can be sent to the provider; do not enable it for files
you cannot send. Private rehydration state includes paths and file text in the
session store, although public entry DTOs and JSONL export omit that state.
Protect session files and backups. Provider projection HTML-escapes file-context
text inside an untrusted-tool-output wrapper; escaping is not a prompt-injection
guarantee. Other derivative blocks are not described by that wrapper.

For rollback, preserve a private copy of the original session first. Disable V2
and native settings before resuming with the current compatible version; do not
assume this removes an existing checkpoint from the reconstructed context. For
the full canonical branch, `SessionManager.build_session_context(raw_messages=True)`
reconstructs raw messages without compaction summaries or derivative blocks;
this is an API, not a user-facing downgrade command. A model switch or invalid
native binding also causes native replay to fall back to raw history. Raw history
may exceed the model budget and needs deliberate handling. For an older version,
keep the original and use a compatible version to inspect or manually transfer
the needed raw conversation into a new session; there is no guaranteed automatic
downgrade or lossless private-state export.

## P4 native Responses contract and compatibility gate

The official [OpenAI compaction guide](https://developers.openai.com/api/docs/guides/compaction)
(accessed 2026-11-12) describes server-side compaction configured with
`context_management` and `compact_threshold`. In stateless input-array chaining,
all output items are carried forward in order, including compaction items;
`store=false` avoids reliance on stored Responses state. The separate standalone
compact endpoint instead returns the canonical whole window to use for subsequent
input, not an item to append to the old window. ONE does not implement standalone
compaction. These public API instructions do not establish subscription backend
behavior or its data-retention guarantees.

ONE's ChatGPT subscription adapter actually uses
`https://chatgpt.com/backend-api/codex/responses`. Native support for that endpoint
and its models, native usage and pricing, and any zero-data-retention guarantees
are **unverified**. `supports_native_compaction_for` returns false for all
production adapters and settings cannot bypass that capability gate. Production
activation remains blocked; this is not a native rollout. The implementation has
been exercised offline with synthetic fixtures and fake providers only, not a live
backend. Local compaction remains the fallback.

The gated implementation sends `store=false` and no `previous_response_id`,
retains ordered output items, and commits a private replay window only after a
successful response and persisted assistant message. The window is bound to the
provider, model, endpoint, effective-context fingerprint and canonical branch
prefix. A changed binding, branch, or rollback cannot reuse it: the local view is
rebuilt from raw canonical history. Raw history is retained. Private native items
are stored in the session JSONL; public entry DTOs and JSONL export redact their
contents. Session files and ordinary exported conversation content remain sensitive.
An exported JSONL cannot restore the redacted private native window; replay must
fall back to raw canonical history rather than resume that window. Older ONE
versions cannot be assumed to understand newer derivative entry types or fully
reconstruct their context. Keep the original session and use a compatible version
when resuming or forking; do not treat an export as a private-window backup.

A native protocol failure or HTTP 400/422 can permit at most one canonical local
retry, subject to normal admission and budgets and only before tool side effects;
other failures do not grant this retry. This prevents duplicate tool execution.
Preflight counts the serialized actual private payload, including opaque items and
tools, with a conservative byte-based estimate rather than claiming exact tokens.
For native requests stream text is buffered until successful completion instead of
being shown incrementally; progress may therefore appear delayed.

## Experimental cache-aware local summary (P3)

With V2 enabled, `cacheAware` enabled and strategy `auto` or
`local_cache_aware`, eligible adapters can reuse a detached snapshot of the
normal request prefix, appending the checkpoint instruction. Tool definitions
remain in the request, while tool calls are disabled and returned tool calls
are rejected. The captured tool definitions are fingerprinted: a change during
compaction aborts the commit rather than publishing a stale checkpoint.

| Adapter / endpoint | Normal V2 summary position | Local summary strategy |
| --- | --- | --- |
| OpenAI official and recognized Azure OpenAI endpoints | Prefix | Eligible for `local_cache_aware` |
| Anthropic, Gemini | Prefix | Eligible for `local_cache_aware` |
| Codex Responses | Suffix | `local_separate` |
| Ollama, custom/proxy and other unverified endpoints | Prefix (adapter default) | `local_separate` |

Eligibility is an application payload capability, not a live endpoint or model
cache guarantee. Codex's suffix is only a normal V2 provider payload projection:
canonical history, entry IDs and rolling summary remain unchanged. With V2 off,
the legacy projection is unchanged. Images or extension compaction context
force a separate request; an oversized cached prefix also falls back to a
separate request, which must itself fit. A typed HTTP 400/422 cache-contract
rejection triggers a bounded separate retry; other provider failures are not
treated as contract rejections. Admission and cancellation remain authoritative.

An exact matching prefix is not proof of a cache hit: provider policy, model,
minimum sizes and changing inputs can affect caching. Provider-reported usage is
accounted per physical attempt under `byPurpose.compaction`, including fallback;
missing usage stays unknown. No live cache benchmark was authorized or is
required for this adapted P3 acceptance; no cache ratio is claimed. Later T011
tracks offline evaluation and separately authorized live measurement.

Focused contracts include `test_normal_wire_projection_and_legacy_bytes`,
`test_cache_prefix_and_no_tool_dispatch`, `test_mcp_schema_change_aborts_without_commit`,
`test_openai_rejection_falls_back_without_leaking_body`,
`test_cached_size_fallback_async`, and the adapter payload and cache telemetry
tests. Provider-request diagnostics are content-free; the existing explicit
compaction end result and legacy `last.summary` can contain summary content.

## Experimental V2 retention and budget policy

V2 defaults to a 15,000-token recent-tail target and an eight-message floor;
explicit `recentTokens` and `minKeptMessages` overrides take precedence. The
latest user exchange and the floor are mandatory, as are native tool replay
pairs crossing the boundary. These priorities can exceed the soft tail target;
a preceding user boundary can expand the tail up to 115% of that target if it
fits. The tail shrinks to available capacity but never sacrifices mandatory
content. Active plans are maintained separately, not duplicated in the tail,
and restored from durable plan messages on resume.

Preflight and after-turn auto compaction share the threshold: the smaller of
`thresholdPercent` of the input budget and the input budget minus the larger
of `buffer.minTokens` and `ceil(window * buffer.percent / 100)`. Unknown
window uses the input budget; if the buffered threshold is nonpositive the
percentage threshold is used. Context-limit recovery uses the same retention
policy and at most one recovery attempt.

Planning reserves fixed request overhead and `summary.maxOutputTokens` for the
prospective summary. The complete resulting candidate is counted when an exact
provider counter is available, otherwise conservatively estimated, and checked
against the input budget before commit. The separate summarizer request is also
checked in its actual provider shape, including the previous summary, fixed
instructions and native tool definitions; an oversized request is not sent.
If retention or the resulting candidate cannot fit, compaction skips with
`skipReason: no_safe_reduction`. An oversized summary request instead produces
`failureReason: oversize`. Neither changes live history or installs a
placeholder. Estimates are not tokenizer guarantees.

Completed records optionally contain nonnegative finite `tokensAfter`,
`tokensKept`, `durationMs`, and `attempts`; bounded `strategy` and
`summarySchema` (`legacy` or `v2`); and boolean `fallback`. The boolean means
that a fallback was used, not which one: current legacy placeholder is true,
normal summary false. Missing means unknown. Strategy identifies the attempted
strategy; failure records separately identify failure and do not imply a
completed strategy succeeded. Constraints contain only numeric `extracted`,
`preserved`, `dropped`; rehydration only `files`, `tokens`; usage only
`inputTokens`, `cachedTokens`, `outputTokens`, `reasoningTokens`. Unknown fields
and invalid numbers are discarded. No content belongs in these metadata fields.

Missing usage is null, not zero; aggregate cache coverage counts missing
requests separately and ratios are null when their denominator is unavailable.
Compaction usage is diagnostic and already included in run accounting: do not
add it to run totals again. Legacy `last.summary` is an explicit content-bearing
exception to the sanitized metrics contract.

## Experimental V2 checkpoints and failures

V2 validates structured checkpoints and a verbatim, provenance-bearing ledger
extracted heuristically from user messages. Invalid output gets at most one
repair request, then a validated deterministic extractive fallback. A failed
fallback, provider error, timeout, ledger overflow or storage error preserves
the live history; V2 never substitutes the legacy placeholder. Manual
instructions are hints, not replacement summaries. Summary input bounds the
previous checkpoint and conversation; model-generated tool calls are rejected.

A candidate is checked against the captured messages, entry IDs, settings,
model and preset, and against abort/run cancellation before transactional disk
commit and live publication. Concurrent compaction is refused. Failed and skip
metadata writes are best effort and transactional; failure to write them does
not change the branch. Started operations emit one end event on these paths.
Admission and cancellation propagate without a paid repair. Two consecutive
failures latch automatic compaction by default; manual attempts bypass the
latch but can increment it. A successful compaction or new user message resets
it. This latch is in-memory, not durable across restart.

The synthetic bilingual tests assert at least 95% extraction recall and rolling
verbatim retention separately on 14 directive patterns and 24 concrete
English/Polish directives, including exact provenance assertions. These are
finite probes, not a guarantee for arbitrary natural language or model output.

## Baseline test map

The six synthetic transcripts and independently frozen prompt/context goldens
are exercised by `test_synthetic_legacy_summary_contract` in
`tests/test_compaction.py`. Medium and large use deterministic descriptors;
large crosses the 80,000-character summary-input limit. Context projections
omit volatile timestamps and token counters. The tool-heavy case retains a
native call/result pair, and the plan is persisted as a custom message.
`tests/test_provider_payloads.py::test_compacted_native_replay_payload_golden`
freezes the corresponding OpenAI-compatible wire payload.

Existing tests cover the remaining baseline paths:

- `test_preflight_compacts_before_oversized_provider_request`,
  `test_auto_compaction_triggers_above_threshold`, and
  `test_retry_context_limit_triggers_compaction` cover the three auto triggers.
- `test_manual_compact_summarizes_short_history_and_persists` covers manual
  compaction; `test_compact_summarize_rolling_with_previous_summary` covers
  rolling summaries; `test_summary_failure_is_recorded_without_diagnostics`
  covers error and empty-response placeholders.
- `tests/test_session_manager.py::test_compaction_telemetry_disk_reload_and_fork`
  checks reconstructed context after both disk reload and fork.

## P2 gate status

P2 gate closed. Full pytest run: 3393 passed, 2 skipped, 771.83s; `ruff check .` and `git diff --check` clean. T004 (structured checkpoint + ledger) and T005 (atomic commit + failure latch) implementation and targeted tests complete. The synthetic bilingual tests (`tests/test_compaction_constraints.py`) assert ≥95% extraction recall and rolling verbatim retention on 14 directive patterns and 24 concrete English/Polish directives, including exact provenance assertions — these are finite probes, not a production guarantee for arbitrary natural language or model output. Reviewer provider error 400 blocked automated review; parent diff/manual review performed. No version bump, no commits.

## P3 gate status

P3 gate closed. Full pytest run: 3418 passed, 2 skipped, 857.41s; `ruff check .` and `git diff --check` clean. T006 (strategies and cache-aware payload projection) implementation and targeted tests complete. Independent read-only senior review identified 5 findings — all corrected: typed status, Anthropic breakpoint, images fallback, endpoint gate, tools fingerprint. No live A/B done; cache ratio not guaranteed. No commits, no version change.

## P4 gate status

P4 offline implementation complete and gate closed. Full pytest run: 3479 passed, 2 skipped, 795.59s; `ruff check .` and `git diff --check` clean. The ChatGPT backend remains unverified, production capability is disabled, and no live rollout was performed.

## P5 gate status

P5 implementation, offline benchmark/report and offline release preparation are complete. The parent full gate passed: 3506 passed, 2 skipped in 747.71s, followed by clean `ruff check .` and `git diff --check`. A prior full attempt had an unrelated `test_sidebar_git_probe` timing failure (0.47s against 0.2s); it passed in isolation (0.77s), then the full gate passed. Live validation and the pilot remain blocked without authorization; production goals remain unproven. Neither default enablement nor native activation follows from the offline gate.

## T011 offline synthetic benchmark

Run from the repository root: `ONE_CODING_AGENT_DIR="$(mktemp -d)" .venv/bin/python scripts/bench_compaction.py`.
The JSON is aggregate-only. The parent rerun used base HEAD
`f865acda17977e2e23127581051e2defd6cdf1e8` plus dirty working-tree content
SHA-256 `95967bac3ccafe867c48b9ac62f2a33b79ada8b939ce25d975a6ca501eadbd41`.
The earlier snapshot had digest `5005efacc00ae46128ceb60b83cbd3978709d70c3475548090a80c52c6c906c7`; the intervening source change was header-only.
The digest includes all `one/**/*.py`, benchmark script and compaction fixture
JSON files, with sorted relative paths and length-prefixed paths and bytes; it
is not a Git revision and does not cover dependencies or the host. Fixtures in
this benchmark are generated in the script; the fixture files are also hashed.
It used fixture schema 1, seed 0, a fixed synthetic fake model and
one manual compaction per scenario. Schema 1 generates 6/30/30 alternating
turns, with 30 paired synthetic tool calls/results in tool-heavy. Settings:
recentTokens=128, minKeptMessages=2, summarizeWithModel=true,
strategy=local_separate, rehydrate=false, thinking=medium; all other settings
are application defaults. The fake intercepts the provider boundary, returns a
fixed checkpoint with the requested ledger, and never makes a network request.
Its fabricated input/output counters use character counts divided by four;
cached=16 and reasoning=8 are arbitrary. They are **not** cache observations,
real provider usage, or pricing. Failed requests have unknown usage. Default
latency is null (not measured); `--measure-latency` measures whole offline
scenario wall time using `perf_counter`, not provider latency. Context sizes below are ONE's approximate message-token counts,
not wire usage. Exact constraint retention means literal synthetic directive
present in active context; task success is only a synthetic marker-survival
proxy, not real task accuracy. The fake deliberately supplies the marker.

Observed offline results (legacy / V2):

| Scenario | Success context before → after | Success synthetic input | Failure context before → after | Failure original messages missing |
|---|---|---|---|---|
| small | 269 → 179 / 218 | 202 / 388 | 269 → 137 / 269 | 7 / 0 |
| medium | 1277 → 179 / 218 | 1147 / 1332 | 1277 → 137 / 1277 | 55 / 0 |
| tool-heavy | 2687 → 150 / 181 | 3445 / 2752 | 2687 → 108 / 2687 | 87 / 0 |

One measured run from the earlier snapshot, milliseconds (legacy / V2; one sample per cell, not a
statistical latency estimate):

| Scenario | Success | Intentional failure |
|---|---|---|
| small | 22.439 / 46.611 | 21.787 / 32.467 |
| medium | 18.874 / 45.202 | 21.177 / 32.934 |
| tool-heavy | 25.783 / 48.488 | 21.061 / 32.612 |

Each row/mode made one request; all six successful calls had usage coverage
1/1, output 49/53, cached 16/16, reasoning 8/8. All six intentional failures
had coverage 0/1 and reported failure; successful calls had zero failures.
All success cases retained the exact directive and marker (1/1 each). On failure,
legacy retained neither and V2 retained both (1/1); V2 changed active history
in 0/3 failure cases versus legacy 3/3. Counts are this finite synthetic sample,
not a failure-rate estimate. No synthetic cost or live pricing is computed.

Live A/B remains blocked pending separate cost/data authorization; no API or
auth was used. The cache, native, failure-rate, task-success and token-reduction
goals are **not proven** by this experiment. The proposed 20-session pilot
requires authorization; experimental V2 remains default false and native
capability remains false/unverified. No production rollout claim follows.
