from __future__ import annotations

import os
import platform
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


def _current_time() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _system_env() -> str:
    """Human-readable OS description, e.g. \"Ubuntu 24.04\" or \"Linux 6.8.0\"."""
    try:
        info = platform.freedesktop_os_release()
        name = info.get("NAME") or platform.system() or "Linux"
        version = info.get("VERSION_ID") or platform.release() or ""
        return f"{name} {version}".strip()
    except Exception:
        return f"{platform.system()} {platform.release()}".strip()


def _user_privileges() -> str:
    """Detect user privilege level: root, sudo with/without password, or plain user."""
    try:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return "root"
    except Exception:
        pass
    try:
        # Test passwordless sudo
        result = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0:
            return "user(sudo nopasswd)"
        # Check if user is in sudo/wheel group (sudo available, needs password)
        result = subprocess.run(
            ["groups"], capture_output=True, text=True, timeout=5,
        )
        groups = result.stdout.strip().split()
        if "sudo" in groups or "wheel" in groups:
            return "user(sudo)"
    except Exception:
        pass
    return "user"


def _build_header(cwd: str) -> str:
    return (
        f"Current time: {_current_time()}\n"
        f"workspace={cwd.replace(chr(92), '/')}\n"
        f"env={_system_env()}\n"
        f"user_privileges={_user_privileges()}\n"
    )


TOOL_ARG_SCHEMAS: dict[str, str] = {
    "read": "{path, offset?, limit?}",
    "bash": "{command, timeout?}  # timeout in seconds; default if omitted",
    "edit": "{path, edits: [{oldString, newString}]}  # path is TOP-LEVEL (never inside edits); oldString must be unique in the file",
    "write": "{path, content}",
    "grep": "{pattern, path?}",
    "find": "{pattern?, path?}",
    "ls": "{path?}",
    "finish": "{summary, goal_success}  # end the task; summary is shown to the user",
    "plan": "{plan}  # the execution plan for the current task; visible to you on every step",
    "spawn_subagent": "{task, tasks?, model?, tools?}  # delegate a subtask to an isolated subagent (returns summary)",
    "ask_user": "{question, timeoutSec?}  # ask the human a question and wait for their answer",
    "apply_patch": "{patchText}  # unified diff patch (*** Begin Patch / *** End Patch envelope; Add/Update/Delete/Move)",
}

# Injected via replace() (not str.format) because the text contains literal JSON braces.
_BASE_PROMPT = """You are an autonomous terminal agent. Solve the task via shell/file ops.

REASONING & ADAPTATION
- Perform internal reasoning BEFORE generating actions
- Base decisions strictly on observed outputs and current system state
- After each result, reassess assumptions
- If assumptions fail, adapt strategy
- Prefer observed evidence over initial expectations
- Do NOT output reasoning

PLANNING RULES
- Use the plan tool to store the plan.
- Create a plan ONLY if no active plan exists and the task requires >2 steps or deep analysis.
- Deep analysis includes: log correlation, root cause investigation, audits, state comparison, hypothesis testing.
- Do NOT plan for single commands, simple reads, or stateless queries.
- Never create a new plan if one is already active.
- Maximum 1 plan creation per task.
- If a plan exists: continue execution within the existing plan; adapt inside the plan instead of creating a new one.

ACTION STRATEGY
- Decide FIRST whether any tool is needed. Greetings, questions, small talk and stateless queries need NO tool.
- If no tool is needed, answer directly: {"tool":"finish","args":{"summary":"<your answer>","goal_success":true}}.
- Return exactly ONE tool call per response; the harness loops until the task is done.
- If the task needs multiple steps, keep returning the next step in the following response.
- If uncertainty exists, prefer the simplest next step.
- Execution order = order of your responses.
- Stop immediately once the answer is delivered. Do not invent extra steps.

EXECUTION FLOW
- Maximum 15 total actions per task.
- If 3 consecutive steps show no progress, change strategy.
- Call 'finish' the moment the objective is reached or the answer is delivered. Never add extra steps after success.
- Never use bash to echo chat/greeting text; respond through finish.summary.

TOOLS (JSON only, double quotes):
__TOOLS__

ERROR HANDLING
- After bash execution check exit_code: 0 -> success; !=0 -> retry (max 2, modified command), fix, skip, or fail.
- Never retry identical failing commands.
- If multiple strategies fail, stop.

IDEMPOTENCY
- Check before modifying files or installing packages.
- Avoid duplicate operations.
- Ensure retries do not create inconsistent state.

RESOURCE CONTROL
- Default timeout __DEFAULT_TOOL_TIMEOUT__s if not specified.
- Avoid recursive filesystem scans unless required.
- Avoid unbounded output.
- No background daemons or infinite loops.

CONSTRAINTS
- Each command runs in an isolated shell (no persistent cd).
- No interactive tools (nano, vim, top, etc.).
- Autonomous mode: do not ask the user.

CONTEXT OPTIMIZATION
- If input data is large, use read/grep to distill it BEFORE further steps.
- Never pass raw large outputs directly to next steps.
- Prefer distilled summaries over full logs.

RESPONSE FORMAT (STRICT JSON ONLY)
Return ONLY JSON. No prose. No explanations.
Return exactly ONE dict per response:
1) No tool needed (greeting, question, task done): {"tool":"finish","args":{"summary":"<answer text>","goal_success":true}}
2) Tool needed: {"tool":"<name>","args":{...}}
RULES:
- Each response must be a single valid tool call.
- No extra fields.
- No text outside JSON.
"""


