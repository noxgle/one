# TODO — Luki `one` względem oryginalnego `pi`

## Status (na teraz)

### P0 — Największe braki funkcjonalne

- [x] Zaimplementować pełny interactive TUI parity z `pi` (praktyczna parity P0)
  - [x] status line (model/thinking/context/cwd/queue/tokens/retry-state)
  - [x] podstawowe komendy slash (`/help`, `/stats`, `/state`, `/model`, `/thinking`, `/steer`, `/follow`, `/compact`, `/login`, `/config`, `/bash`, `/queue`, `/tools`, `/clear`, `/abort`, `/retry`)
  - [x] podstawowa obsługa skrótów/sterowania (`Ctrl+C` -> abort podczas streamingu)
  - [x] parity skrótów klawiszowych i układu TUI na poziomie P0 (aliasy slash, readline/history, stabilny status/footer)
- [ ] Zaimplementować pełny i stabilny tool-calling flow (jak w `pi`)
  - [x] pętla model -> tool -> model
  - [x] eventy `tool_call_start/end`, `tool_call_error`, `turn_start/end`, `auto_retry_start/end`
  - [x] limity bezpieczeństwa (`tools.maxSteps`, `tools.timeoutSec`)
  - [x] abort semantics (przerwanie promptu także podczas requestu do providera)
  - [x] ograniczenie payloadu `toolResult` do kontekstu modelu (cap znaków + head/tail fallback)
  - [x] reason-aware `turn_end` (`completed/abort/error/tool_step_limit`)
  - [x] kolejki `steer/follow_up` zachowane przy abort/retry
  - [x] domknięcie parity edge-case'ów i semantyki retry/abort/queue (P0 practical)
- [ ] Dodać pełne wsparcie providerów + auth/login parity
  - [x] auth precedence `runtime -> file -> env`
  - [x] wsparcie `openai`, `anthropic`, `gemini`, `openrouter`, `ollama-cloud`
  - [x] `/login` flow practical: walidacja provider/model + ustawianie default provider/model
  - [x] `/login status` + `/logout <provider>` + no-auth provider flow (`llama.cpp`)
  - [ ] pełny `/login` parity (subskrypcja/OAuth flow jak w `pi`)
- [x] Uzupełnić CLI parity (komendy pakietowe/config) (P0 practical)
  - [x] komendy `install/remove/update/list/config`
  - [x] idempotencja i walidacja usage + spójne exit codes dla error path
  - [x] semantyka flag `--mode/--print/--session/--fork` + usage errors/exit codes (P0 practical)

## P1 — Integracje i protokoły

- [ ] Uzupełnić RPC parity (komendy, eventy, streaming, extension UI)
- [ ] Zaimplementować pełny runtime extensions/skills/packages
- [ ] Dopracować session/compaction parity (branching, migracje, eksport)

## P2 — Jakość i zgodność

- [ ] Dodać golden compatibility tests 1:1 z TS
- [ ] Dodać snapshot tests dla RPC
- [ ] Dodać testy auth precedence + provider fallback

---

## Szczegóły braków

### 1. Interactive TUI
- [x] Header/status context usage
- [x] Footer/status token usage + retry state (praktyczna parity)
- [x] Komendy i skróty klawiszowe jak w `pi` (P0 practical: aliasy, `Ctrl+C/Ctrl+D/Ctrl+L/Ctrl+R`, queue/model/thinking control)
- [ ] UI hooks dla extension widgets/overlays

### 2. Tool-calling
- [x] Ujednolicony kontrakt wywołań narzędzi (baseline)
- [x] Obsługa błędów/retry/tool-result event parity (praktyczna)
- [x] Snapshoty regresyjne dla `turn_*`, `tool_*`, `retry_*` + edge-case abort/retry/queue
- [ ] Dodać pełny flow 1:1 bez uproszczeń względem `pi` (poza zakresem P0 practical)

### 3. Providers/Auth
- [x] Rozszerzona lista providerów (w tym `ollama-cloud`)
- [x] Adapter `ollama-cloud` bez pól nieobsługiwanych (`reasoning_effort`, wymuszone `temperature`)
- [x] Lepsza diagnostyka błędów provider API (status + body)
- [x] Dodać `/login` practical flow (provider/key/model + defaults)
- [ ] Dodać pełny `/login` parity subskrypcji/OAuth
- [x] Utrzymany precedence: runtime override -> plik auth -> env

### 4. CLI parity
- [x] Dodane i utwardzone komendy: `install/remove/update/list/config`
- [x] Ujednolicić semantykę flag i zachowanie trybów (P0 practical)

### 5. RPC parity
- [ ] Dodać brakujące komendy sesji/modelu/compaction/bash
- [ ] Ujednolicić event streaming i payloady z `pi`

### 6. Extensions/Skills/Packages
- [ ] Dodać pełne uruchamianie rozszerzeń (nie tylko discovery)
- [ ] Dodać package manager flow (npm/git) zgodny z `pi`

### 7. Sessions/Compaction
- [ ] Ujednolicić fidelity `.jsonl` i migracje
- [ ] Ujednolicić branching + branch summary + compaction behavior

### 8. Test parity
- [ ] Golden testy `build_session_context` na danych z TS
- [ ] Snapshoty odpowiedzi/eventów RPC
- [ ] Cross-platform smoke (Linux/macOS path & shell semantics)
