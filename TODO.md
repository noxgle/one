# TODO — Jedno wykonanie dla całego drzewa agentów

## Cel i priorytet

**P0: zarządzanie agentem głównym i wszystkimi potomkami jako jednym wykonaniem (`run`).** Wspólny budżet, hierarchiczny podgląd i anulowanie całego drzewa mają zachować autonomię delegowania bez mnożenia limitów ani pozostawiania osieroconej pracy.

Stan dokumentu: fazy 1–2 (T001–T005) zaimplementowane; fazy 3–4 pozostają otwarte. Pełna weryfikacja offline: 2673 testy przeszły, 2 pominięto; Ruff i `git diff --check` zakończyły się powodzeniem (szczegóły poniżej). Ten dokument nie przenosi ani nie zmienia historycznej roadmapy z `tmp/subagents_TODO.md`.

## Kontekst i dowody z repozytorium

Poniższa mapa T001 to historyczna analiza sprzed integracji (numery linii mogą być nieaktualne), nie opis braków obecnej implementacji. Aktualny stan i dowody T003–T005 są w fazie 2.

- **Potwierdzone:** w katalogu głównym nie było `TODO.md` podczas inspekcji.
- **Potwierdzone:** `one/core/agent_session.py` tworzy dzieci jako osobne `AgentSession`, współdzieląc m.in. ustawienia, providery i ograniczenia narzędzi/MCP. Zapisuje `parentSession` i `subagentDepth`.
- **Potwierdzone:** `_spawn_subagent` sprawdza głębokość i wielkość przekazanej listy względem `maxConcurrent`; sam ten mechanizm nie ustanawia wspólnego limitu dla wszystkich gałęzi.
- **Potwierdzone:** `_budget_exceeded` korzysta ze statystyk danej sesji i czasu `_session_started_at`. `settings_manager.py` ma `budget.maxTokens` i `budget.maxTimeSec`, bez limitu pieniężnego w tym bloku.
- **Potwierdzone:** `abort()` anuluje lokalne aktywne zadania narzędzi i czatu, zatrzymuje bash i budzi pytania. `_run_subagent` ma watchdog, obsługę `CancelledError` i cleanup. Integracja run ujednolica te ścieżki, zachowując lokalne zabezpieczenia.
- **Potwierdzone:** `one/core/subagent_monitor.py` obserwuje bezpośrednie dzieci, ogranicza wielkość podglądów i redaguje dane; `subagent_history.py` zapewnia historię. Nie traktować tych podglądów jako źródła rozliczeń.
- **Potwierdzone:** `tui_runtime_host.py` udostępnia monitor/history przez granicę runtime; `agent_runtime_facade.py` sanitizuje zdarzenia. Zachować owner loop, odłączone DTO, epoch i ochronę przed nieaktualnymi listenerami.
- **Potwierdzone:** istnieją testy subagentów, presetów, monitoringu i granicy TUI; `AGENTS.md`, `CONTRIBUTING.md` i `CHANGELOG.md` określają wymagania regresji i wydania.
- **Założenie produktowe:** jedno wykonanie zaczyna się przy przyjęciu głównego polecenia użytkownika i obejmuje wszystkie jego iteracje, retry, kompakcje i delegacje aż do zakończenia/anulowania. Następne niezależne polecenie tworzy nowy `runId`; pytanie pomocnicze i odpowiedź nie tworzą nowego run.
- **Wynik T001:** mapa poniżej utrwala punkty integracji sprzed implementacji; działający `RunContext` i wspólny budżet opisuje faza 2.

## T001 — mapa przepływów i rozstrzygnięcia dla T002–T004

### Granice i właściciel run

