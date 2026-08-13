# TODO — Plan zadań

## Bugi

- [x] **TUI: /new wyświetla `{"cancelled": false}` zamiast cichego przejścia na nową sesję**
  - **Description:** Po wpisaniu `/new` w TUI, linia `{"cancelled": false}` jest wypisywana w strumieniu chatu (tui_mode.py:1052 — `self._write(json.dumps(result, ensure_ascii=False), "info")`). Wynik `new_session` z RPC zawiera `{"cancelled": false}` i niepotrzebnie pokazuje użytkownikowi.
  - **Files:** `one/modes/tui_mode.py` (linia ~1052)
  - **Acceptance Criteria:** Po `/new` w TUI nie pojawia się JSON `{"cancelled": false}` w strumieniu; sesja jest czyszczona i gotowa do nowej konwersacji.
  - **Verification Commands:** `.venv/bin/python -m pytest tests/ -q`

- [x] **TUI: po zmianie theme kolor czcionek w głównym oknie (stream) się nie zmienia**
  - **Description:** `_apply_theme()` (tui_mode.py:641) ustawia `stream_widget.styles.color = theme.screen_fg`, ale `stream_widget` to `#stream_container` (div/panel), a nie sam widget `#stream` (TextArea/Static ze strumieniem). Kolor tekstu w strumieniu nie odzwierciedla theme'owego koloru FG. Należy sprawdzić, czy widget `#stream` (nie `#stream_container`) ma ustawiony kolor i zaktualizować go w `_apply_theme()`.
  - **Files:** `one/modes/tui_mode.py` (linia ~652-655 — `_apply_theme`)
  - **Acceptance Criteria:** Po `/theme solarized` (lub innym) kolor tekstu w głównym oknie chatu natychmiast się zmienia na nowy, bez restartu aplikacji.
  - **Verification Commands:** `.venv/bin/python -m pytest tests/ -q`

## Usprawnienia TUI

- [x] **TUI: usunięcie sekcji Error / Provider z panelu sidebar (Info)**
  - **Description:** W `build_sidebar_snapshot()` (tui_mode.py:32) i `_refresh_sidebar()` (tui_mode.py:727) sidebar zawiera sekcję `[b ...]Errors[/]` z polami `lastToolError` i `lastProviderError`. Należy usunąć te pola z snapshotu i sekcję Errors z renderowanego sidebaru.
  - **Files:** `one/modes/tui_mode.py` (linia ~32-67 `build_sidebar_snapshot`, linia ~742-762 `_refresh_sidebar`)
  - **Acceptance Criteria:** W sidebarze TUI nie pojawiają się sekcje "Errors", "Tool:" ani "Provider:" — tylko Info, Keys i ew. inne dane.
  - **Verification Commands:** `.venv/bin/python -m pytest tests/ -q`

- [x] **TUI + CLI: toggle subagentów (włącz/wyłącz) — komenda, skrót klawiszowy, status w sidebarze**
  - **Description:** Dodać możliwość włączania/wyłączania subagentów:
    - **CLI:** nowy flag `--no-subagents` (domyślnie on, lub odwrotnie — zależnie od polityki); `/config subagents.enabled false`
    - **TUI:** komenda `/subagents [on|off]`, skrót klawiszowy (np. `Ctrl+S`), status w sidebarze (`Subagents: on/off`)
    - **Settings:** nowy klucz `subagents.enabled: true` w settings (domyślnie `true`); gdy `false`, tool `spawn_subagent` jest pomijany podczas rejestracji narzędzi sesji
    - **AgentSession:** w `_spawn_subagent()` lub przed wywołaniem — sprawdź flagę i zwróć błąd "Subagents disabled"
  - **Files:** `one/cli/args.py`, `one/core/settings_manager.py`, `one/core/agent_session.py`, `one/tools/index.py`, `one/modes/tui_mode.py`, `one/modes/interactive_mode.py`
  - **Acceptance Criteria:**
    - `--no-subagents` w CLI blokuje `spawn_subagent`
    - `/subagents off` w TUI blokuje subagentów; `Subagents: off` widoczny w sidebarze
    - `Ctrl+S` w TUI przełącza stan subagentów
    - `/subagents on` przywraca subagentów
  - **Verification Commands:** `.venv/bin/python -m pytest tests/test_subagents.py -q`

