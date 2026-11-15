---
title: "Quick start"
---

```bash
export OPENAI_API_KEY=sk-...
one run "summarize README.md" --provider openai --model gpt-4o-mini
```

For an interactive terminal UI, start `one` (the default mode is TUI) or use
`one --mode tui`. To authenticate interactively, start the TUI and use:

```text
/login openai sk-... gpt-4o-mini
/login status
```
