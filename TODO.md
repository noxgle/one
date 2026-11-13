# TODO — Compaction 2.0 w ONE

## Cel
Przejść od pojedynczego akapitu summary do bezpiecznego kształtowania kontekstu: structured checkpoint, zachowanie ograniczeń, cache-aware local summary, opcjonalna kompaktacja natywna, bounded rehydratacja i mierzalna telemetria. Historia JSONL i evidence pozostają źródłem prawdy.

Plan zaadaptowany z `/tmp/todo2.md` (2026-11-12), po inspekcji repozytorium. Zakres offline P0–P5 dostarczono i zweryfikowano; walidacja live i rollout pozostają zablokowane. Dokument źródłowy opisuje zamiar, nie potwierdzone możliwości backendów ani wyniki benchmarków.

## Kontekst i dowody
- Potwierdzone: Python 3.12+, pakiet `one-agent`; zasady w `AGENTS.md` i `CONTRIBUTING.md`.
- `one/core/agent_session.py:5210–5395`: `_compact_impl`, `_summarize_context`, summary na indeksie 0, retencja tokenowa, osobny request z `purpose="compaction"`. Po błędzie summary placeholder zastępuje odrzucany fragment **aktywnego kontekstu**; surowa historia sesji nie jest kasowana.
- `one/core/session_manager.py`: `append_compaction`, `get_compaction_stats`, `get_last_compaction`; utrzymanie rekonstrukcji sesji jest częścią zmiany.
- `one/core/settings_manager.py`: obecne defaulty 8192 tokeny / minimum 20 wiadomości / próg 80% / wejście summary 20000 tokenów.
- `one/providers/codex_responses.py:1–23,48`: ChatGPT używa backendu subskrypcyjnego, **nie publicznego OpenAI API**. Dokumentacja publicznego Responses nie dowodzi obsługi `context_management` na tym backendzie.
- `docs/RUNS.md:3–8,35–42`: auto-kompaktacja należy do bieżącego runu, manualna tworzy osobny run. `_summarize_context` używa `_invoke_provider` i przepuszcza `_RunAdmissionError`; nie zakładać braku istniejącego rozliczania.
- `one/core/tui_runtime_host.py`: operacja compact przekazuje `instructions`; `agent_runtime_facade.py` przenosi eventy. Nie omijać granicy owner-loop/DTO.
- Istnieją `tests/test_compaction.py`, `test_provider_payloads.py`, `test_provider_cache_telemetry.py`, `test_codex_affinity.py`, `test_session_manager.py`, `test_settings.py`, `test_run_*.py` i testy TUI/runtime.
- `one/config.py:13`: wersja podczas inspekcji `0.1.140`; docelowo następna dostępna wersja, nie sztywno zarezerwowana `0.1.141`.
- Root nie zawierał TODO.md; obecne jest `.git/`. Odczytano także CHANGELOG i fragment historii Git; nie wykonano testów ani zmian Git.

## Zakres
- W zakresie: P0–P5 poniżej, kompatybilność starych sesji, native tylko po potwierdzeniu capability, dokumentacja i kontrolowany rollout.
- Osobne, opcjonalne rozszerzenia: `/handoff`, Anthropic native i P6 precompute. Nie blokują bezpiecznego lokalnego V2.
- Poza zakresem: trening/RL, KV-cache, przenoszenie opaque items między providerami, przebudowa toolOutputPruning, nieograniczony re-read, publikacja i merge.

## Git workflow
- Repozytorium Git; implementer tworzy nową gałąź `one/compaction-v2` z bieżącego HEAD przed pierwszą zmianą źródeł. Jeśli nazwa zajęta, użyć sufiksu; nie resetować ani nie wykorzystywać cudzej gałęzi.
- Nie implementować bezpośrednio na `main` ani gałęzi współdzielonej.
- Nie commitować, pushować, mergować, tagować ani tworzyć PR bez odrębnego polecenia. Żądanie adaptacji planu nie upoważnia do merge do main; usunięto taki warunek z propozycji źródłowej.