- `AgentSession.prompt` (`one/core/agent_session.py:4061`) jest wspólną granicą przyjęcia: tryb headless `run_mode.py:119`, print/JSON `print_mode.py:15`, interaktywny `interactive_mode.py:1394`, TUI `tui_mode.py:7500` przez host `tui_runtime_host.py:899`, RPC `rpc_mode.py:48,102,167` oraz SDK zwracające sesję/host (`core/sdk.py`). RPC uruchamia prompt w zadaniu i koreluje go z ID komendy; samo przyjęcie RPC nie dowodzi rozpoczęcia generacji. CLI resume/continue (`cli/main.py:467`) przywraca sesję, nie wznawia żywego run.
- Idle prompt korzenia rozpoczyna nowy run; wewnętrzne iteracje narzędzi, naprawa, retry i automatyczna kompakcja należą do niego. `_run_subagent` tworzy osobną sesję i wywołuje `sub.prompt` (`agent_session.py:2578,2716`): musi otrzymać ten sam obiekt koordynatora i nowy węzeł **przed** uruchomieniem zadania, nigdy nowy budżet. Przywrócenie/fork po restarcie nie przywraca aktywnej pracy ani puli; następny idle prompt dostaje nowy run. Wznowienie logicznego zadania po zakończeniu jest nowym run, a nie ukrytym restartem starego limitu.
- Podczas streamu `prompt` tylko kolejkuje `steer`/`followUp` (`agent_session.py:4068`); `ask_user`/`answer_question` (`:2938,2979`) nie inicjuje promptu. Steering wstrzyknięty w bieżącą pętlę należy do run; kolejki automatycznie opróżniane rekurencyjnym `prompt` po `agent_end` (`:4817–4842`) muszą zachować ten sam run aż do zakończenia tego opróżniania, w tym automatyczną kompakcję po odpowiedzi. `finish`, terminalny błąd, abort oraz tryby steering inne niż `interrupt` nie opróżniają kolejki automatycznie; pozostawione po terminalnym run wiadomości nie utrzymują go przy życiu. Przejęcie kontekstu musi być jawne, nie oparte tylko na `_is_streaming` lub `agent_start/end`: każde rekurencyjne wywołanie emituje własne zdarzenia. Po anulowaniu nie startować kolejki w nowym run.
- Limit `tools.maxSteps` kończy prompt komunikatem `tool_step_limit` (`:4146,4459`), nie ma automatycznego kontynuowania; kolejny prompt użytkownika to nowy run, nawet jeśli brzmi „continue”. Osobne `compact` z RPC/TUI/interactive (`rpc_mode.py:278`, `tui_runtime_host.py:1398`, `interactive_mode.py:796`) może wywołać model poza promptem: decyzja T004 — samodzielny run operacji compaction z własnym snapshotem budżetu, również przy wywołaniu przez SDK; pominięta kompakcja nie tworzy płatnej próby. Równoczesną kompakcję busy nadal odrzucać zgodnie z obecnym kontraktem.

### Płatne wywołania i księgowanie

- Jedyny bezpośredni `provider.chat` dla sesji jest w `_invoke_provider` (`agent_session.py:3397,3584`). Wejścia: główna generacja `:4180`, nudge `:4250`, naprawa `:4323`, streszczenie `:5056`. Każde przejście to osobny request, również po błędzie/ponowieniu; rezerwować bezpośrednio przed stworzeniem zadania providera i rozliczać na podstawie surowego `ChatResult.usage` przed przekształceniem w wiadomość, także dla compaction i odpowiedzi nieutrwalonych w historii. `provider_request_start/end` (`:3562–3672`) mają własny UUID i `purpose`, opcjonalne `turnId`, `attempt`, `parentRequestId`; end jest emitowany w `finally` przed obróbką usage. Nie liczyć z samych eventów ani z assistant messages.
- Preflight `:4040`, recovery po context limit `:4648` i automatyczna kompakcja po odpowiedzi `:4817` używają `compact`; ręczna `:4879`, a `_summarize_context` `:5022` wywołuje model tylko przy odpowiednich ustawieniach i bez instrukcji zastępujących podsumowanie. Wyjątki streszczenia obecnie prowadzą do fallbacku (`:5062`): odmowa budżetowa nie może zostać tam po cichu zamieniona na sukces i dalszą generację. Retry `:4572–4648` odczytuje ustawienia ponownie, dopuszcza unlimited oraz jedną niezależną próbę odzyskania kontekstu; wszystkie próby, także nieudane, pozostają w tym samym run.
- Osobny płatny probe logowania `one/core/provider_login.py:188` wywołuje adapter bez sesji; nie przypisywać go arbitralnie do run promptu. Zachować jako oddzielną operację uwierzytelnienia poza `runBudget` i udokumentować to w T010; nie przechodzi przez `_invoke_provider`. Odkrywanie modeli, katalog i lokalne liczenie tokenów to osobne wywołania sieciowe, nie generacje `chat`; nie zakładać, że ich dostawca nigdy nie pobiera opłaty.
- Dzisiejsze mapowanie `:3676` używa `prompt_tokens`/`input_tokens`, `completion_tokens`/`output_tokens`, oddzielnych pól cache Anthropic i kosztu 0. OpenAI-compatible przekazuje surowe usage (`providers/openai_compatible.py:445,496`), Codex Responses podobnie (`codex_responses.py:284,580`), Anthropic scala zdarzenia (`anthropic.py:362,375`), Gemini zwraca `usageMetadata` w innych nazwach (`gemini.py:239,292`), a stream Ollama zwraca pusty usage (`ollama.py:200`). Zatem obecne zera nie są dowodem zerowego zużycia; błędy, timeout i przerwanie mogą nie zwrócić usage. Normalizacja musi zachować raw/źródło i rozróżniać brak od 0.
- Kontrakt ledgeru: sumować rozłączne kategorie wejścia i wyjścia zgodnie z udokumentowaną semantyką konkretnego adaptera; cache wejścia i reasoning wyjścia jako podzbiory, gdy dostawca tak je definiuje, nie dodawać powtórnie; dla Anthropic doliczyć oddzielne cache read/write do wejścia tylko gdy są wyłączone z `input_tokens`. Gemini i inne pola mapować jawnie według semantyki API, nie przez zgadywanie. Nieznane lub sprzeczne kategorie oznaczyć unknown, zachować konserwatywną rezerwację i nie udawać kompletnej sumy. Testować z fixture'ami każdego adaptera i częściowym usage.
- `get_session_stats` sumuje bieżące wiadomości, w tym cache osobno (`agent_session.py:5394,5460`); `_budget_exceeded` (`:5314`) używa tych statystyk i zegara utworzenia sesji `:291`, sprawdzając granicę między krokami (`:4161,4396`). Kompakcja może usunąć historię, a nieudane/ukryte odpowiedzi nie muszą do niej trafić. Zachować to legacy zachowanie osobno, nie używać go jako ledgeru run. `budget` domyślnie ma dwa zera (`settings_manager.py:77,540`), nie jest to `runBudget`.
- W `model_registry.py`, `types.py` i `model_catalog.py` nie znaleziono pól cenowych; koszt wiadomości jest stałym zerem. T003 musi dodać jawne wersjonowane źródło cen/modelu i walidowane stawki, zamiast traktować legacy zero jako cenę. Istniejąca estymacja wejścia (`agent_session.py:3967`) nie jest dowiedzioną górną granicą; przy aktywnym koszcie brak wiarygodnych stawek lub ograniczenia wejścia/wyjścia oznacza odmowę. Adapter bez obsługi `max_tokens` (`:3524`) nie daje wymuszalnego limitu wyjścia: nie dopuszczać go przy wymaganej rezerwacji ograniczonej, zamiast udawać egzekwowanie. Nie obiecywać ścisłego limitu rachunku.

