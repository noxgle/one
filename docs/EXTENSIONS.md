# Kontrakt rozszerzeń (extensions)

Rozszerzenie to zwykły plik `.py`, który eksportuje funkcję `register(ctx) -> hooks`.
Kontrakt jest wzorowany na wtyczkach [opencode](https://opencode.ai) — hooki mają te same
nazwy i zbliżone wejścia/wyjścia. Hooks mogą być funkcjami **synchronicznymi lub async**.

## Discovery

Pliki `.py` z następujących lokalizacji są ładowane przy bindzie sesji:

1. `<agent_dir>/extensions/` — katalog agenta (domyślnie `~/.config/one/extensions`,
   nadpisanie przez `ONE_CODING_AGENT_DIR`); tu instaluje package manager;
2. `<cwd>/.one/extensions/`;
3. ścieżki przekazane przez `--extensions <path>`.

Bind odbywa się automatycznie raz na sesję — `create_agent_session_runtime()` woła
`bind_extensions()` (który z kolei czyta `resource_loader.get_extensions()`).

### Package manager

- `one install <ścieżka-do-pliku-lub-katalogu>` — kopiuje do `agent_dir/extensions/`
  i dopisuje manifest (`packages` w `settings.json`);
- `one remove <nazwa>` — usuwa plik/katalog z dysku i z manifestu;
- `one update [nazwa]` — synchronizuje manifest w obie strony;
- nieistniejąca ścieżka przy `install` → exit code `1`.

## Kontrakt

### `register(ctx) -> hooks`

```python
def register(ctx):
    return {
        "tool.execute.before": before_tool,
        "tool.execute.after": after_tool,
        # ...
    }
```

Pola `ctx` (`ExtensionContext`):

| pole | znaczenie |
|---|---|
| `directory` | cwd resource loadera (katalog projektu) |
| `worktree` | najbliższy przodek zawierający `.git` (fallback: `directory`) |
| `sessionID` | id bieżącej sesji |
| `model` | `"provider/model-id"` lub `None` |
| `settings` | dict globalnych settingsów (`settings.json`) |

### Hooki

| hook | kiedy | input | output | efekt |
|---|---|---|---|---|
| `tool.execute.before` | przed każdym wywołaniem toola (przed `tool_call_start`) | `{directory, worktree, sessionID, tool, args}` | `{"args": args}` | **może mutować** `output["args"]`, zwrócić nowy dict z args, lub **deny** przez rzucenie wyjątku |
| `tool.execute.after` | po wykonaniu toola | `{directory, worktree, sessionID, tool, ok}` | `{"title", "output", "metadata"}` | info-only |
| `chat.message` | dla każdej nowej wiadomości użytkownika (przed promptem) | `{directory, worktree, sessionID, message}` | `{}` | info-only |
| `experimental.session.compacting` | przed wygenerowaniem streszczenia kompakcji | `{directory, worktree, sessionID}` | `{"context": [], "prompt": None}` | pushuje itemy do `context` (dykty z `content`), `prompt` może nadpisać instrukcje kompakcji |
| `dispose` | przy `session.dispose()` | — | — | cleanup |

### Semantyka deny (`tool.execute.before`)

**Dowolny wyjątek rzucony w hooku `before` = odrzucenie wywołania toola**:

- event `tool_approval_rejected` z powodem `Extension <nazwa> denied: <powód>`;
- event `extension_load_error` (stage `hook`);
- pierwszy rzucający hook wygrywa (kolejne hooki nie są wołane).

Dla czytelności rzuć `ExtensionDenied`:

```python
from one.resources.extension_runtime import ExtensionDenied
```

### Błędy

Wszystkie problemy są raportowane jako event `extension_load_error` i **nigdy nie
crashują sesji**:

| stage | kiedy |
|---|---|
| `load` | plik się nie importuje / `register` nie jest callable / zwraca nie-dict |
| `bind` | nieznana nazwa hooka (ignorowana, `UnknownHook`) lub wartość nie-callable |
| `hook` | wyjątek podczas wykonania hooka |

Pola eventu: `path`, `hook`, `error`, `errorType`.

Nieznane nazwy hooków są cicho ignorowane (parity z opencode) z eventem `bind`.

## Przykłady

### 1. Blokada narzędzi (deny)

```python
from one.resources.extension_runtime import ExtensionDenied

BLOCKED = {"bash", "write"}

def register(ctx):
    return {"tool.execute.before": before}

def before(input, output):
    if input["tool"] in BLOCKED:
        raise ExtensionDenied(f"{input['tool']} is blocked by policy")
```

### 2. Mutacja argumentów (wymuszenie limitu czasu)

```python
def register(ctx):
    return {"tool.execute.before": before}

def before(input, output):
    if input["tool"] == "bash":
        output["args"] = {**output["args"], "timeoutSec": 30}
```

### 3. Logowanie wykonanych tooli (info-only)

```python
def register(ctx):
    return {"tool.execute.after": after}

def after(input, output):
    print(f"[ext:{ctx_worktree(input)}] {input['tool']} ok={input['ok']}")
```

### 4. Kontekst i instrukcje do kompakcji

```python
def register(ctx):
    return {"experimental.session.compacting": compacting}

def compacting(input, output):
    output["context"].append({"content": "Prefer pydantic over dataclasses."})
    output["prompt"] = "Compress tightly, keep all decisions."
```

## Extension UI (widget/overlay)

Pokrewny, osobny mechanizm — sesja może renderować prośby UI z rozszerzeń:

- `session.request_extension_ui(extension, ui_type, payload, title)` — `ui_type`:
  `widget` lub `overlay`; emituje event `extension_ui_request`;
- `session.respond_extension_ui(request_id, payload, cancelled)` — emituje
  `extension_ui_response`;
- `session.get_extension_ui_state()` / `session.clear_extension_ui_history()`.

W TUI request renderuje się jako blok, a odpowiedź wpisuje się w Input (JSON lub tekst,
puste = anuluj). Komendy `/extui <list|request|respond|cancel|clear>` dostępne w TUI
i trybie interaktywnym.

## Testy

- `tests/test_extension_runtime.py` — kontrakt hooków (load/bind/deny/kompakcja);
- `tests/test_extension_ui_hooks.py` — flow extension UI.