## Architektura i decyzje
- **ADR-001 — Oddzielić politykę od orkiestracji.** Nowy pakiet `one/core/compaction/` (planowane ścieżki): policy/retention, summary, constraints, rehydrate; AgentSession pozostaje właścicielem eventów, runu i atomowego commitowania kontekstu. Alternatywa: rozbudowa monolitu AgentSession; odrzucona ze względu na testowalność.
- **ADR-002 — Najpierw przygotowanie, potem commit.** Plan retencji i kandydat checkpointu powstają bez mutacji live messages. Walidacja, sprawdzenie aktualności/abort i trwały zapis poprzedzają podmianę widoku. Błąd, odmowa budżetu lub nieskuteczny fallback zachowuje aktywną historię. Udany, zwalidowany fallback ekstrakcyjny jest sukcesem ze wskazaniem strategii, nie failed summary z placeholderem.
- **ADR-003 — Zachować kanoniczną reprezentację.** Pozycję summary dla Responses realizować przy projekcji payloadu, nie przez bezwarunkowe przeniesienie indeksu w `self.messages`. Chroni mapowanie entry IDs i resume. Przetestować rolling summary i kolejność native items. Prefix pozostaje domyślny dla innych adapterów.
- **ADR-004 — Native capability per endpoint/model.** Publiczny Responses dokumentuje server-side `context_management` oraz osobny `/responses/compact`; to różne kontrakty. Wybrany preferowany wariant: server-side, stateless replay, `store=false`, bez `previous_response_id`; backend ChatGPT wymaga osobnego potwierdzenia. Brak potwierdzenia oznacza local fallback, nie wysyłanie nieznanych pól. Standalone endpoint jest alternatywą wyłącznie po jawnej decyzji; jego cały wynik stanowi kanoniczne okno i nie wolno redukować go do jednego itemu.
- **ADR-005 — Ograniczenia bez eskalacji uprawnień.** Ledger z autentycznych wiadomości użytkownika, deduplikowany, verbatim, maks. 2000 tokenów. Wzorce EN/PL są heurystyką, nie gwarancją wykrycia wszystkich reguł. Cytowane logi i tool output nie stają się instrukcjami systemowymi; zachować pochodzenie, kolejność i jawne zmiany ograniczeń. Reguły system/developer/preset nadal pochodzą z normalnego prefixu.
- **ADR-006 — Flaga i defaulty.** `compaction.experimentalV2=false` zachowuje legacy payloady i zachowanie; nowa telemetria additive. Dla V2 cel retencji 15000, minimum 8, ale z adaptacją do efektywnego budżetu. Nie nadpisywać jawnych ustawień użytkownika. Rehydratacja plików i precompute domyślnie off. Nie dodawać przełącznika przywracającego destrukcyjny placeholder w V2.

### Polityka V2
- Trigger: `min(budget * thresholdPercent/100, budget - max(buffer.minTokens, ceil(window * buffer.percent/100)))`; window nieznane → effective input budget. Jeśli wynik <= 0, użyć progu procentowego. Default buffer 15%, minimum 16000; jawnie przetestować małe okna.
- Retencja: cel 15k, preferowana granica wymiany, tolerancja 15% tylko gdy mieści się cały request. Priorytet: poprawne pary tool-call/result i ostatnia wymiana > floor wiadomości > miękki cel tokenów. Jeśli nierozdzielna wymiana przekracza budżet, zwrócić jawny wynik braku bezpiecznej redukcji; nie wysyłać świadomie za dużego requestu ani osieroconych tool results.
- Checkpoint v2: Objective, Constraints, Decisions, Work State (Completed/Active/Blocked), Next Move, Relevant Files (max 15), Important Context, Artifacts (opcjonalnie). Zwięzłe fakty, bez wymyślonych ukończeń; output max 4000 tokenów, tool previews max 2000 znaków. Walidacja wymaganych sekcji, brak tool calls/template residue, jeden repair, potem deterministyczny fallback.
- Failure latch: 2 nieudane próby auto, reset po nowej wiadomości użytkownika; manual może ponowić, ale nie omija budżetu/cancellation. Context overflow ma ograniczone recovery, bez pętli.
- Ustawienia additive: strategy auto/local_cache_aware/local_separate/extractive/native_openai; cacheAware; buffer; summary; constraints; failure; native.chatgpt auto (tylko zweryfikowane capability), native.anthropic off; rehydrate enabled=false/maxFiles=5/maxTokens=12000; precompute enabled=false. Dokładny schema/gettery i walidacja w T002; legacy pola nadal działają.
- Native nie przechodzi przez tekstowy walidator nagłówków: ma odrębny walidator protokołu i wspólne warunki atomowości/bezpieczeństwa. Opaque content wyłącznie w prywatnym storage sesji; nie w eventach, DTO, logach, stats i błędach. Przy zmianie providera/modelu odbudować lokalny kontekst z historii, zanim odrzuci się native widok.

## Plan wykonawczy
Każda faza kończy się reviewowalnym przyrostem. S/M/L to względny nakład, nie termin. Wszystkie komendy weryfikacyjne są przeznaczone dla implementera, nie zostały wykonane podczas planowania.

### P0 — Baseline, kontrakty i obserwowalność
**Wynik:** testowalny baseline, schema i telemetria bez zmiany legacy. **Nakład / pewność:** M / wysoka.

