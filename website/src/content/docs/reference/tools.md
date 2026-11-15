---
title: "Built-in tools reference"
---

Generated from `one/tools/index.py` and `one/resources/resource_loader.py`.
Schemas below are provider-facing native JSON schemas; prompt syntax is listed
separately and is not a substitute for execution validation. Availability depends
on policy, mode and configuration. MCP and extension tools are dynamic and are
not included. Defaults mark `DEFAULT_TOOL_NAMES`, not guaranteed availability.
Consult `one/core/agent_session.py` and each implementation for execution semantics.

## read

Read a text file with original 1-based line numbers. The 'N: ' prefixes are not file content; omit them from edits. Args: 'path' (absolute or workspace-relative); optional 'offset' (1-based starting line) and 'limit' (maximum lines). Output may be bounded or truncated. Use the separate read_image tool instead for image files.

Default list: yes.

Prompt syntax: `{path, offset?, limit?} # Read text with original 1-based line numbers; offset=1-based line. 'N: ' prefixes are not file content; omit them from edits. May truncate. Images: read_image.`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string"
    },
    "offset": {
      "type": "integer"
    },
    "limit": {
      "type": "integer"
    }
  },
  "required": [
    "path"
  ],
  "additionalProperties": false
}
```

## read_image

Load a local PNG/JPEG/WebP image for vision inspection. Args: 'path' (absolute or workspace-relative). Returns metadata; the image is sent to the vision model on the next step.

Default list: yes.

Prompt syntax: `{path}  # read an image file and return its content as a base64-encoded blob with metadata`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "minLength": 1
    }
  },
  "required": [
    "path"
  ],
  "additionalProperties": false
}
```

## bash

Execute bash commands

Default list: yes.

Prompt syntax: `{command, timeout?}  # timeout in seconds; default if omitted`

```json
{
  "type": "object",
  "properties": {
    "command": {
      "type": "string",
      "minLength": 1
    },
    "timeout": {
      "type": "integer",
      "minimum": 1
    }
  },
  "required": [
    "command"
  ],
  "additionalProperties": false
}
```

## edit

Replace a short unique block in an existing file. path is top-level; oldString must be non-empty and unique; newString may be empty to delete. For literal JSON tool calls, escape newlines as JSON newline escapes, quotes as JSON quote escapes and backslashes as JSON backslash escapes. If a long replacement is hard to encode, prefer apply_patch, not write (which overwrites the file).

Default list: yes.

