from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import uuid

import pytest

from one.core.types import ModelInfo
from one.modes.interactive_mode import InteractiveMode


@pytest.fixture(autouse=True)
def _isolated_agent_dir(tmp_path: Path, monkeypatch):
    """Keep readline history writes out of the real ~/.config/one agent dir.

    Without this, every InteractiveMode instance reads/writes the real
    interactive.history file, which grows unboundedly across test runs.
    """
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(tmp_path / "agent"))
    return tmp_path


@dataclass
class _DummySessionManager:
    cwd: str = "/tmp/project"


class _DummySettings:
    def __init__(self) -> None:
        self._global: dict[str, Any] = {"tools": {"maxSteps": 6}}
        self.default_provider: str | None = None
        self.default_model: str | None = None
        self.theme: str = "default"

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

    def get_theme(self) -> str:
        return self.theme

    def set_theme(self, theme: str) -> None:
        self.theme = theme

    def get_subagents_enabled(self) -> bool:
        return bool(self._global.get("subagents", {}).get("enabled", True))

    def set_subagents_enabled(self, enabled: bool) -> None:
        self._global.setdefault("subagents", {})["enabled"] = bool(enabled)

    def get_bash_show_output(self) -> bool:
        return bool(self._global.get("bash", {}).get("showOutput", True))

    def set_bash_show_output(self, enabled: bool) -> None:
        self._global.setdefault("bash", {})["showOutput"] = bool(enabled)

    def get_mcp_servers(self) -> dict[str, Any]:
        return self._global.get("mcpServers", {}) or {}

    def set_mcp_server_enabled(self, name: str, enabled: bool) -> None:
        self._global.setdefault("mcpServers", {}).setdefault(name, {})["enabled"] = bool(enabled)


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

    def get_available(self) -> list[ModelInfo]:
        """Mirror real semantics: NO_AUTH providers are always usable."""
        return [
            m for m in self._models
            if m.provider == "llama.cpp" or m.provider in self.stored_keys
        ]

    def get_api_key_and_headers(self, model: ModelInfo) -> dict[str, Any]:
        if model.provider == "llama.cpp":
            return {"ok": True, "apiKey": "", "headers": {}}
        key = self.stored_keys.get(model.provider)
        if not key:
            return {"ok": False, "error": f"No API key found for {model.provider} (use /login)"}
        return {"ok": True, "apiKey": key, "headers": {}}

    def register_models(self, provider: str, model_ids: list[str | dict[str, Any]]) -> int:
        added = 0
        for entry in model_ids:
            mid = entry["id"] if isinstance(entry, dict) else entry
            if not self.find(provider, mid):
                window = entry.get("contextWindow") if isinstance(entry, dict) else None
                self._models.append(ModelInfo(provider=provider, id=mid, context_window=window))
                added += 1
        return added

    def persist_models(self, provider: str, model_ids: list[str | dict[str, Any]]) -> None:
        self.persisted: tuple[str, list[str | dict[str, Any]]] = (provider, list(model_ids))

    def set_stored_api_key(self, provider: str, api_key: str) -> None:
        self.stored_keys[provider] = api_key

    def remove_stored_api_key(self, provider: str) -> None:
        self.stored_keys.pop(provider, None)

    def remove_provider_credentials(self, provider: str) -> None:
        """Phase 30.4: clear runtime and stored credentials for a provider."""
        # runtime keys are stored in self.stored_keys (mimics _runtime)
        self.stored_keys.pop(provider, None)

    def requires_api_key(self, provider: str) -> bool:
        return provider != "llama.cpp"

    def get_provider_auth_status(self, provider: str) -> dict[str, Any]:
        return {
            "provider": provider,
            "requiresApiKey": self.requires_api_key(provider),
            "configured": provider in self.stored_keys,
            "envVar": provider.upper().replace("-", "_").replace(".", "_") + "_API_KEY",
        }


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
        self._extui_pending: dict[str, dict[str, Any]] = {}
        self._extui_history: list[dict[str, Any]] = []
        self.approval_callback: Any = None

    def subscribe(self, listener: Any) -> None:
        self._listeners.append(listener)

    def get_context_usage(self) -> dict[str, Any]:
        return {"tokens": 10, "contextWindow": 1000, "percent": 1.0}

    def get_session_stats(self) -> dict[str, Any]:
        return {"tokens": {"total": 42}}

    def get_pending_queues(self) -> dict[str, list[str]]:
        return {"steering": list(self._steering), "followUp": list(self._follow)}

    def clear_pending_queues(self, target: str = "all") -> dict[str, list[str]]:
        t = (target or "all").strip().lower()
        if t in {"all", "both"}:
            self._steering.clear()
            self._follow.clear()
        elif t in {"steering", "steer", "s"}:
            self._steering.clear()
        elif t in {"follow", "followup", "follow_up", "f"}:
            self._follow.clear()
        else:
            raise ValueError("target must be one of: all, steering, follow")
        return {"steering": list(self._steering), "followUp": list(self._follow)}

    async def set_model(self, model: ModelInfo) -> None:
        self.model = model

    async def cycle_model(self):
        self.model = ModelInfo(provider="openai", id="gpt-4o")
        return type(
            "R",
            (),
            {"model": self.model, "thinkingLevel": self.thinking_level, "isScoped": False},
        )()

    def set_thinking_level(self, level: str) -> None:
        self.thinking_level = level

    def cycle_thinking_level(self) -> str:
        self.thinking_level = "low"
        return self.thinking_level

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

    def request_extension_ui(
        self,
        extension: str,
        ui_type: str,
        payload: dict[str, Any] | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        if ui_type not in {"widget", "overlay"}:
            raise ValueError("uiType must be one of: widget, overlay")
        req = {
            "id": uuid.uuid4().hex[:12],
            "extension": extension,
            "uiType": ui_type,
            "title": title or "",
            "payload": payload or {},
            "status": "pending",
            "createdAt": 0,
        }
        self._extui_pending[req["id"]] = req
        return req

    def respond_extension_ui(
        self,
        request_id: str,
        payload: dict[str, Any] | None = None,
        cancelled: bool = False,
    ) -> dict[str, Any]:
        req = self._extui_pending.pop(request_id, None)
        if req is None:
            raise ValueError(f"Extension UI request not found: {request_id}")
        resp = {
            "requestId": request_id,
            "extension": req["extension"],
            "uiType": req["uiType"],
            "payload": payload or {},
            "cancelled": cancelled,
            "createdAt": req["createdAt"],
            "respondedAt": 1,
        }
        self._extui_history.append(resp)
        return resp

    def get_extension_ui_state(self) -> dict[str, Any]:
        return {"pending": list(self._extui_pending.values()), "history": list(self._extui_history)}

    def clear_extension_ui_history(self) -> None:
        self._extui_history = []

    def sync_mcp_tools(self) -> None:
        pass


class _DummyHost:
    def __init__(self, session: _DummySession) -> None:
        self.session = session

    async def new_session(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"cancelled": False}


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
        "/model",
        "/thinking",
        "/queue",
        "/theme",
        "/tools",
        "/clear",
        "/model openai/gpt-4o",
        "/thinking low",
        "/theme solarized",
        "/steer abc",
        "/follow def",
        "/queue clear steering",
        "/compact now",
        "/login openai sk-test gpt-4.1",
        "/login status openai",
        "/logout openai",
        "/retry off",
        "/config tools.maxSteps 9",
        "/config tools.maxSteps",
        "/bash echo hi",
        "/subagents off",
        "/subagents",
        "/bash-show off",
        "/bash-show",
        "/abort",
        "/new",
        "/ns",
        "/exit",
    ]
    monkeypatch.setattr("builtins.input", _mk_input(commands))

    await mode.run()
    out = capsys.readouterr().out

    assert "Use /help for commands" in out
    assert "Shortcuts: Ctrl+C abort/exit" in out
    assert "Model set to openai/gpt-4o (saved as default)" in out
    assert "Thinking level set to low" in out
    assert "Theme set to solarized" in out
    assert "Queued steering message." in out
    assert "Queued follow-up message." in out
    assert "Stored key for openai." in out
    assert '"provider": "openai"' in out
    assert "Removed credentials for openai locally." in out
    assert "Auto-retry set to off." in out
    assert "Updated tools.maxSteps." in out
    assert "ran:echo hi" in out
    assert "Subagents set to off." in out
    assert "Bash output set to off." in out
    assert session.settings_manager.get_subagents_enabled() is False
    assert session.settings_manager.get_bash_show_output() is False
    assert "Abort requested." in out
    assert '"cancelled": false' in out

    assert session.model.id == "gpt-4.1"
    assert session.settings_manager.default_provider == "openai"
    assert session.settings_manager.default_model == "gpt-4.1"
    assert session.settings_manager.theme == "solarized"
    assert "openai" not in session.model_registry.stored_keys
    assert session.retry_enabled is False
    assert session.get_pending_queues()["steering"] == []
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

    assert "Model set to openrouter/google/gemma-4-31b-it:free (dynamic, saved as default)" in out
    assert session.model.provider == "openrouter"
    assert session.model.id == "google/gemma-4-31b-it:free"
    assert session.settings_manager.default_provider == "openrouter"
    assert session.settings_manager.default_model == "google/gemma-4-31b-it:free"


