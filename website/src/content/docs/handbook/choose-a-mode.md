---
title: "Choose a mode"
---

`--mode` accepts exactly `tui`, `text`, `json`, or `rpc`. `one run <task>` is a
distinct headless subcommand outside `--mode` routing, not a `--mode` value.
The default settings select the TUI.

| Interface | Interactive | Input | Output | Startup banner | Images | Best for |
| --- | --- | --- | --- | --- | --- | --- |
| `--mode tui` | Yes | Textual editor and slash commands | Rendered stream | Yes | `--image`, pasted/path images, `read_image` (runtime) | Human-driven work |
| `--mode text` | No | Positional messages | Last assistant text | Yes, unless `quietStartup` is set | Startup `--image` | Simple one-shot text output |
| `--mode json` | No | Positional messages | JSON object containing session `messages` | Yes, unless `quietStartup` is set | Startup `--image` | One-shot session data |
| `--mode rpc` | Yes, protocol-driven | One JSON object per stdin line | JSON event/response object per stdout line | No | Startup `--image`; prompt `attachments` | Orchestrators and UI clients |
| `one run` | No | Required task argument | Summary, or a result JSON object with `--json` | Yes unless `--json` or `quietStartup` suppresses it | Startup `--image` | Autonomous CI/headless tasks |

`--mode text` is **not** an interactive REPL. It sends the supplied positional
message or messages and prints after they finish. Do not use `--mode cli`:
it is not an accepted CLI mode.

### TUI

The Info panel shows **Tool Limit**, the effective `tools.maxSteps` value (0
means unlimited). In the command input, Ctrl+U toggles unlimited and the last
positive effective limit saved by the toggle, or 100 if none was saved. The
toggle persists global settings, including the remembered limit, but never edits
project settings; a project override still takes precedence and is reported.
Each tool-loop attempt captures its limit at the start. A step is a model-loop
iteration, not an individual tool call: a response can contain multiple tool
calls in one step. Exhausting a positive limit ends the attempt with a
`tool_step_limit` response; unlimited mode still respects aborts and budgets.

```bash
one
one --mode tui --provider openai --model gpt-4o-mini
one --mode tui --image screenshot.png
```

The Textual TUI is the human-facing interface. It streams responses, displays
tool lifecycle status, retains sessions, and accepts slash commands such as
`/help`, `/agents`, `/steer`, `/follow`, `/abort`, `/login`, and `/mcp list`. It displays a
startup banner and status information.

In explicit TUI mode, Textual owns the UI loop while the agent runtime owns a
separate worker loop. The UI exchanges bounded, detached command/event DTOs
with that runtime; it does not access live session state. This improves
isolation from blocked runtime I/O, but threads still share the Python GIL and
do not provide a security sandbox or an unconditional latency guarantee.

Subagent headers show the effective preset name and actual provider/model.
Legacy history without preset metadata keeps its model-only label. Invalid
provider/model selections return an error to the parent; models dynamically
served by supported providers remain allowed. Permanent model/access errors
end the child as failed rather than waiting indefinitely in unlimited retry.

`/agents` opens a read-only monitor for direct child agents of the current
session only. The inspector fills the terminal, with a fixed status header and
keyboard-help footer. It opens at the beginning of one timeline: the complete,
redacted task prompt followed by recorded work, without a Tab switch. Long prompts
and results are split into bounded pages with wrapped lines and preserved indentation.
Older journals explicitly identify summary-only prompts. Full-prompt recording
applies to newly started children. Home loads the beginning; End selects recent
activity and enables live following. PageUp/PageDown browse pages; Up/Down scroll
within a page and pause following. Operation blocks update in place from running
to success/failure, retaining their arguments and detail-gated results. Paused pages
refresh operation states without jumping to the tail. Block colors use the current
theme. Provider-emitted display Thinking deltas appear in a Thinking block and
activity indicator; no hidden reasoning is inferred when the provider supplies none.
Left/Right select a child; N/P browse pages of 20 children, including older
completed runs. Large bodies are split into bounded pages rather than silently
removed from the journal. The window is read-only and never switches sessions.

Saved parent sessions keep a private `.jsonl.agents` SQLite sidecar, separate from
provider context. Resume restores recorded runs; unfinished runs are marked
interrupted. Deleting a parent removes its journal. Ephemeral sessions use a private
temporary journal that is removed when the session is disposed. Old sessions do
not gain retrospective history, and forks start a new journal. Only visible,
sanitized display data is recorded, including provider-emitted Thinking text but
not undisclosed model reasoning, raw tool arguments,
images, or complete evidence payloads. Result text is recorded only while details
are enabled and is hidden from history pages when details are off. This is a
recorded display history, not an unlimited raw tool-output archive; existing
result projections and redaction still apply. Journals grow on disk with recorded
work, while UI pages and preview caches stay bounded.

