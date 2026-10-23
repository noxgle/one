# TODO — natywne narzędzie `web_connect` dla ONE

## Cel
Udostępnić agentowi pobieranie wskazanego URL HTTP(S) bez dostępu do `bash`: GET, domyślna ekstrakcja HTML do Markdown, JSON i tekst bez HTML-cleaningu oraz jawne tryby `raw` i `text`. Wynik ma mieć ograniczony podgląd i możliwość odczytu całej dopuszczonej treści przez istniejące `evidence_read`.

**Status:** plan implementacji; żadne poniższe zadanie nie zostało wykonane ani zweryfikowane testami w ramach planowania.

## Podstawa i stan repozytorium
- Dokument źródłowy: `tmp/tool_web_connect_minimal.md`. To propozycja, nie dokumentacja działającego narzędzia.
- Potwierdzono katalog roboczy przez `ls .`; przed utworzeniem tego pliku nie było głównego `TODO.md`.
- `pyproject.toml`: Python 3.12+, istniejąca zależność `httpx>=0.27.0`; brak zadeklarowanych BeautifulSoup, lxml i markdownify.
- `one/tools/index.py`: `ToolDef`, `all_tools`, `native_tool_definitions()` i `DEFAULT_TOOL_NAMES`; brak rejestracji `web_connect`.
- `one/core/agent_session.py`: osobny dispatch `_execute_tool_by_name()`, kontrola aktywności i polityki przed wykonaniem, istniejące zapisywanie evidence oraz odczyt fragmentów. `_build_tool_result_message_payload()` ogranicza podgląd; kontrakt ten należy zachować.
- `one/resources/resource_loader.py`: osobny promptowy `TOOL_ARG_SCHEMAS`; sama rejestracja JSON Schema nie wystarczy.
- `one/core/settings_manager.py`: ustawienia łączone globalnie/projektowo, istniejące limity narzędzi; nowa sekcja `webConnect` wymaga implementacji i walidacji.
- `one/core/tool_policy.py`: `READONLY_TOOLS` nie obejmuje sieci; `ToolPolicy.is_readonly` zależy od tego zbioru. Nie zmieniać go automatycznie przy dodaniu GET.
- Istnieją testy `tests/test_durable_evidence.py`, `tests/test_agent_tool_calls.py`, `tests/test_settings.py`, `tests/test_presets.py`, `tests/test_preset_runtime.py`, `tests/test_preset_sessions.py` i `tests/test_subagent_presets.py`.
- Przeczytano wskazaną specyfikację, odpowiednie fragmenty kodu, `CONTRIBUTING.md` i początek `CHANGELOG.md`. Nie uruchamiano testów, instalacji ani połączeń do usług; nie czytano `.one/` ani rzeczywistego stanu użytkownika.

## Zakres i ograniczenia
**MVP:** GET bez konfigurowanych sekretów; dokładnie `url` oraz opcjonalne `mode` (`auto` domyślnie, `raw`, `text`); limity w konfiguracji; HTML/JSON/tekst; rejestr, dispatch, polityki, trwałe evidence i testy bez internetu.

**Po MVP:** nazwane połączenia i uwierzytelnianie, domenowe reguły połączeń, rate limiting i cache; następnie metody zapisujące i osobna polityka zatwierdzania. Realizacja tych faz wymaga odrębnego przeglądu kontraktów bezpieczeństwa.

**Poza zakresem:** wyszukiwarka `web_search`, rendering JavaScript, uruchamianie kodu strony, pobieranie binariów, sandbox sieciowy, własne ACL adresów IP oraz opcja `networkAccess`.

Nie dodawać argumentów `method`, `body`, nagłówków, sekretów, `timeout`, `max_chars`, `offset`, `limit` ani `chunk_size` do MVP. Nie rozszerzać automatycznie uprawnień presetów ani subagentów. GET może mieć skutki uboczne i nie oznacza izolacji sieciowej.

