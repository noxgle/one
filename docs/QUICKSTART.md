# Quickstart

This guide gets a new user from installation to a first useful task in about five minutes.

## 1. Install

From PyPI:

```bash
python3 -m pip install one-agent
```

Or from a source checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Check the installation:

```bash
one --version
one --help
```

## 2. Choose a provider

For a hosted OpenAI-compatible provider, set its key in the current shell:

```bash
export OPENAI_API_KEY=sk-...
```

For Anthropic or Gemini, use `ANTHROPIC_API_KEY` or `GEMINI_API_KEY` instead.
For a local server, no API key is required:

```bash
one --provider ollama --model local --ollama-url http://localhost:11434/v1
```

Never commit a real key to a repository or paste it into a prompt. Prefer an
environment variable or the interactive `/login` command.

## 3. Run the first task

The simplest non-interactive check is:

```bash
one run "summarize README.md" --provider openai --model gpt-4o-mini
```

For the interactive TUI, run:

```bash
one
```

Then authenticate, if needed:

```text
/login openai sk-... gpt-4o-mini
```

`/login status` shows the configured providers. `/help` lists the available
commands. The exact provider and model names depend on your account and setup.

## 4. Try a useful prompt

Start with a bounded, read-only request:

```text
Inspect this repository and explain its structure. Do not edit files or run
commands that change anything. Point me to the three most important files.
```

When the agent proposes a tool action, read the approval request before
accepting it. Use `Ctrl+C` to reject a pending approval or abort the current turn.

More copy-and-paste prompts are in [EXAMPLES.md](EXAMPLES.md).

## 5. Useful next steps

| Goal | Command or action |
| --- | --- |
| See all slash commands | `/help` or `Ctrl+F1` |
| Continue previous work | Start with `--continue` or use `/sessions` |
| Stop a long turn | `Ctrl+C` |
| Toggle cooperation | `Ctrl+Z` in TUI |
| Change retry behavior | `/retry off`, `/retry on`, or `/retry unlimited` |
| Force a context summary | `/compact` |
| Paste an image | `Ctrl+Alt+V` or `/paste-image` |
| Run one task from a script | `one run "..." --json` |

## Safety baseline

Keep cooperation and approval prompts enabled while learning the tool. Treat
bash commands, file writes, extensions, skills, and MCP servers as privileged
operations. Only enable or install integrations you understand, and use
`--no-extensions --no-mcp` when opening an untrusted repository.

## If something fails

Run through the checklist in [FAQ.md](FAQ.md), starting with provider, model,
authentication, and network access. When reporting a problem, include the
sanitized command, provider/model, operating system, and relevant error text;
never include API keys or session files.