### Kontrakty implementacyjne

- `runBudget`: nieobecny blok i brakujące pola oznaczają 0 (bez limitu); obiekt tylko z `maxTokens`, `maxTimeSec`, `maxCostUsd`, `maxActiveSubagents`. Pierwszy, drugi i czwarty to nieujemne całkowite liczby bez bool; koszt nieujemna skończona liczba dziesiętna, nie bool; odrzucać null, tekst, nieznane klucze, wartości ujemne, NaN i nieskończoność z nazwą pola. Przeliczać koszt bez sumowania float; snapshot przed pierwszym admission, walidacja również przy SDK i konfiguracji plikowej. Legacy `budget` nie zmienia semantyki.
- Zdarzenia są addytywne: nowe `run_start`, `run_node_start`, `run_node_end`, `run_end`, `run_budget_update` mają `runId`; węzłowe dodatkowo `nodeId`, nullable `parentNodeId`, `sessionId`, stan i przy końcu reason. Snapshot budżetu i liczniki przekazywać jako odłączone, sanitizowane DTO, bez promptów/sekretów. Do istniejących provider start/end dodać `runId`, `nodeId`, zachować UUID `requestId` i wszystkie obecne pola; `provider_request_end` nie jest potwierdzeniem settlement. `run_end` dopiero po cleanup potomków i kolejki. Istniejące `agent_start/end` i `turn_start/end` nie zastępują cyklu run i nie wolno zmieniać ich kolejności/payloadu inaczej niż addytywnie.
- T002 testuje tożsamości, kolejkę i jednokrotną finalizację; T003 atomowe rezerwacje, niepewność oraz walidację; T004 rzeczywiste punkty wywołań, login poza run, niezależną kompakcję, retry, resume i child/grandchild z fake providerami. Obecne testy wskazujące punkty odniesienia: `tests/test_agent_timeouts_limits.py:21`, `tests/test_agent_retry_abort.py:234`, `tests/test_subagents.py`, `tests/test_agent_tool_parsing.py:118`. Nie są dowodem działania przyszłego kontraktu.

## Zakres

**W zakresie:** współdzielony kontekst wykonania, identyfikatory i stany węzłów, globalne limity tokenów/czasu/kosztu i liczby aktywnych potomków, rozliczanie requestów, anulowanie run/poddrzewa, hierarchiczny monitoring, zapis metadanych i bezpieczna zgodność ze starymi sesjami, TUI oraz wyjście programistyczne.

**Poza zakresem:** rozproszony scheduler, nowa usługa zewnętrzna, sandbox OS, automatyczny dobór agentów, wymuszanie limitu rachunku po stronie dostawcy, cofanie skutków narzędzi, wznowienie żywych zadań po restarcie, limity konta obejmujące niezależne procesy ONE.

## Architektura i decyzje

### ADR-001 — Kontekst wykonania zamiast sumowania sesji po fakcie

