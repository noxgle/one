from __future__ import annotations

import json
from pathlib import Path

import pytest

from one.modes.tui_mode import (
    BUILTIN_TUI_THEMES,
)
from tests.support.tui import _mk_app_session, _submit


class _LoginStub:
    """Phase 11: fake provider adapter for /login tests (no network)."""

    def __init__(
        self,
        models: list[str] | None = None,
        error: Exception | None = None,
        chat_error: Exception | None = None,
    ) -> None:
        self.models = models
        self.error = error
        self.chat_error = chat_error

    async def list_models(self, api_key: str, headers: dict | None = None) -> list[str] | None:
        if self.error is not None:
            raise self.error
        return self.models

    async def chat(self, api_key, model, messages, thinking_level, headers=None, on_delta=None, on_thinking_delta=None, max_tokens=None, images=None, storage_dir=""):
        if self.chat_error is not None:
            raise self.chat_error
        return None


class _FakeMcpManager:
    """Fake MCP manager for slash-command tests."""

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


def _mk_providers_session(tmp_path: Path):
    """Session whose registry has one keyed provider with two models."""
    session = _mk_app_session(tmp_path)
    session.model_registry._auth.set_runtime_api_key("prov-a", "k1")
    session.model_registry.register_models("prov-a", ["m-1", "m-2"])
    return session


@pytest.mark.asyncio
async def test_tui_command_input_has_static_cursor(tmp_path: Path) -> None:
    """The prompt cursor stays visible but does not blink."""
    from one.modes import tui_mode

    app = tui_mode._OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#input").cursor_blink is False


@pytest.mark.asyncio
async def test_tui_command_help_lists_all_commands(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/help")
        stream = "\n".join(app._stream_lines)
        for token in [
            "/stats",
            "/state",
            "/tools",
            "/model-cycle",
            "/thinking-cycle",
            "/steer <text>",
            "/follow <text>",
            "/compact [instructions]",
            "/tree",
            "/navigate <id> [--summary <text>]",
            "/fork <id>",
            "/login [status|refresh <provider>|provider [apiKey] [model] [subscription]]",
            "/logout <provider>",
            "/retry <on|off|unlimited>",
            "/skill [list]",
            "/skill:<name> [args]",
            "/config [key] [value]",
            "/extui <list|request|respond|cancel|clear>",
            "/cooperation [on|off]",
            "/mcp [list|enable|disable]",
            "/bash <command>",
            "/paste-image",
        ]:
            assert token in stream, token


@pytest.mark.asyncio
async def test_tui_skill_commands_list_and_invoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Skill command listings accept the documented syntax and preserve invocation."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    monkeypatch.setattr(
        session.resource_loader,
        "get_skills",
        lambda: {"skills": [{"name": "demo", "description": "Demo skill"}]},
        raising=False,
    )
    invoked: list[tuple[str, str]] = []

    async def invoke_skill(name: str, args: str) -> dict[str, object]:
        invoked.append((name, args))
        return {"ok": True, "bodyLength": 42}

    monkeypatch.setattr(session, "invoke_skill", invoke_skill)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        for command in ("/skill", "/skill list", "/skill:"):
            await _submit(app, pilot, command)
        await _submit(app, pilot, "/skill:demo example args")

    stream = "\n".join(app._stream_lines)
    assert stream.count("Available skills:") == 3
    assert invoked == [("demo", "example args")]
    assert "Skill 'demo' loaded (42 chars)." in stream


@pytest.mark.asyncio
async def test_tui_skill_listing_handles_empty_discovery_and_whitespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    monkeypatch.setattr(session.resource_loader, "get_skills", lambda: {"skills": []}, raising=False)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        for command in ("/skill", "/skill list", "/skill: \t"):
            await app._handle_command(command)

    assert "\n".join(app._stream_lines).count("No skills discovered.") == 3


@pytest.mark.asyncio
async def test_tui_skill_invocation_error_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)

    async def invoke_skill(name: str, args: str) -> dict[str, object]:
        return {"ok": False, "errorType": "BusySessionError" if name == "busy" else "SkillError", "error": "failed"}

    monkeypatch.setattr(session, "invoke_skill", invoke_skill)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/skill:missing")
        await _submit(app, pilot, "/skill:busy")

    stream = "\n".join(app._stream_lines)
    assert "Skill error: failed" in stream
    assert "BusySessionError: failed" in stream


