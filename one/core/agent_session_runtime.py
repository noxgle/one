from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from one.config import get_agent_dir
from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager, get_default_session_dir
from one.core.settings_manager import SettingsManager
from one.resources.resource_loader import DefaultResourceLoader
from one.tools.index import coding_tools


@dataclass
class AgentSessionRuntime:
    session: AgentSession
    model_fallback_message: str | None
    cwd: str
    session_manager: SessionManager
    resource_loader: DefaultResourceLoader


async def create_agent_session_runtime(bootstrap: dict[str, Any], options: dict[str, Any]) -> AgentSessionRuntime:
    cwd = options.get("cwd")
    agent_dir = bootstrap.get("agentDir") or get_agent_dir()
    auth_storage = bootstrap.get("authStorage") or AuthStorage.create()
    model_registry = bootstrap.get("modelRegistry") or ModelRegistry.create(auth_storage)
    settings_manager = bootstrap.get("settingsManager") or SettingsManager.create(cwd, agent_dir)
    resource_loader = options.get("resourceLoader") or bootstrap.get("resourceLoader") or DefaultResourceLoader(
        cwd=cwd,
        agent_dir=agent_dir,
        settings_manager=settings_manager,
        additional_extension_paths=bootstrap.get("resourceLoaderOptions", {}).get("additionalExtensionPaths"),
        additional_skill_paths=bootstrap.get("resourceLoaderOptions", {}).get("additionalSkillPaths"),
        additional_prompt_template_paths=bootstrap.get("resourceLoaderOptions", {}).get("additionalPromptTemplatePaths"),
        additional_theme_paths=bootstrap.get("resourceLoaderOptions", {}).get("additionalThemePaths"),
        no_extensions=bootstrap.get("resourceLoaderOptions", {}).get("noExtensions", False),
        no_skills=bootstrap.get("resourceLoaderOptions", {}).get("noSkills", False),
        no_prompt_templates=bootstrap.get("resourceLoaderOptions", {}).get("noPromptTemplates", False),
        no_themes=bootstrap.get("resourceLoaderOptions", {}).get("noThemes", False),
        system_prompt=bootstrap.get("resourceLoaderOptions", {}).get("systemPrompt"),
        append_system_prompt=bootstrap.get("resourceLoaderOptions", {}).get("appendSystemPrompt"),
    )
    await resource_loader.reload()

    session_manager = options.get("sessionManager") or SessionManager.create(
        cwd,
        settings_manager.get_session_dir() or get_default_session_dir(cwd, agent_dir),
    )

    model = bootstrap.get("model")
    if not model:
        avail = model_registry.get_available()
        if avail:
            default_provider = settings_manager.get_default_provider()
            default_model = settings_manager.get_default_model()
            model = next((m for m in avail if m.provider == default_provider and m.id == default_model), None) or avail[0]

    if not model:
        all_models = model_registry.all()
        model = all_models[0] if all_models else None

    model_fallback_message = None
    if model and not model_registry.has_configured_auth(model):
        model_fallback_message = f"No auth configured for {model.provider}/{model.id}"

    thinking_level = bootstrap.get("thinkingLevel") or settings_manager.get_default_thinking_level()
    if model and not model.reasoning:
        thinking_level = "off"

    tools = bootstrap.get("tools")
    if tools:
        tool_names = [t.name if hasattr(t, "name") else str(t) for t in tools]
    else:
        tool_names = ["read", "bash", "edit", "write", "grep", "find", "ls", "finish"]

    session = AgentSession(
        session_manager=session_manager,
        settings_manager=settings_manager,
        model_registry=model_registry,
        resource_loader=resource_loader,
        model=model,
        thinking_level=thinking_level,
        scoped_models=bootstrap.get("scopedModels") or [],
        tools=tool_names,
    )
    await session.bind_extensions()

    return AgentSessionRuntime(
        session=session,
        model_fallback_message=model_fallback_message,
        cwd=cwd,
        session_manager=session_manager,
        resource_loader=resource_loader,
    )


class AgentSessionRuntimeHost:
    def __init__(self, bootstrap: dict[str, Any], runtime: AgentSessionRuntime) -> None:
        self._bootstrap = bootstrap
        self._runtime = runtime

    @property
    def session(self) -> AgentSession:
        return self._runtime.session

    async def _replace(self, session_manager: SessionManager, cwd: str | None = None) -> dict[str, Any]:
        runtime = await create_agent_session_runtime(
            self._bootstrap,
            {
                "cwd": cwd or session_manager.cwd,
                "sessionManager": session_manager,
                "resourceLoader": self._runtime.resource_loader,
            },
        )
        self._runtime = runtime
        return {"cancelled": False}

    async def new_session(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options or {}
        cwd = self._runtime.session_manager.cwd
        session_dir = self._runtime.session_manager.session_dir
        manager = SessionManager.create(cwd, session_dir)
        if options.get("parentSession"):
            manager.get_header()["parentSession"] = options["parentSession"]
            manager._rewrite()  # noqa: SLF001
        return await self._replace(manager)

    async def switch_session(self, session_path: str) -> dict[str, Any]:
        manager = SessionManager.open(session_path, self._runtime.session_manager.session_dir)
        return await self._replace(manager, cwd=manager.cwd)

    async def fork(self, entry_id: str) -> dict[str, Any]:
        current = self._runtime.session_manager
        old_text = None
        target = current.get_entry(entry_id)
        if target and target.get("type") == "message" and target.get("message", {}).get("role") == "user":
            content = target.get("message", {}).get("content", "")
            if isinstance(content, list):
                old_text = "".join(x.get("text", "") for x in content if x.get("type") == "text")
            else:
                old_text = str(content)
        new_path = current.create_branched_session(entry_id)
        if new_path:
            manager = SessionManager.open(new_path, current.session_dir)
            await self._replace(manager, cwd=manager.cwd)
        return {"cancelled": False, "selectedText": old_text}

    async def import_from_jsonl(self, path: str) -> dict[str, Any]:
        manager = SessionManager.open(path, self._runtime.session_manager.session_dir)
        return await self._replace(manager, cwd=manager.cwd)

    async def dispose(self) -> None:
        await self._runtime.session.dispose()