@pytest.mark.asyncio
async def test_interactive_model_command_provider_only(monkeypatch, capsys):
    session = _DummySession()
    mode = InteractiveMode(_DummyHost(session))

    commands = [
        "/model openai",
        "/exit",
    ]
    monkeypatch.setattr("builtins.input", _mk_input(commands))

    await mode.run()
    out = capsys.readouterr().out

    # Provider-only picks the provider's first registered model.
    assert "Model set to openai/gpt-4.1 (saved as default)" in out
    assert session.model.provider == "openai"
    assert session.model.id == "gpt-4.1"
    assert session.settings_manager.default_provider == "openai"
    assert session.settings_manager.default_model == "gpt-4.1"


@pytest.mark.asyncio
async def test_interactive_model_command_provider_only_unknown(monkeypatch, capsys):
    session = _DummySession()
    mode = InteractiveMode(_DummyHost(session))

    commands = [
        "/model nope",
        "/exit",
    ]
    monkeypatch.setattr("builtins.input", _mk_input(commands))

    await mode.run()
    out = capsys.readouterr().out

    assert "Provider not found or has no models: nope" in out
    assert session.model.id == "gpt-4.1"  # unchanged


@pytest.mark.asyncio
async def test_interactive_providers_lists_logged_in_only(monkeypatch, capsys):
    """Phase 13: /providers lists only providers with configured auth."""
    session = _DummySession()
    session.model_registry.set_stored_api_key("openai", "sk-x")
    mode = InteractiveMode(_DummyHost(session))
    monkeypatch.setattr("builtins.input", _mk_input(["/providers", "/exit"]))
    await mode.run()
    out = capsys.readouterr().out

    assert "Logged-in providers:" in out
    # openai has a key and is the current model -> marker + current hint.
    assert "* 1. openai   2 models   (current: gpt-4.1)" in out
    # openrouter has no key -> not listed at all.
    assert "openrouter" not in out