@pytest.mark.asyncio
async def test_tui_paste_image_command_uses_shortcut_action_and_preserves_errors(tmp_path: Path, monkeypatch):
    from one.core import clipboard_image
    from one.modes.tui_mode import _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    toasts: list[tuple[str, str]] = []
    image = b"clipboard-image"
    clipboard_values = iter([image, None])
    monkeypatch.setattr(clipboard_image, "acquire_clipboard_image", lambda: next(clipboard_values))
    monkeypatch.setattr(app, "_toast", lambda message, severity="info": toasts.append((message, severity)))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._handle_command("/paste-image")
        assert app._pending_clipboard_image_bytes == image
        await app._handle_command("/paste-image")
        assert toasts == [
            ("Image queued — submit to attach", "info"),
            ("No image data in clipboard", "warning"),
        ]


@pytest.mark.asyncio
async def test_tui_command_stats_state_tools(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/stats")
        await _submit(app, pilot, "/state")
        await _submit(app, pilot, "/tools")
        stream = "\n".join(app._stream_lines)
        assert '"userMessages"' in stream
        assert '"thinkingLevel": "medium"' in stream
        assert '"sessionId"' in stream
        assert '"pendingQueues"' in stream
        assert '"tools"' in stream


@pytest.mark.asyncio
async def test_tui_command_model_cycle_and_thinking_cycle(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path, runtime_key="dummy")
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/model-cycle")
        await _submit(app, pilot, "/thinking-cycle")
        stream = "\n".join(app._stream_lines)
        assert "Model cycled to" in stream
        assert "Thinking level cycled to high" in stream
        assert session.thinking_level == "high"
        assert session.settings_manager.get_default_thinking_level() == "high"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("level", "reasoning"),
    [
        ("off", None),
        ("minimal", {"effort": "low", "summary": "auto"}),
        ("low", {"effort": "low", "summary": "auto"}),
        ("medium", {"effort": "medium", "summary": "auto"}),
        ("high", {"effort": "high", "summary": "auto"}),
        ("xhigh", {"effort": "high", "summary": "auto"}),
    ],
)
async def test_tui_thinking_uses_provider_native_payloads(
    tmp_path: Path, level: str, reasoning: dict[str, str] | None
) -> None:
    """The TUI command's persisted level reaches each provider wire dialect."""
    from one.modes.tui_mode import _OneTextualApp
    from one.providers.codex_responses import CodexResponsesAdapter
    from one.providers.ollama import OllamaCloudAdapter
    from one.providers.openai_compatible import OpenAICompatibleAdapter

    session = _mk_app_session(tmp_path, runtime_key="dummy")
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, f"/thinking {level}")
        assert session.thinking_level == level

    messages = [{"role": "user", "content": "hi"}]
    codex = CodexResponsesAdapter()._build_payload("gpt", messages, session.thinking_level, stream=True)
    openrouter = OpenAICompatibleAdapter(
        "openrouter", "https://openrouter.ai/api", reasoning_mode="openrouter"
    )._build_payload("provider/model", messages, session.thinking_level)
    ollama_cloud = OllamaCloudAdapter("https://ollama.com")._build_payload(
        "glm-5:cloud", messages, session.thinking_level
    )
    llama_cpp = OpenAICompatibleAdapter(
        "llama.cpp", "http://localhost:8080", supports_reasoning_effort=False,
        default_temperature=None,
    )._build_payload("local", messages, session.thinking_level)

    assert codex.get("reasoning") == reasoning
    openrouter_effort = {
        "minimal": "minimal", "low": "low", "medium": "medium",
        "high": "high", "xhigh": "high",
    }.get(level)
    assert openrouter.get("reasoning") == (
        {"effort": openrouter_effort} if openrouter_effort else None
    )
    assert "reasoning_effort" not in openrouter
    assert ollama_cloud["think"] == (False if level == "off" else ("low" if level == "minimal" else level if level != "xhigh" else "high"))
    assert "reasoning_effort" not in llama_cpp
    assert llama_cpp.get("chat_template_kwargs") == (
        {"enable_thinking": False} if level == "off" else None
    )


