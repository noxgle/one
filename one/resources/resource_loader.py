from __future__ import annotations

import os
import platform
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Skill constants and validation helpers (pi-compatible contract).
# ---------------------------------------------------------------------------

_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_SKILL_NAME_MIN = 1
_SKILL_NAME_MAX = 64
_SKILL_DESC_MAX = 1024


def _validate_skill_name(name: str) -> str | None:
    """Return *None* if the name is valid, or an error message."""
    if not name:
        return "name is empty"
    if len(name) < _SKILL_NAME_MIN or len(name) > _SKILL_NAME_MAX:
        return f"name length {len(name)} not in [{_SKILL_NAME_MIN}, {_SKILL_NAME_MAX}]"
    if not _SKILL_NAME_RE.match(name):
        return "name must contain only lowercase letters, digits, and single hyphens (no edge hyphens)"
    if name.startswith("-") or name.endswith("-"):
        return "name must not begin or end with a hyphen"
    return None


def _parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from SKILL.md content.

    Returns (frontmatter_dict, body_without_frontmatter).
    If no frontmatter is found, returns ({}, full_content).
    """
    body = raw
    fm: dict[str, Any] = {}
    stripped = raw.strip()
    if stripped.startswith("---"):
        # Find closing --- on its own line (start of string or after newline).
        sep = _find_closing_dash_dash_dash(stripped)
        if sep is not None:
            yaml_block = stripped[3:sep].strip()
            try:
                fm = yaml.safe_load(yaml_block) or {}
            except yaml.YAMLError:
                fm = {}
            body = stripped[sep + 3:].lstrip("\n")
    return fm, body


def _find_closing_dash_dash_dash(text: str) -> int | None:
    """Return the index of the closing ``---`` delimiter.

    The closing ``---`` must appear at the start of a line (preceded by
    ``\\n``) and must NOT be followed by an alphanumeric character — this
    rejects ``---more`` as a valid delimiter while still matching ``---\\n``
    and ``--- `` (trailing whitespace).

    Returns *None* when no valid closing delimiter exists.
    """
    idx = 3
    while True:
        pos = text.find("---", idx)
        if pos == -1:
            return None
        # Must start a new line.
        if text[pos - 1] != "\n":
            idx = pos + 3
            continue
        # Must NOT be followed by an alphanumeric character (rejects "---more").
        end = pos + 3
        if end < len(text) and text[end].isalnum():
            idx = end
            continue
        return pos
    return None  # unreachable, but satisfies mypy


def _validate_skill_fm(
    fm: dict[str, Any], base_dir: str
) -> tuple[dict[str, Any], list[str]]:
    """Parse and validate a skill frontmatter dict.

    Returns (validated_skill_dict, diagnostics_list).
    If the skill is invalid, the dict will have ``valid=False`` and diagnostics
    will explain the problem.
    """
    diagnostics: list[str] = []

    name = fm.get("name")
    if not isinstance(name, str):
        diagnostics.append(f"{base_dir}: missing or invalid 'name'")
        return {"name": "unknown", "valid": False, "baseDir": base_dir, "description": "", "diagnostics": diagnostics}, diagnostics

    name = name.strip()
    name_err = _validate_skill_name(name)
    if name_err:
        diagnostics.append(f"{base_dir}: invalid name '{name}' — {name_err}")
        return {"name": name, "valid": False, "baseDir": base_dir, "description": "", "diagnostics": diagnostics}, diagnostics

    description = fm.get("description")
    if not isinstance(description, str):
        diagnostics.append(f"{base_dir}: missing or non-string 'description'")
        return {"name": name, "valid": False, "baseDir": base_dir, "description": "", "diagnostics": diagnostics}, diagnostics

    description = description.strip()
    if not description:
        diagnostics.append(f"{base_dir}: description is empty")
        return {"name": name, "valid": False, "baseDir": base_dir, "description": "", "diagnostics": diagnostics}, diagnostics
    if len(description) > _SKILL_DESC_MAX:
        diagnostics.append(
            f"{base_dir}: description too long ({len(description)} > {_SKILL_DESC_MAX} chars)"
        )
        return {"name": name, "valid": False, "baseDir": base_dir, "description": "", "diagnostics": diagnostics}, diagnostics

    # Build the validated skill dict — only known optional fields + metadata.
    skill: dict[str, Any] = {
        "name": name,
        "description": description,
        "valid": True,
        "baseDir": base_dir,
        "diagnostics": diagnostics,
    }

    # Optional fields — copy only if present and the right type.
    for key in ("license", "compatibility", "allowed-tools", "disable-model-invocation"):
        if key in fm:
            skill[key] = fm[key]

    # metadata → dict only
    meta = fm.get("metadata")
    if meta is not None:
        if isinstance(meta, dict):
            skill["metadata"] = meta
        else:
            skill["metadata"] = {"raw": str(meta)}

    return skill, diagnostics


def load_skill_body(file_path: str) -> dict[str, Any]:
    """Load the full SKILL.md body given a file path.

    Returns a dict with ``body`` (str, full content without frontmatter) and
    ``fm`` (parsed frontmatter), or ``{"error": str}`` if the file cannot be
    read.
    """
    p = Path(file_path)
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception as exc:
        return {"error": str(exc), "filePath": file_path, "valid": False}
    fm, body = _parse_frontmatter(raw)
    return {"body": body, "fm": fm, "filePath": file_path, "baseDir": str(p.parent), "valid": True}


def _current_date() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


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
        f"Current date: {_current_date()}\n"
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
    "evidence_read": "{evidenceId, offset?, maxChars?}  # retrieve a bounded chunk of prior durable tool evidence; use nextOffset to continue",
    "finish": "{summary, goal_success}  # end the task; summary is shown to the user",
    "plan": "{plan}  # the execution plan for the current task; visible to you on every step",
    "spawn_subagent": "{task, tasks?, model?, tools?}  # delegate a subtask to an isolated subagent (returns summary)",
    "ask_user": "{question, timeoutSec?}  # ask the human a question and wait for their answer",
    "apply_patch": "{patchText}  # unified diff patch (*** Begin Patch / *** End Patch envelope; Add/Update/Delete/Move; Add/Move targets must be absent; conflicts and symlinks rejected; staged backup/rollback-protected)",
    "read_image": "{path}  # read an image file and return its content as a base64-encoded blob with metadata",
}

# Injected via replace() (not str.format) because the text contains literal JSON braces.
_BASE_PROMPT = """You are an autonomous terminal agent. Solve the task via shell/file ops.