Dodać `RunContext`/koordynator w nowym `one/core/run_context.py`, przekazywany jawnie do korzenia i każdego potomka. Właścicielem aktywnych uchwytów jest runtime event loop; UI otrzymuje tylko sanitizowane DTO. Węzeł ma `runId`, `nodeId`, `parentNodeId`, `sessionId`, głębokość, stan i powód zakończenia. `sessionId` nie jest tożsamością wykonania: ta sama sesja może zawierać wiele run.

- Alternatywa: agregacja JSONL/historii po zakończeniu — dobra do audytu, za późna do admission control i anulowania.
- Alternatywa: centralny serwis/kolejka — niepotrzebny koszt operacyjny dla obecnego runtime jednego procesu.
- Wybór: koordynator w pamięci oraz trwałe metadane/historia. Brak nowej zależności infrastrukturalnej.

### ADR-002 — Budżet jest wspólny, rezerwacje są atomowe

Nowy, opt-in blok `runBudget` z `maxTokens`, `maxTimeSec`, `maxCostUsd`, `maxActiveSubagents`; zero oznacza brak danego limitu. Istniejący `budget` i lokalne timeouty pozostają bez zmiany znaczenia, jako dodatkowe ograniczenia. Snapshot limitów przy starcie run; dziecko/preset nie może ich zwiększyć ani wyzerować. Widok rozróżnia limity lokalne i run.

Przed każdym wywołaniem providera: atomowa rezerwacja wejścia i ograniczonego maksymalnego wyjścia; po zakończeniu rozliczenie dokładnie raz po `requestId`. Retry i kompakcja to oddzielne, płatne próby. Całkowite usage nie może zależeć od przycinanej historii wiadomości. Sumować tylko własne usage węzłów, nie ich sumy poddrzew. Cache/reasoning raportować osobno i zdefiniować normalizację bez podwójnego naliczania. Pieniądze przechowywać jako Decimal albo całkowite jednostki, nie sumować float.

Przy aktywnym limicie pieniężnym brak wiarygodnej ceny lub górnej granicy żądania blokuje admission z czytelną przyczyną; nie oznacza ceny zero. Przy braku usage po przerwaniu zachować konserwatywną rezerwację i oznaczyć niepewność. Koszt to estymacja wg zapisanej wersji cen, nie faktura. Nie obiecywać bezwzględnego limitu rachunku dostawcy: dokumentować różnice tokenizacji, opóźnione usage i ewentualne przekroczenie. Deadline czasu jest monotoniczny i aktywnie budzi anulowanie także podczas streamu/narzędzia, nie tylko między turami.

Globalne pozwolenie na aktywnego potomka pobierać bez czekania: przy braku miejsca zwrócić kontrolowany wynik odmowy. Nie trzymać kolejki zagnieżdżonych spawnów, która może zakleszczyć rodzica oczekującego na dziecko. Istniejący `maxConcurrent` nadal działa lokalnie. Zarezerwować batch atomowo przed utworzeniem dzieci; odrzucić cały batch przy braku miejsc i zawsze oddać pozwolenia w cleanup.

### ADR-003 — Anulowanie i obserwacja to kontrakty wykonania

Stany węzłów: `pending → running → cancelling → cancelled` lub `completed/failed/timed_out/interrupted`. Przejścia terminalne są jednokrotne; powód (`user_cancel`, `budget_exceeded`, `parent_cancel`, `timeout`, `runtime_shutdown`) jest oddzielny od statusu. Run staje się terminalny dopiero po zamknięciu potomków lub jawnym oznaczeniu niepewnego cleanup.

Anulowanie najpierw zamyka admission danego poddrzewa, następnie sygnalizuje wszystkie poziomy i dołącza do zadań z ograniczonym czasem cleanup. Rodzic anuluje potomków; anulowanie pojedynczej gałęzi nie anuluje rodzeństwa. Zwykły błąd dziecka nie uruchamia domyślnie globalnego fail-fast. Globalny limit anuluje cały run. Root `finish` nie może pozostawić pracujących potomków. Lokalny timeout rodzica anuluje jego poddrzewo. Nie oznaczać częściowych skutków zewnętrznych jako cofniętych.

Monitoring korzysta z tego samego rejestru węzłów i ledgeru, nie z rekursywnego odpytywania sesji przez UI. Oddzielić własne zużycie od sum poddrzewa. Globalnie ograniczyć liczbę payloadów podglądu, stronicować historię i zachować podsumowania liczników nawet po usunięciu szczegółów.

## Plan wykonania

### Faza 1 — Kontrakty i model run
- **Wynik:** jednoznaczne granice wykonania i testowalny koordynator.
- **Nakład / pewność:** M / średnia.