## Architektura i decyzje
Przepływ: kontrola uprawnień ONE → walidacja argumentów → asynchroniczny klient HTTP → kontrola statusu/typu/rozmiaru → parser i ekstrakcja → wynik narzędzia → istniejący magazyn evidence i ograniczony podgląd → opcjonalne `evidence_read`.

- **ADR-001 — granica sieci:** zgodnie z jednoznaczną decyzją sekcji 5 specyfikacji nie implementować blokad prywatnych IP, localhost ani osobnych ACL DNS/IP. Osiągalność wynika z uprawnień procesu i przyszłego sandboxa. Ogólne wzmianki o „pełnej walidacji sieciowej” w diagramie/MVP interpretować jako walidację URL, protokołu, przekierowań i limitów, nie jako sprzeczną z tym decyzją blokadę IP. Alternatywa: wbudowana ochrona SSRF; odrzucona w tym zakresie. Koszt: jawny dostęp także do usług wewnętrznych i metadanych infrastruktury, jeśli proces je osiąga.
- **ADR-002 — transport:** wykorzystać istniejący `httpx.AsyncClient`, strumieniowanie i jawne, ograniczone przekierowania zamiast `bash`/curl lub dodatkowego klienta. Weryfikacja TLS pozostaje włączona. Anulowanie ma zamykać odpowiedź i klienta.
- **ADR-003 — ekstrakcja:** planowany stos to BeautifulSoup + lxml + markdownify, zgodny z inspiracją dokumentu; przed implementacją potwierdzić kompatybilność wersji i zachowanie tabel, linków i kodu na lokalnych fixture'ach. Alternatywa: wyspecjalizowany ekstraktor artykułów; prostszy wybór głównej treści, ale dodatkowa zależność i ryzyko utraty dokumentacji technicznej. Własny parser oparty o stdlib zmniejsza zależności, lecz zwiększa koszt poprawnej obsługi HTML. Nie kopiować kodu z zewnętrznego projektu bez weryfikacji licencji. Zewnętrznych bibliotek ani źródła inspiracji nie badano w ramach tego planu.
- **ADR-004 — evidence już w MVP:** repozytorium ma gotową ścieżkę evidence, więc integrację przenieść z proponowanego etapu 2 do MVP. Nie generować niezapisanego `evidence_id` ani drugiego magazynu. Użyć istniejącego `evidenceId` w opakowaniu runtime, a wewnętrzny wynik dostosować do konwencji `output`/`details`. Limit podglądu nie może bezpowrotnie obcinać treści przed zapisem evidence.
- **ADR-005 — jawne udostępnienie:** zarejestrować narzędzie, lecz początkowo nie dodawać go do `DEFAULT_TOOL_NAMES` ani `READONLY_TOOLS`. Włączać przez istniejący mechanizm jawnego wyboru narzędzi/presetu, bez `bash`. To konserwatywne założenie wdrożeniowe; domyślne włączenie wymaga osobnej decyzji po ocenie ryzyka sieciowego.