Click an application-generated inline **[view]** link beside a `spawn_subagent`
tool call to open the same monitor with that child selected. `/subagents on|off`
remains only the enable/disable control for subagent tools.

Completed durable tool results can also have an application-generated **[view]**
action. It opens a bounded, sanitized page viewer; PageUp/PageDown retrieves the
next worker-side page. It never exposes tool arguments or raw evidence records,
and links become unavailable after a session change or unavailable evidence.

Configure the startup information panel in `settings.json`:

```json
{"tui": {"infoPanel": "top", "toolResultPreviewLines": 100}}
```

`top` is the default minimal layout with no right sidebar; `sidebar` enables the
detailed right sidebar. These startup settings retain their existing semantics.
`toolResultPreviewLines` controls the positive 1–1000 logical-line limit for
tool-result previews (default `100`); larger results retain a safe `[view]`
action when durable evidence is available.
At runtime, the information layouts are mutually exclusive:

- `/layout compact` shows the top status panel and hides the detailed sidebar.
- `/layout wide` hides the top status panel and shows the detailed right sidebar.
- `/layout focus` hides both information panels.

Use `/sidebar hide` or `/sidebar show` to hide or show the detailed sidebar;
showing it selects the `wide` layout, while hiding it from `wide` returns to
`compact`. The separate visual logo in the conversation window remains visible
in every layout and is not transcript content. The top status panel explicitly
shows `COOP: ON` or `COOP: OFF`.

Current application shortcuts are:

| Shortcut | Action |
| --- | --- |
| `Ctrl+P` | Command center: search actions and open submenus |
| `Ctrl+K` | Searchable provider/model picker; Enter applies for this session and `Ctrl+S` also saves the default |
| `Shift+Tab` | Next preset (main input, idle only; wraps to the first preset) |
| `Ctrl+C` | Abort turn |
| `Ctrl+L` | Clear stream |
| `Ctrl+Q` | Quit |
| `Ctrl+Z` | Toggle cooperation |
| `Alt+A` / `Alt+R` | Approve / reject the pending tool approval |
| `Ctrl+S` | Toggle subagents |
| `Ctrl+O` | Toggle bash output |
| `Ctrl+V` | Paste host/system clipboard text |
| `Ctrl+Shift+V` | Paste terminal text (SSH-safe) |
| `Ctrl+Alt+V` | Paste an image from the system clipboard |
| `Ctrl+T` | Cycle thinking level (same as `/thinking-cycle`; main view only) |
| `Ctrl+Left` / `Ctrl+Right` | Decrease / increase temperature |
| `Ctrl+R` | Cycle retry mode (`off` → `on` → `unlimited` → `off`) |
| `Ctrl+Up` / `Ctrl+Down` | Navigate the local input history (up to 50 prompts and commands) |
| `Ctrl+F1` | Show slash-command help |
| `Esc` | Close the shortcuts panel |

Use `/paste-image` if the terminal intercepts `Ctrl+Alt+V`.

#### Command center (Ctrl+P)

Open **Ctrl+P** to search grouped session, model/agent, tools, appearance, and
help actions. Search by name, description, shortcut, or slash command; selected
Polish aliases such as `sesje` and `motyw` are also supported. Toggle actions
show their current ON/OFF state, and unavailable actions explain why.

Use Up/Down or PageUp/PageDown to navigate, and Enter or a mouse click to run an
action or open its choices. Themes, presets, thinking levels, temperature,
layout, and sidebar visibility have submenus. Esc returns from a submenu or
closes the command center; Ctrl+P closes the command center directly. Opening
and cancelling it preserves the conversation draft.