class DefaultResourceLoader:
    def __init__(
        self,
        cwd: str,
        agent_dir: str,
        settings_manager: Any,
        additional_extension_paths: list[str] | None = None,
        additional_skill_paths: list[str] | None = None,
        additional_prompt_template_paths: list[str] | None = None,
        additional_theme_paths: list[str] | None = None,
        no_extensions: bool = False,
        no_skills: bool = False,
        no_prompt_templates: bool = False,
        no_themes: bool = False,
        system_prompt: str | None = None,
        append_system_prompt: str | None = None,
    ) -> None:
        self.cwd = cwd
        self.agent_dir = agent_dir
        self.settings_manager = settings_manager
        self.additional_extension_paths = additional_extension_paths or []
        self.additional_skill_paths = additional_skill_paths or []
        self.additional_prompt_template_paths = additional_prompt_template_paths or []
        self.additional_theme_paths = additional_theme_paths or []
        self.no_extensions = no_extensions
        self.no_skills = no_skills
        self.no_prompt_templates = no_prompt_templates
        self.no_themes = no_themes
        self.system_prompt = system_prompt
        self.append_system_prompt = append_system_prompt

        self._extensions: list[dict[str, Any]] = []
        self._skills: list[dict[str, Any]] = []
        self._prompts: list[dict[str, Any]] = []
        self._themes: list[dict[str, Any]] = []
        self._agents_files: list[dict[str, Any]] = []

    async def reload(self) -> None:
        self._extensions = []
        self._skills = []
        self._prompts = []
        self._themes = []
        self._agents_files = []

        if not self.no_extensions:
            self._extensions = self._discover_extensions()
        if not self.no_skills:
            self._skills = self._discover_skills()
        if not self.no_prompt_templates:
            self._prompts = self._discover_prompts()
        if not self.no_themes:
            self._themes = self._discover_themes()
        self._agents_files = self._discover_agents_files()

    def _collect_files(self, roots: list[Path], suffix: str) -> list[str]:
        out: list[str] = []
        for root in roots:
            if root.is_file() and root.name.endswith(suffix):
                out.append(str(root.resolve()))
            elif root.is_dir():
                out.extend(str(p.resolve()) for p in root.rglob(f"*{suffix}") if p.is_file())
        return sorted(set(out))

    def _discover_extensions(self) -> list[dict[str, Any]]:
        roots = [
            Path(self.agent_dir) / "extensions",
            Path(self.cwd) / ".one" / "extensions",
            *[Path(p) for p in self.additional_extension_paths],
        ]
        files = self._collect_files(roots, ".py")
        return [{"path": f} for f in files]

    def _discover_skills(self) -> list[dict[str, Any]]:
        roots = [
            Path(self.agent_dir) / "skills",
            Path.home() / ".agents" / "skills",
            Path(self.cwd) / ".one" / "skills",
            *[Path(p) for p in self.additional_skill_paths],
        ]
        files = self._collect_files(roots, "SKILL.md")
        out = []
        for f in files:
            p = Path(f)
            out.append({"name": p.parent.name, "filePath": f, "baseDir": str(p.parent), "source": "discovered"})
        return out

    def _discover_prompts(self) -> list[dict[str, Any]]:
        roots = [
            Path(self.agent_dir) / "prompts",
            Path(self.cwd) / ".one" / "prompts",
            *[Path(p) for p in self.additional_prompt_template_paths],
        ]
        files = self._collect_files(roots, ".md")
        out = []
        for f in files:
            p = Path(f)
            out.append({"name": p.stem, "description": "", "source": f, "content": p.read_text(encoding="utf-8")})
        return out

    def _discover_themes(self) -> list[dict[str, Any]]:
        roots = [
            Path(self.agent_dir) / "themes",
            Path(self.cwd) / ".one" / "themes",
            *[Path(p) for p in self.additional_theme_paths],
        ]
        files = self._collect_files(roots, ".json")
        return [{"path": f} for f in files]

    def _discover_agents_files(self) -> list[dict[str, Any]]:
        out = []
        cur = Path(self.cwd)
        while True:
            p = cur / "AGENTS.md"
            if p.exists():
                out.append({"path": str(p), "content": p.read_text(encoding="utf-8")})
            if cur.parent == cur:
                break
            cur = cur.parent
        global_agents = Path(self.agent_dir) / "AGENTS.md"
        if global_agents.exists():
            out.append({"path": str(global_agents), "content": global_agents.read_text(encoding="utf-8")})
        return out

    def get_extensions(self) -> dict[str, Any]:
        return {"extensions": self._extensions, "errors": [], "runtime": {"pendingProviderRegistrations": []}}

    def get_skills(self) -> dict[str, Any]:
        return {"skills": self._skills, "diagnostics": []}

    def get_prompts(self) -> dict[str, Any]:
        return {"prompts": self._prompts, "diagnostics": []}

    def get_themes(self) -> dict[str, Any]:
        return {"themes": self._themes, "diagnostics": []}

    def get_agents_files(self) -> dict[str, Any]:
        return {"agentsFiles": self._agents_files}

    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:
        tools = selected_tools or ["read", "bash", "edit", "write"]

        if self.system_prompt:
            prompt = f"{_build_header(self.cwd)}\n\n{self.system_prompt}"
        else:
            tools_list = "\n".join(f"- {t} {TOOL_ARG_SCHEMAS.get(t, '{}')}" for t in tools) if tools else "(none)"
            default_timeout = getattr(self.settings_manager, "get_tool_timeout_sec", lambda: 30)()
            prompt = _build_header(self.cwd) + _BASE_PROMPT.replace("__TOOLS__", tools_list).replace(
                "__DEFAULT_TOOL_TIMEOUT__", str(default_timeout)
            )

            agents_files = self.get_agents_files().get("agentsFiles", [])
            if agents_files:
                prompt += "\n# Project Context\n\n"
                for entry in agents_files:
                    p = entry.get("path", "")
                    c = (entry.get("content", "") or "").strip()
                    if not c:
                        continue
                    prompt += f"## {p}\n\n{c}\n\n"

            if "read" in tools:
                skills = self.get_skills().get("skills", [])
                if skills:
                    prompt += "\n# Skills\n\n"
                    for skill in skills:
                        name = skill.get("name") or "unknown-skill"
                        path = skill.get("filePath") or ""
                        prompt += f"- {name}: {path}\n"

        if self.append_system_prompt:
            prompt = f"{prompt}\n\n{self.append_system_prompt}"

        return prompt
