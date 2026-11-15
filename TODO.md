# Szczegółowa dokumentacja ONE — propozycja

Data przeglądu: 2026-11-13. Status: propozycja do oceny, nie wykonana implementacja.

Warto wzorować się na OpenCode, ale przede wszystkim na **sposobie organizacji i szczegółowości dokumentacji**, nie kopiować jego treści ani konfiguracji. Dla ONE proponuję osobny portal dokumentacji z wyszukiwaniem, krótkimi poradnikami i dokładną referencją.

## 1. Co znalazłem w ONE

Po przejrzeniu repozytorium problem wygląda trochę inaczej niż „brak dokumentacji”:

- `README.md` ma ponad **1000 linii** i łączy instalację, obsługę TUI, konfigurację, integracje i informacje techniczne. Trudno szybko znaleźć konkretną odpowiedź.
- `docs/` zawiera **15 dokumentów**, m.in. `QUICKSTART.md`, `PRESETS.md`, `SKILLS.md`, `EXTENSIONS.md`, `RUNS.md`, `COMPACTION.md`, `DOCTOR.md` i `architecture.md`.
- Część materiałów jest już szczegółowa, ale dokumentacja miesza odbiorców: początkującego użytkownika, autora rozszerzeń i developera samego ONE.
- Niektóre strony zawierają informacje o fazach implementacji i testach, np. sekcje P0–P5 w dokumentacji kompaktacji. To przydatne dla projektu, ale nie powinno dominować w podręczniku użytkownika.
- W `pyproject.toml` adres dokumentacji prowadzi obecnie do README na GitHubie.
- W `.github/workflows/` są workflowy CI i wydania; nie ma osobnego workflow dokumentacji. `landing_page/` zawiera prosty landing, nie istniejący portal dokumentacyjny.

**Wniosek:** nie pisałbym wszystkiego od zera. Najpierw uporządkowałbym istniejącą wiedzę, następnie uzupełnił luki i dopiero na tym zbudował wygodny portal.

## 2. Co warto przejąć z OpenCode

Sprawdziłem oficjalne strony wprowadzenia, konfiguracji i uprawnień OpenCode. Szczególnie dobre są:

1. **Osobne strony dla konkretnych zagadnień** zamiast rozbudowywania jednego README.
2. **Przykład na początku strony**, a dopiero później szczegółowe reguły.
3. **Dokładna semantyka:** lokalizacje plików, kolejność nadpisywania konfiguracji, wartości domyślne i wyjątki.
4. **Tabele kontraktów:** pole → znaczenie → zachowanie.
5. **Połączenia między tematami**, np. konfiguracja odsyła do modeli, agentów i uprawnień.

W ONE należy zastosować ten sam standard, ale opisywać jego własne mechanizmy. Przykładowo preset `readonly` nie jest sandboxem systemowym, a ograniczenia subagentów muszą uwzględniać politykę rodzica. Nie wolno przenosić reguł OpenCode jako opisu ONE.

## 3. Rekomendowane rozwiązanie techniczne

**Moja rekomendacja: Astro + Starlight, treść w Markdown, statyczny portal dokumentacji.**

Dlaczego:
- gotowa nawigacja, wyszukiwanie, spis treści, ciemny motyw i obsługa wielu języków;
- wygląd odpowiedni dla dokumentacji narzędzia developerskiego;
- brak potrzeby tworzenia własnego interfejsu dokumentacji;
- statyczny wynik można publikować niezależnie od aplikacji ONE.

To propozycja dla ONE — **nie potwierdziłem stosu technologicznego aktualnej witryny OpenCode**. Próba odczytu pliku jej projektu z przypuszczanej ścieżki repozytorium zakończyła się 404.

| Opcja | Zalety | Koszty |
|---|---|---|
| **Astro + Starlight** | Kompletny interfejs dokumentacji, dobre możliwości rozbudowy i lokalizacji | Osobny toolchain Node.js obok Pythona |
| **MkDocs** | Naturalne dopasowanie do projektu Python, Markdown i prosta konfiguracja YAML | Wygląd i dodatkowe możliwości zależą od motywu oraz rozszerzeń |

Jeżeli priorytetem jest portal zbliżony użytkowo do OpenCode, wybrałbym **Starlight**. Jeśli ważniejszy jest minimalizm utrzymania — **MkDocs** jest sensowną alternatywą.

Nie łączyłbym tego projektu z przebudową landingu. Landing ma prezentować produkt, dokumentacja ma pomagać go używać.

## 4. Proponowana struktura dokumentacji

Poniższe nazwy są propozycją nawigacji, nie istniejącymi plikami:

```text
Start
  Czym jest ONE
  Instalacja i aktualizacje
  Pierwsze zadanie
  Pierwsze zadanie z modelem lokalnym
  Wybór trybu pracy

Używanie ONE
  TUI i skróty klawiszowe
  Komendy slash
  Sesje: otwieranie, resume, fork, usuwanie
  Planowanie i wykonywanie zadań
  Subagenci i ich monitorowanie
  Obrazy i załączniki
  Checkpointy i odtwarzanie plików

Konfiguracja
  Lokalizacje i priorytety ustawień
  Referencja ustawień
  Providerzy i uwierzytelnianie
  Modele lokalne: Ollama i llama.cpp
  Wybór modeli i limity generacji
  Budżety runów, koszty i diagnostyka
  Kontekst, kompaktacja i evidence

Bezpieczeństwo
  Zgody i cooperation
  Presety oraz ograniczenia narzędzi
  Granice readonly i brak sandboxa
  Sekrety, dane sesji i zaufanie do repozytorium

Rozszerzanie
  MCP
  Skills
  Extensions
  Własne presety

Automatyzacja i referencja
  CLI i kody wyjścia
  one run oraz CI
  Tryby text i JSON
  Protokół RPC
  Katalog narzędzi
  Zmienne środowiskowe

Rozwiązywanie problemów
  Instalacja
  Połączenia i uwierzytelnianie
  Modele lokalne
  TUI i terminal
  Limity, błędy kontekstu i kompaktacja
  Doctor i bezpieczne zgłaszanie błędów

Dla developerów ONE
  Architektura i przepływ zdarzeń
  Granica TUI/runtime
  Dodawanie narzędzia lub providera
  Testy i proces wydania
```

**Najpierw dokumentacja angielska**, zgodna z obecnymi materiałami. Polska wersja może powstać później, zaczynając od Start i najczęstszych problemów. Utrzymywanie dwóch pełnych wersji od pierwszego dnia zwiększa ryzyko rozjazdu treści.

## 5. Co oznacza „szczegółowa” dokumentacja

Nie chodzi o dłuższe akapity, tylko o odpowiedzi na konkretne pytania.

**Każdy poradnik:**
- kiedy go użyć i jakie są wymagania;
- kroki z informacją, gdzie wykonać komendy;
- oczekiwany wynik;
- typowe błędy i sposób sprawdzenia przyczyny;
- ostrzeżenia przy operacjach zmieniających stan.

**Każde ustawienie:** nazwa, typ, domyślna wartość, dopuszczalne wartości, zakres, priorytet, moment zastosowania zmiany i przykład.

**Każde narzędzie:** argumenty wymagane i opcjonalne, wynik, limity, błędy, skutki uboczne i wpływ polityki uprawnień.

**RPC:** rzeczywiste komunikaty i odpowiedzi, zdarzenia, błędy, anulowanie oraz minimalna rozmowa klient–ONE. Nie zakładać, że to HTTP API.

Źródłem prawdy powinny być implementacja i testy, m.in. `one/cli/main.py`, `one/core/settings_manager.py`, `one/tools/index.py` oraz schematy w `one/resources/resource_loader.py`. Przygotowanie treści wymaga ich dokładnego audytu — obecny przegląd nie jest jeszcze potwierdzeniem wszystkich kontraktów.

## 6. Jak przeprowadzić pracę

| Etap | Konkretny rezultat | Nakład / pewność |
|---|---|---|
| **1. Audyt i mapa treści** | Lista stron, braków, źródeł w kodzie i mapowanie obecnych dokumentów na nową strukturę | M / wysoka |
| **2. Szkielet portalu** | Nawigacja, wyszukiwanie, styl, podgląd i jedna kompletna strona wzorcowa | M / średnia |
| **3. Podręcznik użytkownika** | Instalacja, local-first quickstart, TUI, sesje, modele, konfiguracja i bezpieczeństwo | L / średnia |
| **4. Dokładna referencja** | CLI, ustawienia, narzędzia, RPC, MCP, skills i extensions | L / średnia |
| **5. Migracja i kontrola jakości** | Krótsze README, poprawne linki, kontrola przykładów, CI dokumentacji i procedura aktualizacji | M / wysoka |

Etap 1 poprzedza migrację; etap 3 i 4 korzystają ze wzorca z etapu 2. Publikacja następuje po kontroli treści i linków. Proponowana przyszła gałąź: `one/docs-portal`, z sufiksem, jeśli nazwa jest zajęta.

Nie utrzymywałbym dwóch niezależnych kopii tych samych artykułów. Istniejące treści trzeba przenieść lub wykorzystać bez duplikacji, a stare odnośniki zachować przez przekierowania tam, gdzie to możliwe, lub krótkie strony odsyłające. Informacje o benchmarkach i statusach implementacji należy oddzielić od instrukcji użytkowych, nie usuwać.

## 7. Warunki odbioru