- [x] **T001 — Utrwalić kontrakty integracji i zgodności**
  - **Zmiany:** prześledzić prompt/continue, retry, kompakcję, usage i wszystkie drogi providerów; doprecyzować schemat zdarzeń, walidację `runBudget`, semantykę normalizacji tokenów i mapę wywołań. Uaktualnić ten plan przed implementacją, jeśli źródła ujawnią konflikt.
  - **Pliki:** `TODO.md`; inspekcja `one/core/agent_session.py`, `one/core/agent_session_runtime.py`, `one/core/sdk.py`, `one/modes/`, `one/providers/`, `one/core/settings_manager.py`.
  - **Zależności:** brak.
  - **Akceptacja:** każda płatna ścieżka i granica run ma wskazany punkt integracji; opisane legacy budget, kontynuacja, retry i niepewne usage. Brak niejawnego resetu limitu przez dziecko lub resume.
  - **Weryfikacja:** review mapy przepływów względem źródeł i istniejących testów; bez wywołań dostawców.

- [x] **T002 — Dodać koordynator, tożsamość i stany drzewa**
  - **Zmiany:** rejestracja węzłów, immutable limits, uchwyty zadań na owner loop, kontrola admission, jednokrotna finalizacja i zegar wstrzykiwalny do testów.
  - **Pliki:** nowe `one/core/run_context.py`, nowe `tests/test_run_context.py`; `one/core/types.py` jeśli kontrakty wymagają wspólnych DTO.
  - **Zależności:** T001.
  - **Akceptacja:** niemożliwe cykle, duplikaty i rejestracja po anulowaniu; rozdzielone tożsamości run/sesji; finalizacja korzenia nie ukrywa aktywnego wnuka.
  - **Weryfikacja:** implementer uruchamia `pytest -q tests/test_run_context.py`, z testami niepoprawnych przejść i ponownej finalizacji.

### Faza 2 — Wspólny budżet i kontrola pracy — zakończona
- **Wynik:** korzeń, dzieci i wnuki korzystają z jednego `RunContext`, snapshotu limitów i ledgeru; delegowanie nie tworzy nowej puli.
- **Zakres dowodu:** testy jednostkowe i integracyjne z fake-providerami oraz pełna istniejąca regresja; nie jest to zakończenie pozostałego zakresu faz 3–4.

- [x] **T003 — Wdrożyć ledger, rezerwacje i konfigurację budżetu**
  - **Implementacja:** walidowany opt-in `runBudget`, atomowe rezerwacje i batch admission, idempotentne rozliczanie po request ID, pieniądze w `Decimal`, monotoniczny deadline i globalne sloty potomków. Snapshot cen wiąże dokładną tożsamość modelu/provider/endpoint z jawną wersją i źródłem z `models.json`; brak wbudowanego fallbacku cen.
  - **Niepewność:** normalizacja zachowuje raw/source, rozróżnia brak od zera i nie podwaja cache/reasoning. Niepełne/sprzeczne usage po sukcesie, błędzie lub anulowaniu pozostawia konserwatywną rezerwację, nie fikcyjne zerowe zużycie. Brak stosownej ceny blokuje aktywny limit kosztu.
  - **Pliki:** `one/core/run_context.py`, `run_pricing.py`, `run_usage.py`, `model_registry.py`, `settings_manager.py`, `types.py`.
  - **Dowody:** `tests/test_run_budget.py`, `tests/test_run_pricing.py`, `tests/test_run_usage.py`, `tests/test_pricing_registry.py`: konkurencyjne rezerwacje, dokładna granica kosztu, cache, nieznane usage/ceny, walidacja schematu i zachowanie cen po zapisie/odczycie rejestru.

- [x] **T004 — Podłączyć run do sesji, spawnów i wszystkich wywołań modeli sesji**
  - **Implementacja:** `_run_operation` obejmuje prompt i samodzielną kompakcję; `_invoke_provider` rezerwuje i rozlicza każdą próbę, również retry, nudge/repair i streszczenie. Automatyczna kompakcja i opróżnianie kolejki pozostają w run; następny niezależny prompt i ręczna kompakcja dostają nowy run. Dzieci dziedziczą kontekst i zamrożone limity. Batch jest rezerwowany przed startem, a błąd inicjalizacji wycofuje rezerwacje i sloty.
  - **Konserwatywne admission:** dla limitu tokenów lub kosztu rezerwowane jest pełne znane capacity wejścia (ograniczone wybranym oknem kontekstu), a nie oszacowana długość promptu, plus maksymalne wyjście. Wymagany jest znany limit wejścia/kontekstu i wymuszany przez adapter limit wyjścia (`max_tokens`). Brak granicy lub niewystarczająca pula odmawia przed wywołaniem providera; mały prompt nie oznacza małej rezerwacji. Legacy `budget`, lokalne timeouty i ceilings preset/tool/MCP nadal obowiązują dodatkowo.
  - **Poza zakresem:** płatny probe logowania w `provider_login.py` jest osobną operacją uwierzytelnienia poza run i `runBudget`.
  - **Dowody:** `tests/test_run_budget_runtime.py`, `tests/test_run_integration.py`: korzeń/dziecko/wnuk, wspólna pula po wydatku korzenia, retry/kompakcja/kolejka, nowy run, zamrożone limity, globalny limit potomków, atomowy batch i rollback błędu startu.

