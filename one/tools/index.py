from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .apply_patch import apply_patch_tool
from .ask_user import ask_user_tool
from .bash import bash_tool
from .edit import edit_tool
from .find import find_tool
from .finish import finish_tool
from .grep import grep_tool
from .ls import ls_tool
from .plan import plan_tool
from .read import read_tool
from .read_image import read_image_tool
from .spawn_subagent import spawn_subagent_tool
from .write import write_tool

ToolFunc = Callable[..., Any]


class ToolDef:
    def __init__(self, name: str, description: str, fn: ToolFunc, schema: dict[str, Any] | None = None) -> None:
        self.name = name
        self.description = description
        self.fn = fn
        self.schema = schema or {"type": "object", "additionalProperties": False}


def native_tool_definitions(names: list[str]) -> list[dict[str, Any]]:
    """Return strict provider-neutral JSON Schema definitions for built-ins."""
    return [
        {"name": tool.name, "description": tool.description, "parameters": tool.schema}
        for name in names if (tool := all_tools.get(name)) is not None
    ]


DEFAULT_TOOL_NAMES: list[str] = [
    "read", "read_image", "bash", "edit", "write", "grep", "find", "ls", "evidence_read", "finish", "plan", "spawn_subagent", "ask_user", "apply_patch",
]

all_tools: dict[str, ToolDef] = {
    "read": ToolDef("read", "Read file contents", read_tool, {"type":"object","properties":{"path":{"type":"string"},"offset":{"type":"integer"},"limit":{"type":"integer"}},"required":["path"],"additionalProperties":False}),
    "read_image": ToolDef(
        "read_image",
        "Load a local PNG/JPEG/WebP image for vision inspection. Args: 'path' (absolute or workspace-relative). Returns metadata; the image is sent to the vision model on the next step.",
        read_image_tool,
        {"type": "object", "properties": {"path": {"type": "string", "minLength": 1}}, "required": ["path"], "additionalProperties": False},
    ),
    "bash": ToolDef("bash", "Execute bash commands", bash_tool, {"type":"object","properties":{"command":{"type":"string","minLength":1},"timeout":{"type":"integer","minimum":1}},"required":["command"],"additionalProperties":False}),
    "edit": ToolDef("edit", "Edit files with exact replacement", edit_tool, {"type":"object","properties":{"path":{"type":"string","minLength":1},"edits":{"type":"array","minItems":1,"items":{"type":"object","properties":{"oldString":{"type":"string"},"newString":{"type":"string"},"oldText":{"type":"string"},"newText":{"type":"string"}},"additionalProperties":False,"anyOf":[{"required":["oldString","newString"]},{"required":["oldText","newText"]}]}}},"required":["path","edits"],"additionalProperties":False}),
    "write": ToolDef("write", "Write files", write_tool, {"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"],"additionalProperties":False}),
    "grep": ToolDef("grep", "Search file contents", grep_tool, {"type":"object","properties":{"pattern":{"type":"string"},"path":{"type":"string"}},"required":["pattern"],"additionalProperties":False}),
    "find": ToolDef("find", "Find files by pattern", find_tool, {"type":"object","properties":{"pattern":{"type":"string"},"path":{"type":"string"}},"additionalProperties":False}),
    "ls": ToolDef("ls", "List directory contents", ls_tool, {"type":"object","properties":{"path":{"type":"string"}},"additionalProperties":False}),
    # Dispatched by AgentSession because evidence is scoped to the active session.
    "evidence_read": ToolDef("evidence_read", "Read a bounded chunk of durable tool evidence by evidenceId; never re-runs the original tool", lambda: {}, {"type":"object","properties":{"evidenceId":{"type":"string"},"offset":{"type":"integer"},"maxChars":{"type":"integer"}},"required":["evidenceId"],"additionalProperties":False}),
    "finish": ToolDef("finish", "End the task with a summary and success flag", finish_tool, {"type":"object","properties":{"summary":{"type":"string"},"goal_success":{"type":"boolean"}},"required":["summary","goal_success"],"additionalProperties":False}),
    "plan": ToolDef("plan", "Store an execution plan for the current task; visible on every step", plan_tool, {"type":"object","properties":{"plan":{"type":"array","items":{"type":"object"}}},"required":["plan"],"additionalProperties":False}),
    "ask_user": ToolDef(
        "ask_user",
        "Ask the human a question and wait for their answer. Args: 'question' (str, required); optional 'timeoutSec' (int). Use only when you genuinely need human input (ambiguity, missing access, policy decision).",
        ask_user_tool, {"type":"object","properties":{"question":{"type":"string"},"timeoutSec":{"type":"integer"}},"required":["question"],"additionalProperties":False},
    ),
    "spawn_subagent": ToolDef(
        "spawn_subagent",
        "Delegate a subtask to an isolated subagent. Args: 'task' (str) for one subtask, or 'tasks' (list[str]) for parallel subtasks; optional 'model' ('provider/model'); optional 'tools' (non-empty list of tool names; 'finish' is added automatically if omitted). Returns the subagent summary, success flag and session id.",
        spawn_subagent_tool, {"type":"object","properties":{"task":{"type":"string","minLength":1},"tasks":{"type":"array","minItems":1,"items":{"type":"string","minLength":1}},"model":{"type":"string","minLength":1},"tools":{"type":"array","minItems":1,"items":{"type":"string","minLength":1}}},"additionalProperties":False,"oneOf":[{"required":["task"]},{"required":["tasks"]}]},
    ),
    "apply_patch": ToolDef("apply_patch", "Apply an OpenCode patch-format patch, NOT a standard ---/+++ unified diff. patchText must be a non-empty string, for example: *** Begin Patch\n*** Update File: file.txt\n@@\n-old\n+new\n*** End Patch. Supports Add/Update/Delete/Move; staged, backup/rollback-protected; Add/Move targets must be absent; conflicts and symlinks rejected.", apply_patch_tool, {"type":"object","properties":{"patchText":{"type":"string","minLength":1}},"required":["patchText"],"additionalProperties":False}),
}

coding_tools = [all_tools["read"], all_tools["read_image"], all_tools["bash"], all_tools["edit"], all_tools["write"], all_tools["apply_patch"]]
read_only_tools = [all_tools["read"], all_tools["read_image"], all_tools["grep"], all_tools["find"], all_tools["ls"]]