- [x] **TUI + CLI: wyświetlanie rezultatu komend bash + toggle**
  - **Description:** Dodać opcję wyświetlania pełnego rezultatu komend `bash` (output + exit code) w strumieniu TUI i CLI:
    - **Settings:** nowy klucz `bash.showOutput: true` (domyślnie `true`)
    - **TUI:** komenda `/bash-show [on|off]`, status w sidebarze (`Bash: on/off`)
    - **CLI:** flaga `--no-bash-output` (ukrywa output, pokazuje tylko exit code)
    - **Interactive mode:** `/bash-show [on|off]`
    - W `execute_bash()` lub w warstwie renderowania — gdy `showOutput=false`, pokazuj tylko `exitCode=N`
  - **Files:** `one/cli/args.py`, `one/core/settings_manager.py`, `one/core/agent_session.py`, `one/modes/tui_mode.py`, `one/modes/interactive_mode.py`
  - **Acceptance Criteria:**
    - Domyślnie output basha jest widoczny w TUI i CLI
    - `/bash-show off` w TUI ukrywa output (tylko exit code)
    - `--no-bash-output` w CLI ukrywa output
    - Status `Bash: on/off` widoczny w sidebarze TUI
  - **Verification Commands:** `.venv/bin/python -m pytest tests/test_tools.py -q`

## Dokumentacja

- [x] **README.md: dodanie opisu wszystkich komend slash**
  - **Description:** Obecny README.md ma tylko fragmentaryczne opisy komend. Należy dodać sekcję "Slash Commands" z pełną listą komend i opisami (zgodnie z `/help` outputem):
    - `/help`, `/stats`, `/status`, `/queue`, `/tools`, `/clear`, `/abort`
    - `/model [provider/model]`, `/model-cycle`, `/thinking [level]`, `/thinking-cycle`
    - `/theme [name]`, `/steer <text>`, `/follow <text>`, `/compact [instructions]`
    - `/tree`, `/navigate <id>`, `/fork <id>`, `/new`, `/login`, `/logout`
    - `/retry [on|off]`, `/config [key] [value]`, `/extui`, `/cooperation`
    - `/bash <command>`, `/exit`, `/subagents [on|off]`, `/bash-show [on|off]`
  - **Files:** `README.md`
  - **Acceptance Criteria:** README zawiera pełną, sformatowaną sekcję z wszystkimi komendami slash i krótkimi opisami.
  - **Verification Commands:** `cat README.md | grep -c "^- \`/"`

## Architektura

- [x] **Po zmianie modelu: dynamiczne ustawienie wielkości kontekstu (ctx) z fallbackiem**
  - **Description:** Po zmianie modelu (`/model`, `/model-cycle`, `/login`) — próbuj dynamicznie ustawić kontekst modelu na podstawie `contextWindow` z registry. Jeśli model nie ma zdefiniowanego `contextWindow` (dynamiczny), ustaw sensowny fallback (np. 8192 dla małych, 128000 dla dużych). Zaktualizuj sidebar z nową wartością ctx.
  - **Files:** `one/core/agent_session.py` (`set_model`), `one/modes/tui_mode.py` (`_complete_login`, `/model` handler), `one/modes/interactive_mode.py` (`/model` handler)
  - **Acceptance Criteria:**
    - Przy zmianie modelu na model z registry — ctx w sidebarze pokazuje poprawny contextWindow
    - Przy dynamicznym modelu — ctx pokazuje fallback (np. 8192 lub 128000)
    - W sidebarze widoczna aktualna wartość kontekstu
  - **Verification Commands:** `.venv/bin/python -m pytest tests/ -q`