- [x] **T005 — Wprowadzić propagację anulowania i deadline całego run**
  - **Implementacja:** abort, deadline i wyczerpanie budżetu zamykają admission i sygnalizują zadania drzewa; cancel poddrzewa nie anuluje rodzeństwa. Integracja obejmuje aktywny chat/narzędzia, lokalne watchdogi, zatrzymanie bash i oczekujące pytania; po anulowaniu nie opróżnia kolejki. Nie zamyka współdzielonego managera MCP dla pojedynczej gałęzi.
  - **Cleanup:** ograniczone oczekiwania używają `_RUN_CLEANUP_TIMEOUT_SEC = 1.0` (na etap oczekiwania, nie gwarantowany całkowity czas zatrzymania). Niedokończony cleanup oznacza unknown external state i `RunCleanupTimeoutError`; sesja pozostaje zablokowana dla nowej pracy, zamiast zgłaszać bezpieczny sukces. To kontrola aplikacyjna: brak gwarancji zatrzymania zewnętrznych operacji, izolacji OS lub cofnięcia skutków.
  - **Dowody:** `tests/test_run_cancellation.py`: root abort/deadline/budget i trzy poziomy, cancel gałęzi z żywym rodzeństwem, lokalne timeouty, odpowiedź/abort/cancel/deadline przy pytaniu, cleanup timeout blokujący ponowne admission. `tests/test_run_context.py` pokrywa stany i powtarzany cancel.

#### Konfiguracja i jawny schemat cen

`settings.json` (globalny lub projektowy): limity całego drzewa, snapshot przy starcie run. Zero lub brak pola wyłącza dany limit; liczby tokenów, sekund i potomków muszą być nieujemnymi liczbami całkowitymi, koszt skończoną nieujemną liczbą. Nie zastępuje lokalnego `budget`.

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

`models.json` w katalogu konfiguracji agenta: **syntetyczny przykład schematu, nie cennik produkcyjny**. Zastąpić stawki, wersję i źródło zweryfikowanym harmonogramem dla konkretnego endpointu/modelu, wszystkich używanych progów kontekstu i czasu przechowywania cache. Nie ma automatycznej aktualizacji ani domyślnych cen.

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

Schema 1 wymaga wszystkich pokazanych pól poza `cacheRead`/`cacheWrite`; nieznane pola są odrzucane. Stawki to nieujemne skończone ciągi dziesiętne (lub liczby całkowite, nie float). `cacheMode` to `none`, `read` lub `read_write`: `none` jawnie deklaruje brak cache i zabrania stawek cache, `read` zabrania `cacheWrite`. Brak stawki dla włączonego trybu cache oznacza unknown, nie zero; przy aktywnym limicie kosztu uniemożliwia bezpieczne admission. Rezerwacja wejścia używa najwyższej stosownej stawki, rozliczenie pełnego usage używa rozłącznych kategorii. Cena i zużycie są estymacją aplikacyjną, **nie gwarancją rachunku dostawcy**; błędny harmonogram, tokenizacja, opóźnione lub brakujące usage i skutki zewnętrzne pozostają ryzykiem operatora.

#### Weryfikacja fazy 2

Uruchomiono offline z tymczasowym `ONE_CODING_AGENT_DIR`:

```bash
.venv/bin/python -m pytest -q tests/test_run_context.py tests/test_run_budget.py tests/test_run_pricing.py tests/test_run_usage.py tests/test_pricing_registry.py tests/test_run_budget_runtime.py tests/test_run_integration.py tests/test_run_cancellation.py
```

Wynik ukierunkowany: **134 passed**. Następnie 2026-11-03 wykonano pełne bramki w wymaganej kolejności, z izolowanym `ONE_CODING_AGENT_DIR`: `.venv/bin/python -m pytest -q` — **2673 passed, 2 skipped**; `.venv/bin/ruff check .` — **All checks passed**; `git diff --check` — bez błędów. Pełny zestaw obejmuje regresje interfejsów, granicy runtime i zdarzeń. Przejście obecnych testów nie zamyka nowych scenariuszy i funkcji zaplanowanych w fazach 3–4.

### Faza 3 — Historia i hierarchiczny monitoring
- **Wynik:** spójne drzewo i liczniki dostępne bez dostępu UI do żywych sesji.
- **Nakład / pewność:** L / średnia.