Choose **Sessions** to open the [session browser](sessions-and-configuration.md#tui-session-browser), or
**Select model** to open the model picker below.

#### Theme picker (Ctrl+P → Theme)

The **Theme** submenu lists all built-in colour themes. Selecting one applies it
immediately and persists it in `settings.json` (`tui.theme`). The built-in themes
are `default`, `light`, `hacker`, `solarized`, `fallout`, and `cybertrunk`.

`cybertrunk` is a dark theme with a deep navy background (`#090B10`), high-
contrast light text (`#E6EDF3`), cyan accents (`#00E5FF`), and yellow highlights
(`#FCEE09`). It is suitable for extended coding sessions.

Set the theme via the slash command in the main input:

```text
/theme cybertrunk
```

#### Model picker (Ctrl+K)

**Ctrl+K** opens a searchable list of available models with a provider filter.
Search tokens match the provider and model ID (and a display name when supplied).
The current model is marked `●`; the selected model's full ID, context limit,
reasoning capability, and image-input capability appear below the list.
`unknown` means metadata is unavailable, not that the model has zero capacity.

- Up/Down, PageUp/PageDown, and the mouse navigate the list.
- **Session (Enter)** changes the model for the current session only.
- **Default (Ctrl+S)** also saves the selection as the startup default.
- **Cancel (Esc)** or Ctrl+K closes the picker without changing the model or draft.

Model changes are blocked during generation. A selection failure leaves the
picker open with feedback. The meaning of Ctrl+S here is local to this window;
in the main conversation it toggles subagents.

### TUI `/bash` command profile

TUI `/bash <command>` is a restricted, shell-free manual-command interface. Its
default `strict` profile permits a small read-only set (workspace file views,
searches, disk information, and selected Git inspection). Query/change the
**global** profile with `/config tui.manualBashMode` and
`/config tui.manualBashMode dev` (or global `settings.json`); project
`.one/settings.json` cannot enable `dev`.

| Profile | Permitted purpose |
| --- | --- |
| `strict` (default) | `pwd`, conservative `ls`/`tree`/file/search/disk forms, and selected read-only Git forms |
| `dev` | strict plus selected workspace mutations and fixed Python, npm, Cargo, and Go development forms |

Strict families are `pwd`; conservative `ls`, `tree -L`, `cat`, `head`/`tail
-n`, `wc`, `stat`, `file`, `rg`, `grep`, `du`, and `df`; and selected
`git status`, `diff`, `log`, `show`, `branch`, and `rev-parse` forms. The
executables must resolve from `/usr/bin` or `/bin`; a missing tool is denied.

Dev additionally permits `mkdir [-p]`, `touch`, and no-clobber `cp`/`mv` with
selected workspace paths; `git add -- <paths>` and `git restore --staged --
<paths>`; `python -m pytest [-q|-x|--tb=short] [tests]`,
`.venv/bin/python -m ruff check .`, `python -m ruff format --check .`,
`python -m compileall [-q]`, `python -m build`, and `python -m pip check`,
`python -m pip list`, or `python -m pip show <package>`; plus exact `npm test`,
`npm run lint`, `npm run build`, `cargo check`, `cargo test`, `cargo fmt
--check`, `go test`, and `go vet` forms. These are
grammars, not general executable permissions: arbitrary flags remain denied.

This is an invocation restriction, **not an OS sandbox**: it does not sandbox
repository code or eliminate TOCTOU races, and `dev` can execute project code.
Shell operators, redirects, substitutions, arbitrary options, arbitrary
interpreters, and paths outside the workspace are denied. A configured
`shellCommandPrefix` is incompatible with TUI `/bash` and is rejected rather
than run. Existing model `bash`, interactive `/bash`, and RPC bash behavior are
unchanged.

Manual TUI `/bash` commands and their output stay in local session history and
are never sent to the model, including during compaction. They may persist
sensitive output on disk, so avoid printing secrets. This does not apply to the
model `bash` tool: its bounded results remain model context as usual.

### One-shot text and JSON

```bash
# Prints the final assistant text after the prompt completes.
one --mode text "summarize README.md"

# --print is the same one-shot print path.
one --print "summarize README.md"

# Serializes the full session message list.
one --mode json "summarize README.md"

```

Text and JSON modes consume positional messages; they do not read prompts from
stdin and do not offer an interactive approval, question, or steering channel.
The one-shot JSON shape is `{"messages": [...]}`. It is not the result schema
of `one run --json` and it is not JSON-RPC. `--mode json` still shows the
startup banner unless `quietStartup` is configured. A print-mode invocation
with images but no text fails rather than silently succeeding.

### RPC

```bash
one --mode rpc
```

RPC is a long-lived JSON-lines protocol: write one JSON command per stdin line;
read JSON event and response objects from stdout, one per line. It emits no
startup banner, so stdout remains protocol data. A `prompt` command is accepted
before its asynchronous work completes; session events follow on stdout. Include
an `id` to correlate responses and request events.

```json
{"id":"p1","type":"prompt","message":"summarize README.md"}
{"id":"idle","type":"wait_for_idle"}
```

The first command receives a successful response, then prompt/tool/message
events; `wait_for_idle` receives its response after work completes. Use `steer`,
`follow_up`, `abort`, `answer_question`, and `get_pending_questions` for the
corresponding control channels. RPC prompt images use an `attachments` array of
paths. `steer` and `follow_up` reject image attachments. See
[`one/modes/rpc_mode.py`](https://github.com/noxgle/one/blob/main/one/modes/rpc_mode.py) and RPC snapshots under
[`tests/snapshots/rpc/`](https://github.com/noxgle/one/blob/main/tests/snapshots/rpc) for the implemented command set
and event shapes.
