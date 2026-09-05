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
from .spawn_subagent import spawn_subagent_tool
from .write import write_tool

ToolFunc = Callable[..., Any]


class ToolDef:
    def __init__(self, name: str, description: str, fn: ToolFunc) -> None:
        self.name = name
        self.description = description
        self.fn = fn


DEFAULT_TOOL_NAMES: list[str] = [
    "read", "bash", "edit", "write", "grep", "find", "ls", "finish", "plan", "spawn_subagent", "ask_user", "apply_patch",
]

all_tools: dict[str, ToolDef] = {
    "read": ToolDef("read", "Read file contents", read_tool),
    "bash": ToolDef("bash", "Execute bash commands", bash_tool),
    "edit": ToolDef("edit", "Edit files with exact replacement", edit_tool),
    "write": ToolDef("write", "Write files", write_tool),
    "grep": ToolDef("grep", "Search file contents", grep_tool),
    "find": ToolDef("find", "Find files by pattern", find_tool),
    "ls": ToolDef("ls", "List directory contents", ls_tool),
    "finish": ToolDef("finish", "End the task with a summary and success flag", finish_tool),
    "plan": ToolDef("plan", "Store an execution plan for the current task; visible on every step", plan_tool),
    "ask_user": ToolDef(
        "ask_user",
        "Ask the human a question and wait for their answer. Args: 'question' (str, required); optional 'timeoutSec' (int). Use only when you genuinely need human input (ambiguity, missing access, policy decision).",
        ask_user_tool,
    ),
    "spawn_subagent": ToolDef(
        "spawn_subagent",
        "Delegate a subtask to an isolated subagent. Args: 'task' (str) for one subtask, or 'tasks' (list[str]) for parallel subtasks; optional 'model' ('provider/model'); optional 'tools' (list of tool names). Returns the subagent summary, success flag and session id.",
        spawn_subagent_tool,
    ),
    "apply_patch": ToolDef("apply_patch", "Apply a unified-diff patch to files (opencode format: *** Begin Patch / *** End Patch; Add/Update/Delete/Move; staged, backup/rollback-protected; Add/Move targets must be absent; conflicts and symlinks rejected)", apply_patch_tool),
}

coding_tools = [all_tools["read"], all_tools["bash"], all_tools["edit"], all_tools["write"], all_tools["apply_patch"]]
read_only_tools = [all_tools["read"], all_tools["grep"], all_tools["find"], all_tools["ls"]]
