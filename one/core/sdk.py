from __future__ import annotations

from pathlib import Path
from typing import Any

from one.config import get_agent_dir
from one.core.agent_session_runtime import AgentSessionRuntimeHost, create_agent_session_runtime
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager, get_default_session_dir
from one.core.settings_manager import SettingsManager
from one.resources.resource_loader import DefaultResourceLoader


def _resolve_agent_dir(options: dict[str, Any]) -> str:
    """Resolve *agentDir* from options — explicit nonempty value wins, otherwise global helper."""
    explicit = options.get("agentDir")
    if explicit:
        return str(Path(explicit).resolve())
    return get_agent_dir()


async def create_agent_session(options: dict[str, Any] | None = None) -> dict[str, Any]:
    options = options or {}
    cwd = options.get("cwd") or __import__("os").getcwd()
    agent_dir = _resolve_agent_dir(options)

    # ── Auth storage ────────────────────────────────────────────────────
    explicit_auth = options.get("authStorage")
    if explicit_auth is not None:
        auth_storage = explicit_auth
    else:
        auth_storage = AuthStorage.create(str(Path(agent_dir) / "auth.json"))

    # ── Model registry ──────────────────────────────────────────────────
    explicit_registry = options.get("modelRegistry")
    if explicit_registry is not None:
        model_registry = explicit_registry
    else:
        model_registry = ModelRegistry.create(auth_storage, str(Path(agent_dir) / "models.json"))

    # ── Settings manager ────────────────────────────────────────────────
    explicit_settings = options.get("settingsManager")
    if explicit_settings is not None:
        settings_manager = explicit_settings
    else:
        settings_manager = SettingsManager.create(cwd, agent_dir)

    # ── Resource loader ─────────────────────────────────────────────────
    explicit_loader = options.get("resourceLoader")
    if explicit_loader is not None:
        resource_loader = explicit_loader
    else:
        resource_loader = DefaultResourceLoader(
            cwd=cwd,
            agent_dir=agent_dir,
            settings_manager=settings_manager,
        )
    await resource_loader.reload()

    # ── Session manager ─────────────────────────────────────────────────
    explicit_session = options.get("sessionManager")
    if explicit_session is not None:
        session_manager = explicit_session
    else:
        session_dir = settings_manager.get_session_dir() or get_default_session_dir(cwd, agent_dir)
        session_manager = SessionManager.create(cwd, session_dir)

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
