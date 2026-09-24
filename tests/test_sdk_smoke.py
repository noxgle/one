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
