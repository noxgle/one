---
title: "Cooperation and safety"
---

> ## ⚠️ Workspace trust warning
>
> Starting `one` in a repository can execute arbitrary Python from
> `.one/extensions/*.py` and shell commands from MCP configuration in
> `.one/settings.json`. **Use it only in repositories you trust.** To inspect an
> untrusted repository, start with:
>
> ```bash
> one --no-extensions --no-mcp
> ```
>
> Extensions and MCP servers are not sandboxed; they run with `one`’s
> permissions. Cooperation mode is an approval gate, not a sandbox or security
> boundary.

`--cooperation` requests approval before mutating tools (`bash`, `write`,
`edit`, `plan`, and `apply_patch`). In the TUI, toggle it with `Ctrl+Z` and use
the visible Approve/Reject controls (or `Alt+A`/`Alt+R`); normal prompt input
remains available while an approval is pending. Use `Ctrl+A` in the interactive
fallback. Rejections include a reason returned to the agent. Interactive modes provide `/steer`, `/follow`, and abort controls;
RPC and `one run` provide the channels described above.

When cooperation and `ask_user` are both enabled, the model is instructed to
ask one focused question for necessary preferences or genuinely blocking
ambiguity, then resume the same task rather than putting a question in its final
summary.

`apply_patch` validates collisions and symlink paths and uses staging/rollback,
but it cannot guarantee recovery from power loss, hostile concurrent filesystem
changes, or rollback I/O failure. Review changes and retained backups when an
error is reported.
