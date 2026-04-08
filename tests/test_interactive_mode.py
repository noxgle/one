from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from one.core.types import ModelInfo
from one.modes.interactive_mode import InteractiveMode


@dataclass
class _DummySessionManager:
    cwd: str = "/tmp/project"


class _DummySettings:
    def __init__(self) -> None:
        self._global: dict[str, Any] = {"tools": {"maxSteps": 6}}
        self.default_provider: str | None = None
        self.default_model: str | None = None

    def get_global_settings(self) -> dict[str, Any]:
        return self._global

    def merged(self) -> dict[str, Any]:
        return self._global

    def set_config_value(self, key: str, value: Any) -> None:
        parts = [p for p in key.split(".") if p]
        cur = self._global
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value

    def set_default_provider(self, provider: str | None) -> None:
        self.default_provider = provider

    def set_default_model(self, model: str | None) -> None:
        self.default_model = model


class _DummyModelRegistry:
    def __init__(self) -> None:
        self._models = [
            ModelInfo(provider="openai", id="gpt-4.1"),
            ModelInfo(provider="openai", id="gpt-4o"),
            ModelInfo(provider="openrouter", id="openai/gpt-4.1"),
        ]
        self.stored_keys: dict[str, str] = {}

    def find(self, provider: str, model_id: str) -> ModelInfo | None:
        for m in self._models:
            if m.provider == provider and m.id == model_id:
                return m
        return None

    def resolve(self, provider: str, model_id: str, *, allow_dynamic: bool = False) -> ModelInfo | None:
        found = self.find(provider, model_id)
        if found:
            return found
        if allow_dynamic and provider.strip() and model_id.strip():
            return ModelInfo(provider=provider, id=model_id)
        return None

    def providers(self) -> list[str]:
        return sorted({m.provider for m in self._models})

    def models_for_provider(self, provider: str) -> list[ModelInfo]:
        return [m for m in self._models if m.provider == provider]

    def set_stored_api_key(self, provider: str, api_key: str) -> None:
        self.stored_keys[provider] = api_key


class _DummySession:
    def __init__(self) -> None:
        self.model = ModelInfo(provider="openai", id="gpt-4.1")
        self.thinking_level = "medium"
        self.is_streaming = False
        self.pending_message_count = 0
        self.session_id = "sid"
        self.session_file = "/tmp/session.jsonl"
        self.active_tools = ["read", "bash", "edit", "write", "grep", "find", "ls"]
        self.session_manager = _DummySessionManager()
        self.settings_manager = _DummySettings()
        self.model_registry = _DummyModelRegistry()
        self._listeners: list[Any] = []
        self._steering: list[str] = []
        self._follow: list[str] = []
        self.retry_enabled = True
        self.aborted = False
        self.prompt_calls: list[str] = []

    def subscribe(self, listener: Any) -> None:
        self._listeners.append(listener)

    def get_context_usage(self) -> dict[str, Any]:
        return {"tokens": 10, "contextWindow": 1000, "percent": 1.0}

    def get_session_stats(self) -> dict[str, Any]:
        return {"tokens": {"total": 42}}

    def get_pending_queues(self) -> dict[str, list[str]]:
        return {"steering": list(self._steering), "followUp": list(self._follow)}

    async def set_model(self, model: ModelInfo) -> None:
        self.model = model

    def set_thinking_level(self, level: str) -> None:
        self.thinking_level = level

    async def steer(self, text: str) -> None:
        self._steering.append(text)

    async def follow_up(self, text: str) -> None:
        self._follow.append(text)

    async def compact(self, custom_instructions: str | None = None) -> dict[str, Any]:
        return {"summary": custom_instructions or "", "aborted": False}

    async def execute_bash(self, command: str) -> dict[str, Any]:
        return {"output": f"ran:{command}"}

    async def abort(self) -> None:
        self.aborted = True

    def set_auto_retry_enabled(self, enabled: bool) -> None:
        self.retry_enabled = enabled

    async def prompt(self, text: str) -> None:
        self.prompt_calls.append(text)


class _DummyHost:
    def __init__(self, session: _DummySession) -> None:
        self.session = session


def _mk_input(commands: list[str]):
    it = iter(commands)

    def _fake_input(_prompt: str = "") -> str:
        return next(it)

    return _fake_input


@pytest.mark.asyncio
async def test_interactive_slash_commands_smoke(monkeypatch, capsys):
    session = _DummySession()
    mode = InteractiveMode(_DummyHost(session))

    commands = [
        "/help",
        "/stats",
        "/state",
        "/queue",
        "/tools",
        "/clear",
        "/model openai/gpt-4o",
        "/thinking low",
        "/steer abc",
        "/follow def",
        "/compact now",
        "/login openai sk-test gpt-4.1",
        "/retry off",
        "/config tools.maxSteps 9",
        "/config tools.maxSteps",
        "/bash echo hi",
        "/abort",
        "/exit",
    ]
    monkeypatch.setattr("builtins.input", _mk_input(commands))

    await mode.run()
    out = capsys.readouterr().out

    assert "Use /help for commands" in out
    assert "Model set to openai/gpt-4o" in out
    assert "Thinking level set to low" in out
    assert "Queued steering message." in out
    assert "Queued follow-up message." in out
    assert "Stored key for openai." in out
    assert "Auto-retry set to off." in out
    assert "Updated tools.maxSteps." in out
    assert "ran:echo hi" in out
    assert "Abort requested." in out

    assert session.model.id == "gpt-4.1"
    assert session.settings_manager.default_provider == "openai"
    assert session.settings_manager.default_model == "gpt-4.1"
    assert session.model_registry.stored_keys["openai"] == "sk-test"
    assert session.retry_enabled is False
    assert session.get_pending_queues()["steering"] == ["abc"]
    assert session.get_pending_queues()["followUp"] == ["def"]
    assert session.aborted is True
    assert session.prompt_calls == []


@pytest.mark.asyncio
async def test_interactive_model_command_supports_dynamic_known_provider(monkeypatch, capsys):
    session = _DummySession()
    mode = InteractiveMode(_DummyHost(session))

    commands = [
        "/model openrouter/google/gemma-4-31b-it:free",
        "/exit",
    ]
    monkeypatch.setattr("builtins.input", _mk_input(commands))

    await mode.run()
    out = capsys.readouterr().out

    assert "Model set to openrouter/google/gemma-4-31b-it:free (dynamic)" in out
    assert session.model.provider == "openrouter"
    assert session.model.id == "google/gemma-4-31b-it:free"


@pytest.mark.asyncio
async def test_interactive_invalid_slash_inputs(monkeypatch, capsys):
    session = _DummySession()
    mode = InteractiveMode(_DummyHost(session))

    commands = [
        "/model",
        "/model nope",
        "/retry maybe",
        "/login custom-provider sk",
        "/unknown-cmd",
        "/bash",
        "/exit",
    ]
    monkeypatch.setattr("builtins.input", _mk_input(commands))

    await mode.run()
    out = capsys.readouterr().out

    assert "Usage: /model <provider>/<model-id>" in out
    assert "Usage: /retry <on|off>" in out
    assert "Stored key for custom-provider." in out
    assert "Unknown command: /unknown-cmd. Use /help." in out
    assert session.prompt_calls == []
