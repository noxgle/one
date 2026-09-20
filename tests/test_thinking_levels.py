from __future__ import annotations

from pathlib import Path

import pytest

from one.core.agent_session import AgentSession
from one.core.agent_session_runtime import AgentSessionRuntimeHost, create_agent_session_runtime
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelInfo, ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager


class _Loader:
    async def reload(self) -> None:
        pass

    def get_extensions(self) -> dict[str, list[object]]:
        return {"extensions": []}


def _model_registry() -> tuple[ModelRegistry, ModelInfo]:
    registry = ModelRegistry.create(AuthStorage.in_memory())
    registry.register_models("openai", [{"id": "reasoner", "reasoning": True}])
    model = registry.find("openai", "reasoner")
    assert model is not None
    return registry, model


def test_thinking_default_round_trip_and_invalid_value_does_not_mutate(tmp_path: Path) -> None:
    settings = SettingsManager(str(tmp_path), str(tmp_path / "agent"))
    settings.set_default_thinking_level("xhigh")
    assert SettingsManager(str(tmp_path), str(tmp_path / "agent")).get_default_thinking_level() == "xhigh"
    with pytest.raises(ValueError, match="Invalid thinking level"):
        settings.set_default_thinking_level("invalid")
    assert settings.get_default_thinking_level() == "xhigh"


def test_session_thinking_change_persists_default_and_rejects_invalid(tmp_path: Path) -> None:
    registry, model = _model_registry()
    settings = SettingsManager.in_memory()
    session = AgentSession(SessionManager.in_memory(str(tmp_path)), settings, registry, _Loader(), model, "medium")
    session.set_thinking_level("high")
    assert settings.get_default_thinking_level() == "high"
    assert session.session_manager.build_session_context()["thinkingLevel"] == "high"
    with pytest.raises(ValueError, match="Invalid thinking level"):
        session.set_thinking_level("invalid")
    assert session.thinking_level == "high"
    assert settings.get_default_thinking_level() == "high"


@pytest.mark.asyncio
async def test_runtime_new_uses_saved_default_and_loaded_session_wins(tmp_path: Path) -> None:
    registry, model = _model_registry()
    settings = SettingsManager.in_memory({"defaultThinkingLevel": "medium"})
    loader = _Loader()
    fresh_manager = SessionManager.in_memory(str(tmp_path))
    bootstrap = {"settingsManager": settings, "modelRegistry": registry, "model": model, "thinkingLevel": "low"}
    runtime = await create_agent_session_runtime(
        bootstrap, {"cwd": str(tmp_path), "sessionManager": fresh_manager, "resourceLoader": loader}
    )
    host = AgentSessionRuntimeHost(bootstrap, runtime)
    runtime.session.set_thinking_level("high")
    await host.new_session()
    assert host.session.thinking_level == "high"

    loaded = SessionManager.in_memory(str(tmp_path))
    loaded.append_thinking_level_change("low")
    restored = await create_agent_session_runtime(
        {"settingsManager": settings, "modelRegistry": registry, "model": model},
        {"cwd": str(tmp_path), "sessionManager": loaded, "resourceLoader": loader},
    )
    assert restored.session.thinking_level == "low"