T001 i T002 ukończone: fixtures/goldens kompletne (`tests/test_compaction.py` używa sześciu realistycznych transkryptów plus zamrożone goldeny; `tests/test_provider_payloads.py` zamrożony payload summary; `tests/test_session_manager.py` telemetria disk reload/fork). Zaimplementowano jawne gettery ustawień i schema telemetrii. Weryfikacja skupiona: 153 passed (coder), 1 passed (disk test). Pełny gate ukończony: `.venv/bin/python -m pytest -q` → 3300 passed, 2 skipped w 743,54 s; `.venv/bin/ruff check .` → clean; `git diff --check` → clean. Poprzednia próba zawierała pojedynczy, niepowiązany timeout `test_auth_and_cli.py::test_cli_run_accepts_flags` (20 s); izolowany rerun przeszedł w 12,60 s, pełny rerun green.

- [x] **T001 — Zamrozić baseline i kontrakty integracji**
  - Zmiany: syntetyczne transkrypty mały/średni/duży/tool-heavy/plan/constraints; golden payloadów legacy, resume/fork i eventów. Prześledzić istniejące admission/usage/abort oraz compact hint; zapisać ustalenia w przyszłym docs/COMPACTION.md. Nie używać realnych sesji.
  - Pliki: `tests/test_compaction.py`, `tests/test_provider_payloads.py`, `tests/fixtures/compaction/` (nowy), `docs/COMPACTION.md` (nowy), `one/core/agent_session.py` (inspekcja).
  - Zależności: brak.
  - Akceptacja: deterministyczne fixtures i golden pokrywają trzy auto-triggery, manual, rolling summary i placeholder legacy; opisany owner runu i mapowanie entry IDs.
  - Weryfikacja: `.venv/bin/python -m pytest -q tests/test_compaction.py tests/test_provider_payloads.py tests/test_run_budget_runtime.py`.

- [x] **T002 — Dodać ustawienia i additive telemetry V2**
  - Zmiany: gettery/walidacja flag i limitów; opcjonalne pola tokensAfter/tokensKept/strategy/summarySchema/attempts/fallback/usage/constraints/rehydrated/durationMs; trwała ewidencja porażek i additive compaction_failed. Rozszerzyć stats o failed/failureReasons/byStrategy/cache/średni czas; brak usage to null, nie zero. Nie zmieniać znaczenia historycznego tokensBefore bez wersjonowania.
  - Pliki: `one/core/settings_manager.py`, `one/core/session_manager.py`, `one/core/agent_session.py`, `one/core/agent_runtime_facade.py`, `one/core/tui_runtime_host.py`, `tests/test_settings.py`, `tests/test_session_manager.py`, `tests/test_session_stats.py`, `tests/test_event_snapshots.py`.
  - Zależności: T001.
  - Akceptacja: stare JSONL czytają się bez migracji; flag off zachowuje baseline; nowe eventy docierają przez facade; stats bez treści i bez podwójnego naliczania usage.
  - Weryfikacja: `.venv/bin/python -m pytest -q tests/test_settings.py tests/test_session_manager.py tests/test_session_stats.py tests/test_event_snapshots.py tests/test_agent_runtime_facade.py`.

### P1 — Retencja i trigger
**Wynik:** czysta polityka mieszcząca request w budżecie. **Nakład / pewność:** M / średnia.

- [x] **T003 — Wydzielić planner i wdrożyć politykę V2** (implementacja i focused checks; pełna bramka rodzica: pytest 3336 passed, 2 skipped, 747.93s; ruff check . passed; git diff --check passed. P0 preserved, P1 opt-in behind flag; no further edits needed.)
  - Zmiany: czyste funkcje planowania; exchange boundaries, istniejąca ochrona native replay pairs, aktywny plan bez duplikacji, floor i adaptacyjny tail, wspólny trigger preflight/auto/overflow. W razie braku redukcji jawny bounded wynik; toolOutputPruning pozostaje osobną polityką.
  - Pliki: `one/core/compaction/__init__.py`, `one/core/compaction/retention.py`, `one/core/compaction/policy.py` (nowe), `one/core/agent_session.py`, `tests/test_compaction_retention.py` (nowy), `tests/test_context_budget.py`, `tests/test_compaction.py`.
  - Zależności: T002.
  - Akceptacja: 32k/200k/1M i unknown window, duży output, pojedyncza nadmiarowa wymiana, krótka historia, plany, pary narzędziowe i explicit overrides pokryte testami; legacy golden bez zmian.
  - Weryfikacja: `.venv/bin/python -m pytest -q tests/test_compaction_retention.py tests/test_context_budget.py tests/test_compaction.py`.

