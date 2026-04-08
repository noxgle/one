from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any


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
            prompt = self.system_prompt
        else:
            has_bash = "bash" in tools
            has_read = "read" in tools
            has_grep = "grep" in tools
            has_find = "find" in tools
            has_ls = "ls" in tools
            if has_bash and (has_grep or has_find or has_ls):
                file_ops_guideline = "Prefer grep/find/ls tools over bash for file exploration."
            elif has_bash:
                file_ops_guideline = "Use bash for file operations when dedicated tools are unavailable."
            else:
                file_ops_guideline = "Use available read/search tools for file exploration."

            tools_list = "\n".join(f"- {t}" for t in tools) if tools else "(none)"
            prompt = (
                "You are an expert coding assistant operating inside one, a terminal coding agent harness.\n"
                "You help users by reading files, executing commands, editing code, and writing new files.\n\n"
                f"Available tools:\n{tools_list}\n\n"
                "Guidelines:\n"
                f"- {file_ops_guideline}\n"
                "- Be concise in responses.\n"
                "- Show file paths clearly when changing or discussing files.\n"
                "- Use tools when needed instead of claiming no access.\n"
                "- For tool calls, return only valid JSON {\"tool\":\"...\",\"args\":{...}}.\n"
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

            if has_read:
                skills = self.get_skills().get("skills", [])
                if skills:
                    prompt += "\n# Skills\n\n"
                    for skill in skills:
                        name = skill.get("name") or "unknown-skill"
                        path = skill.get("filePath") or ""
                        prompt += f"- {name}: {path}\n"

        if self.append_system_prompt:
            prompt = f"{prompt}\n\n{self.append_system_prompt}"

        prompt += f"\nCurrent date: {date.today().isoformat()}"
        prompt += f"\nCurrent working directory: {self.cwd.replace('\\\\', '/')}"
        return prompt