@pytest.mark.asyncio
async def test_tui_thinking_command_reports_capability_coerced_level(tmp_path: Path) -> None:
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path, runtime_key="dummy")
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/model llama.cpp/local")
        await _submit(app, pilot, "/thinking high")
        assert session.thinking_level == "off"
        assert "Thinking level set to off" in "\n".join(app._stream_lines)


@pytest.mark.asyncio
async def test_tui_command_model_provider_only_and_alias(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Provider-only auto-picks the first registered model.
        await _submit(app, pilot, "/model openai")
        stream = "\n".join(app._stream_lines)
        assert "Model set to openai/" in stream
        # /tc alias for /thinking-cycle.
        await _submit(app, pilot, "/tc")
        stream = "\n".join(app._stream_lines)
        assert "Thinking level cycled to high" in stream


@pytest.mark.asyncio
async def test_tui_command_steer_follow_compact_tree(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/steer abc")
        await _submit(app, pilot, "/follow def")
        await _submit(app, pilot, "/compact now")
        await _submit(app, pilot, "/tree")
        stream = "\n".join(app._stream_lines)
        assert "Queued steering message." in stream
        assert "Queued follow-up message." in stream
        assert "Compaction skipped: the history is already within the configured limit." in stream
        assert '"skipped"' not in stream
        # The tree renders session entries with a '*' marker on the leaf.
        assert "model_change" in stream
        assert "* thinking_level_change" in stream
        assert session.get_pending_queues()["steering"] == ["abc"]
        assert session.get_pending_queues()["followUp"] == ["def"]


@pytest.mark.asyncio
async def test_tui_manual_compact_short_history_with_custom_instructions(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(
        tmp_path,
        settings_override={"compaction": {"summarizeWithModel": False}},
    )
    for role, content in [("user", "Please inspect the parser."), ("assistant", "I will inspect it.")]:
        session.session_manager.append_message({"role": role, "content": content})
    session.messages = session.session_manager.build_session_context()["messages"]
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/compact Preserve the parser investigation.")
        stream = "\n".join(app._stream_lines)
        assert "Compaction complete." in stream
        assert "Summary:" in stream
        assert "Preserve the parser investigation." in stream
        assert "Tokens before:" in stream
        assert "Messages kept:" in stream
        assert '"tokensBefore"' not in stream
        assert '"tokensBefore": 0' not in stream
        assert session.session_manager.get_last_compaction() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            {"aborted": True, "summary": "", "tokensBefore": 0, "kept": 0, "skipped": False},
            "Compaction aborted.",
        ),
        (
            {"aborted": False, "summary": "", "tokensBefore": 0, "kept": 0, "skipped": True, "busy": True},
            "Compaction skipped: the session is busy.",
        ),
    ],
)
async def test_tui_compact_renders_non_success_results(
    tmp_path: Path, result: dict[str, object], expected: str
) -> None:
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)

    async def fake_compact(_instructions: str | None = None) -> dict[str, object]:
        return result

    session.compact = fake_compact  # type: ignore[method-assign]
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/compact")
        stream = "\n".join(app._stream_lines)
        assert expected in stream
        assert '"aborted"' not in stream