### P2 — Checkpoint, ledger i safe failure
**Wynik:** bezpieczna lokalna kompaktacja V2. **Nakład / pewność:** L / średnia.

- [x] **T004 — Zbudować structured checkpoint i ledger** (implementacja i testy ukierunkowane ukończone; bramka zamknięta — pełny test pytest 3393 passed, 2 skipped, 771.83s; ruff check . i git diff --check czyste)
  - Zmiany: szablon i walidator, bounded ledger EN/PL, zachowanie poprzednich ograniczeń i jawnych korekt, 1 repair, deterministic extractive fallback. Fallback wybiera cel, faktyczne wyniki narzędzi, plan, ostatnie błędy i ścieżki bez traktowania samych tool calls jako ukończenia pracy. Hint jest instrukcją summary, nie samym summary; legacy semantyka zostaje za flagą off.
  - Pliki: `one/core/compaction/summary.py`, `one/core/compaction/constraints.py`, `tests/test_compaction_summary.py`, `tests/test_compaction_constraints.py` (nowe), `one/core/agent_session.py`.
  - Zależności: T003.
  - Akceptacja: wymagane sekcje i limity egzekwowane, brak tool execution; ledger verbatim i provenance; >=95% zachowania jawnych ograniczeń na zdefiniowanym syntetycznym probe set, osobno raportowana skuteczność ekstrakcji i przepisywania; fuzzy score nie zastępuje exact testu verbatim.
  - Weryfikacja: `.venv/bin/python -m pytest -q tests/test_compaction_summary.py tests/test_compaction_constraints.py tests/test_compaction.py`.

- [x] **T005 — Wdrożyć atomowy commit i latch awarii** (implementacja i testy ukierunkowane ukończone; bramka zamknięta — pełny test pytest 3393 passed, 2 skipped, 771.83s; ruff check . i git diff --check czyste)
  - Zmiany: przygotuj/waliduj/persistuj/podmień; nie zmieniaj messages ani granicy aktywnego kontekstu przy error/timeout/empty/invalid/abort/storage failure. Zachowaj event cleanup i busy guards. Latch 2 porażek, manual bypass tylko latch. Admission error nie jest pretekstem do kolejnego płatnego retry. Porażka fallbacku nie produkuje placeholdera V2.
  - Pliki: `one/core/agent_session.py`, `one/core/session_manager.py`, `tests/test_compaction.py`, `tests/test_run_budget_runtime.py`, `tests/test_run_cancellation.py`, `tests/test_event_snapshots.py`.
  - Zależności: T004.
  - Akceptacja: fault injection na każdym etapie zachowuje historię i poprawny resume; udany extractive fallback oznaczony oddzielnie; brak pętli overflow i wywołań po abort; nowa wiadomość resetuje latch, manual retry działa.
  - Weryfikacja: `.venv/bin/python -m pytest -q tests/test_compaction.py tests/test_run_budget_runtime.py tests/test_run_cancellation.py tests/test_event_snapshots.py`.

### P3 — Cache-aware local summary
**Wynik:** współdzielony prefix i bezpieczne projekcje providerów. **Nakład / pewność:** M–L / średnia.

- [x] **T006 — Dodać strategie i cache-aware payload projection**
  - Zmiany: capability pozycji summary, normalny prefix system/tools/messages i dopisana instrukcja; immutable snapshot. Zachować definicje tools dla cache, ale wymusić brak ich wykonania i odrzucić tool-call output; jeśli adapter nie pozwala bezpiecznie wyłączyć wywołań lub prefix nie mieści się w oknie, local_separate. Rozwiązać consecutive user messages zgodnie z kontraktem adaptera. Responses suffix tylko w payloadzie, bez zerwania entry IDs/rolling summary.
  - Pliki: `one/core/compaction/summary.py`, `one/core/compaction/policy.py`, `one/core/agent_session.py`, `one/providers/base.py`, `one/providers/codex_responses.py`, `one/providers/openai_compatible.py`, `one/providers/anthropic.py`, `one/providers/gemini.py`, `tests/test_provider_payloads.py`, `tests/test_provider_cache_telemetry.py`, `tests/test_compaction.py`.
  - Zależności: T005.
  - Akceptacja (adaptowana): payload contracts dla wszystkich adapterów, flag off identyczne; summary call nie mutuje messages ani nie uruchamia narzędzi; purpose=compaction zachowuje run accounting. Prefix nie gwarantuje cache ratio; live benchmark nieautoryzowany i niewymagany dla T006, pomiary pozostają w T011.
  - Weryfikacja: `.venv/bin/python -m pytest -q tests/test_provider_payloads.py tests/test_provider_cache_telemetry.py tests/test_providers.py tests/test_compaction.py tests/test_run_budget_runtime.py`.
  - Status: zaimplementowane; pełny gate ukończony — 3418 passed, 2 skipped, 857.41s; `ruff check .` i `git diff --check` czyste. Senior review (read-only) 5 findings all corrected: typed status, Anthropic breakpoint, images fallback, endpoint gate, tools fingerprint. No live A/B done; cache ratio not guaranteed. No commits, no version change.

