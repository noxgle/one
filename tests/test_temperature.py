# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import math
from pathlib import Path

import pytest

from one.core.temperature import nearest_temperature_mode, normalize_temperature, temperature_for_mode
from one.providers.gemini import GeminiAdapter
from one.providers.ollama import OllamaCloudAdapter
from one.providers.openai_compatible import OpenAICompatibleAdapter


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0.0), ("0.0", 0.0), ("-0.0", 0.0), ("0.54", 0.5), ("0.55", 0.6), (1.2, 1.2)],
)
def test_temperature_normalizes_to_tenths(value: object, expected: float) -> None:
    assert normalize_temperature(value) == expected


def test_temperature_negative_zero_is_canonical_positive_zero() -> None:
    assert math.copysign(1, normalize_temperature("-0.0")) == 1


@pytest.mark.parametrize("value", [-0.1, 1.3, "no", True, float("inf")])
def test_temperature_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError):
        normalize_temperature(value)


def test_temperature_presets_and_deterministic_nearest_mode() -> None:
    assert temperature_for_mode("creative") == 0.8
    assert nearest_temperature_mode(1.0) == "creative"  # tie uses preset order
    assert nearest_temperature_mode(0.9) == "creative"


def test_supported_provider_payloads_receive_exact_temperature() -> None:
    messages = [{"role": "user", "content": "hi"}]
    openai = OpenAICompatibleAdapter("test", "https://example.invalid")
    assert openai._build_payload("m", messages, "off", temperature=0.9)["temperature"] == 0.9

    ollama = OllamaCloudAdapter("https://example.invalid")
    assert ollama._build_payload("m", messages, "off", temperature=0.9)["options"]["temperature"] == 0.9

    assert "temperature" in __import__("inspect").signature(GeminiAdapter.chat).parameters


def _session(tmp_path: Path):
    from tests.support.tui import _mk_app_session

    return _mk_app_session(tmp_path, runtime_key="sk-test")


def test_session_temperature_change_is_emitted_and_persisted(tmp_path: Path) -> None:
    session = _session(tmp_path)
    events: list[dict] = []
    session.subscribe(events.append)

    assert session.set_temperature(0.0) == 0.0
    assert session.adjust_temperature(1.2) == 1.2
    assert session.temperature_mode == "experimental"
    assert events == [
        {"type": "temperature_change", "temperature": 0.0, "temperatureMode": "coder"},
        {"type": "temperature_change", "temperature": 1.2, "temperatureMode": "experimental"},
    ]
    context = session.session_manager.build_session_context()
    assert context["temperature"] == 1.2
    assert [entry["type"] for entry in session.session_manager.get_entries() if entry["type"] == "temperature_change"] == [
        "temperature_change",
        "temperature_change",
    ]


@pytest.mark.asyncio
async def test_subagent_temperature_inherits_overrides_and_does_not_change_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session(tmp_path)
    session.set_temperature(0.5)
    seen: list[float] = []

    async def capture(task, model, tools, depth, temperature=None, **kwargs):  # noqa: ANN001
        assert temperature is not None
        seen.append(temperature)
        return {"ok": True, "goalSuccess": True, "summary": task, "finished": True}

    monkeypatch.setattr(session, "_run_subagent", capture)
    await session._spawn_subagent({"task": "inherit"})
    await session._spawn_subagent({"task": "numeric", "temperature": "0.2", "temperatureMode": "creative"})
    await session._spawn_subagent({"task": "mode", "temperatureMode": "experimental"})
    assert seen == [0.5, 0.2, 1.2]
    assert session.temperature == 0.5
    with pytest.raises(ValueError):
        await session._spawn_subagent({"task": "bad", "temperature": "wat"})
    with pytest.raises(ValueError):
        await session._spawn_subagent({"task": "bad", "temperatureMode": "wat"})


@pytest.mark.asyncio
async def test_subagent_provider_receives_inherited_temperature(tmp_path: Path) -> None:
    from one.providers.base import ChatResult

    session = _session(tmp_path)
    session.set_temperature(0.8)
    captured: list[float] = []

    class Provider:
        async def chat(self, api_key, model, messages, thinking_level, temperature=None, **kwargs):  # noqa: ANN001
            captured.append(temperature)
            return ChatResult(text="done", raw={}, usage={}, stop_reason="stop")

    session.providers = {"openai": Provider()}
    result = await session._run_subagent("child", session.model, ["finish"], 0)
    assert captured and set(captured) == {0.8}
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_runtime_restores_persisted_temperature(tmp_path: Path) -> None:
    from one.core.agent_session_runtime import create_agent_session_runtime
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.settings_manager import SettingsManager

    class Loader:
        async def reload(self) -> None:
            pass

        def get_extensions(self) -> dict[str, list[object]]:
            return {"extensions": []}

    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    manager = SessionManager.in_memory(str(tmp_path))
    manager.append_temperature_change(0.2)
    runtime = await create_agent_session_runtime(
        {"settingsManager": SettingsManager.in_memory(), "modelRegistry": registry, "model": model},
        {"cwd": str(tmp_path), "sessionManager": manager, "resourceLoader": Loader()},
    )
    assert runtime.session.temperature == 0.2
    assert runtime.session.temperature_mode == "coder"