### Kontrakt do wdrożenia
- URL: niepusty HTTP(S), poprawny host/port, bez osadzonych danych logowania; walidować także każdy redirect. Fragment nie trafia do żądania. Inne schematy i nieznane argumenty odrzucać również w runtime.
- Proponowane wartości startowe `webConnect`: `timeoutSec=15`, `maxResponseBytes=2000000`, `maxOutputChars=12000`, `maxRedirects=5`, `extractMainContent=true`, `preserveLinks=true`, `preserveCodeBlocks=true`. Są to nowe ustawienia, nie obecny kontrakt ONE. `maxResponseBytes` egzekwować niezależnie dla bajtów przesłanych i rozpakowanych; udokumentować semantykę.
- Limit czasu całej operacji obejmuje przekierowania, transfer i przetwarzanie; działa razem z limitem runtime, wygrywa wcześniejszy deadline. Czas pojedynczego odczytu nie zastępuje całkowitego deadline.
- Twarde przekroczenie transferu/dekompresji: kontrolowany błąd, bez prezentowania częściowego JSON jako poprawnego wyniku. `truncated` opisuje ograniczenie podglądu; jawnie odróżnić je od błędu transferu i niedostępności evidence.
- `auto`: HTML → główna treść w Markdown; JSON → zachowana treść poprawnego JSON; tekst → tekst. `text`: HTML → tekst, JSON nadal bez HTML-cleaningu. `raw`: zdekodowane źródło tekstowej odpowiedzi bez ekstrakcji; nie binarny dump i nie wyłączenie limitów ani redakcji sekretów.
- Metadane w `details`: żądany i końcowy URL, status HTTP, Content-Type, wybrany tryb, tytuł jeśli dostępny, flagi limitów. Ich wartości pochodzą z transportu/parsera, nie z instrukcji strony. Nie zwracać wrażliwych nagłówków.
- Jawnie obsłużyć brak typu, błędne kodowanie, nieprawidłowy JSON, nieobsługiwane binaria, odpowiedzi bez treści i statusy nie-2xx; opisać decyzje i pokryć je testami przed publikacją kontraktu.
- Zawartość z sieci i odczytana z evidence pozostaje niezaufanymi danymi w istniejącym opakowaniu ONE. Nie dodawać jej do system promptu ani nie wykonywać kodu.

## Plan realizacji
Szacunki jakościowe: S — mały, M — średni, L — duży zakres; nie są deklaracją terminów. Wszystkie wskazane polecenia/testy są przeznaczone dla agenta implementującego, nie zostały tu uruchomione. Nowe ścieżki opisano jako „nowy”.

### Faza 1 — kontrakty i konfiguracja
**Rezultat:** jednoznaczne interfejsy oraz walidowane ustawienia. **Warunki:** brak. **Nakład / pewność:** M / średnia.