### P4 — Native Responses z bramką zgodności
**Wynik:** native tylko tam, gdzie kontrakt potwierdzono; zawsze lokalna ścieżka awaryjna. **Nakład / pewność:** L / niska dla backendu ChatGPT.

- [x] **T007 — Udokumentować decyzję o kontrakcie native dla rzeczywistego endpointu**
  - Zmiany: ustalić obsługiwane modele, request fields, streaming item ordering, replay, usage/pricing, granicę odrzucania historii i błędy unsupported dla backendu używanego przez ONE. Publiczne docs są wskazówką, nie dowodem zgodności ChatGPT. Bez upoważnionego live potwierdzenia pozostawić capability false; fake test dowodzi implementacji kontraktu, nie dostępności usługi.
  - Pliki: `one/providers/codex_responses.py` (inspekcja), `docs/COMPACTION.md`, `tests/fixtures/compaction/` (wyłącznie syntetyczne kontrakty).
  - Zależności: T002.
  - Akceptacja: spisany kontrakt i decyzja supported/unsupported/unverified per endpoint/model; brak domyślnego native na podstawie samej nazwy providera. Nie ujawniać auth ani danych rozmów.
  - Weryfikacja: review aktualnej oficjalnej dokumentacji i dokumentacji backendu; opcjonalna ręczna próba dopiero po osobnej zgodzie na koszt/dane, poza CI.
  - Decyzja offline: backend ChatGPT i modele pozostają unverified; capability produkcyjne wyłączone. Publiczny guide opisuje kontrakt publiczny, nie dowodzi zgodności backendu. Weryfikacja backendu i aktywacja produkcyjna pozostają zablokowane; zamknięcie dotyczy decyzji, nie potwierdzenia wsparcia.

- [x] **T008 — Zaimplementować capability, parsing i trwały replay native offline**
  - Zmiany: additive struktury wyniku zachowujące **kolejność wszystkich** output items (nie tylko jeden native_compaction); context_management tylko dla wspieranych endpointów; store=false, bez previous_response_id. Prywatna persistencja opaque itemów i bezpieczny DTO. Atomowy update widoku po sukcesie; brak lokalnego summary przy skutecznym native. Przy unsupported/zmianie modelu/rollback odbudowa local view z raw JSONL przed kolejnym requestem. Retry local ograniczony, bez powielania tool side effects ani omijania runBudget.
  - Pliki: `one/providers/base.py`, `one/providers/codex_responses.py`, `one/core/agent_session.py`, `one/core/session_manager.py`, `one/core/agent_session_runtime.py`, `tests/test_provider_payloads.py`, `tests/test_providers.py`, `tests/test_session_manager.py`, `tests/test_codex_affinity.py`, `tests/test_compaction_native.py` (nowy).
  - Zależności: T006, T007. Aktywacja produkcyjna zablokowana do potwierdzenia supported w T007; implementacja offline możliwa wcześniej po ustaleniu kontraktu.
  - Akceptacja: stream z wieloma items, resume/fork, drugie native, model/provider switch, rollback, malformed/partial stream i redakcja pokryte fake testami; unsupported adapter nie dostaje pól native; opaque content nie wycieka do stats/eventów/RPC/logów.
  - Weryfikacja: `.venv/bin/python -m pytest -q tests/test_compaction_native.py tests/test_provider_payloads.py tests/test_session_manager.py tests/test_codex_affinity.py tests/test_rpc_mode.py tests/test_run_budget_runtime.py`.
  - Status: implementacja offline ukończona; pełna bramka zamknięta — pytest: 3479 passed, 2 skipped, 795.59 s; `ruff check .` i `git diff --check` czyste. Backend ChatGPT pozostaje niezweryfikowany, capability produkcyjne jest wyłączone; nie przeprowadzono wdrożenia live.

### P5 — Rehydratacja, interfejs i wydanie
**Wynik:** bounded przywracanie kontekstu, walidacja offline i instrukcja użytkownika. **Nakład / pewność:** L / średnia.

Status bramki offline: rodzic wykonał pełny pytest: 3506 passed, 2 skipped, 747.71 s; następnie `ruff check .` i `git diff --check` czyste. W poprzedniej próbie niezwiązany test timingowy `test_sidebar_git_probe` przekroczył 0.2 s (0.47 s); przeszedł osobno (0.77 s), a następnie cały gate był zielony. To nie jest walidacja produkcyjna.

