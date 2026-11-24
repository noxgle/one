# TODO — Compaction V2: naprawa kompaktacji w sesjach jednokomunikatowych

## Kontekst incydentu

Sesja `5cc2f3f2-d68d-42ee-87f8-fe68dc29854f` (llama.cpp, `cyber-tiel-coder-35b-a3b-ud-q5_k_xl`,
`compaction.experimentalV2=true`, `enabled=true`):

| Metryka | Wartość |
|---|---|
| `userMessages` | 1 |
| `assistantMessages` / `toolCalls` | 76 / 75 |
| `compaction.completed` | 0 |
| `compaction.skipped` | 20 |
| `compaction.failed` | 0 |
| `contextUsage.tokens` | 99 475 |
| `input_budget` (`effectiveContextWindow`) | 107 520 |
| `selectedContextWindow` | 122 880 |
| `outputReserve` | 15 360 |
| trigger V2 | 86 016 |
| `providerCache.requestsWithUsage` | 0 / 75 |
| `outputGeneration.missingMeasurements` | 75 |

Efekt: `[error] Request exceeds the input budget after compaction; shorten the latest
exchange or use a larger context window.` — turn trwał 59 min i zakończył się `error`.

## Root causes

1. **`plan_retention` traktuje całą historię jako „ostatnią wymianę”.**
   `one/core/compaction/retention.py:69-75`: przy jednej wiadomości `user`
   `user_boundaries == [0]`, więc `latest = 0`, a `suffix[0]` to cała historia.
   `available_tokens` (`agent_session.py:5671-5673`) ≈ 80–81k < ~86k → `oversized_exchange`
   → `skipped`. Kompaktacja jest **strukturalnie niemożliwa** w sesji z jednym `user`.
2. **Fail-closed bez planu B.** Brak podziału wymiany na granicy kroku narzędziowego,
   brak elizji `toolResult`, brak adaptacji `minKeptMessages` w dół.
3. **Floor `minKeptMessages` nie adaptuje się do budżetu** — ADR-006 wymaga adaptacji,
   kod jej nie ma (`retention.py:73-75`).
4. **Próg akceptacji kandydata = próg triggera** (`agent_session.py:5898`): kandydat
   musi zejść pod 86 016 zamiast pod 107 520, więc ratująca sesję kompaktacja do 95k
   jest odrzucana.
5. **Niespójne jednostki** w `available`: `fixed` już ×1.15, `available` dzielone przez
   1.15, `suffix` to surowe `chars/3`, akceptacja używa exact count.
6. **Telemetria skipów bezużyteczna**: `append_compaction_skipped` nie zapisuje
   `skipReason`, więc `skipped: 20` nie mówi, co się stało. Komunikat błędu twierdzi
   „after compaction”, choć żadna kompaktacja się nie odbyła.
7. **Brak usage z llama.cpp** → `_record_context_usage` nigdy nie kalibruje estymatora
   (`_context_calibration == 1.0`), `outputGeneration.reliable == false`.

## Propozycje poprawek (wszystkie zaimplementowane)

### [DONE] P6 — telemetria skipów i uczciwy komunikat błędu (pierwsze, zero ryzyka)

- `one/core/session_manager.py:626-637` — `append_compaction_skipped` przyjmuje
  `skip_reason`, `tokens_before`, `available_tokens`; zapisuje je additive w JSONL.
- `get_compaction_stats` (`session_manager.py:650-706`) — agreguje
  `skipReasons: {reason: count}` oraz `lastSkip`.
- `one/core/agent_session.py:4683-4686` — komunikat z liczbami i realną przyczyną
  (`tokens`, `input_budget`, `skipReason`) + wskazówka (skróć wymianę, włącz
  `toolOutputPruning`, zwiększ okno serwera).
- Ostrzeżenie `warning` raz na turn, gdy skip następuje przy `tokens > trigger`.

### [DONE] P1 — `plan_retention`: podział przepełnionej wymiany

`one/core/compaction/retention.py` — gdy `suffix[latest] > available_tokens`, szukaj
wewnętrznej granicy kroku (`assistant` rozpoczynający nową grupę tool calls, albo
`start` po `toolResult`), przechodząc przez `safe()` (ochrona par native
tool-call/result). Nowy `reason="split_exchange"`.

### [DONE] P2 — adaptacyjny floor `minKeptMessages`

`retention.py:73-75` — schodzić z floorem od `min_kept_messages` do 1, aż
`suffix[floor] <= available_tokens`; dopiero potem `no_safe_reduction`.

### [DONE] P4 — rozdziel „przeżycie” od „histerezy”

`agent_session.py:5898` — dla `auto_preflight` / `context_limit_retry` limitem jest
`input_budget`, nie trigger; dla `auto` / `manual` pozostaje trigger. Licznik
`_v2_preflight_compactions` (max 2 na turn, reset w `_prompt_impl`) chroni przed pętlą.

### [DONE] P5 — spójne jednostki w `available`

`agent_session.py:5671-5673` — gdy `_exact_request_tokens([])` zwraca wartość, liczyć
`available = input_budget - fixed_exact - reserve` bez dzielenia przez 1.15; fallback
estymowany zachowuje dotychczasową ostrożność.

### [DONE] P3 — elizja `toolResult` jako ostatnia deska ratunku

Nowe ustawienie `compaction.elideToolResults` (default `true` dla V2, `false` legacy).
W `agent_session.py` po zbudowaniu `candidate`, jeśli nadal przekracza budżet, użyć
`prune_stale_tool_outputs` na **kopii** kandydata (evidence i `self.messages`
nietknięte). Zapisać `elided: {count, tokensReclaimed}` w wpisie kompaktacji i pokazać
w `get_compaction_stats`.

### [DONE] P7 — usage z llama.cpp + `countSource`

`one/providers/llama_cpp.py` — gdy `usage` puste, wyprowadzić `prompt_tokens` /
`completion_tokens` z `timings.prompt_n` / `timings.predicted_n` ostatniego chunku SSE.
`get_context_usage` — nowe pole `countSource: "exact" | "estimate"`.

## Testy (dodane)

- `tests/test_compaction_retention.py`: `split_exchange` dla 1 user + N kroków,
  adaptacyjny floor, brak osieroconych par native.
- `tests/test_compaction.py`: akceptacja kandydata powyżej triggera dla
  `auto_preflight`, elizja `toolResult` bez mutacji `self.messages`.
- `tests/test_session_manager.py`: `skipReason` w JSONL i w `get_compaction_stats`.
- Nowy test adaptera: `timings.prompt_n` → `usage.prompt_tokens`.

## Weryfikacja (wykonana)

`export ONE_CODING_AGENT_DIR="$(mktemp -d)"`, potem `.venv/bin/python -m pytest -q`,
`.venv/bin/ruff check .`, `git diff --check`. CHANGELOG `Unreleased` + bump
`one/config.py:VERSION` (zmiana user-visible) + regeneracja snapshotów TUI.

## Otwarte decyzje

- `elideToolResults` domyślnie `true` — przyjęte; udokumentowane w
  `website/src/content/docs/library/compaction.md`.
- ADR-006 w `tmp/TODO.md` opisuje jeszcze nierozdzielną „ostatnią wymianę”;
  wymaga aktualizacji, aby nie kolidował z nowym `split_exchange`.

- P1/P3 zmieniają semantykę „ostatniej wymiany” — wymaga aktualizacji ADR-006
  (`tmp/TODO.md`) i dokumentacji w `website/src/content/docs/library/`.
- P3: czy `elideToolResults` ma być domyślnie `true` w V2 (rekomendacja: tak,
  alternatywą jest martwa sesja).