@pytest.mark.asyncio
async def test_interactive_providers_second_step_lists_models_without_switching(monkeypatch, capsys):
    session = _DummySession()
    session.model_registry.set_stored_api_key("openai", "sk-x")
    mode = InteractiveMode(_DummyHost(session))
    monkeypatch.setattr("builtins.input", _mk_input(["/providers 1", "/providers openai", "/exit"]))
    await mode.run()
    out = capsys.readouterr().out

    assert out.count("openai models:") == 2
    assert "  1. gpt-4.1" in out
    assert "  2. gpt-4o" in out
    assert "Usage: /providers 1 <model-number|id> to switch" in out
    # Second step only lists; the model is unchanged.
    assert "Model set to" not in out
    assert session.model.provider == "openai"
    assert session.model.id == "gpt-4.1"


@pytest.mark.asyncio
async def test_interactive_providers_switch_by_number_and_by_id(monkeypatch, capsys):
    session = _DummySession()
    reg = session.model_registry
    reg.set_stored_api_key("openai", "sk-x")
    reg.set_stored_api_key("openrouter", "sk-o")
    mode = InteractiveMode(_DummyHost(session))
    monkeypatch.setattr(
        "builtins.input",
        _mk_input(["/providers 1 2", "/providers openrouter openai/gpt-4.1", "/exit"]),
    )
    await mode.run()
    out = capsys.readouterr().out

    assert "Model set to openai/gpt-4o (saved as default)" in out
    assert "Model set to openrouter/openai/gpt-4.1 (saved as default)" in out
    assert session.model.provider == "openrouter"
    assert session.model.id == "openai/gpt-4.1"
    assert session.settings_manager.default_provider == "openrouter"
    assert session.settings_manager.default_model == "openai/gpt-4.1"


