# Extension Runtime Contract

```
 ██████╗ ███╗   ██╗███████╗
██╔═══██╗████╗  ██║██╔════╝
██║   ██║██╔██╗ ██║█████╗
██║   ██║██║╚██╗██║██╔══╝
╚██████╔╝██║ ╚████║███████╗
 ╚═════╝ ╚═╝  ╚═══╝╚══════╝
```

[Full TUI logo artwork](https://github.com/noxgle/one/blob/main/one/assets/logo.txt)

An extension is a plain Python file that exports a `register(ctx) -> hooks` function.
The contract is inspired by [opencode](https://opencode.ai) plugins — hooks share the same
names and similar inputs/outputs. Hooks may be **synchronous or async** functions.

## ⚠️ Trust warning

Extension files (`.py`) are imported and executed during session initialization.
Project-level extensions in `.one/extensions/` and MCP server commands configured
in `.one/settings.json` are **not sandboxed** — they run with the same permissions
as `one` itself. **Only use extensions and MCP servers from trusted repositories.**

To start `one` without loading project extensions or MCP servers:

```bash
one --no-extensions --no-mcp
```

## Discovery

Python files from the following locations are loaded at session bind time:

1. `<agent_dir>/extensions/` — the agent directory (default `~/.config/one/extensions`,
   overridable via `ONE_CODING_AGENT_DIR`); this is where a package manager installs packages;
2. `<cwd>/.one/extensions/`;
3. Paths passed via `--extension <path>` (alias `-e`; repeatable).

Binding happens automatically once per session — `create_agent_session_runtime()` calls
`bind_extensions()` (which in turn reads `resource_loader.get_extensions()`).

### Package manager

- `one install <path-to-file-or-directory>` — copies to `agent_dir/extensions/`
  and updates the manifest (`packages` in `settings.json`);
- `one remove <name>` — deletes the file/directory from disk and from the manifest;
- `one update [name]` — synchronizes the manifest in both directions;
- non-existent path during `install` → exit code `1`.

## Contract

### `register(ctx) -> hooks`

```python
def register(ctx):
    return {
        "tool.execute.before": before_tool,
        "tool.execute.after": after_tool,
        # ...
    }
```

`ctx` fields (`ExtensionContext`):

| field | meaning |
|---|---|
| `directory` | cwd of the resource loader (project directory) |
| `worktree` | nearest ancestor containing `.git` (fallback: `directory`) |
| `sessionID` | id of the current session |
| `model` | `"provider/model-id"` or `None` |
| `settings` | dict of global settings (`settings.json`) |

### Hooks

| hook | when | input | output | effect |
|---|---|---|---|---|
| `tool.execute.before` | before every tool call (before `tool_call_start`) | `{directory, worktree, sessionID, tool, args}` | `{"args": args}` | **chaining**: each hook sees the effective args from the previous hook. See below for precedence, validation, and chaining details. |
| `tool.execute.after` | after tool execution | `{directory, worktree, sessionID, tool, ok}` | `{"title", "output", "metadata"}` | info-only |
| `chat.message` | for every new user message (before the prompt) | `{directory, worktree, sessionID, message}` | `{}` | info-only |
| `experimental.session.compacting` | before generating a compaction summary | `{directory, worktree, sessionID}` | `{"context": [], "prompt": None}` | pushes items to `context` (dicts with `content`), `prompt` may override compaction instructions |
| `dispose` | at `session.dispose()` | — | — | cleanup |

#### Precedence, chaining, and validation

- **Precedence** (same hook): a returned dict **overrides** the assignment to
  `output["args"]` from the *same* hook.  If a hook returns a dict,
  `output["args"]` (whether set or not) is not validated — the returned
  dict becomes the effective args directly.
- **Chaining**: the next hook in the chain sees the effective args (the current
  hook's result) in `input["args"]` and `output["args"]`.
- **Validation**: when a hook returns `None`, `output["args"]` must exist and be
  a valid `dict`; missing key or non-`dict` value raises `TypeError` → deny.
- **Allowed values**: only `None` or `dict`. Any other value (e.g.
  `42`, `"string"`) is an error — raises `TypeError` → deny and breaks the chain.

### Deny semantics (`tool.execute.before`)

**Any exception raised in a `before` hook = denial of the tool call**:

- event `tool_approval_rejected` with reason `Extension <name> denied: <reason>`;
- event `extension_load_error` (stage `hook`);
- the first raising hook wins (subsequent hooks are not called).

For clarity, raise `ExtensionDenied`:

```python
from one.resources.extension_runtime import ExtensionDenied
```

### Errors

All issues are reported as the `extension_load_error` event and **never
crash the session**:

| stage | when |
|---|---|
| `load` | file fails to import / `register` is not callable / returns non-dict |
| `bind` | unknown hook name (ignored, `UnknownHook`) or value is not callable |
| `hook` | exception during hook execution |

Event fields: `path`, `hook`, `error`, `errorType`.

Unknown hook names are silently ignored (parity with opencode) with an `extension_load_error` event at stage `bind`.

## Examples

### 1. Blocking tools (deny)

```python
from one.resources.extension_runtime import ExtensionDenied

BLOCKED = {"bash", "write"}


def register(ctx):
    return {"tool.execute.before": before}


def before(input, output):
    if input["tool"] in BLOCKED:
        raise ExtensionDenied(f"{input['tool']} is blocked by policy")
```

### 2. Hook chaining and precedence with returned dict

Each hook in `tool.execute.before` sees the effective arguments from the previous
hook (in `input["args"]` and `output["args"]`).

**Precedence — same hook**: returning a dict **wins** over assigning to
`output["args"]` from the *same* hook. Each result is validated before
invoking the next hook — invalid state does not propagate between hooks.

**Example — same hook sets invalid output but returns valid dict;
next hook observes the returned dict:**

```python
# Extension A — output["args"] = "string" (invalid), but returns dict
def register(ctx):
    return {"tool.execute.before": hook_a}


def hook_a(input, output):
    output["args"] = "invalid-string"  # set, but overridden by return
    return {"path": "b.txt", "encoding": "utf-8"}  # wins (same hook)


# Extension B — sees the returned dict from hook_a (chaining)
def register(ctx):
    return {"tool.execute.before": hook_b}


def hook_b(input, output):
    # input["args"] == {"path": "b.txt", "encoding": "utf-8"}
    output["args"] = {**input["args"], "chained": True}
```

### 3. Mutating arguments (enforce timeout)

```python
def register(ctx):
    return {"tool.execute.before": before}


def before(input, output):
    if input["tool"] == "bash":
        output["args"] = {**output["args"], "timeoutSec": 30}
```

### 4. Logging executed tools (info-only)

```python
def register(ctx):
    return {"tool.execute.after": after}


def after(input, output):
    print(f"[ext:{ctx_worktree(input)}] {input['tool']} ok={input['ok']}")
```

### 5. Context and instructions for compaction

```python
def register(ctx):
    return {"experimental.session.compacting": compacting}


def compacting(input, output):
    output["context"].append({"content": "Prefer pydantic over dataclasses."})
    output["prompt"] = "Compress tightly, keep all decisions."
```

## Extension UI (widget/overlay)

A related but separate mechanism — sessions can render UI requests from extensions:

- `session.request_extension_ui(extension, ui_type, payload, title)` — `ui_type`:
  `widget` or `overlay`; emits event `extension_ui_request`;
- `session.respond_extension_ui(request_id, payload, cancelled)` — emits
  `extension_ui_response`;
- `session.get_extension_ui_state()` / `session.clear_extension_ui_history()`.

In the TUI, a request renders as a block and the answer is typed into the Input (JSON
or text; empty = cancel). Commands `/extui <list|request|respond|cancel|clear>` are
available in the TUI and interactive mode.

## Tests

- `tests/test_extension_runtime.py` — hook contract (load/bind/deny/compaction);
- `tests/test_extension_ui_hooks.py` — extension UI flow.