- Nowy użytkownik znajduje ścieżkę od instalacji do pierwszego zadania, również lokalnie.
- Wszystkie publiczne opcje CLI, ustawienia i narzędzia mają opis zgodny z kodem albo jawnie oznaczoną lukę do uzupełnienia przed publikacją.
- Przykłady są sprawdzone w izolowanym stanie, bez prawdziwych sekretów i bez niekontrolowanych wywołań providerów.
- Wyszukiwanie znajduje m.in. `readonly`, `ONE_CODING_AGENT_DIR`, `one run`, MCP i kompaktację.
- Kontrola linków i budowanie dokumentacji działają w CI.
- README pozostaje krótkim wejściem do projektu, a nie równoległym podręcznikiem.
- Dokumentacja rozróżnia funkcje stabilne, eksperymentalne i ograniczenia platformowe — szczególnie Windows.

Hosting, domena i publikacja pozostają do ustalenia przed wdrożeniem; nie blokują audytu ani przygotowania treści.

## Źródła

Sprawdzone 13.11.2026; strony opisują bieżącą dokumentację, nie przypiętą wersję:
- [OpenCode — Intro](https://opencode.ai/docs/)
- [OpenCode — Config](https://opencode.ai/docs/config/)
- [OpenCode — Permissions](https://opencode.ai/docs/permissions/)
- [Starlight — oficjalna strona](https://starlight.astro.build/)
- [MkDocs — oficjalna strona](https://www.mkdocs.org/)

**Rekomendacja końcowa:** Starlight + uporządkowanie istniejących materiałów + szczegółowa referencja oparta na kodzie. Największą wartość przyniesie spójność i kompletność treści, nie sam nowy wygląd.

Podczas przygotowania propozycji nie zmieniono plików repozytorium, w tym `TODO.md`, który zawiera odrębny plan kompaktacji. Nie wykonano implementacji ani testów. Po wyborze kierunku agent implementujący powinien realizować osobno przygotowany plan dokumentacji, bez zastępowania istniejących zadań.

## Realizacja — 2026-11-15

Wybrano **Astro + Starlight, build lokalny i ręczne wgrywanie na serwer**.
Gałąź robocza: `one/docs-portal`. Powyższa propozycja jest zachowana jako
pierwotny zakres; nie oznacza już braku implementacji.

### Wykonane

- [x] Portal `website/`: nawigacja, spis treści, motyw i statyczny indeks Pagefind.
- [x] Migracja istniejących artykułów do `website/src/content/docs/library/`;
  stare pliki `docs/` są odsyłaczami, bez drugiej kopii treści.
- [x] Podział długiego README na rozdziały `handbook/`, krótkie nowe README.
- [x] Poradniki local-first, bezpieczeństwa/konfiguracji, automatyzacji/RPC
  i granicy runtime; generowane offline inwentarze CLI, ustawień i narzędzi.
- [x] Mapa migracji i jawne luki audytu: `maintainers/migration.md`.
- [x] Procedura lokalnego buildu, DOCS_SITE/DOCS_BASE, ręcznego uploadu,
  weryfikacji i rollbacku w `website/README.md`; brak automatycznego deploymentu.
- [x] CI buduje i sprawdza odnośniki dla `/` oraz `/one/`; nie publikuje.
- [x] Lokalny build obu wariantów i kontrola wewnętrznych URL, fragmentów
  oraz zasobów: 39 HTML (38 artykułów + 404). `npm audit`: 0 podatności.
- [x] 64 testy CLI/presetów, RPC, one run i snapshotów TUI przeszły.
  Wersja 0.1.144; snapshoty zmienione wyłącznie o numer wersji.

### Pozostałe bramki pełnego odbioru

- [ ] Pełny opis kontraktów wszystkich opcji CLI, również handlerów subkomend.
- [ ] Ustawienia: dopuszczalne zakresy, typy opcjonalnych kluczy, zakres,
  wyjątki priorytetów i moment zastosowania — dla każdego pola, nie tylko defaults.
- [ ] Narzędzia: wyniki, błędy, skutki uboczne, limity i polityka dla każdego.
- [ ] Pełny protokół RPC: zdarzenia, błędy, anulowanie i przykłady klienta.
- [ ] Audyt starych artykułów linia po linii; rozdzielenie raportów faz i benchmarków
  od instrukcji użytkowych. Migracja zachowała treść, nie potwierdza jej aktualności.
- [ ] Interaktywny przegląd mobile/theme/search (readonly, ONE_CODING_AGENT_DIR,
  one run, MCP, compaction) i kontrola zewnętrznych linków.
- [ ] Pełny pytest: próby przerwane limitami 180/600 s; w dłuższym przebiegu
  był jeden błąd przed 69%, bez końcowego raportu. Nie uznano pełnej walidacji
  za zakończoną. Zestaw skupiony 64 testów przeszedł osobno.
- [ ] Wybór docelowej domeny/ścieżki i ręczna publikacja przez operatora.

Nie wykonano uploadu, commita, push ani publikacji. Nie kontaktowano providerów
ani prawdziwych MCP w ramach walidacji przykładów.

- [x] Integracja `landing_page/deploy.sh`: lokalny build i upload statycznego
  `website/dist/`, wybór docs/landing/all, dry-run oraz testy offline.
  Nie wykonano połączenia z produkcją; konfiguracja `.env` nie była odczytywana.
