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

- [ ] Headless tryb zadania (`one run "zadanie"`): pełna pętla do `finish`, kontrakt wyniku
      (summary + exit code 0/1), limity kroków, auto-retry, resume po przerwaniu
- [ ] Subagenci — delegowanie podzadań: tworzenie subagentów (osobne sesje z `parentSession`,
      izolowany kontekst, równoległe wykonanie, scalanie wyników); nowy tool (np.
      `spawn_subagent`/`delegate`) zarejestrowany w `tools/index.py`; limity równoległości
      i głębokości zagnieżdżenia
- [ ] Eskalacja agent→człowiek: agent pauzuje zadanie i pyta (niejednoznaczność, brak dostępu,
      decyzja polityczna); odpowiedź wraca do kontekstu; kanały: interactive, TUI, RPC,
      headless (file/pipe)
- [ ] Polityka kooperacji w trybie autonomicznym: bramki `--cooperation` opt-in per run;
      domyślnie agent działa bez pytań

## P1 — Integracje i operacje

- [ ] Intake zadań: zadanie z pliku/spec, `@file`, parametryzacja
- [ ] Limity budżetu: tokens/czas/kroki z konfiguracją
- [ ] Raport końca zadania (log/notyfikacja) + utrzymanie RPC (`wait_for_idle`, steer w headless)
- [ ] Podpięcie extension widgets/overlays do warstwy TUI
- [ ] Snapshot/regression testy renderingu TUI

## P2 — Jakość i zgodność

- [ ] Testy integracyjne headless (end-to-end: zadanie -> wynik -> exit code)
- [ ] Snapshot tests dla RPC
- [ ] Testy auth precedence + provider fallback
- [ ] Cross-platform smoke (Linux/macOS path & shell semantics)

## Poza zakresem

- Parity 1:1 z `pi` (np. `/login` OAuth/subskrypcja, event payloady 1:1, golden tests z TS)
- Package manager npm/git jak w `pi` (rozszerzenia działają wg własnego kontraktu)