@pytest.mark.asyncio
async def test_tui_command_login_inline_stores_key(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session.providers = {"openai": _LoginStub(models=[])}
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/login openai sk-test gpt-4.1")
        stream = "\n".join(app._stream_lines)
        assert "Stored key for openai." in stream
        assert "Default model set to gpt-4.1." in stream
        keys = session.model_registry._auth._data.get("apiKeys", {})
        assert keys.get("openai") == "sk-test"
        assert session.settings_manager.merged().get("defaultProvider") == "openai"
        assert session.settings_manager.merged().get("defaultModel") == "gpt-4.1"
        assert session.model.id == "gpt-4.1"


@pytest.mark.asyncio
async def test_tui_command_login_pending_flow_via_input(tmp_path: Path):
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session.providers = {"anthropic": _LoginStub(error=NotImplementedError())}
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/login anthropic")
        assert app._login_pending is not None
        assert app._login_pending["provider"] == "anthropic"
        stream = "\n".join(app._stream_lines)
        assert "API key is required for anthropic" in stream
        input_widget = app.query_one("#input", TextArea)
        assert input_widget.placeholder == "API key for anthropic:"

        input_widget.text = "sk-ant-test"
        await input_widget.action_submit()
        await pilot.pause()

        assert app._login_pending is None
        keys = session.model_registry._auth._data.get("apiKeys", {})
        assert keys.get("anthropic") == "sk-ant-test"
        stream = "\n".join(app._stream_lines)
        assert "Stored key for anthropic." in stream


@pytest.mark.asyncio
async def test_tui_command_login_pending_cancel_on_empty(tmp_path: Path):
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/login gemini")
        assert app._login_pending is not None
        input_widget = app.query_one("#input", TextArea)
        input_widget.text = ""
        await input_widget.action_submit()
        await pilot.pause()
        assert app._login_pending is None
        stream = "\n".join(app._stream_lines)
        assert "API key is required for gemini" in stream


@pytest.mark.asyncio
async def test_tui_command_logout_and_cooperation(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session.providers = {"openai": _LoginStub(models=[])}
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/login openai sk-test")
        await _submit(app, pilot, "/logout openai")
        stream = "\n".join(app._stream_lines)
        assert "Removed credentials for openai locally." in stream
        keys = session.model_registry._auth._data.get("apiKeys", {})
        assert "openai" not in keys

        await _submit(app, pilot, "/cooperation")
        stream = "\n".join(app._stream_lines)
        assert '"enabled": false' in stream
        await _submit(app, pilot, "/cooperation on")
        assert session.approval_callback is not None
        assert session.settings_manager.get_tool_approval() is True
        await _submit(app, pilot, "/cooperation")
        stream = "\n".join(app._stream_lines)
        assert '"enabled": true' in stream
        await _submit(app, pilot, "/cooperation off")
        assert session.approval_callback is None
        assert session.settings_manager.get_tool_approval() is False


@pytest.mark.asyncio
async def test_tui_command_bash_echo(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/bash echo hi")
        stream = "\n".join(app._stream_lines)
        assert "hi" in stream


@pytest.mark.asyncio
async def test_tui_command_providers_lists_no_auth_without_keys(tmp_path: Path):
    """NO_AUTH providers (llama.cpp, ollama) count as logged-in without keys."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)  # no keys at all
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/providers")
        stream = "\n".join(app._stream_lines)
        assert "Logged-in providers:" in stream
        assert "llama.cpp" in stream
        assert "ollama" in stream


@pytest.mark.asyncio
async def test_tui_command_providers_lists_logged_in_only(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_providers_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/providers")
        stream = "\n".join(app._stream_lines)
        assert "Logged-in providers:" in stream
        # sorted: llama.cpp, ollama, prov-a; keyed providers without keys are absent.
        assert "1. llama.cpp" in stream
        assert "2. ollama" in stream
        assert "3. prov-a   2 models" in stream
        assert "openai" not in stream.split("Logged-in providers:")[-1].split("Usage:")[0]


@pytest.mark.asyncio
async def test_tui_command_providers_second_step_and_switch(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_providers_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/providers prov-a")
        stream = "\n".join(app._stream_lines)
        assert "prov-a models:" in stream
        assert "  1. m-1" in stream
        assert "  2. m-2" in stream
        assert session.model.id == "gpt-4.1"  # second step does not switch

        await _submit(app, pilot, "/providers 3 2")
        stream = "\n".join(app._stream_lines)
        assert "Model set to prov-a/m-2 (saved as default)" in stream
        assert session.model.provider == "prov-a"
        assert session.model.id == "m-2"
        assert session.settings_manager.merged().get("defaultProvider") == "prov-a"
        assert session.settings_manager.merged().get("defaultModel") == "m-2"

        await _submit(app, pilot, "/providers prov-a m-1")
        stream = "\n".join(app._stream_lines)
        assert "Model set to prov-a/m-1 (saved as default)" in stream
        assert session.model.id == "m-1"


@pytest.mark.asyncio
async def test_tui_command_providers_error_paths(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_providers_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/providers nope")
        stream = "\n".join(app._stream_lines)
        assert "Provider not logged in or unknown: nope (see /providers)" in stream

        await _submit(app, pilot, "/providers prov-a 99")
        stream = "\n".join(app._stream_lines)
        assert "Model not found for prov-a: 99 (see /providers prov-a)" in stream

        await _submit(app, pilot, "/providers 9 x")
        stream = "\n".join(app._stream_lines)
        assert "Provider not logged in or unknown: 9 (see /providers)" in stream

        assert session.model.id == "gpt-4.1"


def test_slash_commands_include_providers():
    from one.modes.tui_mode import _SLASH_COMMANDS

    assert "/providers" in _SLASH_COMMANDS
    assert "/skill" in _SLASH_COMMANDS
    assert "/skill list" in _SLASH_COMMANDS


@pytest.mark.asyncio
async def test_tui_action_help_lists_providers(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_help()
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "/providers" in stream
        assert "/login [status|refresh <provider>|provider [apiKey] [model] [subscription]]" in stream


@pytest.mark.asyncio
async def test_ctrl_p_palette_lists_only_current_one_commands(tmp_path: Path):
    from textual.command import CommandPalette

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        commands = app.get_system_commands(app.screen)
        names = [name for name, _help, _callback, _discover in commands]

        assert names.count("Theme") == 0
        assert "Keys" not in names
        assert "Show one keyboard shortcuts" in names
        assert {name.removeprefix("Theme: ") for name in names if name.startswith("Theme: ")} == set(
            BUILTIN_TUI_THEMES
        )

        # Textual owns the Ctrl+P binding; invoking the bound action directly
        # keeps this regression test independent of TextArea key handling.
        app.action_command_palette()
        await pilot.pause()
        assert isinstance(app.screen, CommandPalette)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, CommandPalette)


@pytest.mark.asyncio
async def test_tui_shortcuts_panel_and_palette_theme_use_one_state(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_shortcuts()
        overlay = app.query_one("#shortcuts_overlay")
        assert overlay.has_class("visible")
        assert "Ctrl+P command palette" in str(overlay.content)

        await pilot.press("escape")
        assert not overlay.has_class("visible")

        commands = app.get_system_commands(app.screen)
        callback = next(callback for name, _help, callback, _discover in commands if name == "Theme: fallout")
        callback()
        await pilot.pause()
        assert app._theme.name == "fallout"
        assert session.settings_manager.get_theme() == "fallout"


@pytest.mark.asyncio
async def test_tui_login_refresh_fetches_and_persists(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session.model_registry._auth.set_runtime_api_key("prov-a", "k1")
    session.providers = {"prov-a": _LoginStub(models=["m-1", "m-2"])}
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/login refresh prov-a")
        stream = "\n".join(app._stream_lines)
        assert "Authorized. Fetched 2 models (2 new)." in stream
        assert "  1. m-1" in stream
        assert "Pick with /providers prov-a <model-number|id>" in stream
        assert session.model_registry.find("prov-a", "m-1") is not None
        data = json.loads((tmp_path / "models.json").read_text(encoding="utf-8"))
        assert {m["id"] for m in data["providers"]["prov-a"]} >= {"m-1", "m-2"}


@pytest.mark.asyncio
async def test_tui_login_refresh_missing_key_and_unknown_adapter(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)  # no keys stored
    session.providers = {"openai": _LoginStub(models=[])}
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/login refresh openai")
        stream = "\n".join(app._stream_lines)
        assert "No API key found for openai" in stream

        await _submit(app, pilot, "/login refresh nope")
        stream = "\n".join(app._stream_lines)
        assert "Provider adapter not found for nope." in stream


@pytest.mark.asyncio
async def test_tui_command_mcp_list(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session._mcp_manager = _FakeMcpManager()
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/mcp list")
        stream = "\n".join(app._stream_lines)
        assert "demo: running (stdio) [demo_tool]" in stream


@pytest.mark.asyncio
async def test_tui_command_mcp_disable(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session._mcp_manager = _FakeMcpManager()
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/mcp disable demo")
        stream = "\n".join(app._stream_lines)
        assert "Server 'demo' disabled. Removed tools: demo_tool" in stream
        # Verify persistence: enabled flag stored in settings.
        servers = session.settings_manager.get_mcp_servers()
        assert servers.get("demo", {}).get("enabled") is False


@pytest.mark.asyncio
async def test_tui_command_mcp_enable(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    # Pre-configure the server so /mcp enable demo finds it.
    session.settings_manager.set_config_value("mcpServers.demo", {"command": "true"})
    # Start the fake manager with the server not running so enable actually starts it.
    fake = _FakeMcpManager()
    for s in fake._status:
        if s["name"] == "demo":
            s["running"] = False
            s["enabled"] = False
    session._mcp_manager = fake
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/mcp enable demo")
        stream = "\n".join(app._stream_lines)
        assert "Server 'demo' enabled. Tools: demo_tool" in stream
        # Verify the fake manager was called with correct args.
        calls = session._mcp_manager._enable_calls
        assert len(calls) == 1
        assert calls[0][0] == "demo"
        assert calls[0][1] == "true"
        # Verify persistence: enabled flag stored in settings.
        servers = session.settings_manager.get_mcp_servers()
        assert servers.get("demo", {}).get("enabled") is True


@pytest.mark.asyncio
async def test_tui_command_mcp_enable_no_config(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session._mcp_manager = _FakeMcpManager()
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/mcp enable ghost")
        stream = "\n".join(app._stream_lines)
        assert "No MCP config for 'ghost'" in stream


@pytest.mark.asyncio
async def test_tui_command_retry_and_config(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/retry off")
        stream = "\n".join(app._stream_lines)
        assert "Auto-retry set to off." in stream
        assert session.settings_manager.get_retry_enabled() is False

        await _submit(app, pilot, "/config tools.maxSteps 9")
        stream = "\n".join(app._stream_lines)
        assert "Updated tools.maxSteps." in stream
        await _submit(app, pilot, "/config tools.maxSteps")
        stream = "\n".join(app._stream_lines)
        assert '"value": 9' in stream
        assert session.settings_manager.merged()["tools"]["maxSteps"] == 9


@pytest.mark.asyncio
async def test_tui_command_extui_request_and_cancel(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, '/extui request test-ext widget {"q":1}')
        await pilot.pause()
        assert app._extension_ui_pending_request is not None
        req_id = app._extension_ui_pending_request["id"]
        stream = "\n".join(app._stream_lines)
        assert "[ExtUI] test-ext (widget)" in stream
        # Drop the pending state so the cancel command is not eaten by the
        # input router (the /extui cancel path mirrors interactive parity).
        app._extension_ui_pending_request = None
        await _submit(app, pilot, f"/extui cancel {req_id}")
        assert session._extension_ui_history[-1]["cancelled"] is True
        await _submit(app, pilot, "/extui list")
        stream = "\n".join(app._stream_lines)
        assert '"pending": []' in stream


@pytest.mark.asyncio
async def test_tui_slash_completion_tab_cycles(tmp_path: Path):
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input", TextArea)
        input_widget.focus()
        input_widget.text = "/mo"
        input_widget.move_cursor((0, 3))  # cursor at end after setting text
        await pilot.press("tab")
        assert input_widget.text == "/model"
        await pilot.press("tab")
        assert input_widget.text == "/model-cycle"
        await pilot.press("tab")
        assert input_widget.text == "/model"
        stream = "\n".join(app._stream_lines)
        assert "[completion] /model /model-cycle" in stream


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["ordinary text", "/not-a-command"])
async def test_tui_tab_without_completion_keeps_command_input_focused(tmp_path: Path, text: str):
    """Tab must not fall through to Textual's default focus traversal."""
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input", TextArea)
        input_widget.focus()
        input_widget.text = text
        input_widget.move_cursor((0, len(text)))

        await pilot.press("tab")

        assert app.focused is input_widget
        assert input_widget.text == text


@pytest.mark.asyncio
async def test_tui_slash_completion_single_match(tmp_path: Path):
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input", TextArea)
        input_widget.focus()
        input_widget.text = "/ne"
        input_widget.move_cursor((0, 3))  # cursor at end after setting text
        await pilot.press("tab")
        assert input_widget.text == "/new"
        stream = "\n".join(app._stream_lines)
        assert "[completion]" not in stream


@pytest.mark.asyncio
async def test_tui_slash_completion_includes_skill_list(tmp_path: Path):
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input", TextArea)
        input_widget.focus()
        input_widget.text = "/skill"
        input_widget.move_cursor((0, 6))
        await pilot.press("tab")
        assert input_widget.text == "/skill"
        await pilot.press("tab")
        assert input_widget.text == "/skill list"


@pytest.mark.asyncio
async def test_tui_slash_completion_resets_after_edit(tmp_path: Path):
    """Completing one command must not lock stale matches for a later different prefix."""
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input", TextArea)
        input_widget.focus()
        input_widget.text = "/mo"
        input_widget.move_cursor((0, 3))
        await pilot.press("tab")
        assert input_widget.text == "/model"
        # Type a different command: the completion cycle must reset.
        input_widget.text = "/ne"
        input_widget.move_cursor((0, 3))
        await pilot.press("tab")
        assert input_widget.text == "/new"


@pytest.mark.asyncio
async def test_tui_retry_cycle_command(tmp_path: Path):
    """/retry-cycle cycles through all three modes."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Start: on
        assert session.settings_manager.get_retry_mode() == "on"
        await _submit(app, pilot, "/retry-cycle")
        stream = "\n".join(app._stream_lines)
        assert "Auto-retry set to unlimited." in stream
        assert session.settings_manager.get_retry_mode() == "unlimited"

        await _submit(app, pilot, "/retry-cycle")
        stream = "\n".join(app._stream_lines)
        assert "Auto-retry set to off." in stream
        assert session.settings_manager.get_retry_mode() == "off"

        await _submit(app, pilot, "/retry-cycle")
        stream = "\n".join(app._stream_lines)
        assert "Auto-retry set to on." in stream
        assert session.settings_manager.get_retry_mode() == "on"


def _persistent_tui_session(tmp_path: Path, name: str, message: str):
    from one.core.agent_session import AgentSession
    from one.core.session_manager import SessionManager

    template = _mk_app_session(tmp_path)
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    manager.set_session_name(name)
    manager.append_message({"role": "user", "content": message})
    return AgentSession(
        manager,
        template.settings_manager,
        template.model_registry,
        template.resource_loader,
        template.model,
        "medium",
    )


@pytest.mark.asyncio
async def test_tui_sessions_lists_renames_and_confirms_delete(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _persistent_tui_session(tmp_path, "First session", "first")
    other = _persistent_tui_session(tmp_path, "Second session", "second")
    assert other.session_file is not None
    other_path = Path(other.session_file)
    sidecar = Path(str(other_path) + ".evidence")
    sidecar.write_text("sidecar\n", encoding="utf-8")
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/sessions")
        stream = "\n".join(app._stream_lines)
        assert "First session" in stream and "Second session" in stream
        await _submit(app, pilot, "/sessions rename First session Renamed session")
        assert "Renamed session" in "\n".join(app._stream_lines)
        await _submit(app, pilot, "/sessions delete Second session")
        await _submit(app, pilot, "no")
        assert other_path.exists()
        await _submit(app, pilot, "/sessions delete Second session")
        await _submit(app, pilot, "yes")
        assert not other_path.exists()
        assert not sidecar.exists()


@pytest.mark.asyncio
async def test_tui_sessions_load_exact_name_and_ignore_old_listener(tmp_path: Path):
    from one.core.agent_session import AgentSession
    from one.core.session_manager import SessionManager
    from one.modes.tui_mode import _OneTextualApp

    first = _persistent_tui_session(tmp_path, "First session", "first transcript")
    second = _persistent_tui_session(tmp_path, "Second session", "second transcript")
    second.session_manager.append_custom_message("compaction_summary", "compaction transcript", True)
    second.session_manager.append_branch_summary(second.session_manager.get_leaf_id(), "branch transcript")

    class Host:
        def __init__(self):
            self.session = first

        async def switch_session(self, path: str):
            manager = SessionManager.open(path, self.session.session_manager.session_dir)
            self.session = AgentSession(
                manager,
                first.settings_manager,
                first.model_registry,
                first.resource_loader,
                first.model,
                "medium",
            )

        async def new_session(self, options):  # noqa: ARG002
            manager = SessionManager.create(str(tmp_path), self.session.session_manager.session_dir)
            self.session = AgentSession(
                manager,
                first.settings_manager,
                first.model_registry,
                first.resource_loader,
                first.model,
                "medium",
            )

    host = Host()
    app = _OneTextualApp(first, runtime_host=host)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/sessions Second session")
        assert app.session.session_id == second.session_id
        assert "second transcript" in "\n".join(app._stream_lines)
        assert "compaction transcript" in "\n".join(app._stream_lines)
        assert "branch transcript" in "\n".join(app._stream_lines)
        first_number = next(str(i) for i, info in enumerate(app._session_infos(), 1) if info.id == first.session_id)
        await _submit(app, pilot, f"/sessions {first_number}")
        assert app.session.session_id == first.session_id
        first._emit({"type": "message_end", "message": {"role": "assistant", "content": "stale old event"}})
        await pilot.pause()
        assert "stale old event" not in "\n".join(app._stream_lines)
        await _submit(app, pilot, "/sessions Second")
        assert "No session has that exact name" in "\n".join(app._stream_lines)
        first_path = Path(first.session_file or "")
        await _submit(app, pilot, "/sessions delete First session")
        await _submit(app, pilot, "yes")
        assert not first_path.exists()
        assert app.session.session_id != first.session_id


@pytest.mark.asyncio
async def test_tui_session_switch_discards_queued_old_transcript_events(tmp_path: Path):
    from one.core.agent_session import AgentSession
    from one.core.session_manager import SessionManager
    from one.modes.tui_mode import _OneTextualApp

    first = _persistent_tui_session(tmp_path, "First session", "first transcript")
    second = _persistent_tui_session(tmp_path, "Second session", "selected transcript")

    class Host:
        def __init__(self):
            self.session = first

        async def switch_session(self, path: str):
            manager = SessionManager.open(path, self.session.session_manager.session_dir)
            self.session = AgentSession(
                manager,
                first.settings_manager,
                first.model_registry,
                first.resource_loader,
                first.model,
                "medium",
            )

    host = Host()
    app = _OneTextualApp(first, runtime_host=host)
    async with app.run_test() as pilot:
        await pilot.pause()
        first._emit({"type": "message_start", "message": {"role": "assistant"}})
        first._emit(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "Request aborted."},
            }
        )
        first._emit({"type": "message_end", "message": {"role": "assistant", "content": "Request aborted."}})
        assert app._pending_ui_events

        target = next(info for info in app._session_infos() if info.id == second.session_id)
        await app._switch_to_session(target)
        await pilot.pause()

        stream = "\n".join(app._stream_lines)
        assert "Request aborted." not in stream
        assert "selected transcript" in stream
        assert "Loaded session: Second session." in stream

        first._emit({"type": "message_end", "message": {"role": "assistant", "content": "stale old event"}})
        await pilot.pause()
        assert "stale old event" not in "\n".join(app._stream_lines)


@pytest.mark.asyncio
async def test_tui_sessions_target_names_precede_indexes_and_rename_is_unambiguous(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _persistent_tui_session(tmp_path, "Current", "current")
    numeric_name = _persistent_tui_session(tmp_path, "42", "numeric name")
    _persistent_tui_session(tmp_path, "Project", "short")
    _persistent_tui_session(tmp_path, "Project Notes", "long")
    _persistent_tui_session(tmp_path, "Name With Spaces", "spaces")
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        target, error = app._resolve_session_target("42")
        assert error is None
        assert target is not None and target.id == numeric_name.session_id
        indexed, error = app._resolve_session_target("1")
        assert error is None
        assert indexed is not None and indexed.id == app._session_infos()[0].id
        _persistent_tui_session(tmp_path, "42", "duplicate numeric name")
        target, error = app._resolve_session_target("42")
        assert target is None
        assert error == "Session name is ambiguous; use its number from /sessions."

        await _submit(app, pilot, "/sessions rename Project Notes renamed")
        assert "Session rename target is ambiguous" in "\n".join(app._stream_lines)
        await _submit(app, pilot, "/sessions rename Name With Spaces renamed")
        assert any(info.name == "renamed" for info in app._session_infos())


@pytest.mark.asyncio
async def test_tui_sessions_rename_is_blocked_while_streaming(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _persistent_tui_session(tmp_path, "Current", "current")
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._is_streaming = True
        await _submit(app, pilot, "/sessions rename Current renamed")
        assert "before renaming" in "\n".join(app._stream_lines)
        assert session.session_manager.get_session_name() == "Current"