- [x] **T009 — Dodać bounded rehydratację i zweryfikować compact hints**
  - Status: implementacja i zakres offline ukończone; bramka offline P5 zamknięta.
  - Zmiany: plan i ledger jako pochodne bloki, bez duplikacji kolejnych kompaktacji; rozdzielić ich obowiązkowe zachowanie od opcjonalnego file reread. File reread off, max 5 plików/12k tokenów i dodatkowo available request budget; dedupe na cykl, brak samonapędzających kolejnych cykli. Respektować tool policy przy wykonaniu, workspace boundaries, symlinki, wykluczenia sekretów i brakujące pliki. Hint przechodzi TUI/interactive/facade do generatora, nie zastępuje checkpointu; sprawdzić istniejący interfejs przed dodawaniem komendy.
  - Pliki: `one/core/compaction/rehydrate.py` (nowy), `one/core/agent_session.py`, `one/core/tool_policy.py` (integracja/inspekcja), `one/core/tui_runtime_host.py`, `one/core/agent_runtime_facade.py`, `one/modes/tui_commands.py`, `one/modes/interactive_mode.py`, `tests/test_compaction.py`, `tests/test_tui_commands.py`, `tests/test_tui_runtime_boundary.py`, `tests/test_agent_runtime_facade.py`.
  - Zależności: T006.
  - Akceptacja: brak natychmiastowej rekompaktacji/pętli; denial/oversize/missing file nie obchodzi policy i nie niszczy summary; telemetry files/tokens; hint działa na owner loop.
  - Weryfikacja: `.venv/bin/python -m pytest -q tests/test_compaction.py tests/test_tui_commands.py tests/test_interactive_mode.py tests/test_tui_runtime_boundary.py tests/test_agent_runtime_facade.py tests/test_manual_command_policy.py`.

- [x] **T010 — Przeprowadzić security, compatibility i budżetowy regression gate**
  - Status: implementacja i zakres offline ukończone; bramka offline P5 zamknięta.
  - Zmiany: zbiorcze testy fault injection, stare/nowe JSONL, resume/fork/export, brak sekretów/summary/prompts/tool args/ścieżek/opaque content w nowej diagnostyce. Obecne jawne UI summary nie jest równoznaczne z logowaniem; nie usuwać istniejących kontraktów bez decyzji. Redagować body błędów native. Udokumentować rzeczywiste ograniczenia odczytu nowych entry types przez starszą wersję zamiast obiecywać automatyczną kompatybilność.
  - Pliki: `tests/test_compaction.py`, `tests/test_compaction_native.py`, `tests/test_session_branching_export.py`, `tests/test_provider_cache_telemetry.py`, `tests/test_event_snapshots.py`, `tests/test_run_budget_runtime.py`, `tests/test_run_cancellation.py`, `docs/COMPACTION.md`.
  - Zależności: T008, T009.
  - Akceptacja: 0 przypadków utraty aktywnej historii przy nieudanym V2; brak wycieków testowych markerów; auto/retry/manual liczone raz we właściwym runie; legacy goldens bez zmian; cancellation zatrzymuje dalsze admission.
  - Weryfikacja: wskazane moduły pytest, następnie pełny gate opisany poniżej; wyłącznie fake providers/MCP.

- [x] **T011 — Benchmark i raport offline**
  - Status: benchmark offline i raport ukończone; walidacja LIVE i pilot ZABLOKOWANE bez autoryzacji, cele produkcyjne nie zostały osiągnięte ani udowodnione. Oznaczenie dotyczy wyłącznie zakresu offline.
  - [ ] Walidacja live i rollout: uzyskać autoryzację kosztów i danych, wykonać live A/B i pilot oraz ocenić cele przed decyzją o rozszerzeniu defaultów.
  - Zmiany: deterministyczny offline benchmark z synthetic fixtures; opcjonalny `scripts/bench_compaction.py` (nowy). Metryki: requests, input/cached/output/reasoning, usage coverage, failures, constraints, task success, latency i wielkość kontekstu. Live A/B tylko po osobnej autoryzacji kosztów i danych; przynajmniej mały/średni/tool-heavy, przypięte rewizje, modele i ustawienia, porównywalne powtórzenia. Brak live zgody blokuje rozszerzenie defaultów, nie testy offline.
  - Pliki: `scripts/bench_compaction.py`, `tests/fixtures/compaction/`, `docs/COMPACTION.md` (raport zagregowany, bez rozmów).
  - Zależności: T010.
  - Akceptacja: raport odróżnia wyniki fake od live; cele do walidacji, nie obietnice: >=50% cache ratio local na wspieranym ChatGPT, >=90% native w kwalifikujących się sesjach po aktywacji, <1% failure, +10% task success i -10–20% tokenów względem baseline. Brak wyniku nie jest sukcesem. Wstępny pilot 20 autoryzowanych sesji przed rozszerzeniem.
  - Weryfikacja: powtarzalny benchmark offline i review raportu; live poza CI dopiero za zgodą.

