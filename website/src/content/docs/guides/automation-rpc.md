---
title: Automation and RPC
---

For a single autonomous task, use `one run "summarize README.md" --json`.
Its JSON result has `summary`, `goalSuccess`, and `finished`; exit status 0
requires goal success, 1 indicates failure or unfinished work, and 2 indicates
usage or validation failure. `@task.txt` reads task text from a file and
`--param name=value` substitutes `{{name}}` in that file. See
[CLI reference](../reference/cli.md) and [runs](../library/runs.md).

RPC is a long-lived JSON-lines protocol, not JSON-RPC. For a state query, send
one object per stdin line and read one object per stdout line:

```sh
printf '%s\n' '{"id":"s1","type":"get_state"}' | one --mode rpc
```

On stdin EOF the current implementation raises `EOFError` and may print a
traceback; a production client should maintain the stream for the session and
handle process termination. The response has `type: "response"`, `id: "s1"`, `command: "get_state"`,
`success`, and on success `data`. For an actual model task, send these two lines
on stdin (with a configured provider):

```json
{"id":"p1","type":"prompt","message":"summarize README.md"}
{"id":"w1","type":"wait_for_idle"}
```

Prompt acceptance is asynchronous; events can interleave with responses. The
wait response arrives after accepted work completes. Correlate responses by `id`
and prompt events by `requestId` where present; do not assume every event has
an ID. Keep reading stdout while writing commands. For command-specific fields,
error cases and events consult `one/modes/rpc_mode.py` and
`tests/test_rpc_mode.py`; this guide does not claim a complete protocol schema.