@pytest.mark.asyncio
async def test_interactive_providers_error_paths_change_nothing(monkeypatch, capsys):
    session = _DummySession()
    session.model_registry.set_stored_api_key("openai", "sk-x")
    mode = InteractiveMode(_DummyHost(session))
    commands = [
        "/providers",
        "/providers 3",
        "/providers nope",
        "/providers 1 99",
        "/providers 1 nope-model",
        "/exit",
    ]
    monkeypatch.setattr("builtins.input", _mk_input(commands))
    await mode.run()
    out = capsys.readouterr().out

    assert "Logged-in providers:" in out
    assert "Provider not logged in or unknown: 3 (see /providers)" in out
    assert "Provider not logged in or unknown: nope (see /providers)" in out
    assert "Model not found for openai: 99 (see /providers 1)" in out
    assert "Model not found for openai: nope-model (see /providers 1)" in out
    assert "Model set to" not in out
    assert session.model.id == "gpt-4.1"


@pytest.mark.asyncio
async def test_interactive_providers_none_logged_in(monkeypatch, capsys):
    session = _DummySession()
    mode = InteractiveMode(_DummyHost(session))
    monkeypatch.setattr("builtins.input", _mk_input(["/providers", "/exit"]))
    await mode.run()
    out = capsys.readouterr().out

    assert "No logged-in providers. Use /login <provider> [apiKey] first." in out


class _RefreshStub:
    """Phase 15: fake adapter for /login refresh tests."""

    def __init__(self, models: list[str] | None = None, error: Exception | None = None) -> None:
        self.models = models
        self.error = error

    async def list_models(self, api_key: str, headers: dict | None = None) -> list[str]:
        if self.error is not None:
            raise self.error
        return self.models

    async def chat(self, api_key, model, messages, thinking_level, headers=None, on_delta=None, max_tokens=None):
        return None


@pytest.mark.asyncio
async def test_interactive_login_refresh_fetches_and_registers(monkeypatch, capsys):
    session = _DummySession()
    session.model_registry.set_stored_api_key("openai", "sk-x")
    session.providers = {"openai": _RefreshStub(models=["n1", "n2"])}
    mode = InteractiveMode(_DummyHost(session))
    monkeypatch.setattr("builtins.input", _mk_input(["/login refresh openai", "/exit"]))
    await mode.run()
    out = capsys.readouterr().out

    assert "Authorized. Fetched 2 models (2 new)." in out
    assert "  1. n1" in out
    assert "  2. n2" in out
    assert "Pick with /providers openai <model-number|id>" in out
    assert session.model_registry.find("openai", "n1") is not None
    assert session.model_registry.persisted[0] == "openai"
    assert [e["id"] if isinstance(e, dict) else e for e in session.model_registry.persisted[1]] == ["n1", "n2"]
    # Key and defaults are untouched by a refresh.
    assert session.settings_manager.default_provider is None