- [x] **T012 — Uzupełnić dokumentację i przygotować wydanie**
  - Status: dokumentacja i przygotowanie wydania offline ukończone; bramka offline zamknięta, bez publikacji ani decyzji o wdrożeniu live.
  - Zmiany: ustawienia, provider matrix z verified/unverified, limity ledger, safe failure, run accounting, recovery native i rollback; VERSION następny dostępny numer, jeden bump na wydanie, Unreleased i snapshots. Domyślne włączenie V2/15k/8 dopiero po pozytywnym T011 i jawnej decyzji wydaniowej; zachować rollback co najmniej 2 wydania.
  - Pliki: `docs/COMPACTION.md`, `docs/architecture.md`, `docs/EXTENSIONS.md`, `docs/RUNS.md`, `README.md`, `CHANGELOG.md`, `one/config.py`, `one/core/settings_manager.py`, `tests/snapshots/tui/`.
  - Zależności: T011.
  - Akceptacja: przykłady zgodne ze schema, brak niezweryfikowanych claims; bump i snapshots spójne; feature off i native off mają przetestowane procedury odbudowy widoku; brak publikacji/tagów/merge.
  - Weryfikacja: pełny gate; `ONE_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest -q tests/test_tui_snapshots.py`, review diff, potem ponowny zwykły test snapshots.

### Opcjonalne rozszerzenia — nie blokują P0–P5
**Nakład / pewność:** każde M–L / niska; wymagają osobnej decyzji produktowej przed implementacją.

- [ ] **T013 — Zaprojektować i dodać oddzielny /handoff**
  - Zmiany: jawnie nowa sesja z bounded briefem i celem, bez zastępowania kompaktacji; potwierdzić UX, provenance, politykę przenoszenia presetów i brak automatycznego przenoszenia native opaque state.
  - Pliki: `one/core/agent_session_runtime.py`, `one/core/tui_runtime_host.py`, `one/core/agent_runtime_facade.py`, `one/modes/tui_commands.py`, `one/modes/interactive_mode.py`, `tests/test_tui_commands.py`, `tests/test_tui_runtime_boundary.py`, `docs/COMPACTION.md`.
  - Zależności: T009.
  - Akceptacja: zgoda na osobny feature; nowa sesja z poprawną izolacją, oryginał nienaruszony, anulowanie bez osieroconego przełączenia.
  - Weryfikacja: testy runtime/facade/TUI/interactive z in-memory session managers.

- [ ] **T014 — Ocenić Anthropic native**
  - Zmiany: osobny spike aktualnego API/modeli/beta headers, kosztów, replay i fallback; bez potwierdzenia `native.anthropic=off`. Nie zakładać identycznego opaque kontraktu jak Responses.
  - Pliki: `one/providers/anthropic.py`, `tests/test_providers.py`, `tests/test_provider_payloads.py`, `docs/COMPACTION.md`.
  - Zależności: T010.
  - Akceptacja: ADR z verified kontraktem i decyzją go/no-go; implementacja wyłącznie po go, fake testy oraz brak pól beta w legacy.
  - Weryfikacja: review aktualnych oficjalnych docs; pytest providerów; live wymaga odrębnej zgody.

- [ ] **T015 — Zbadać i wdrożyć precompute tylko po dodatnim eksperymencie**
  - Zmiany: flaga off; immutable snapshot z revision/provider/model/preset fingerprint, bounded background task na owner loop, odrzucenie stale result, anulowanie abort/switch, admission i accounting przed requestem. Najpierw ustalić jednostki triggera: propozycja miesza leadMs z tokenowym lead; nie odejmować milisekund od tokenów. Cache lifetime tylko jeśli provider ją rzeczywiście udostępnia, nie z wymyślonego TTL; limit 2 failures i brak wycieków.
  - Pliki: `one/core/compaction/precompute.py` (nowy), `one/core/agent_session.py`, `one/core/settings_manager.py`, `tests/test_compaction_precompute.py` (nowy), `tests/test_run_budget_runtime.py`, `tests/test_run_cancellation.py`, `docs/COMPACTION.md`.
  - Zależności: T011.
  - Akceptacja: osobny go po pomiarze; 0 stale commits/leaked tasks, poprawny budget/cancel, cel foreground P95 -30% bez pogorszenia jakości/failure/cache; niespełnienie celu pozostawia off.
  - Weryfikacja: fake clock/concurrent provider tests, jawne uruchomienie nowego modułu pytest i autoryzowany A/B poza CI.