- [ ] **T001 — doprecyzować kontrakt i zweryfikować zależności ekstraktora**
  - **Zmiany:** potwierdzić wersje/licencje BeautifulSoup, lxml i markdownify dla Python 3.12+, porównać na fixture'ach ze specjalizowanym ekstraktorem; ustalić błędy statusów, JSON/kodowania/Content-Type oraz kontrakt wyniku i dwóch rodzajów limitów. Uzgodnić sposób ograniczenia podglądu bez utraty pełnego wyniku w evidence.
  - **Pliki:** `pyproject.toml`, `TODO.md` (aktualizacja decyzji), `tests/test_web_connect.py` (nowy; fixture'y inline lub ścieżka ustalona tutaj).
  - **Zależności:** brak.
  - **Akceptacja:** wybrany stos ma kompatybilne wersje i licencje; każda wymieniona niejednoznaczność ma udokumentowane zachowanie i scenariusz testowy; brak zmiany zakresu ACL IP.
  - **Weryfikacja:** przegląd dokumentacji/licencji zależności i lokalna próba ekstrakcji dokumentacji, artykułu, tabeli i kodu bez żywego HTTP.

- [ ] **T002 — dodać i walidować konfigurację `webConnect`**
  - **Zmiany:** dodać domyślne wartości, odczyt i walidację typów/zakresów w istniejącym modelu ustawień; odrzucać m.in. bool zamiast liczby, wartości niedodatnie, nieskończone, ujemną liczbę redirectów. Zero redirectów jest poprawną konfiguracją. Zachować nakładanie ustawień globalnych/projektowych i działanie starej konfiguracji bez sekcji.
  - **Pliki:** `one/core/settings_manager.py`, `tests/test_settings.py`.
  - **Zależności:** T001.
  - **Akceptacja:** stare ustawienia uruchamiają się z nowymi domyślnymi wartościami; niepoprawne ustawienia dają kontrolowany komunikat po angielsku; agent nie może nadpisywać limitów argumentami narzędzia.
  - **Weryfikacja:** `.venv/bin/python -m pytest -q tests/test_settings.py`; testy domyślnych wartości, merge, walidacji i granic.

### Faza 2 — ograniczony klient HTTP i parsery
**Rezultat:** niezależny od sesji, testowalny asynchroniczny tool. **Warunki:** T001–T002. **Nakład / pewność:** L / średnia.

- [ ] **T003 — zaimplementować GET i limity transportu**
  - **Zmiany:** asynchroniczny klient z wstrzykiwalnym transportem, walidacja URL/argumentów, standardowy TLS, przekierowania względne i limit ich liczby, strumieniowy limit bajtów przesłanych i rozpakowanych oraz całkowity deadline. Nie dziedziczyć cookies ani poświadczeń API z innych klientów ONE; jawnie ustalić obsługę proxy środowiskowego. Czyścić zasoby po błędzie i anulowaniu. Nie dodawać blokad IP.
  - **Pliki:** `one/tools/web_connect.py` (nowy), `tests/test_web_connect.py` (nowy).
  - **Zależności:** T001, T002.
  - **Akceptacja:** wyłącznie GET HTTP(S); brak obejścia limitów przez chunking, kompresję lub redirect; brak żądań po przekroczeniu deadline; błędy transportu nie ujawniają poświadczeń. Redirect do innego originu nie przenosi Authorization/Cookie, także po późniejszym dodaniu auth.
  - **Weryfikacja:** testy z `httpx.MockTransport`/kontrolowanym strumieniem: schematy, URL z userinfo, port, localhost i prywatny adres bez kontaktu z siecią, pętla redirectów, downgrade/protokół, timeout, anulowanie, kompresja i brak Content-Length.

- [ ] **T004 — dodać parser HTML/JSON/tekst i trzy tryby**
  - **Zmiany:** parsowanie typu i kodowania; ranking kandydatów głównej treści zamiast pierwszego dopasowania DOM, fallback do treści dokumentu; usuwanie skryptów, stylów, nawigacji, reklam i cookie-bannerów bez usuwania kodu/tabel. Markdown z nagłówkami, linkami i blokami kodu; linki względne rozwiązywać względem końcowego URL, nie emitować wykonywalnych schematów. Nie wykonywać ani nie pobierać zasobów HTML. `raw` omija ekstrakcję, a JSON nigdy nie trafia do HTML-cleaningu.
  - **Pliki:** `one/tools/web_connect.py` (nowy), `pyproject.toml`, `tests/test_web_connect.py` (nowy).
  - **Zależności:** T001, T003.
  - **Akceptacja:** fixture dokumentacji zachowuje kod, tabelę i strukturę; artykuł traci szum; ranking wybiera treść zamiast wcześniejszego pustego kontenera; `raw` zachowuje znaczniki; JSON pozostaje poprawny i kompletny; brak/błędny typ i binaria mają ustalone wyniki lub kontrolowane błędy.
  - **Weryfikacja:** parametryzowane testy typów i trybów, pustych/malformed dokumentów, Unicode i kodowań, linków po redirect, wielu kandydatów oraz ustawień ekstrakcji; `.venv/bin/python -m pytest -q tests/test_web_connect.py`.

### Faza 3 — integracja runtime, evidence i uprawnienia
**Rezultat:** działający tool dostępny jawnie, bez rozszerzenia uprawnień. **Warunki:** faza 2. **Nakład / pewność:** L / średnia.

- [ ] **T005 — zarejestrować minimalny schemat i asynchroniczny dispatch**
  - **Zmiany:** dodać `ToolDef`, opis, ścisły JSON Schema (`url` wymagany, `mode` opcjonalne, brak dodatkowych pól), promptowy schemat i gałąź dispatch z ustawieniami runtime. Walidować argumenty także przy bezpośrednim wykonaniu. Zachować istniejące kontrole polityki, zatwierdzenia i kontrakt zdarzeń.
  - **Pliki:** `one/tools/index.py`, `one/resources/resource_loader.py`, `one/core/agent_session.py`, `tests/test_agent_tool_calls.py`, `tests/test_resource_loader.py`, `tests/test_tools.py`.
  - **Zależności:** T002, T003, T004.
  - **Akceptacja:** rejestr, prompt i wykonanie zgadzają się; `method`, `body` i parametry limitów są odrzucane; wyłączony tool nie otwiera połączenia; brak zmian domyślnej listy narzędzi i zbioru readonly; timeout/anulowanie nie blokują pętli sesji.
  - **Weryfikacja:** testy schematu, promptu, bezpośredniego dispatch i zdarzeń; `.venv/bin/python -m pytest -q tests/test_agent_tool_calls.py tests/test_resource_loader.py tests/test_tools.py tests/test_agent_timeouts_limits.py tests/test_event_snapshots.py`.

- [ ] **T006 — połączyć ograniczony podgląd z trwałym evidence**
  - **Zmiany:** przeprowadzić pełny dopuszczony wynik przez istniejący zapis evidence przed cięciem podglądu; egzekwować `maxOutputChars` niezależnie od ogólnego limitu runtime bez podwójnego magazynu. Zwracać rzeczywiste `evidenceId` i instrukcję kontynuacji przez `nextOffset`; rozróżniać truncation podglądu, odrzucenie transferu i niedostępny magazyn. Zredagować wrażliwe dane w URL/diagnostyce przed eventami i persistencją; nie obiecywać automatycznego wykrywania dowolnych sekretów w treści dokumentu.
  - **Pliki:** `one/core/agent_session.py`, `one/tools/web_connect.py` (nowy), `tests/test_durable_evidence.py`, `tests/test_tool_output_pruning.py`; `one/core/session_manager.py` tylko jeśli testy wykażą potrzebę rozszerzenia istniejącego kontraktu.
  - **Zależności:** T005.
  - **Akceptacja:** długa odpowiedź daje mały podgląd, a kolejne `evidence_read` odtwarzają całą dopuszczoną treść bez ponownego HTTP, także po wznowieniu sesji; brak wymyślonego ID przy awarii zapisu; odczyt evidence nadal jest ograniczony i nie tworzy rekurencyjnie dowodów; granice sesji zachowane.
  - **Weryfikacja:** test trwałej sesji w katalogu tymczasowym, rekonstrukcji fragmentów, awarii zapisu i działania bez trwałości; `.venv/bin/python -m pytest -q tests/test_durable_evidence.py tests/test_tool_output_pruning.py`.

- [ ] **T007 — sprawdzić pułapy uprawnień i granicę niezaufanej treści**
  - **Zmiany:** testy sesji z jawnym `web_connect` i bez `bash`, bez rozszerzania reserved readonly; rodzic i CLI ograniczają żądane narzędzia dziecka, również przy presetach i odświeżeniu MCP. Sprawdzić istniejące blokady ręcznej powłoki i hooków rozszerzeń; nie przedstawiać listy narzędzi jako sandboxa procesu. Treść strony i evidence ma zachować istniejący wrapper niezaufanych wyników.
  - **Pliki:** `tests/test_preset_runtime.py`, `tests/test_presets.py`, `tests/test_preset_sessions.py`, `tests/test_subagent_presets.py`, `tests/test_manual_command_policy.py`, `tests/test_system_prompt.py`; `one/core/tool_policy.py` i mechanizmy presetów tylko przy udowodnionej regresji.
  - **Zależności:** T005, T006.
  - **Akceptacja:** sesja może wykonać mockowany GET bez bash; dziecko nie uzyskuje bash ani web_connect zabronionego rodzicowi; readonly zachowuje blokady; strona z instrukcją zmiany system promptu pozostaje danymi także po odczycie evidence. Nie deklarować odporności modelu na wszystkie prompt injection.
  - **Weryfikacja:** `.venv/bin/python -m pytest -q tests/test_presets.py tests/test_preset_runtime.py tests/test_preset_sessions.py tests/test_subagent_presets.py tests/test_manual_command_policy.py tests/test_system_prompt.py`; wszystkie MCP/provider testy wyłącznie na atrapach.

### Faza 4 — akceptacja i przygotowanie wydania MVP
**Rezultat:** udokumentowane, zweryfikowane MVP gotowe do przeglądu, bez automatycznej publikacji. **Warunki:** T003–T007. **Nakład / pewność:** M / wysoka.

- [ ] **T008 — uzupełnić macierz regresji i scenariusz end-to-end**
  - **Zmiany:** zintegrować fixture'y HTML/JSON/raw, limitów, nie-2xx, złego JSON, kompresji i powolnego serwera; scenariusz agent → web_connect → evidence_read → wynik bez bash. Sprawdzić, że transport nigdy nie dotyka prawdziwej sieci i brak sekretów w błędach/metadanych. Nowy moduł uruchamiać jawnie, bo helper ma stałe listy zestawów.
  - **Pliki:** `tests/test_web_connect.py` (nowy), `tests/test_headless_e2e.py`, `tests/test_durable_evidence.py`, `scripts/test.sh` tylko jeśli uzasadnione dodanie modułu do zestawu tools.
  - **Zależności:** T003, T004, T005, T006, T007.
  - **Akceptacja:** scenariusze akceptacyjne ze specyfikacji są pokryte; wszystkie async testy mają jawny marker; powtórny odczyt dowodu nie zwiększa liczby żądań HTTP; wyniki nie zależą od sieci hosta.
  - **Weryfikacja:** `.venv/bin/python -m pytest -q tests/test_web_connect.py tests/test_headless_e2e.py tests/test_durable_evidence.py`; następnie pełna bramka opisana poniżej.

- [ ] **T009 — opisać użycie, ryzyko i przygotować zmianę wersji**
  - **Zmiany:** opisać jawne włączenie toola bez bash, dwa argumenty, ustawienia, ograniczenia raw, evidence i błędy. Wyraźnie ostrzec o dostępie do lokalnej/prywatnej sieci, możliwych skutkach GET i braku sandboxa. Dodać wpis `Unreleased`, podnieść `one/config.py:VERSION` zgodnie z polityką projektu i odświeżyć snapshoty TUI. Nie zmieniać domyślnej polityki przy tej okazji.
  - **Pliki:** `README.md`, `SECURITY.md`, `docs/PRESETS.md` (jeśli potrzebny przykład), `CHANGELOG.md`, `one/config.py`, `tests/snapshots/tui/`, `tests/test_tui_snapshots.py`.
  - **Zależności:** T008.
  - **Akceptacja:** przykłady odpowiadają faktycznemu CLI/konfiguracji; nie sugerują istniejącego sandboxa, ACL IP ani auth; wpisy changeloga zastane w repo pozostają zachowane; snapshoty różnią się wyłącznie oczekiwanymi zmianami.
  - **Weryfikacja:** przegląd przykładów i diffów; `ONE_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest -q tests/test_tui_snapshots.py`, potem pełne testy, lint i `git diff --check`. Bez tagowania/publikacji.

### Faza 5 — kontrolowane połączenia API (po MVP)
**Rezultat:** oddzielnie zatwierdzony projekt i wdrożenie auth/cache/rate limit. **Warunki:** zaakceptowane MVP i przegląd bezpieczeństwa. **Nakład / pewność:** L / niska.

- [ ] **T010 — zaprojektować i wdrożyć nazwane połączenia bez sekretów w promptach**
  - **Zmiany:** przed kodem ustalić magazyn i właściciela konfiguracji sekretów, dokładne dopasowanie domen/originów i ścieżek, reguły redirect i wyłączenia cookies. Sekret runtime wyłącznie do przypisanego celu; zakaz przekazywania go do innego originu, także przez redirect. Reguły połączeń dotyczą auth, nie stanowią ACL IP. Odrębnie ustalić limity tempa i opcjonalny cache z izolacją odpowiedzi uwierzytelnionych; MVP nadal działa bez zmian.
  - **Pliki:** `one/core/settings_manager.py`, `one/tools/web_connect.py`, `tests/test_web_connect.py`, `SECURITY.md`; magazyn poświadczeń i moduł cache: TBD po przeglądzie obecnego auth i osobnej decyzji o zakresie (nie zakładać ponownego użycia sekretów providerów).
  - **Zależności:** T009.
  - **Akceptacja:** przed implementacją zapisany kontrakt i przegląd bezpieczeństwa; żaden sekret nie trafia do promptu, eventów, evidence, logów ani cross-origin; cache nie miesza sesji/tożsamości, respektuje warunki HTTP; rate limit daje deterministyczny błąd lub ograniczone oczekiwanie.
  - **Weryfikacja:** testy redirect między originami, redakcji sekretów, braku dopasowania, izolacji cache i czasu z kontrolowanym zegarem; mock transport, bez realnych kluczy i usług.

### Faza 6 — operacje zapisujące HTTP (osobne rozszerzenie)
**Rezultat:** jawnie autoryzowane metody zapisu, bez zmiany GET-only dla obecnych sesji. **Warunki:** osobna decyzja bezpieczeństwa po fazie 5. **Nakład / pewność:** L / niska.

- [ ] **T011 — zaprojektować i wdrożyć `method`/`body` za osobną polityką**
  - **Zmiany:** określić uprawnienia i zatwierdzanie POST/PUT/PATCH/DELETE, typ i limit body, wpływ na readonly oraz zgodność snapshotów presetów/resume. Rozszerzony schemat ma maksymalnie cztery pola; sesjom GET-only nadal reklamować tylko `url` i `mode`. Zablokować ponawianie i przekierowywanie operacji modyfikujących bez jawnie ustalonej semantyki/idempotencji. Wyszukiwarka i dodatkowe formaty pozostają odrębnymi inicjatywami.
  - **Pliki:** `one/tools/web_connect.py`, `one/tools/index.py`, `one/resources/resource_loader.py`, `one/core/agent_session.py`, `one/core/settings_manager.py`, `one/core/tool_policy.py`, `tests/test_web_connect.py`, `tests/test_approval.py`, `tests/test_subagent_presets.py`, `tests/test_preset_sessions.py`, `README.md`, `SECURITY.md`; dodatkowy mechanizm schematu zależnego od capabilities: TBD w projekcie tej fazy.
  - **Zależności:** T010.
  - **Akceptacja:** przed implementacją zatwierdzony kontrakt; nieautoryzowany zapis odrzucony przed HTTP, także przez bezpośredni dispatch i dziecko; brak cichego retry; odmowa zatwierdzenia nie wykonuje żądania; GET-only zachowuje minimalny schemat i zgodność.
  - **Weryfikacja:** testy macierzy metod/uprawnień/approval, redirect 301/302/303/307/308, anulowania, timeout po potencjalnym zapisie oraz ponowienia; wyłącznie atrapy HTTP i providerów.

## Weryfikacja dla agenta implementującego
Przed jakimkolwiek testem lub manualnym CLI odizolować stan w tej samej powłoce: `export ONE_CODING_AGENT_DIR="$(mktemp -d)"`. Nie używać rzeczywistego `.one/`, serwerów MCP ani API providerów. Poza lokalnymi testami transportu nie kontaktować się z prawdziwą siecią.

Bramka MVP po T009, w kolejności:
1. `.venv/bin/python -m pytest -q tests/test_web_connect.py` (nowy moduł uruchomić jawnie).
2. `.venv/bin/python -m pytest -q`.
3. `.venv/bin/ruff check .`.
4. `git diff --check`.
5. Jeśli zmieniono zależności: sprawdzenie budowy/instalacji pakietu według `CONTRIBUTING.md` w izolowanym środowisku; bez publikacji.

## Wdrożenie i wycofanie
- MVP wdrażać jako jawnie wybierane narzędzie; nie migrować istniejących presetów ani nie włączać sieci wszystkim agentom. Brak migracji danych sesji powinien zostać potwierdzony przez testy evidence i resume.
- W razie regresji wyłączyć `web_connect` w konfiguracji aktywnych narzędzi/presetu lub wrócić do poprzedniej wersji; zachować istniejące evidence sesji, nie usuwać ich przy rollbacku.
- Auth i metody zapisujące pozostają oddzielnymi etapami; ukończenie MVP nie autoryzuje ich automatycznej aktywacji. Nie commitować, tagować ani publikować bez odrębnej zgody.

## Ryzyka i otwarte decyzje
- **Dostęp do sieci wewnętrznej/SSRF:** świadomie nie blokowany przez tool. Minimalizować przez jawne udostępnienie i uprawnienia procesu; pełną politykę egress wdrożyć w osobnym projekcie sandboxa. Nie przedstawiać walidacji URL jako izolacji.
- **Prompt injection:** zachować wrapper niezaufanych danych; instrukcje stron i evidence nigdy nie otrzymują priorytetu systemowego. Nie deklarować pełnej ochrony wynikającej wyłącznie z wrappera.
- **Kompresja i duży HTML:** limitować obie reprezentacje bajtów przed parsowaniem; testować deadline i koszt ekstrakcji na maksymalnym dopuszczonym dokumencie. Jeśli parser blokuje pętlę, w T004 dobrać izolowane przetwarzanie bez przekazywania żywej sesji do workerów; timeout wątku sam nie kończy pracy parsera.
- **Utrata treści:** test integracyjny evidence obowiązkowy, nie wystarczy pole z ID. Pełna treść oznacza tylko wynik w dozwolonym limicie transferu, nie nieograniczony dokument.
- **Nowe zależności:** kompatybilność i licencje do potwierdzenia w T001; brak zewnętrznego researchu w tej sesji.
- **Nieustalone detale kontraktu:** statusy HTTP, brak Content-Type, kodowania, raw JSON i proxy do zamknięcia w T001/T003 przed publikacją; nie blokują utworzenia planu.
- **Domyślne włączenie:** przyjęto opt-in; rozszerzenie `DEFAULT_TOOL_NAMES` albo `READONLY_TOOLS` wymaga odrębnej decyzji i analizy wpływu na `is_readonly`, ręczną powłokę oraz hooki.

## Kryteria akceptacji MVP
- [ ] Tool działa w sesji bez bash, z dokładnie dwoma polami interfejsu i jednym wymaganym argumentem.
- [ ] Dokumentacja HTML daje Markdown z kodem, linkami, nagłówkami i tabelami; artykuł jest oczyszczony; JSON nie jest modyfikowany przez ekstraktor; raw zachowuje źródło tekstowe.
- [ ] Timeouty, redirecty, wielkość odpowiedzi i ekspansja kompresji są ograniczone oraz przetestowane bez rzeczywistej sieci.
- [ ] Długi wynik ma ograniczony podgląd i prawdziwe, trwałe evidence odczytywane fragmentami bez kolejnego HTTP; awaria zapisu nie tworzy pozornej dostępności.
- [ ] Uprawnienia rodzica/CLI/presetu są egzekwowane przy wykonaniu; brak automatycznego rozszerzenia readonly i narzędzi domyślnych.
- [ ] Dokumentacja jasno opisuje brak ACL IP i sandboxa, dostępność prywatnych usług oraz ryzyko efektów ubocznych GET.
- [ ] T001–T009 zakończone na podstawie dowodów wykonania; pełne testy, lint, kontrola diffów i wymagane snapshoty przechodzą. T010–T011 nie są warunkiem przyjęcia MVP.