REASONING & ADAPTATION
- Perform internal reasoning BEFORE generating actions
- Base decisions strictly on observed outputs and current system state
- After each result, reassess assumptions
- If assumptions fail, adapt strategy
- Prefer observed evidence over initial expectations
- Do not claim changes or verification without a successful, observed tool result.
- Do NOT output reasoning

PLANNING RULES
- Use the plan tool to store the plan.
- Create a plan ONLY if no active plan exists and the task requires >2 steps or deep analysis.
- Deep analysis includes: log correlation, root cause investigation, audits, state comparison, hypothesis testing.
- Do NOT plan for single commands, simple reads, or stateless queries.
- Never create a new plan if one is already active.
- Maximum 1 plan creation per task.
- If a plan exists: continue execution within the existing plan; adapt inside the plan instead of creating a new one.
After creating a plan, do not call finish immediately. Execute the planned steps and verify the result first. A plan is not task completion.

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
        # Explicit skill paths always load, even when no_skills=True
        has_explicit_skills = bool(self.additional_skill_paths)
        if not self.no_skills or has_explicit_skills:
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
        """Discover, parse, validate and deduplicate skills.

        Deterministic order (agent_dir → ~/.agents → .one → explicit paths).
        First valid skill wins on duplicate names; subsequent dups produce
        diagnostics. Malformed / invalid-name / missing-description skills
        produce non-fatal diagnostics and are omitted from the active list.
        """
        roots = [
            Path(self.agent_dir) / "skills",
            Path.home() / ".agents" / "skills",
            Path(self.cwd) / ".one" / "skills",
            *[Path(p) for p in self.additional_skill_paths],
        ]
        files = self._collect_files(roots, "SKILL.md")

        all_valid: list[dict[str, Any]] = []
        all_diagnostics: list[str] = []
        seen_names: dict[str, str] = {}  # frontmatter name -> baseDir of first occurrence

        for f in files:
            p = Path(f)
            base_dir = str(p.parent)
            # Directory name is used as fallback, but frontmatter name takes precedence
            dir_name = p.parent.name

            # Read and parse frontmatter
            try:
                raw = p.read_text(encoding="utf-8")
            except Exception:
                all_diagnostics.append(f"{base_dir}: unreadable SKILL.md")
                continue

            fm, _body = _parse_frontmatter(raw)

            skill, diagnostics = _validate_skill_fm(fm, base_dir)
            all_diagnostics.extend(diagnostics)

            if not skill.get("valid", False):
                # Attach diagnostics to skill so they're returned by get_skills()
                skill["diagnostics"] = diagnostics
                skill["valid"] = False
                all_valid.append(skill)
                continue

            # Use frontmatter name (with dir_name fallback) for deduplication
            skill_name = skill.get("name", dir_name)

            # Dedup: first valid wins
            if skill_name in seen_names:
                dup_diag = f"duplicate skill name '{skill_name}': first at {seen_names[skill_name]}, skipping {base_dir}"
                all_diagnostics.append(dup_diag)
                # Add duplicate to all_valid with diagnostics so get_skills() can report it
                skill["diagnostics"] = [dup_diag]
                skill["valid"] = False
                all_valid.append(skill)
                continue

            seen_names[skill_name] = base_dir

            # Attach resources metadata (scan for scripts/, references/, assets/)
            resources: list[str] = []
            for res_dir in ("scripts", "references", "assets"):
                res_path = p.parent / res_dir
                if res_path.is_dir():
                    resources.append(res_dir)
            if resources:
                skill["resources"] = resources

            skill["filePath"] = f
            skill["source"] = "discovered"
            all_valid.append(skill)

        self._skills = all_valid
        return all_valid

    def get_skills(self) -> dict[str, Any]:
        """Return only valid, active skills with name+description.

        Full bodies are NOT included here — progressive disclosure: load on
        demand via ``get_skill``.
        """
        skills_out = []
        all_diagnostics: list[str] = []
        for s in self._skills:
            if s.get("valid", False):
                skills_out.append({
                    "name": s["name"],
                    "description": s["description"],
                    "baseDir": s["baseDir"],
                    "filePath": s.get("filePath", ""),
                })
            all_diagnostics.extend(s.get("diagnostics", []))
        # Deduplicate diagnostics
        seen_diag: set[str] = set()
        unique_diag: list[str] = []
        for d in all_diagnostics:
            if d not in seen_diag:
                seen_diag.add(d)
                unique_diag.append(d)
        return {"skills": skills_out, "diagnostics": unique_diag}

    def get_skill(self, name: str) -> dict[str, Any]:
        """Load the full body for a skill by name.

        Returns ``{"body": str, "fm": dict, "name": str, "baseDir": str, "filePath": str}``
        for valid skills, ``{"error": str, "invalid": True, "diagnostics": [...]}`` when a skill
        with that name exists but is invalid, or ``{"error": str}`` when no skill with that name
        exists at all.
        """
        for s in self._skills:
            if s.get("name") == name and s.get("valid", False):
                fp = s.get("filePath", "")
                if fp:
                    loaded = load_skill_body(fp)
                    loaded["name"] = name
                    return loaded
                return {"error": f"Skill '{name}' found but filePath missing"}
        # Check if a skill with this name exists but is invalid (not found → truly unknown).
        for s in self._skills:
            if s.get("name") == name:
                diagnostics = s.get("diagnostics", ["invalid frontmatter or missing fields"])
                return {
                    "error": f"Skill '{name}' found but is invalid",
                    "invalid": True,
                    "diagnostics": diagnostics,
                }
        return {"error": f"Unknown skill: {name}"}

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
            content = p.read_text(encoding="utf-8")
            # Extract a short description: first non-empty line after the title,
            # or the title itself, truncated to 120 chars.
            description = ""
            lines = [l.strip() for l in content.splitlines() if l.strip()]
            if lines:
                # Skip the first line if it looks like a title (starts with #).
                first = lines[0].lstrip("# ").strip()
                description = first if first else lines[0][:120]
            out.append({"name": p.stem, "description": description, "source": f, "content": content})
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
                    prompt += (
                        "Available skills (metadata only — full skill loaded on demand via `/skill:<name>`):\n"
                    )
                    for skill in skills:
                        name = skill.get("name") or "unknown"
                        desc = skill.get("description", "")
                        prompt += f"- **{name}**: {desc} (filePath: {skill.get('filePath', '')})\n"
                    prompt += (
                        "\n"
                        "**Skill invocation syntax — read carefully:**\n"
                        "- `/skill:<name> [args]` is a **TUI/interactive UI command** typed by the user. "
                        "It is NOT a tool call and is NOT a filesystem path for the `read` tool.\n"
                        "- The autonomous model MUST NEVER pass `/skill:<name>` or `skill:<name>` as the `path` "
                        "argument to the `read` tool.\n"
                        "- When the model decides to use a skill, it must call `read` with the **exact `filePath`** "
                        "shown in the skill metadata above (e.g. `/home/user/.config/one/skills/example-skill/SKILL.md`).\n"
                        "- Resource paths inside a skill are relative to that skill's base directory.\n"
                        "- If the model accidentally passes `/skill:<name>` or `skill:<name>` as a `read` path, "
                        "the agent will return a diagnostic explaining the syntax and providing the correct filePath.\n"
                        "- This rule preserves the existing progressive-disclosure flow: skills are listed in metadata "
                        "only, and full bodies are loaded on demand via the explicit `filePath`.\n"
                    )

        if self.append_system_prompt:
            prompt = f"{prompt}\n\n{self.append_system_prompt}"

        return prompt
