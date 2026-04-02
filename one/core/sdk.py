from __future__ import annotations

from typing import Any

from one.config import get_agent_dir
from one.core.agent_session_runtime import AgentSessionRuntimeHost, create_agent_session_runtime
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.resources.resource_loader import DefaultResourceLoader


async def create_agent_session(options: dict[str, Any] | None = None) -> dict[str, Any]:
    options = options or {}
    cwd = options.get("cwd") or __import__("os").getcwd()
    agent_dir = options.get("agentDir") or get_agent_dir()

    auth_storage = options.get("authStorage") or AuthStorage.create()
    model_registry = options.get("modelRegistry") or ModelRegistry.create(auth_storage)
    settings_manager = options.get("settingsManager") or SettingsManager.create(cwd, agent_dir)
    resource_loader = options.get("resourceLoader") or DefaultResourceLoader(
        cwd=cwd,
        agent_dir=agent_dir,
        settings_manager=settings_manager,
    )
    await resource_loader.reload()

    session_manager = options.get("sessionManager") or SessionManager.create(cwd)

    bootstrap = {
        "agentDir": agent_dir,
        "authStorage": auth_storage,
        "modelRegistry": model_registry,
        "settingsManager": settings_manager,
        "resourceLoader": resource_loader,
        "model": options.get("model"),
        "thinkingLevel": options.get("thinkingLevel"),
        "scopedModels": options.get("scopedModels"),
        "tools": options.get("tools"),
    }

    runtime = await create_agent_session_runtime(
        bootstrap,
        {
            "cwd": session_manager.cwd,
            "sessionManager": session_manager,
            "resourceLoader": resource_loader,
        },
    )
    runtime_host = AgentSessionRuntimeHost(bootstrap, runtime)

    return {
        "session": runtime.session,
        "extensionsResult": resource_loader.get_extensions(),
        "modelFallbackMessage": runtime.model_fallback_message,
        "runtimeHost": runtime_host,
    }