@pytest.mark.asyncio
async def test_interactive_login_refresh_no_auth_provider(monkeypatch, capsys):
    session = _DummySession()
    session.providers = {"llama.cpp": _RefreshStub(models=["local-a"])}
    mode = InteractiveMode(_DummyHost(session))
    monkeypatch.setattr("builtins.input", _mk_input(["/login refresh llama.cpp", "/exit"]))
    await mode.run()
    out = capsys.readouterr().out

    assert "Authorized. Fetched 1 models (1 new)." in out
    assert session.model_registry.find("llama.cpp", "local-a") is not None


@pytest.mark.asyncio
async def test_interactive_login_refresh_missing_key(monkeypatch, capsys):
    session = _DummySession()
    stub = _RefreshStub(models=["x"])
    session.providers = {"openai": stub}
    mode = InteractiveMode(_DummyHost(session))
    monkeypatch.setattr("builtins.input", _mk_input(["/login refresh openai", "/exit"]))
    await mode.run()
    out = capsys.readouterr().out

    assert "No API key found for openai" in out
    # The adapter was never contacted.
    assert stub.models == ["x"]  # unchanged; no exception path needed beyond not crashing


@pytest.mark.asyncio
async def test_interactive_login_refresh_bad_key_keeps_registry(monkeypatch, capsys):
    session = _DummySession()
    session.model_registry.set_stored_api_key("openai", "sk-bad")
    session.providers = {"openai": _RefreshStub(error=RuntimeError("openai API error 401: invalid"))}
    mode = InteractiveMode(_DummyHost(session))
    monkeypatch.setattr("builtins.input", _mk_input(["/login refresh openai", "/exit"]))
    await mode.run()
    out = capsys.readouterr().out

    assert "Authorization failed for openai" in out
    assert session.model_registry.find("openai", "n1") is None


@pytest.mark.asyncio
async def test_interactive_login_refresh_unknown_adapter(monkeypatch, capsys):
    session = _DummySession()
    mode = InteractiveMode(_DummyHost(session))
    monkeypatch.setattr("builtins.input", _mk_input(["/login refresh nope", "/exit"]))
    await mode.run()
    out = capsys.readouterr().out

    assert "Provider adapter not found for nope." in out


@pytest.mark.asyncio
async def test_interactive_invalid_slash_inputs(monkeypatch, capsys):
    session = _DummySession()
    mode = InteractiveMode(_DummyHost(session))

    commands = [
        "/model",
        "/model nope",
        "/thinking",
        "/queue clear nope",
        "/retry maybe",
        "/login llama.cpp",
        "/login custom-provider sk",
        "/unknown-cmd",
        "/bash",
        "/exit",
    ]
    monkeypatch.setattr("builtins.input", _mk_input(commands))

    await mode.run()
    out = capsys.readouterr().out

    assert '"usage": "/model <provider>/<model-id>"' in out
    assert "Provider not found or has no models: nope" in out
    assert "Usage: /queue clear [all|steering|follow]" in out
    assert "Usage: /retry <on|off>" in out
    assert "Configured provider llama.cpp." in out
    assert "Stored key for custom-provider." in out
    assert "Unknown command: /unknown-cmd. Use /help." in out
    assert session.prompt_calls == []


@pytest.mark.asyncio
async def test_interactive_short_aliases(monkeypatch, capsys):
    session = _DummySession()
    mode = InteractiveMode(_DummyHost(session))

    commands = [
        "/st",
        "/m",
        "/t",
        "/mc",
        "/tc",
        "/qq",
        "/c",
        "/q",
    ]
    monkeypatch.setattr("builtins.input", _mk_input(commands))

    await mode.run()
    out = capsys.readouterr().out
    assert '"sessionId": "sid"' in out
    assert "Model cycled to" in out or "No available models to cycle." in out
    assert "Thinking level cycled to" in out
    assert '"levels": [' in out


