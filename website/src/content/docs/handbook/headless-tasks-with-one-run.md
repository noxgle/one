---
title: "Headless tasks with `one run`"
---

```bash
one run "fix the failing tests and summarize the changes"
one run "refactor the auth module" --provider openai --model gpt-4o --json
one run @task.txt --param component=auth
one run "implement feature X" --answer-file /tmp/answer.txt --steer-file /tmp/steer.txt
```

`one run <task>` runs the autonomy loop and prints its final summary. It is a
subcommand, not a `--mode` route. `@task.txt` expands the task from that file;
`--param name=value` substitutes `{{name}}` in the expanded task file. For this
subcommand, `--json` is a separate clean-output contract: it suppresses the
startup banner and instead prints exactly:

```json
{"summary":"...","goalSuccess":true,"finished":true}
```

The task command can cooperate with a human without an interactive UI:

- `--cooperation` prompts on stdin before mutating tools; EOF rejects.
- `--answer-file PATH` writes an `ask_user` question to the file and polls until
  its content changes to an answer. Without it, questions receive a deterministic
  “proceed with best judgment” answer.
- `--steer-file PATH` polls for text, submits non-empty contents as steering,
  then clears the file. These channels are text-only.

Exit status is `0` only when the agent finishes with `goalSuccess`; `1` means a
failed, unfinished, aborted, or resumed-without-task result; `2` is usage or
validation failure. More generally, CLI validation errors use `2`; normal
subcommand failures use `1`. TUI and RPC processes normally exit `0` when
closed. `one run` writes a report to `reports.jsonl` when it has an agent dir.