## Weryfikacja implementera
Przed testami/manual CLI ustawić w tej samej powłoce `export ONE_CODING_AGENT_DIR="$(mktemp -d)"`. Python 3.12+, fake providers i stub MCP; nie dotykać `.one/` ani realnych sesji. Nowe moduły testowe uruchamiać jawnie, bo helpery mają stałe listy.

Pełny gate w kolejności:
1. `.venv/bin/python -m pytest -q`
2. `.venv/bin/ruff check .`
3. `git diff --check`

## Rollout i rollback
1. P0–P2 i następne przyrosty z V2 off; jawny opt-in do testów lokalnych.
2. Lokalna V2 może być użyteczna bez native; nie reklamować wtedy native jako ukończonego.
3. Pilot ChatGPT tylko po T007 i zgodzie na live; następnie kolejne adaptery po payload/quality gate.
4. Zmiana defaultów tylko po raporcie i decyzji; precompute niezależnie off.
5. Rollback: V2 false lub native off wybiera starszą/lokalną ścieżkę **po odbudowie** z historii dla sesji z opaque stanem. `strategy=local_separate` przełącza sposób requestu, nie gwarantuje automatycznie pełnego legacy zachowania. Przed użyciem starszej binarki zweryfikować odczyt nowych entries; nie obiecywać braku migracji bez testu. Nigdy nie usuwać raw historii/evidence.

## Ryzyka i otwarte bramki
- Backend ChatGPT może nie wspierać publicznego kontraktu; T007, native off i lokalny fallback.
- 15k tail + ledger + summary może nie mieścić się w małym oknie; adaptive planner i testy oversize, nie sztywna gwarancja 15k.
- Regex ledger pomija niejawne wymagania i może utrwalać nieaktualne; provenance, jawne korekty, bounded limity i osobny pomiar extraction recall.
- Cache-aware request może być zbyt duży lub wymagać zmiany tool_choice; mierzyć realne payloady, fallback separate, nie gwarantować cache hit.
- Redakcja błędów: obecny adapter ma body w RuntimeError; nowe native ścieżki muszą sanitizować przed persistencją/diagnostyką.
- Native opaque item nie jest kopią portable summary; model/provider switch i downgrade wymagają raw-history recovery.
- Badania i procenty konkurencyjnych produktów z dokumentu źródłowego nie zostały niezależnie zweryfikowane i nie są warunkiem doboru architektury.
- Nie ma blokera dla rozpoczęcia P0. Native activation, live benchmark, zmiana defaultów i opcjonalne rozszerzenia mają osobne bramki opisane wyżej.

## Źródła
Sprawdzono 2026-11-12; dokumentacja publicznego OpenAI Responses, bez przypisywania zgodności backendowi subskrypcyjnemu ONE:
- OpenAI API — **Compaction**: https://developers.openai.com/api/docs/guides/compaction — server-side context_management, stateless replay, store=false/ZDR oraz różnica canonical standalone window.
- OpenAI API — **Keep the prefix unchanged**: https://developers.openai.com/api/docs/guides/preserved-reasoning — custom compaction suffix, wpływ zmian prefixu na reasoning, odrębne zachowanie native compaction.
- Dokument bazowy `/tmp/todo2.md`: inspiracje OpenCode/Cline/APEC i cele eksperymentów; pozostałe zewnętrzne twierdzenia pozostają niezweryfikowane. Dokładne wersje/model support rzeczywistego endpointu ustala T007; przykładowe nazwy modeli z propozycji nie są rekomendacją.

## Kryteria akceptacji projektu
- [ ] P0–P5 zrealizowane i zweryfikowane; opcjonalne T013–T015 nie blokują wydania.
- [ ] Legacy flag off zachowuje golden payloady, eventy i rekonstrukcję.
- [ ] V2: structured checkpoint, bounded ledger, adaptive tail/buffer, cache-aware fallback i 0 destrukcyjnych porażek.
- [ ] Native aktywne wyłącznie dla potwierdzonych capability; replay/resume/fork/provider switch/rollback i redakcja przetestowane. Brak wsparcia jawnie opisany jako odroczenie, nie ukończone native.
- [ ] Rehydratacja bounded, domyślnie file reread off; hints działają przez facade.
- [ ] Usage, cache i failures mierzone bez wycieków i podwójnego naliczania runów.
- [ ] Pełny gate, dokumentacja, bump i snapshots zgodne; raport odróżnia pomiary od celów.
- [ ] Nie wykonano merge/push/tag/PR/publikacji bez osobnej autoryzacji.