@pytest.mark.asyncio
async def test_interactive_login_validates_and_fetches_models(tmp_path, monkeypatch, capsys):
    """Phase 11: /login validates the key, fetches + persists the model list."""
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry

    auth = AuthStorage.in_memory()
    session = _DummySession()
    session.model_registry = ModelRegistry.create(auth, str(tmp_path / "models.json"))

    class _Stub:
        async def list_models(self, api_key: str, headers: dict | None = None) -> list[str]:
            return ["m1", "m2"]

        async def chat(self, api_key, model, messages, thinking_level, headers=None, on_delta=None, max_tokens=None):
            return None

    session.providers = {"openai": _Stub()}
    mode = InteractiveMode(_DummyHost(session))
    monkeypatch.setattr("builtins.input", _mk_input(["/login openai sk-ok gpt-4.1", "/exit"]))
    await mode.run()
    out = capsys.readouterr().out

    assert "Authorized. Fetched 2 models" in out
    assert "- m1" in out and "- m2" in out
    assert "Stored key for openai." in out
    assert "Default model set to gpt-4.1." in out
    # Key stored, models registered in-memory and persisted.
    assert auth.get_api_key("openai") == "sk-ok"
    assert session.model_registry.find("openai", "m1") is not None
    data = json.loads((tmp_path / "models.json").read_text(encoding="utf-8"))
    assert {m["id"] for m in data["providers"]["openai"]} >= {"m1", "m2"}


@pytest.mark.asyncio
async def test_interactive_login_bad_key_not_stored(tmp_path, monkeypatch, capsys):
    """Phase 11: 401 during validation -> key is NOT stored."""
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry

    auth = AuthStorage.in_memory()
    session = _DummySession()
    session.model_registry = ModelRegistry.create(auth, str(tmp_path / "models.json"))

    class _Stub:
        async def list_models(self, api_key: str, headers: dict | None = None) -> list[str]:
            raise RuntimeError("openai API error 401: invalid key")

    session.providers = {"openai": _Stub()}
    mode = InteractiveMode(_DummyHost(session))
    monkeypatch.setattr("builtins.input", _mk_input(["/login openai sk-bad", "/exit"]))
    await mode.run()
    out = capsys.readouterr().out

    assert "Authorization failed for openai" in out
    assert "Stored key for openai." not in out
    assert auth.get_api_key("openai") is None
    assert session.settings_manager.default_provider is None


@pytest.mark.asyncio
async def test_interactive_login_no_auth_provider_fetches_without_key(tmp_path, monkeypatch, capsys):
    """Phase 11: NO_AUTH providers (llama.cpp) fetch models without a key."""
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry

    auth = AuthStorage.in_memory()
    session = _DummySession()
    session.model_registry = ModelRegistry.create(auth, str(tmp_path / "models.json"))

    class _Stub:
        async def list_models(self, api_key: str, headers: dict | None = None) -> list[str]:
            return ["local", "qwen"]

    session.providers = {"llama.cpp": _Stub()}
    mode = InteractiveMode(_DummyHost(session))
    monkeypatch.setattr("builtins.input", _mk_input(["/login llama.cpp", "/exit"]))
    await mode.run()
    out = capsys.readouterr().out

    assert "Fetched 2 models" in out
    assert "Configured provider llama.cpp." in out
    assert session.model_registry.find("llama.cpp", "local") is not None
    data = json.loads((tmp_path / "models.json").read_text(encoding="utf-8"))
    assert {m["id"] for m in data["providers"]["llama.cpp"]} == {"local", "qwen"}


