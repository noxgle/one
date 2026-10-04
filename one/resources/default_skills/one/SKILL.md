---
name: one
description: Information about the 'one' autonomous terminal agent (noxgle/one). Use when asked about one's features, installation, modes, tools, configuration, or how to use it. Covers headless/REPL/TUI/RPC modes, cooperation mode, MCP tools, skills, extensions, sessions, and troubleshooting.
---

# one — Autonomous Terminal Agent

## Overview

**one** is an autonomous Python terminal agent that executes assigned tasks on its own (shell / files / code) — plan, run tools, verify, report — and can optionally work in a cooperation mode where a human approves mutating tools, steers or aborts mid-task, and answers agent questions.

- **Repository:** https://github.com/noxgle/one
- **Language:** Python
- **Maturity:** Alpha (0.1.x). Public APIs, tool contracts, and storage formats may change in 0.1 releases.
- **Distribution name:** `one-agent` (PyPI); import and console-script name: `one`

## Installation

### From PyPI
```bash
pip install one-agent
one --help
```

### From source
```bash
git clone https://github.com/noxgle/one.git
cd one
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
one --help
```

### Verify
```bash
one --version
python -m one --help
```

### Updating
```bash
# From PyPI
python -m pip install --upgrade one-agent

# From source
git pull
python -m pip install -e '.[dev]'
```

## Modes

| Mode | Interactive | Input | Output | Best for |
|------|------------|-------|--------|----------|
| `--mode tui` | Yes | Textual editor and slash commands | Rendered stream | Human-driven work |
| `--mode text` | No | Positional messages | Last assistant text | Simple one-shot output |
| `--mode json` | No | Positional messages | JSON object with session messages | One-shot session data |
| `--mode rpc` | Yes (protocol-driven) | One JSON per stdin line | JSON event per stdout line | Orchestrators and UI clients |
| `one run <task>` | No | Required task argument | Summary or result JSON | Autonomous CI/headless tasks |

Default mode is **TUI**.

## Quick Start

```bash
export OPENAI_API_KEY=sk-...
one run "summarize README.md" --provider openai --model gpt-4o-mini
```

For interactive TUI:
```bash
one --mode tui --provider openai --model gpt-4o-mini
```

Authenticate interactively in TUI:
```
/login openai sk-... gpt-4o-mini
/login status
```

## 14 Built-in Tools

1. **read** — Read file contents
2. **read_image** — Read image files (base64-encoded blob with metadata)
3. **bash** — Execute shell commands
4. **edit** — Edit files with targeted replacements
5. **write** — Overwrite entire files
6. **grep** — Search files with patterns
7. **find** — Find files by pattern
8. **ls** — List directory contents
9. **evidence_read** — Retrieve durable tool evidence in bounded chunks
10. **finish** — Terminal task result (summary + goal_success)
11. **plan** — Create and manage execution plans
12. **spawn_subagent** — Spawn subagent tasks
13. **ask_user** — Ask the user for preferences or clarification
14. **apply_patch** — Apply non-unified diff patches with backup/rollback

## Key Features

### Cooperation Mode
- Human can approve mutating tools, steer, or abort mid-task
- Toggle with `Ctrl+Z` in TUI
- Shows `COOP: ON` or `COOP: OFF` in the status panel

### MCP Tools
- Supports Model Context Protocol (MCP) tools
- List servers: `/mcp list`
- Servers appear in the sidebar with status and tool counts

### Sessions & Configuration
- Sessions are retained across runs
- Config stored in `~/.config/one` (or `ONE_CODING_AGENT_DIR`)
- Project config: `.one/settings.json`
- Global config: `settings.json`

### Images
- TUI supports image input via `--image`, pasted/path images, and `read_image`
- Paste from clipboard: `Ctrl+Alt+V` (or `/paste-image`)

### Skills & Extensions
- Skills are directories containing `SKILL.md`
- Discovered in `~/.agents/skills/` and `.agents/skills/`
- Skills are loaded on demand when a task matches
- Use `/skill:name [args]` to force-load a skill

### TUI Slash Commands
- `/help` — Show help
- `/steer` — Steer the agent
- `/follow` — Follow up
- `/abort` — Abort current turn
- `/login` — Authenticate with a provider
- `/mcp list` — List MCP servers
- `/config` — View/change configuration
- `/layout` — Change layout (compact, wide, focus)
- `/sidebar` — Show/hide sidebar
- `/paste-image` — Paste image from clipboard
- `/reload` — Reload skills during active session

### TUI Keyboard Shortcuts
| Shortcut | Action |
|----------|--------|
| `Ctrl+P` | Command palette |
| `Ctrl+K` | Provider/model picker |
| `Ctrl+C` | Abort turn / reject approval |
| `Ctrl+L` | Clear stream |
| `Ctrl+Q` | Quit |
| `Ctrl+Z` | Toggle cooperation |
| `Ctrl+S` | Toggle subagents |
| `Ctrl+O` | Toggle bash output |
| `Ctrl+V` | Paste host clipboard text |
| `Ctrl+Shift+V` | Paste terminal text (SSH-safe) |
| `Ctrl+Alt+V` | Paste image from clipboard |
| `Ctrl+R` | Cycle retry mode |
| `Ctrl+Up/Down` | Navigate input history |
| `Ctrl+F1` | Slash-command help |
| `Esc` | Close shortcuts panel |

## Configuration

### Startup info panel
```json
{
  "tui": {
    "infoPanel": "top"
  }
}
```
- `top` — Minimal layout, no right sidebar (default)
- `sidebar` — Detailed right sidebar

### Layouts (mutually exclusive at runtime)
- `/layout compact` — Top status panel, no sidebar
- `/layout wide` — No top panel, detailed sidebar
- `/layout focus` — No information panels

### Bash mode
- `/bash` uses a restricted, shell-free manual-command interface
- Default profile is `strict` (read-only set)
- Change with `/config tui.manualBashMode` or `settings.json`

## Architecture

- **Config directory:** `~/.config/one` (or `ONE_CODING_AGENT_DIR`)
- **Skill locations:** `~/.agents/skills/`, `.agents/skills/`
- **Project config:** `.one/settings.json` (cannot override global settings)
- **Sessions:** Stored outside the repository; preserved across updates

## Safety

- Cooperation mode provides approval gates for mutating operations
- Bash mode can be restricted to read-only profiles
- Skills should be reviewed before granting project trust
- Malformed `SKILL.md` files produce warnings rather than stopping startup
- Name collisions keep the first discovered skill with a warning

## FAQ

**Q: How do I switch providers?**
A: Use `Ctrl+K` in TUI or `--provider <name>` on the CLI.

**Q: Can I use this headless?**
A: Yes — `one run "<task>"` or `--mode text` / `--mode json` / `--mode rpc`.

**Q: Where are sessions stored?**
A: Under `~/.config/one` (or the path set by `ONE_CODING_AGENT_DIR`).

**Q: Does updating remove my config?**
A: No. Configuration, credentials, and saved sessions are preserved.

**Q: What's the difference between `one` and `one-agent`?**
A: `one-agent` is the PyPI distribution name. The import and console-script name is `one`.

## References

- **GitHub:** https://github.com/noxgle/one
- **PyPI:** https://pypi.org/project/one-agent/
- **CHANGELOG:** https://github.com/noxgle/one/blob/main/CHANGELOG.md
- **TODO:** https://github.com/noxgle/one/blob/main/TODO.md
