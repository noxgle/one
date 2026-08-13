# TODO — Roadmapa autonomicznego agenta `one`

Celem projektu jest **autonomiczny agent terminalowy** wykonujący zlecone zadania
(shell / pliki / kod) z **opcjonalnym trybem kooperacji** z człowiekiem (bramki
zatwierdzania narzędzi, sterowanie w trakcie, docelowo pytania agenta).

Projekt powstał jako re-implementacja agenta `pi` i rozwija się samodzielnie —
parity 1:1 z `pi` nie jest już celem (patrz „Poza zakresem").

## Fundament (zrealizowane)

- [x] Tryby interfejsu: print (one-shot), interactive, TUI (Textual), RPC (JSON-RPC)
- [x] Sesje `.jsonl` z branchingiem/forkiem (drzewo sesji, `parentSession` — baza pod subagentów) + compaction
- [x] Pętla model -> tool -> model z eventami `turn_*`, `tool_*`, `retry_*` i live streamingiem
- [x] Auto-retry, abort (także w trakcie requestu), kolejki steer/follow_up
- [x] Limity bezpieczeństwa (`tools.maxSteps`, `tools.timeoutSec`), cap payloadu `toolResult`
- [x] Kooperacja: bramki zatwierdzania `bash`/`write`/`edit` (`--cooperation`, `/cooperation`, Ctrl+A)
- [x] Providerzy: `openai`, `anthropic`, `gemini`, `openrouter`, `ollama-cloud`, `llama.cpp` (lokalny, bez klucza)
- [x] Rozszerzenia/skille/prompty/themes (loader + runtime hooków opencode-style)
- [x] RPC: sesje, model, komendy, streaming eventów, extension UI, `wait_for_idle`

## P0 — Autonomia

- [x] Headless tryb zadania (`one run "zadanie"`): pełna pętla do `finish`, kontrakt wyniku
      (summary + exit code 0/1), limity kroków, auto-retry, resume po przerwaniu;
      `--json`/`--answer-file`/`--steer-file`, raport `reports.jsonl` w agent dir
- [x] Subagenci — delegowanie podzadań: tool `spawn_subagent` (osobne sesje z `parentSession`,
      izolowany kontekst, równoległe wykonanie, scalanie wyników) zarejestrowany
      w `tools/index.py`; limity równoległości (`subagents.maxConcurrent`) i głębokości
      zagnieżdżenia (`subagents.maxDepth`)
- [x] Eskalacja agent→człowiek: tool `ask_user` — agent pauzuje zadanie i pyta; odpowiedź
      wraca do kontekstu; kanały: interactive, TUI, RPC (`answer_question`), headless
      (`--answer-file`/canned fallback), timeout + abort
- [x] Polityka kooperacji w trybie autonomicznym: bramki `--cooperation` opt-in per run;
      domyślnie agent działa bez pytań

## P1 — Integracje i operacje

- [x] Intake zadań: zadanie z pliku/spec, `@file`, parametryzacja
- [x] Provider lokalnego Ollamy: adapter OpenAI-compatible (domyślnie `http://localhost:11434/v1`,
      env `OLLAMA_BASE_URL`), obsługa lokalnych modeli; wzorzec jak `llama.cpp` (bez klucza API)
- [x] `/new` w TUI i interactive: tworzenie nowej sesji (RPC `new_session` już istnieje)
      + przebindowanie eventów; alias `/ns`
- [x] TUI: autouzupełnianie komend slash w polu input wg listy komend z `/help`
      (sugestie na `Tab` z cyklem, uzupełnianie prefiksu `/`)
- [x] Klient MCP: podłączanie zewnętrznych serwerów MCP jako źródła narzędzi
      (stdio, własny protokół JSON-RPC, bez nowych zależności); konfiguracja serwerów
      w settings.json (`mcpServers`), narzędzia MCP dostępne jak narzędzia sesji
- [x] Limity budżetu: tokens/czas z konfiguracją (`budget.maxTokens`/`budget.maxTimeSec`); kroki: `tools.maxSteps`
- [x] Raport końca zadania (log `reports.jsonl` w agent dir) + utrzymanie RPC (`wait_for_idle` istnieje; steer w headless przez `--steer-file`)
- [x] Podpięcie extension widgets/overlays do warstwy TUI (dedykowany panel dla widgetów, panel-overlay dla overlay; odpowiedzi przez istniejący input, reset przy /new i /fork)
- [x] Snapshot/regression testy renderingu TUI (3 deterministyczne pełnoekranowe snapshoty SVG: baza, widget, overlay; goldeny w tests/snapshots/tui)

## P2 — Jakość i zgodność

- [x] Testy integracyjne headless (E2E subprocess: CLI -> lokalny OpenAI-compatible HTTP -> finish -> wynik/exit code 0/1 + reports.jsonl)
- [x] Snapshot tests dla RPC (deterministyczne JSONL: sukces i błąd, goldeny w tests/snapshots/rpc)
- [x] Testy auth precedence + provider fallback (runtime > auth file > env, placeholder keys, fallback wyboru modelu)
- [x] Cross-platform smoke (portable ścieżki/sanitizacja + POSIX smoke cwd/quoting/pipeline/prefix/exit code; testy shellowe skip na Windows)
- [x] Providerzy OpenAI-compatible: xAI (Grok), DeepSeek, Mistral, Groq — registry, env keys, builtin modele i testy

## Poza zakresem

- Parity 1:1 z `pi` (np. `/login` OAuth/subskrypcja, event payloady 1:1, golden tests z TS)
- Package manager npm/git jak w `pi` (rozszerzenia działają wg własnego kontraktu)