- [ ] **T006 — Zapisać metadane run i bezpieczne odtworzenie historii**
  - **Zmiany:** wersjonowane rekordy wykonania i agregatów w istniejącym systemie persistence/sidecar; nie nadpisywać jednego runId w nagłówku wielokrotnie używanej sesji. Po restarcie aktywne rekordy oznaczać `interrupted`; resume/fork rozpoczyna nowy run z odnośnikiem do historii, bez reaktywacji starych uchwytów.
  - **Pliki:** `one/core/session_manager.py`, `one/core/subagent_history.py`, `one/core/persistence.py`, `one/core/agent_session_runtime.py`; nowe `tests/test_run_history.py`, `tests/test_subagent_history.py`.
  - **Zależności:** T004, T005.
  - **Akceptacja:** stara sesja bez run metadata nadal się otwiera; częściowy zapis nie odtwarza aktywnego zadania; odczyt historii nie resetuje budżetu w aktywnym run; zachowane ceilings presetów i dotychczasowe sidecary.
  - **Weryfikacja:** `pytest -q tests/test_run_history.py tests/test_subagent_history.py tests/test_preset_sessions.py`; fixtures starych, uciętych i wielorunowych sesji, wyłącznie scratch state.

- [ ] **T007 — Udostępnić ograniczone snapshoty drzewa i kontrolę przez runtime**
  - **Zmiany:** hierarchia, własne/agregowane usage, rezerwacje, limity i powody zatrzymania; stronicowanie węzłów i detali, revision/epoch, komendy cancel run/subtree z walidacją przynależności. Zdarzenia additive, sanitizowane; zachować istniejące bezpośrednie listy dzieci jako adapter zgodności.
  - **Pliki:** `one/core/subagent_monitor.py`, `one/core/agent_runtime_facade.py`, `one/core/tui_runtime_host.py`, `one/core/threaded_agent_runtime.py`; `tests/test_subagent_monitor.py`, `tests/test_agent_runtime_facade.py`, `tests/test_tui_runtime_boundary.py`.
  - **Zależności:** T005, T006.
  - **Akceptacja:** UI nie otrzymuje uchwytów sesji ani surowych wyników/sekretów; suma run zgadza się z ledgerem mimo retencji podglądów; stale epoch i obce nodeId nie anulują nowego run; drzewo nie powoduje nieograniczonego payloadu ani rekursji.
  - **Weryfikacja:** wskazane trzy moduły pytest; test szerokiego drzewa, paginacji, redakcji split credentials i przełączenia sesji w trakcie zdarzeń.

- [ ] **T008 — Pokazać drzewo, budżet i anulowanie w interfejsach**
  - **Zmiany:** rozwinąć istniejący inspector o hierarchię, postęp i pozostały budżet; anulowanie całości/wybranej gałęzi z czytelną informacją o nieodwracalności efektów. W headless/JSON/RPC udostępnić identyfikatory, status i podsumowanie; istniejący abort oznacza cały bieżący run. Utrzymać dotychczasowe exit codes i zdarzenia.
  - **Pliki:** `one/modes/tui_mode.py`, `one/modes/rpc_mode.py`, `one/modes/run_mode.py`, `one/modes/print_mode.py`, `one/modes/interactive_mode.py`, `one/core/sdk.py`, `one/cli/main.py`; testy poszczególnych trybów w `tests/` (dokładne moduły ustalić w T001), `tests/test_tui_snapshots.py`.
  - **Zależności:** T007.
  - **Akceptacja:** użytkownik widzi wnuki i powód stopu; anulowanie gałęzi nie zatrzymuje innej; niepewny koszt jest oznaczony; opóźnione zdarzenie starego run nie zmienia aktualnego widoku. Pause inspektora i załadowana historia nie migoczą przy aktualizacji.
  - **Weryfikacja:** Textual pilot dla drzewa i cancel, snapshoty małego terminala, offline scenariusze RPC/JSON oraz regresja istniejącego inspektora i exit codes 0/1/2.

### Faza 4 — Dowód działania i przygotowanie wydania
- **Wynik:** kontrolowany rollout z udokumentowanymi ograniczeniami.
- **Nakład / pewność:** M / wysoka po zakończeniu wcześniejszych faz.

