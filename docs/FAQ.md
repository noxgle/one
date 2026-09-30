# FAQ and troubleshooting

## Which provider should I choose?

Use a provider you already have access to and a model listed by that provider.
OpenAI-compatible providers are a practical starting point; Anthropic and
Gemini are also supported. Ollama and llama.cpp are useful when the model runs
locally and no hosted API key is desired. Provider/model availability changes,
so verify the current model name with the provider.

## How do I check authentication?

In the TUI, use:

```text
/login status
```

Credentials supplied at runtime take precedence over stored credentials, which
take precedence over provider environment variables. Common variables are
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and `GEMINI_API_KEY`. Do not paste keys
into bug reports or commit them.

## I get an authentication or unauthorized error

1. Confirm the provider name and model name.
2. Check that the environment variable is set in the same shell that starts
   `one` (`test -n "$OPENAI_API_KEY"` checks presence without printing it).
3. Try `/login status` and, if appropriate, `/login refresh <provider>`.
4. Check that the account has access to the selected model and remaining quota.
5. Retry with a minimal read-only prompt.

## The model is not found

The model name must be valid for the selected provider. Remove a copied model
suffix or use the provider's exact spelling. For local services, verify the
server is running and that the configured base URL and model match the server.

## The request times out or stops unexpectedly

Check network access, provider status, model load time, and configured timeout
values. Try a shorter prompt and a smaller model first. A local model may need
time to load. A timed-out subagent is not proof that an external operation
stopped; inspect external state before retrying a potentially destructive task.

## How do I avoid accidental changes?

Say explicitly that the task is read-only, keep cooperation enabled, and review
every approval. You can disable all built-in tools with `--no-tools` for a
text-only run. For an untrusted repository use:

```bash
one --no-extensions --no-mcp
```

Do not enable extensions, skills, MCP servers, or shell commands unless you
understand what they can execute.

## How do I continue or find a session?

Use `--continue` to continue the most recent session, or `--resume` with a
session identifier. In the TUI, `/sessions` opens the session browser. Avoid
copying session files into bug reports: they may contain prompts and tool output.

## Why does a command work in TUI but not in text mode?

TUI supports interactive approvals, questions, steering, slash commands, and
session controls. `--mode text` is a one-shot mode and does not provide an
interactive approval or steering channel. Use `one run` for a headless task and
its documented file or JSON controls when automation is needed.

## How do I use images?

Use `--image PATH`, paste from the system clipboard with `Ctrl+Alt+V`, or use
`/paste-image` when the terminal intercepts the shortcut. The selected provider
and model must support image input, and the image must still be available at
the time it is read.

## What information should I include in a bug report?

Include the `one --version` output, operating system, installation method,
provider/model (but never the key), sanitized command, exact error message, and
small reproduction steps. Mention whether the issue occurs in TUI, text, RPC,
or `one run`. Remove prompts, paths, customer data, tokens, and session files.
