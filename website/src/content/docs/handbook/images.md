---
title: "Images"
---

Image input supports PNG, JPEG, and WebP: at most four images per prompt, with
a 10 MB source-file limit and decoded Base64 limited to 5 MiB. Use startup
`--image` (repeatable) or TUI image paste:

```bash
one run "describe this diagram" --image diagram.png
one --mode text "describe this diagram" --image diagram.png
```

Images are transient for the current turn and are not persisted in session JSONL.
Errors and events use only source basenames. Vision-incapable models reject
images before an HTTP request. ChatGPT/Codex vision-capable models send native
Responses API `input_image` parts; invalid or missing blobs fail explicitly.

TUI, text/json, and `one run` accept startup `--image`; RPC accepts startup
images and prompt `attachments`. `read_image` is a runtime tool the agent can
call during its turn to inspect a file; it is not a startup image-input channel
for `--mode text` or `one run`. Steering and follow-up messages are text-only.