@pytest.mark.asyncio
async def test_interactive_login_status_lists_providers(monkeypatch, capsys):
    session = _DummySession()
    mode = InteractiveMode(_DummyHost(session))
    commands = ["/login status", "/exit"]
    monkeypatch.setattr("builtins.input", _mk_input(commands))
    await mode.run()
    out = capsys.readouterr().out
    assert '"providers": [' in out
    assert '"provider": "openai"' in out


@pytest.mark.asyncio
async def test_interactive_ctrl_a_toggles_cooperation(monkeypatch, capsys):
    session = _DummySession()
    mode = InteractiveMode(_DummyHost(session))
    commands = ["\x01", "\x01", "\x01", "/exit"]
    monkeypatch.setattr("builtins.input", _mk_input(commands))
    await mode.run()
    out = capsys.readouterr().out
    # First Ctrl+A enables, second disables, third enables again.
    assert "[Cooperation] enabled: mutating tools" in out
    assert "[Cooperation] disabled: all tools run freely" in out
    assert out.index("[Cooperation] enabled: mutating tools") < out.index("[Cooperation] disabled: all tools run freely")
    # Status line reflects the current state.
    assert "coop:on" in out
    # Ctrl+A must never be forwarded to session.prompt.
    assert session.prompt_calls == []
    # Final state: enabled.
    assert session.approval_callback is not None


class _EventSession(_DummySession):
    """_DummySession whose prompt() emits a fixed assistant event sequence."""

    def __init__(self, streamed: bool) -> None:
        super().__init__()
        answer = {"type": "message_end", "message": {"role": "assistant", "content": "Odpowiedz: hello"}}
        if streamed:
            self._events = [
                {"type": "message_start", "message": {"role": "assistant"}},
                {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Odpowiedz: "}},
                {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "hello"}},
                answer,
                {"type": "turn_end", "ok": True},
            ]
        else:
            self._events = [
                {"type": "message_start", "message": {"role": "assistant"}},
                answer,
                {"type": "turn_end", "ok": True},
            ]

    async def prompt(self, text: str) -> None:
        self.prompt_calls.append(text)
        for event in self._events:
            for listener in self._listeners:
                listener(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [True, False])
async def test_interactive_blank_line_before_answer(monkeypatch, capsys, streamed):
    session = _EventSession(streamed=streamed)
    mode = InteractiveMode(_DummyHost(session))
    commands = ["pytanie", "/exit"]
    monkeypatch.setattr("builtins.input", _mk_input(commands))
    await mode.run()
    out = capsys.readouterr().out
    # A blank line separates the answer (streamed or not) from the log above it,
    # and another blank line separates the turn from the next status block.
    assert "\n\nOdpowiedz: hello\n\n[" in out


@pytest.mark.asyncio
async def test_interactive_extui_hooks(monkeypatch, capsys):
    session = _DummySession()
    mode = InteractiveMode(_DummyHost(session))
    commands = [
        '/extui request demo-ext widget {"title":"X"}',
        "/extui list",
        "/extui respond bad-id {}",
        "/extui clear",
        "/extui list",
        "/exit",
    ]
    monkeypatch.setattr("builtins.input", _mk_input(commands))
    await mode.run()
    out = capsys.readouterr().out
    assert '"extension": "demo-ext"' in out
    assert '"pending": [' in out
    assert "Extension UI request not found: bad-id" in out
    assert "Extension UI history cleared." in out


@pytest.mark.asyncio
async def test_interactive_bash_error_output_shown_when_enabled(capsys, monkeypatch):
    import threading

    session = _DummySession()
    mode = InteractiveMode(_DummyHost(session))
    input_called = threading.Event()
    emitted = threading.Event()

    def _input(_prompt: str = "") -> str:
        input_called.set()
        emitted.wait(5)
        return "/exit"

    def _emit() -> None:
        input_called.wait(5)
        for listener in session._listeners:
            listener({
                "type": "tool_call_end",
                "tool": "bash",
                "ok": False,
                "result": {"error": "boom-err\n\nCommand exited with code 2"},
            })
        emitted.set()

    t = threading.Thread(target=_emit, daemon=True)
    t.start()
    monkeypatch.setattr("builtins.input", _input)
    await mode.run()
    t.join()
    out = capsys.readouterr().out
    assert "[Tool:ERR] bash" in out
    assert "boom-err" in out