Prompt syntax: `{path, edits: [{oldString, newString}]}  # path is TOP-LEVEL; match a short, non-empty, unique oldString; newString may be empty to delete. In literal JSON escape newlines, quotes and backslashes according to JSON rules. For long/hard-to-escape edits prefer apply_patch over overwriting the whole file with write. Example JSON: {"path":"a.txt","edits":[{"oldString":"a\n","newString":"b\n"}]}`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "minLength": 1
    },
    "edits": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "properties": {
          "oldString": {
            "type": "string"
          },
          "newString": {
            "type": "string"
          },
          "oldText": {
            "type": "string"
          },
          "newText": {
            "type": "string"
          }
        },
        "additionalProperties": false,
        "anyOf": [
          {
            "required": [
              "oldString",
              "newString"
            ]
          },
          {
            "required": [
              "oldText",
              "newText"
            ]
          }
        ]
      }
    }
  },
  "required": [
    "path",
    "edits"
  ],
  "additionalProperties": false
}
```

## write

Overwrite one complete file. path and content must be explicit strings; content:'' deliberately empties the file. Never use writeContentOmitted or '[omitted from model context]' history metadata as arguments. Prefer edit or apply_patch for localized changes when available. If bash is available and JSON serialization is difficult, use <<'ONE_EOF' with a delimiter absent as a standalone body line, quote the destination, preserve literal $/backticks/backslashes, and verify the file; the JSON command string must still be valid.

Default list: yes.

Prompt syntax: `{path, content}  # content must be an explicit string; overwrites the entire file and content:'' deliberately empties it. Never use writeContentOmitted or '[omitted from model context]' history metadata as arguments. Prefer write for complete files; use edit/apply_patch for localized changes when available. If bash is available and JSON serialization is difficult, use <<'ONE_EOF' with a delimiter absent as a standalone body line, quote the destination, preserve literal $/backticks/backslashes, and verify the resulting file (the JSON command string must still be valid).`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "minLength": 1
    },
    "content": {
      "type": "string"
    }
  },
  "required": [
    "path",
    "content"
  ],
  "additionalProperties": false
}
```

## grep

Search file contents

Default list: yes.

Prompt syntax: `{pattern, path?}`

```json
{
  "type": "object",
  "properties": {
    "pattern": {
      "type": "string"
    },
    "path": {
      "type": "string"
    }
  },
  "required": [
    "pattern"
  ],
  "additionalProperties": false
}
```

## find

Find files by pattern

Default list: yes.

Prompt syntax: `{pattern?, path?}`

```json
{
  "type": "object",
  "properties": {
    "pattern": {
      "type": "string"
    },
    "path": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

## ls

List directory contents

Default list: yes.

Prompt syntax: `{path?}`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

## evidence_read

Read a bounded chunk of durable tool evidence by evidenceId; never re-runs the original tool

Default list: yes.

Prompt syntax: `{evidenceId, offset?, maxChars?}  # retrieve a bounded chunk of prior durable tool evidence; use nextOffset to continue`

```json
{
  "type": "object",
  "properties": {
    "evidenceId": {
      "type": "string"
    },
    "offset": {
      "type": "integer"
    },
    "maxChars": {
      "type": "integer"
    }
  },
  "required": [
    "evidenceId"
  ],
  "additionalProperties": false
}
```

## finish

End the task with a terminal summary and success flag. When cooperation has an active ask_user tool, do not use finish to ask for missing information or escalate a question.

Default list: yes.

Prompt syntax: `{summary, goal_success}  # terminal task result with full summary or answer; do not use summary to ask for missing information when ask_user is available`

```json
{
  "type": "object",
  "properties": {
    "summary": {
      "type": "string"
    },
    "goal_success": {
      "type": "boolean"
    }
  },
  "required": [
    "summary",
    "goal_success"
  ],
  "additionalProperties": false
}
```

## plan

Store an execution plan for the current task; visible on every step

Default list: yes.

Prompt syntax: `{plan: [{step, status}]}  # status is pending|in_progress|completed|blocked; send the complete plan on every update`

```json
{
  "type": "object",
  "properties": {
    "plan": {
      "type": "array",
      "items": {
        "type": "object"
      }
    }
  },
  "required": [
    "plan"
  ],
  "additionalProperties": false
}
```

## ask_user

Ask the human one necessary question and wait for their answer, then continue the same task. Use for preferences, materially ambiguous requirements after available context is read, or unapproved destructive, security, account, or authorization decisions; not routine details or approvals handled elsewhere. Args: 'question' (str, required); optional 'timeoutSec' (int).

Default list: yes.

Prompt syntax: `{question, timeoutSec?}  # for necessary preferences, materially ambiguous requirements, or unapproved destructive/security/account decisions; waits for one human answer, then continue the same task`

```json
{
  "type": "object",
  "properties": {
    "question": {
      "type": "string"
    },
    "timeoutSec": {
      "type": "integer"
    }
  },
  "required": [
    "question"
  ],
  "additionalProperties": false
}
```

## spawn_subagent

Delegate a subtask to an isolated subagent. For smaller/less capable models, prefer one child with exactly one non-empty 'task' string. Use non-empty 'tasks' (list[str]) only when multiple independent parallel subtasks are explicitly requested. Provide exactly one of 'task' or 'tasks'; never both. Examples: {"task":"inspect X"}; {"tasks":["inspect X","inspect Y"]}. Optional 'model' ('provider/model'); optional 'tools' (non-empty list of tool names; 'finish' is added automatically if omitted); optional 'mcp_servers' is a list of known server names: omit to inherit, [] denies MCP, and named servers restrict access within the parent ceiling and tool policy; optional 'preset' selects a named child configuration, while explicit model/tools override it; optional 'timeout' (seconds, overrides the child idle timeout). Returns the subagent summary, success flag and session id.

Default list: yes.

Prompt syntax: `{task, tasks?, model?, tools?, mcp_servers?, preset?, temperature?, temperatureMode?, timeout?}  # For smaller/less capable models, prefer one child with exactly one non-empty task string. Use tasks as a non-empty list only when multiple independent parallel subtasks are explicitly requested. Provide exactly one of task or tasks; never both. Examples: {"task":"inspect X"}; {"tasks":["inspect X","inspect Y"]}. Optional mcp_servers is a list of known server names: omit to inherit, [] denies MCP, and named servers restrict access within the parent ceiling and tool policy. A preset supplies child configuration; model and temperature overrides take precedence. timeout overrides child idle timeout; numeric temperature overrides temperatureMode, otherwise inherits parent`

```json
{
  "type": "object",
  "properties": {
    "task": {
      "type": "string",
      "minLength": 1
    },
    "tasks": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "string",
        "minLength": 1
      }
    },
    "model": {
      "type": "string",
      "minLength": 1
    },
    "tools": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "string",
        "minLength": 1
      }
    },
    "mcp_servers": {
      "type": "array",
      "items": {
        "type": "string",
        "minLength": 1
      },
      "description": "Known MCP server names; omit to inherit, [] denies MCP. Access is intersected with parent restrictions and tool policy."
    },
    "preset": {
      "type": "string",
      "minLength": 1
    },
    "timeout": {
      "type": "integer",
      "minimum": 0
    }
  },
  "additionalProperties": false,
  "oneOf": [
    {
      "required": [
        "task"
      ]
    },
    {
      "required": [
        "tasks"
      ]
    }
  ]
}
```

## apply_patch

Apply an OpenCode patch-format patch, NOT a standard ---/+++ unified diff. patchText must be a non-empty string, for example: *** Begin Patch
*** Update File: file.txt
@@
-old
+new
*** End Patch. For updates, prefix unchanged context lines with a space; read the current file first. Matches are exact and ambiguous hunks fail (no fuzzy matching); on mismatch read again and rebuild the patch. Supports Add/Update/Delete/Move; staged, backup/rollback-protected; Add/Move targets must be absent; conflicts and symlinks rejected.

Default list: yes.

Prompt syntax: `{patchText}  # non-empty string in OpenCode patch format, NOT ---/+++ unified diff. Exact Update example: *** Begin Patch
*** Update File: file.txt
@@
-old
+new
*** End Patch. For updates, prefix unchanged context lines with a space; read the current file first. Matches are exact; ambiguous hunks fail and there is no fuzzy matching. On mismatch re-read and rebuild the patch. Supports Add/Update/Delete/Move; Add/Move targets must be absent; conflicts and symlinks rejected; staged backup/rollback-protected`

```json
{
  "type": "object",
  "properties": {
    "patchText": {
      "type": "string",
      "minLength": 1
    }
  },
  "required": [
    "patchText"
  ],
  "additionalProperties": false
}
```

## web_connect

Fetch one HTTP(S) URL with GET. Args: required 'url'; optional mode 'auto', 'raw', or 'text'.

Default list: yes.

Prompt syntax: `{url, mode?}  # GET one HTTP(S) URL; mode is auto, raw, or text`

```json
{
  "type": "object",
  "properties": {
    "url": {
      "type": "string",
      "minLength": 1
    },
    "mode": {
      "type": "string",
      "enum": [
        "auto",
        "raw",
        "text"
      ]
    }
  },
  "required": [
    "url"
  ],
  "additionalProperties": false
}
```