- [ ] **T009 — Zabezpieczyć scenariusze end-to-end i wyścigi**
  - **Zmiany:** deterministyczna macierz trzech poziomów, równoległych gałęzi, wyczerpania każdego limitu, przerwania streamu, timeout rodzica, finish kontra cancel i restartu; sprawdzać brak double-count i task leaks. Nowe moduły uruchamiać jawnie, nie polegać na stałych listach helpera.
  - **Pliki:** `tests/test_run_integration.py`, `tests/test_run_budget.py`, `tests/test_run_cancellation.py`, `tests/test_run_history.py`, `tests/test_event_snapshots.py`, odpowiednie testy CLI/RPC/TUI z T001.
  - **Zależności:** T008.
  - **Akceptacja:** jedna pula i jedna przyczyna globalnego zatrzymania; brak pracy po zamknięciu admission; zarchiwizowane liczniki i UI zgodne z ledgerem; dawne kontrakty presetów i zdarzeń zachowane lub jawnie rozszerzone.
  - **Weryfikacja:** implementer ustawia `export ONE_CODING_AGENT_DIR="$(mktemp -d)"` w zachowanej powłoce; następnie `.venv/bin/python -m pytest -q`, `.venv/bin/ruff check .`, `git diff --check` — w tej kolejności. Zero realnych API/MCP; explicit `@pytest.mark.asyncio`.

- [ ] **T010 — Udokumentować i przygotować zgodne wydanie**
  - **Zmiany:** opisać run lifetime, konfigurację opt-in i przykład budżetu, odrzucanie spawnów bez deadlocku, koszty estymowane, brak sandboxa/rollback efektów, resume i diagnostykę. Wersja jest już `0.1.127`, a snapshoty zaktualizowano wcześniej; nie wymaga to kolejnego bumpa. Unreleased i konfigurację fazy 2 opisano tutaj, ale końcowy przegląd dokumentacji/wydania i pełne bramki nadal są otwarte.
  - **Pliki:** `README.md`, nowe `docs/RUNS.md`, `CHANGELOG.md`, `one/config.py`, `tests/snapshots/tui/`, `tests/test_tui_snapshots.py`.
  - **Zależności:** T009.
  - **Akceptacja:** użytkownik odróżnia runBudget od legacy budget; dokumentacja nie obiecuje limitu faktury ani cofnięcia efektów; wersja pochodzi z `one/config.py`; brak publikacji/tagowania bez osobnej zgody.
  - **Weryfikacja:** przegląd przykładów względem walidatora; `ONE_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest -q tests/test_tui_snapshots.py`, review diffów i ponowne pełne bramki z T009.

## Wdrożenie i wycofanie

Najpierw wspólna tożsamość/ledger i testy, następnie egzekwowanie opt-in runBudget, dopiero potem interfejsy. Nie zmieniać po cichu znaczenia istniejącego `budget`. Nie wymagać migracji destrukcyjnej; zachować stare JSONL i sidecary. Wyłączenie nowych limitów dotyczy przyszłych run, nie mutuje snapshotu aktywnego wykonania. Wycofanie wersji dopiero po zatrzymaniu aktywnych run i sprawdzeniu, że stary czytnik toleruje additive metadata; przy braku zgodności zachować dane i przywrócić wersję aplikacji bez ich nadpisywania.

## Ryzyka i kwestie otwarte

- **Usage/cena po fakcie:** konserwatywne rezerwacje, obowiązkowa wiarygodna cena dla cost admission i widoczna niepewność; nie składać gwarancji billingowych.
- **Nieanulowalny kod rozszerzeń/MCP lub zewnętrzny proces:** ograniczony cleanup i status niepewności; to kontrola aplikacyjna, nie izolacja OS.
- **Deadlock zagnieżdżonych agentów:** odmowa admission zamiast kolejki trzymającej sloty; test pełnego globalnego limitu.
- **Wyciek danych i eksplozja monitoringu:** istniejąca redakcja, globalny limit podglądów i stronicowanie; finansowy ledger nie zależy od retencji widoków.
- **Różne granice wykonania między trybami:** testy fazy 2 pokrywają granice sesji, kolejkę i samodzielną kompakcję; pełna regresja CLI/RPC/TUI/SDK i nowe widoki pozostają w fazach 3–4.
- **Kalibracja limitów:** brak ustalonego domyślnego limitu pieniężnego; wybrano zero/opt-in zamiast zgadywania budżetu użytkownika. Cleanup ma ograniczone oczekiwania po 1 s; nie jest to gwarancja czasu zatrzymania niekooperującego kodu ani procesów zewnętrznych.

## Kryteria końcowe projektu

- [ ] Jeden `runId` obejmuje korzeń, dzieci i wnuki; kolejne polecenie ma odrębny run.
- [ ] Globalne tokeny/czas/koszt i aktywni potomkowie nie mnożą się przez delegowanie; niepewność rozliczeń jest jawna.
- [ ] Cancel korzenia i wyczerpanie globalnego budżetu propagują się na wszystkie poziomy; cancel poddrzewa nie narusza rodzeństwa.
- [ ] Monitoring pokazuje hierarchię oraz własne i agregowane zużycie bez double-count i bez dostępu UI do żywych sesji.
- [ ] Stare sesje i polityki presetów nadal działają; restart nie reaktywuje pracy ani starego budżetu.
- [ ] Offline regresje, granica runtime i snapshoty przechodzą; ograniczenia kosztu i anulowania są udokumentowane.
