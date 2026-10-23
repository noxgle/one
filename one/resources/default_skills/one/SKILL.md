---
name: one
description: Information about the 'one' autonomous terminal agent (noxgle/one). Use when asked about one's features, installation, modes, tools, configuration, or how to use it. Covers headless/REPL/TUI/RPC modes, cooperation mode, MCP tools, skills, extensions, sessions, and troubleshooting.
---

# one — Autonomous Terminal Agent

## Overview

**one** is an autonomous Python terminal agent that executes assigned tasks on its own (shell / files / code) — plan, run tools, verify, report — and can optionally work in a cooperation mode where a human approves mutating tools, steers or aborts mid-task, and answers agent questions. Read the installed version's `one --help` for current CLI options; details here may vary between releases.

- **Repository:** https://github.com/noxgle/one
- **Language:** Python
- **Maturity:** Alpha (0.1.x). Public APIs, tool contracts, and storage formats may change in 0.1 releases. Requires Python 3.12 or newer.
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

The default mode is configurable and currently falls back to **TUI** when unset. Use `one --help` to check supported modes and options.

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

## Built-in Tools

The built-in tool set includes file and shell operations, search, planning, subagents, user interaction, patching, and durable evidence retrieval. Availability depends on the active mode, configuration, and preset; consult `one --help` and the active session's tool listing rather than relying on a fixed tool count. Tool results may be shortened in provider context; when an evidence ID is supplied, use `evidence_read` with bounded offsets to retrieve the complete sanitized result in chunks.

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
- Runtime state defaults to `~/.config/one`; `XDG_CONFIG_HOME` changes the config root, and `ONE_CODING_AGENT_DIR` can override the agent state directory
- Project config: `.one/settings.json`
- Global config: `settings.json`
- In the TUI, `/sessions` lists sessions; `/sessions rename &lt;number|full exact name&gt; &lt;new name&gt;` renames one, and `/sessions delete &lt;number|full exact name&gt;` requests explicit confirmation before deletion

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
- `/sessions` — List/select sessions; supports `rename` and confirmed `delete` subcommands

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
- The `readonly` preset is enforced by the application; it is not an operating-system sandbox and does not confine already-running extension code
- Extensions and MCP servers execute with the agent's permissions and are not sandboxed. For an untrusted repository, start with `--no-extensions --no-mcp`
- Skills should be reviewed before granting project trust
- Malformed `SKILL.md` files produce warnings rather than stopping startup
- Name collisions keep the first discovered skill with a warning
- Sessions and authentication state may contain sensitive information. Do not copy, publish, or inspect real state directories unnecessarily

## Presets and tool policy

Named presets request a set of capabilities, but requested capabilities are intersected with CLI and parent-agent ceilings and enforced at tool execution, not only during tool discovery. Child agents and refreshed MCP tools cannot exceed those ceilings. Readonly is application policy, not process isolation. Resuming or forking a session restores its saved effective preset snapshot; reapply a preset explicitly to adopt changes to its file. See `docs/PRESETS.md` for preset format and behavior.

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