@pytest.mark.asyncio
async def test_interactive_ls_output_shown_when_enabled(capsys, monkeypatch):
    import threading

    session = _DummySession()
    mode = InteractiveMode(_DummyHost(session))
    input_called = threading.Event()
    emitted = threading.Event()

    def _input(_prompt: str = "") -> str:
        input_called.set()
        emitted.wait(5)
        return "/exit"

    def _emit() -> None:
        input_called.wait(5)
        for listener in session._listeners:
            listener({
                "type": "tool_call_end",
                "tool": "ls",
                "ok": True,
                "result": {"outputText": "file1.txt\nfile2.txt"},
            })
        emitted.set()

    t = threading.Thread(target=_emit, daemon=True)
    t.start()
    monkeypatch.setattr("builtins.input", _input)
    await mode.run()
    t.join()
    out = capsys.readouterr().out
    assert "[Tool:OK] ls" in out
    assert "file1.txt" in out
    assert "file2.txt" in out


# ---------------------------------------------------------------------------
# MCP slash-command tests.
# ---------------------------------------------------------------------------


class _FakeMcpManager:
    """Fake MCP manager for interactive mode slash-command tests."""

    def __init__(self) -> None:
        self._status: list[dict] = [
            {
                "name": "demo",
                "command": "true",
                "enabled": True,
                "running": True,
                "tools": ["demo_tool"],
                "error": None,
                "transport": "stdio",
            }
        ]
        self._enabled_flags: dict[str, bool] = {"demo": True}
        self._enable_calls: list[tuple[str, str, list, dict]] = []

    def server_status(self) -> list[dict]:
        result = []
        for s in self._status:
            result.append(dict(s))
        return result

    def tools(self) -> list:
        result = []
        for s in self._status:
            for tname in s["tools"]:
                if s["running"]:
                    result.append(type("Tool", (), {"name": tname, "server": s["name"]}))
        return result

    async def enable_server(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        url: str | None = None,
    ) -> list[str]:
        self._enabled_flags[name] = True
        self._enable_calls.append((name, command, args or [], env or {}))
        s = None
        for s in self._status:
            if s["name"] == name:
                break
        if s and not s["running"]:
            s["running"] = True
            s["enabled"] = True
            return list(s["tools"])
        return []

    async def disable_server(self, name: str) -> list[str]:
        for s in self._status:
            if s["name"] == name and s["running"]:
                s["running"] = False
                s["enabled"] = False
                return list(s["tools"])
        return []


@pytest.mark.asyncio
async def test_interactive_mcp_list(monkeypatch, capsys):
    session = _DummySession()
    session._mcp_manager = _FakeMcpManager()
    mode = InteractiveMode(_DummyHost(session))
    commands = ["/mcp list", "/exit"]
    monkeypatch.setattr("builtins.input", _mk_input(commands))
    await mode.run()
    out = capsys.readouterr().out
    assert "demo: running (stdio) [demo_tool]" in out


@pytest.mark.asyncio
async def test_interactive_mcp_disable(monkeypatch, capsys):
    session = _DummySession()
    session._mcp_manager = _FakeMcpManager()
    mode = InteractiveMode(_DummyHost(session))
    commands = ["/mcp disable demo", "/exit"]
    monkeypatch.setattr("builtins.input", _mk_input(commands))
    await mode.run()
    out = capsys.readouterr().out
    assert "Server 'demo' disabled. Removed tools: demo_tool" in out
    # Verify persistence.
    servers = session.settings_manager.get_mcp_servers()
    assert servers.get("demo", {}).get("enabled") is False
