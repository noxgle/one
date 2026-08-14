from __future__ import annotations

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

    def set_stored_api_key(self, provider: str, api_key: str) -> None:
        self.stored_keys[provider] = api_key

    def remove_stored_api_key(self, provider: str) -> None:
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
    assert "Removed stored key for openai." in out
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
