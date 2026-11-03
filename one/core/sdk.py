# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from pathlib import Path
from typing import Any

from one.config import get_agent_dir
from one.core.agent_session_runtime import AgentSessionRuntimeHost, create_agent_session_runtime
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager, get_default_session_dir
from one.core.settings_manager import SettingsManager
from one.resources.default_skill_installer import install_default_skills
from one.resources.resource_loader import DefaultResourceLoader


def run_tree_page(session: Any, offset: int = 0, limit: int = 100) -> dict[str, Any]:
    """Return a detached page for the current or most recently completed run."""
    context = getattr(session, "_run_context", None) or getattr(session, "_last_run_context", None)
    if context is None:
        # Validate pagination even when there is no run.
        from one.core.run_context import MAX_TREE_PAGE_SIZE

        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_TREE_PAGE_SIZE:
            raise ValueError("Invalid run tree pagination")
        return {"available": False}
    return {"available": True, **context.tree_snapshot(offset=offset, limit=limit)}


def run_history_page(session: Any, run_id: str, offset: int = 0, limit: int = 100) -> dict[str, Any]:
    """Read a bounded node page from a persisted run, without restoring execution."""
    from one.core.run_context import MAX_TREE_PAGE_SIZE

    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a nonempty string")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_TREE_PAGE_SIZE:
        raise ValueError("Invalid run history pagination")
    manager = getattr(session, "session_manager", None)
    history = manager.run_history() if manager is not None else None
    for snapshot in history.read() if history is not None else []:
        if snapshot["runId"] == run_id:
            nodes = snapshot["nodes"]
            metadata = {key: value for key, value in snapshot.items() if key != "nodes"}
            return {"available": True, **metadata, "nodes": nodes[offset:offset + limit],
                    "offset": offset, "totalNodes": len(nodes),
                    "nextOffset": offset + limit if offset + limit < len(nodes) else None}
    return {"available": False}


def cancel_run(session: Any, run_id: str, node_id: str | None = None) -> bool:
    """Cancel a matching live run or subtree; external effects are not rolled back."""
    if not isinstance(run_id, str) or not run_id or (node_id is not None and
                                                     (not isinstance(node_id, str) or not node_id)):
        raise ValueError("runId and nodeId must be nonempty strings")
    context = getattr(session, "_run_context", None)
    if context is None or context.run_id != run_id:
        return False
    target = node_id if node_id is not None else context.root_id
    try:
        context.node(target)
    except KeyError:
        return False
    context.cancel_tasks_subtree(target, "user_cancelled")
    return True


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
    install_default_skills(agent_dir, cwd)

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
