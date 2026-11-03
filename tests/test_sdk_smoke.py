# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from pathlib import Path

import pytest

from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.sdk import create_agent_session
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager

pytestmark = pytest.mark.smoke


@pytest.mark.asyncio
async def test_create_agent_session_smoke(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    settings = SettingsManager.in_memory({"defaultProvider": "openai", "defaultModel": "gpt-4.1"})
    session_manager = SessionManager.in_memory(str(tmp_path))

    result = await create_agent_session(
        {
            "cwd": str(tmp_path),
            "authStorage": auth,
            "modelRegistry": registry,
            "settingsManager": settings,
            "sessionManager": session_manager,
        }
    )
    assert result["session"].session_id


@pytest.mark.asyncio
async def test_run_helpers_validate_and_cancel():
    from types import SimpleNamespace

    from one.core.run_context import RunContext, RunLimits
    from one.core.sdk import cancel_run, run_tree_page

    context = RunContext("session", RunLimits())
    session = SimpleNamespace(_run_context=context)
    child = context.register(context.root_id, "child")
    grandchild = context.register(child.node_id, "grandchild")
    assert run_tree_page(session, 2, 1)["nodes"][0]["nodeId"] == grandchild.node_id
    with pytest.raises(ValueError):
        run_tree_page(session, limit=False)
    with pytest.raises(ValueError):
        cancel_run(session, "")
    assert not cancel_run(session, "stale")
    assert cancel_run(session, context.run_id)
    assert context.node(grandchild.node_id).state.value == "cancelling"
