from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from one.cli.args import parse_args
from one.modes.tui_mode import (
    BUILTIN_TUI_THEMES,
    _THINKING_FRAMES,
    _THINKING_MARK,
    advance_thinking_frame,
    build_sidebar_snapshot,
    evaluate_waiting,
    resolve_tui_theme,
)


class _DummyModel:
    provider = "openrouter"
    id = "google/gemma-4-31b-it:free"


class _DummySessionManager:
    cwd = "/tmp/project"


class _DummySession:
    def __init__(self) -> None:
        self.model = _DummyModel()
        self.thinking_level = "medium"
        self.session_manager = _DummySessionManager()
        self.session_id = "s-test"
        self.is_streaming = False
        self.is_compacting = False
        self._plan = None

    def get_context_usage(self) -> dict:
        return {"percent": 12.5}

    def get_pending_queues(self) -> dict:
        return {"steering": [1, 2], "followUp": [3]}

    def get_session_stats(self) -> dict:
        return {
            "tokens": {
                "input": 100,
                "output": 50,
                "cacheRead": 10,
                "cacheWrite": 5,
                "total": 165,
            },
            "cost": 0.0123,
        }


def test_parse_args_accepts_tui_mode() -> None:
    parsed = parse_args(["--mode", "tui"])
    assert parsed.errors == []
    assert parsed.mode == "tui"


def test_resolve_tui_theme_includes_fallout() -> None:
    fallout = resolve_tui_theme("fallout")
    assert fallout.name == "fallout"
    assert "fallout" in BUILTIN_TUI_THEMES
    assert resolve_tui_theme("no-such-theme").name == "default"


def test_build_sidebar_snapshot_contains_runtime_details() -> None:
    snapshot = build_sidebar_snapshot(
        _DummySession(),
        retry_state="retry-1",
    )

    assert snapshot["model"] == "openrouter/google/gemma-4-31b-it:free"
    assert snapshot["thinking"] == "medium"
    assert snapshot["cwd"] == "/tmp/project"
    assert snapshot["contextPercent"] == 12.5
    assert snapshot["queueSteer"] == 2
    assert snapshot["queueFollow"] == 1
    assert snapshot["queueTotal"] == 3
    assert snapshot["tokenInput"] == 100
    assert snapshot["tokenOutput"] == 50
    assert snapshot["tokenTotal"] == 165
    assert snapshot["cost"] == 0.0123
    assert snapshot["coop"] == "off"
    assert snapshot["subagents"] is True
    assert snapshot["bashOutput"] is True
    assert snapshot["mcpEnabled"] is False
    assert snapshot["mcpServers"] == []


def test_build_sidebar_snapshot_lists_enabled_mcp_servers() -> None:
    class _FakeMcpManager:
        def server_status(self) -> list[dict]:
            return [
                {"name": "demo", "enabled": True, "running": True, "tools": ["a", "b", "c"], "transport": "stdio", "error": None},
                {"name": "web", "enabled": True, "running": False, "tools": ["x"], "transport": "http", "error": "boom"},
                {"name": "old", "enabled": False, "running": False, "tools": [], "transport": "stdio", "error": None},
            ]

    session = _DummySession()
    session._mcp_manager = _FakeMcpManager()
    snapshot = build_sidebar_snapshot(session, retry_state="idle")

    assert snapshot["mcpEnabled"] is True
    assert [s["name"] for s in snapshot["mcpServers"]] == ["demo", "web"]
    assert snapshot["mcpServers"][0]["toolCount"] == 3
    assert snapshot["mcpServers"][0]["transport"] == "stdio"
    assert snapshot["mcpServers"][1]["error"] == "boom"


def test_build_sidebar_snapshot_mcp_manager_raises_is_safe() -> None:
    class _FakeMcpManagerRaises:
        def server_status(self) -> list[dict]:
            raise RuntimeError("boom")

    session = _DummySession()
    session._mcp_manager = _FakeMcpManagerRaises()
    snapshot = build_sidebar_snapshot(session, retry_state="idle")

    assert snapshot["mcpEnabled"] is True
    assert snapshot["mcpServers"] == []


@pytest.mark.asyncio
async def test_tui_sidebar_renders_mcp_off_without_manager(tmp_path: Path) -> None:
    """Verify the sidebar shows MCP section with 'off' when no _mcp_manager."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not hasattr(session, "_mcp_manager") or session._mcp_manager is None
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        s = str(sidebar.content)
        assert "MCP" in s
        assert "off" in s
        assert s.index("MCP") < s.index("Keys")


@pytest.mark.asyncio
async def test_tui_sidebar_renders_mcp_clients_list(tmp_path: Path) -> None:
    """Verify enabled MCP clients are listed as bullets; disabled excluded."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    class _McpManagerWithServers:
        def server_status(self) -> list[dict]:
            return [
                {"name": "demo", "enabled": True, "running": True, "tools": ["a", "b", "c"], "transport": "stdio", "error": None},
                {"name": "web", "enabled": True, "running": False, "tools": ["x"], "transport": "http", "error": "boom"},
                {"name": "old", "enabled": False, "running": False, "tools": [], "transport": "stdio", "error": None},
            ]

    session = _mk_app_session(tmp_path)
    session._mcp_manager = _McpManagerWithServers()
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        s = str(sidebar.content)
        assert "- demo" in s
        assert "- web" in s
        assert "- old" not in s
        assert s.index("MCP") < s.index("Keys")


@pytest.mark.asyncio
async def test_tui_prompt_queued_while_streaming(tmp_path: Path) -> None:
    """A plain prompt submitted while the agent is streaming is queued as a
    follow-up instead of failing with 'streamingBehavior is required'."""
    import asyncio

    from one.modes.tui_mode import _OneTextualApp
    from one.providers.base import ChatResult

    class _SlowStreamProvider:
        async def chat(self, api_key, model, messages, thinking_level, headers=None):
            await asyncio.sleep(1.0)
            return ChatResult(text="DONE", raw={}, usage={}, stop_reason="stop")

    session = _mk_app_session(tmp_path, runtime_key="sk-test")
    session.providers = {"openai": _SlowStreamProvider()}
    session.settings_manager.set_retry_enabled(False)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "first prompt")
        for _ in range(100):
            await pilot.pause()
            if session.is_streaming:
                break
        assert session.is_streaming
        await _submit(app, pilot, "second prompt")
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "[error]" not in stream
        assert "second prompt" in session.get_pending_queues()["followUp"]
        for _ in range(200):
            await pilot.pause()
            if not app._turn_active and not session.is_streaming:
                break
        assert "second prompt" not in session.get_pending_queues()["followUp"]


@pytest.mark.asyncio
async def test_tui_spinner_survives_queued_prompt_and_resumes_for_followup(tmp_path: Path) -> None:
    """The waiting spinner must stay armed when a prompt is queued mid-turn and
    must reappear when the session starts the follow-up turn on its own."""
    import asyncio

    from one.modes.tui_mode import _OneTextualApp, _THINKING_MARK
    from one.providers.base import ChatResult

    class _SlowStreamProvider:
        async def chat(self, api_key, model, messages, thinking_level, headers=None):
            await asyncio.sleep(1.0)
            return ChatResult(text="DONE", raw={}, usage={}, stop_reason="stop")

    session = _mk_app_session(tmp_path, runtime_key="sk-test")
    session.providers = {"openai": _SlowStreamProvider()}
    session.settings_manager.set_retry_enabled(False)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "first prompt")
        for _ in range(100):
            await pilot.pause()
            if session.is_streaming:
                break
        assert session.is_streaming
        # Queue a second prompt while the first turn is still in flight.
        await _submit(app, pilot, "second prompt")
        for _ in range(10):
            await pilot.pause()
        # The queued submission must NOT clear the turn state: the first turn
        # is still running, so the spinner stays armed.
        assert app._turn_active is True

        # Track how many distinct spinner windows appear; there must be at
        # least two (turn 1 and the follow-up turn 2).
        spinner_windows = 0
        spinner_visible = False
        for _ in range(300):
            await pilot.pause()
            any_spinner = any(line.startswith(_THINKING_MARK) for line in app._stream_lines)
            if any_spinner and not spinner_visible:
                spinner_windows += 1
                spinner_visible = True
            elif not any_spinner:
                spinner_visible = False
            if not app._turn_active and not session.is_streaming:
                break
        assert spinner_windows >= 2, "spinner must appear for the running turn AND the follow-up turn"


def test_thinking_frames_are_single_width() -> None:
    assert len(_THINKING_FRAMES) >= 2
    # Each braille frame must occupy exactly one terminal cell.
    for frame in _THINKING_FRAMES:
        assert len(frame) == 1


def test_evaluate_waiting_rule() -> None:
    now = 100.0
    # No active turn -> never waiting.
    assert evaluate_waiting(False, now - 10.0, now) is False
    # Active turn, deltas streaming recently -> not waiting.
    assert evaluate_waiting(True, now - 0.2, now) is False
    # Active turn, no deltas for a while -> waiting (first token / between
    # tool calls / tool execution / retry delay).
    assert evaluate_waiting(True, now - 2.0, now) is True
    # Custom idle threshold.
    assert evaluate_waiting(True, now - 0.5, now, idle_threshold=0.4) is True


def test_advance_thinking_frame_wraps() -> None:
    n = len(_THINKING_FRAMES)
    assert advance_thinking_frame(0) == 1
    assert advance_thinking_frame(n - 1) == 0
    # A few steps keep cycling.
    frame = 0
    for _ in range(3 * n):
        frame = advance_thinking_frame(frame)
    assert frame == 0


# ---------------------------------------------------------------------------
# Headless TUI app tests (extension UI flow).
# ---------------------------------------------------------------------------


class _FakeLoader:
    cwd = "/tmp/project"

    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
        return "You are a coding agent."


def _mk_app_session(tmp_path: Path, runtime_key: str | None = None):
    from one.core.agent_session import AgentSession
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.settings_manager import SettingsManager

    auth = AuthStorage.in_memory()
    if runtime_key:
        auth.set_runtime_api_key("openai", runtime_key)
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session_manager = SessionManager.in_memory(str(tmp_path))
    return AgentSession(session_manager, settings, registry, _FakeLoader(), model, "medium")


class _ErrorProvider:
    async def chat(self, api_key, model, messages, thinking_level, headers=None):
        raise RuntimeError("All connection attempts failed")


@pytest.mark.asyncio
async def test_extension_ui_request_renders_and_answer_routes(tmp_path: Path):
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        req = session.request_extension_ui(extension="test-ext", ui_type="widget", payload={"q": 1}, title="Wizard")
        await pilot.pause()

        assert app._extension_ui_pending_request is not None
        stream = "\n".join(app._stream_lines)
        assert "test-ext (widget)" in stream
        assert "Wizard" in stream
        assert '"q": 1' in stream

        input_widget = app.query_one("#input", TextArea)
        input_widget.text = '{"answer": 42}'
        await input_widget.action_submit()
        await pilot.pause()

        assert app._extension_ui_pending_request is None
        assert session._extension_ui_history[-1]["payload"] == {"answer": 42}
        assert session._extension_ui_history[-1]["requestId"] == req["id"]
        stream = "\n".join(app._stream_lines)
        assert f"[ExtUI] response {req['id']} cancelled=False" in stream


@pytest.mark.asyncio
async def test_extension_ui_cancel_via_empty_input(tmp_path: Path):
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        req = session.request_extension_ui(extension="x", ui_type="overlay", payload={})
        await pilot.pause()
        assert app._extension_ui_pending_request is not None

        input_widget = app.query_one("#input", TextArea)
        input_widget.text = ""
        await input_widget.action_submit()
        await pilot.pause()

        assert app._extension_ui_pending_request is None
        assert session._extension_ui_history[-1]["cancelled"] is True
        assert session._extension_ui_history[-1]["requestId"] == req["id"]


@pytest.mark.asyncio
async def test_extension_ui_external_response_clears_pending(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        req = session.request_extension_ui(extension="x", ui_type="widget", payload={})
        await pilot.pause()
        assert app._extension_ui_pending_request is not None

        session.respond_extension_ui(request_id=req["id"], payload={"ok": True})
        await pilot.pause()

        assert app._extension_ui_pending_request is None
        stream = "\n".join(app._stream_lines)
        assert f"[ExtUI] response {req['id']} cancelled=False" in stream


@pytest.mark.asyncio
async def test_extension_ui_answer_error_preserves_pending_state(tmp_path: Path):
    """Regression: _handle_extension_ui_answer must NOT clear pending state/panel/placeholder
    when respond_extension_ui raises — user must be able to retry."""
    from textual.widgets import Static, TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Create a widget extension request.
        req = session.request_extension_ui(extension="err-ext", ui_type="widget", payload={"q": 1}, title="ErrorTest")
        await pilot.pause()

        pending_id = app._extension_ui_pending_request["id"]

        # Monkeypatch respond_extension_ui to raise.
        session.respond_extension_ui = lambda **kw: (_ for _ in ()).throw(ValueError("gone"))  # type: ignore[method-assign]

        # Submit a nonempty answer through the normal input flow.
        input_widget = app.query_one("#input", TextArea)
        input_widget.text = '{"answer": 42}'
        await input_widget.action_submit()
        await pilot.pause()

        # (a) pending request still exists with same id
        assert app._extension_ui_pending_request is not None
        assert app._extension_ui_pending_request["id"] == pending_id

        # (b) widget panel remains visible
        panel = app.query_one("#ext_panel", Static)
        assert panel.has_class("visible")

        # (c) input placeholder remains extension-answer placeholder
        assert input_widget.placeholder == "Odpowiedź dla rozszerzenia (JSON lub tekst; puste = anuluj)"

        # (d) stream includes error message
        stream = "\n".join(app._stream_lines)
        assert "[ExtUI] error responding" in stream
        assert "gone" in stream


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


# ---------------------------------------------------------------------------
# Headless TUI command-parity tests (/help commands vs interactive mode).
# ---------------------------------------------------------------------------


async def _submit(app, pilot, text: str) -> None:
    from textual.widgets import TextArea

    input_widget = app.query_one("#input", TextArea)
    input_widget.text = text
    await input_widget.action_submit()
    await pilot.pause()


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
            "/login [status|provider [apiKey] [model]]",
            "/logout <provider>",
            "/retry <on|off>",
            "/config [key] [value]",
            "/extui <list|request|respond|cancel|clear>",
            "/cooperation [on|off]",
            "/mcp [list|enable|disable]",
            "/bash <command>",
        ]:
            assert token in stream, token


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
        assert '"skipped": true' in stream
        # The tree renders session entries with a '*' marker on the leaf.
        assert "model_change" in stream
        assert "* thinking_level_change" in stream
        assert session.get_pending_queues()["steering"] == ["abc"]
        assert session.get_pending_queues()["followUp"] == ["def"]


class _FakeRuntimeHost:
    def __init__(self, session: Any, new_session: Any | None = None) -> None:
        self.session = session
        self._new_session = new_session

    async def fork(self, entry_id: str) -> dict:
        if self._new_session is not None:
            self.session = self._new_session
        return {"cancelled": False, "entryId": entry_id}

    async def new_session(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._new_session is not None:
            self.session = self._new_session
        return {"cancelled": False}


@pytest.mark.asyncio
async def test_tui_command_fork_rebinds_session(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    old_session = _mk_app_session(tmp_path)
    new_session = _mk_app_session(tmp_path)
    app = _OneTextualApp(old_session, runtime_host=_FakeRuntimeHost(old_session, new_session=new_session))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/fork entry-1")
        stream = "\n".join(app._stream_lines)
        assert '"cancelled": false' in stream
        assert app.session is new_session
        assert callable(app._off_listener)
        # Events from the new session must now be delivered to the app.
        new_session.request_extension_ui(extension="post-fork", ui_type="widget", payload={})
        await pilot.pause()
        assert app._extension_ui_pending_request is not None
        assert app._extension_ui_pending_request["extension"] == "post-fork"


@pytest.mark.asyncio
async def test_tui_command_new_rebinds_session(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    old_session = _mk_app_session(tmp_path)
    new_session = _mk_app_session(tmp_path)
    app = _OneTextualApp(old_session, runtime_host=_FakeRuntimeHost(old_session, new_session=new_session))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/new")
        stream = "\n".join(app._stream_lines)
        assert "New session started." in stream
        assert app.session is new_session
        assert callable(app._off_listener)


@pytest.mark.asyncio
async def test_tui_new_clears_extension_ui_state(tmp_path: Path):
    """P1-8: /new must clear pending extension request, hide panels, and reset placeholder."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    old_session = _mk_app_session(tmp_path)
    new_session = _mk_app_session(tmp_path)
    app = _OneTextualApp(old_session, runtime_host=_FakeRuntimeHost(old_session, new_session=new_session))
    async with app.run_test() as pilot:
        await pilot.pause()

        # Create a widget extension request on the old session.
        old_session.request_extension_ui(extension="stale-ext", ui_type="widget", payload={"x": 1}, title="Stale")
        await pilot.pause()

        panel = app.query_one("#ext_panel", Static)
        assert panel.has_class("visible")
        assert app._extension_ui_pending_request is not None
        assert app._extension_ui_pending_request["extension"] == "stale-ext"
        from textual.widgets import TextArea
        input_widget = app.query_one("#input", TextArea)
        assert input_widget.placeholder == "Odpowiedź dla rozszerzenia (JSON lub tekst; puste = anuluj)"

        # Submit /new.
        await _submit(app, pilot, "/new")
        await pilot.pause()

        # After /new: panel hidden, pending cleared, default placeholder, session swapped.
        assert not panel.has_class("visible")
        assert app._extension_ui_pending_request is None
        assert input_widget.placeholder == "Wpisz polecenie lub /help"
        assert app.session is new_session


@pytest.mark.asyncio
async def test_tui_fork_clears_extension_ui_state(tmp_path: Path):
    """P1-8: /fork must clear pending extension request, hide panels, and reset placeholder."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    old_session = _mk_app_session(tmp_path)
    new_session = _mk_app_session(tmp_path)
    app = _OneTextualApp(old_session, runtime_host=_FakeRuntimeHost(old_session, new_session=new_session))
    async with app.run_test() as pilot:
        await pilot.pause()

        # Trigger an overlay request in the old session.
        old_session.request_extension_ui(extension="fork-ext", ui_type="overlay", payload={"key": "val"}, title="Fork")
        await pilot.pause()

        overlay = app.query_one("#ext_overlay", Static)
        assert overlay.has_class("visible")
        assert app._extension_ui_pending_request is not None
        assert app._extension_ui_pending_request["extension"] == "fork-ext"
        from textual.widgets import TextArea
        input_widget = app.query_one("#input", TextArea)
        assert input_widget.placeholder == "Odpowiedź dla rozszerzenia (JSON lub tekst; puste = anuluj)"

        # Execute /fork.
        await _submit(app, pilot, "/fork entry-1")
        await pilot.pause()

        # After /fork: overlay hidden, pending cleared, default placeholder, session swapped.
        assert not overlay.has_class("visible")
        assert app._extension_ui_pending_request is None
        assert input_widget.placeholder == "Wpisz polecenie lub /help"
        assert app.session is new_session


@pytest.mark.asyncio
async def test_tui_command_fork_without_runtime_host(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/fork abc")
        stream = "\n".join(app._stream_lines)
        assert "Runtime host unavailable" in stream
        assert app.session is session


@pytest.mark.asyncio
async def test_tui_command_login_inline_stores_key(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
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
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/login openai sk-test")
        await _submit(app, pilot, "/logout openai")
        stream = "\n".join(app._stream_lines)
        assert "Removed stored key for openai." in stream
        keys = session.model_registry._auth._data.get("apiKeys", {})
        assert "openai" not in keys

        await _submit(app, pilot, "/cooperation")
        stream = "\n".join(app._stream_lines)
        assert '"enabled": false' in stream
        await _submit(app, pilot, "/cooperation on")
        assert session.approval_callback is not None
        await _submit(app, pilot, "/cooperation")
        stream = "\n".join(app._stream_lines)
        assert '"enabled": true' in stream
        await _submit(app, pilot, "/cooperation off")
        assert session.approval_callback is None


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


# ---------------------------------------------------------------------------
# MCP slash-command tests.
# ---------------------------------------------------------------------------


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
    session.settings_manager.set_config_value(
        "mcpServers.demo", {"command": "true"}
    )
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
async def test_tui_command_navigate_unknown_id_error(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/navigate nope-123")
        stream = "\n".join(app._stream_lines)
        assert "not found" in stream


# ---------------------------------------------------------------------------
# Clipboard: copy (mouse selection), paste (ctrl+v) and the ctrl+a binding.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_ctrl_a_toggles_cooperation(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Focus is on the Input widget, which binds ctrl+a to "home" by
        # default; the app's priority binding must win.
        input_widget = app.query_one("#input")
        assert input_widget.has_focus
        await pilot.press("ctrl+a")
        assert session.approval_callback is not None
        await pilot.press("ctrl+a")
        assert session.approval_callback is None


@pytest.mark.asyncio
async def test_tui_copy_selected_stream_text(tmp_path: Path):
    from textual.geometry import Offset
    from textual.selection import Selection

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._write("hello world", "info")
        await pilot.pause()

        received: list[str] = []

        def fake_copy(text: str) -> tuple[str, str]:
            received.append(text)
            return ("copied", "fake")

        app._copy_to_clipboard = fake_copy  # type: ignore[method-assign]
        stream_widget = app.query_one("#stream")
        # Selection offsets are (x, y): line 1 ("hello world"), columns 0..5.
        app.screen.selections = {stream_widget: Selection(Offset(0, 1), Offset(5, 1))}
        app._try_auto_copy_selected_stream_text()

        assert received == ["hello"]
        assert app._last_auto_copied == "hello"
        # Dedup: the same selection must not copy twice.
        app._try_auto_copy_selected_stream_text()
        assert received == ["hello"]


@pytest.mark.asyncio
async def test_tui_paste_from_system_clipboard(tmp_path: Path, monkeypatch):
    from one.modes import tui_mode

    session = _mk_app_session(tmp_path)
    app = tui_mode._OneTextualApp(session)
    monkeypatch.setattr(tui_mode, "_paste_from_system_clipboard", lambda: "pasted-text")
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        input_widget.action_paste()
        await pilot.pause()
        assert input_widget.text == "pasted-text"


@pytest.mark.asyncio
async def test_tui_paste_fallback_app_clipboard(tmp_path: Path, monkeypatch):
    from one.modes import tui_mode

    session = _mk_app_session(tmp_path)
    app = tui_mode._OneTextualApp(session)
    monkeypatch.setattr(tui_mode, "_paste_from_system_clipboard", lambda: None)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._clipboard = "in-app-text"
        input_widget = app.query_one("#input")
        input_widget.action_paste()
        await pilot.pause()
        assert input_widget.text == "in-app-text"


@pytest.mark.asyncio
async def test_tui_paste_multiline_preserved(tmp_path: Path, monkeypatch):
    """Multi-line paste stays multi-line: the input field is now a TextArea."""
    from one.modes import tui_mode

    session = _mk_app_session(tmp_path)
    app = tui_mode._OneTextualApp(session)
    monkeypatch.setattr(tui_mode, "_paste_from_system_clipboard", lambda: "line1\nline2\nline3")
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        input_widget.action_paste()
        await pilot.pause()
        assert input_widget.text == "line1\nline2\nline3"


@pytest.mark.asyncio
async def test_tui_paste_long_text_truncated(tmp_path: Path, monkeypatch):
    """Huge single-line pastes are capped so the UI does not blow up."""
    from one.modes import tui_mode

    session = _mk_app_session(tmp_path)
    app = tui_mode._OneTextualApp(session)
    monkeypatch.setattr(tui_mode, "_paste_from_system_clipboard", lambda: "x" * 50_000)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        input_widget.action_paste()
        await pilot.pause()
        assert len(input_widget.text) == tui_mode._CommandTextArea._PASTE_MAX_CHARS
        assert input_widget.text == "x" * tui_mode._CommandTextArea._PASTE_MAX_CHARS


@pytest.mark.asyncio
async def test_tui_shift_enter_inserts_newline(tmp_path: Path):
    """Shift+Enter inserts a newline; Enter submits (see _submit tests)."""
    from one.modes import tui_mode

    session = _mk_app_session(tmp_path)
    app = tui_mode._OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        input_widget.focus()
        await pilot.press("a", "b")
        await pilot.press("shift+enter")
        await pilot.press("c")
        await pilot.pause()
        assert input_widget.text == "ab\nc"


@pytest.mark.asyncio
async def test_tui_error_turn_resets_spinner(tmp_path: Path):
    """A turn ending with an error must not leave the 'Ctrl+C abort' spinner running forever."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path, runtime_key="sk-test")
    session.providers = {"openai": _ErrorProvider()}
    session.settings_manager.set_retry_enabled(False)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "check server")
        # Wait until the error turn fully settles.
        for _ in range(100):
            await pilot.pause()
            if not app._turn_active and not session.is_streaming:
                break
        stream = "\n".join(app._stream_lines)
        assert "All connection attempts failed" in stream
        assert app._turn_active is False
        assert not any(line.startswith(_THINKING_MARK) for line in app._stream_lines)


@pytest.mark.asyncio
async def test_tui_ctrl_c_after_finished_error_turn(tmp_path: Path):
    """Ctrl+C after a finished (error) turn must acknowledge and keep the spinner dead."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path, runtime_key="sk-test")
    session.providers = {"openai": _ErrorProvider()}
    session.settings_manager.set_retry_enabled(False)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "check server")
        for _ in range(100):
            await pilot.pause()
            if not app._turn_active:
                break
        await pilot.press("ctrl+c")
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "[abort requested]" in stream
        assert app._turn_active is False
        assert not any(line.startswith(_THINKING_MARK) for line in app._stream_lines)


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


# ---------------------------------------------------------------------------
# Bash error output display (P1-6 regression).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_plan_update_renders_block(tmp_path: Path):
    """plan_update with a plan text renders a 'Plan:' block in the stream."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit({
            "type": "plan_update",
            "plan": "1. read file\n2. edit content",
        })
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "Plan:" in stream
        assert "1. read file" in stream
        assert "2. edit content" in stream


@pytest.mark.asyncio
async def test_tui_plan_clear_emits_block(tmp_path: Path):
    """plan_update with empty text renders 'Plan: cleared' in the stream."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit({"type": "plan_update", "plan": ""})
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "Plan: cleared" in stream


@pytest.mark.asyncio
async def test_tui_sidebar_shows_plan_section(tmp_path: Path):
    """Sidebar renders a Plan section when session._plan is set."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session._plan = "my plan text"
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        assert "Plan" in sidebar.content
        assert "my plan text" in sidebar.content


@pytest.mark.asyncio
async def test_tui_sidebar_hides_plan_section_when_none(tmp_path: Path):
    """Sidebar does not show a Plan section when session._plan is None."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session._plan = None
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        # The Info and MCP sections must still be present; Plan block is absent
        assert "Info" in sidebar.content
        assert "MCP" in sidebar.content


@pytest.mark.asyncio
async def test_tui_bash_error_output_shown_when_enabled(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit({
            "type": "tool_call_end",
            "tool": "bash",
            "ok": False,
            "result": {"error": "ls: cannot access '/nonexistent': No such file or directory\n\nCommand exited with code 2"},
        })
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "tool err: bash" in stream
        assert "No such file" in stream
        assert "'/nonexistent'" in stream
        assert "Command exited with code 2" in stream


@pytest.mark.asyncio
async def test_tui_bash_error_output_hidden_when_disabled(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session.settings_manager.set_bash_show_output(False)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit({
            "type": "tool_call_end",
            "tool": "bash",
            "ok": False,
            "result": {"error": "ls: cannot access '/nonexistent': No such file or directory\n\nCommand exited with code 2"},
        })
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "tool err: bash" in stream
        assert "No such file" not in stream
        assert "'/nonexistent'" not in stream
        assert "Command exited with code 2" not in stream


# ---------------------------------------------------------------------------
# Tool output display for non-bash tools (bash-show applies to all).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_ls_output_shown_when_enabled(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit({
            "type": "tool_call_end",
            "tool": "ls",
            "ok": True,
            "result": {"outputText": "file1.txt\nfile2.txt"},
        })
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "tool ok: ls" in stream
        assert "file1.txt" in stream
        assert "file2.txt" in stream


@pytest.mark.asyncio
async def test_tui_finish_output_not_duplicated(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit({
            "type": "tool_call_end",
            "tool": "finish",
            "ok": True,
            "result": {"outputText": "THE-FINAL-SUMMARY"},
        })
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "tool ok: finish" in stream
        assert "THE-FINAL-SUMMARY" not in stream


@pytest.mark.asyncio
async def test_tui_ls_output_hidden_when_disabled(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session.settings_manager.set_bash_show_output(False)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit({
            "type": "tool_call_end",
            "tool": "ls",
            "ok": True,
            "result": {"outputText": "file1.txt\nfile2.txt"},
        })
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "tool ok: ls" in stream
        assert "file1.txt" not in stream
        assert "file2.txt" not in stream


# ---------------------------------------------------------------------------
# Extension widget/overlay panel rendering (P1-8).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extension_widget_panel_renders_and_hides(tmp_path: Path):
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one("#ext_panel", Static)
        assert not panel.has_class("visible")

        req = session.request_extension_ui(extension="ext", ui_type="widget", payload={"mode": "quick"}, title="Panel")
        await pilot.pause()

        assert panel.has_class("visible")
        content = str(panel.content)
        assert "Panel" in content
        assert '"mode": "quick"' in content

        session.respond_extension_ui(request_id=req["id"], payload={"ok": True})
        await pilot.pause()
        assert not panel.has_class("visible")


@pytest.mark.asyncio
async def test_extension_overlay_panel_renders_and_hides(tmp_path: Path):
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        overlay = app.query_one("#ext_overlay", Static)
        assert not overlay.has_class("visible")

        req = session.request_extension_ui(extension="ext", ui_type="overlay", payload={"mode": "modal"}, title="Overlay")
        await pilot.pause()

        assert overlay.has_class("visible")
        content = str(overlay.content)
        assert "Overlay" in content
        assert '"mode": "modal"' in content

        session.respond_extension_ui(request_id=req["id"], payload={"ok": True})
        await pilot.pause()
        assert not overlay.has_class("visible")


@pytest.mark.asyncio
async def test_extension_panels_hide_on_new_session(tmp_path: Path):
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session.request_extension_ui(extension="ext", ui_type="widget", payload={"x": 1}, title="P")
        await pilot.pause()
        panel = app.query_one("#ext_panel", Static)
        assert panel.has_class("visible")

        # Simulate the /new reset path: pending cleared + panels hidden.
        app._extension_ui_pending_request = None
        app._hide_extension_panels()
        await pilot.pause()
        assert not panel.has_class("visible")


# ---------------------------------------------------------------------------
# Phase 6: plan text with markup chars (no MarkupError) + render guards.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_sidebar_plan_with_markup_chars_no_crash(tmp_path: Path):
    """Plan text containing [ ], <, > must not raise MarkupError in the sidebar.

    Phase 8: plan data lines are appended as literal Text (not parsed by
    Textual's markup parser), so raw brackets appear as-is in the output.
    """
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session._plan = "[b]bold[/] and [{\"plan\": \">\", \"x\": 1}]"
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        # _refresh_sidebar must not raise even with markup-breaking plan text.
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        assert "Plan" in sidebar.content
        # Plan data is literal text — brackets appear as-is (not escaped).
        assert "[b]bold[/]" in sidebar.content


@pytest.mark.asyncio
async def test_tui_plan_update_with_markup_chars_in_stream(tmp_path: Path):
    """plan_update with markup chars renders the Plan block in the stream without error.

    Phase 8: stream data lines are appended as literal Text (not parsed by
    Textual's markup parser), so raw brackets appear as-is in the output.
    """
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit({
            "type": "plan_update",
            "plan": "[b]bold[/] and [{\"plan\": \">\", \"x\": 1}]",
        })
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "Plan:" in stream
        # Stream data is literal text — brackets appear as-is (not escaped).
        widget = app.query_one("#stream", Static)
        assert "[b]bold[/]" in widget.content


# ---------------------------------------------------------------------------
# Phase 8: Textual MarkupError regression — any `[` in raw content must not
# crash Textual's own markup parser (textual/markup.py).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_stream_markup_chars_dns_type(tmp_path: Path):
    """Tool result with [type='CNAME'] must not raise MarkupError."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit({
            "type": "tool_call_end",
            "tool": "read",
            "ok": True,
            "result": {"outputText": "DNS entry: [type='CNAME']"},
        })
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "DNS entry:" in stream
        assert "[type='CNAME']" in stream
        widget = app.query_one("#stream", Static)
        assert "[type='CNAME']" in widget.content


@pytest.mark.asyncio
async def test_tui_stream_markup_chars_bracket_list(tmp_path: Path):
    """Tool result with [1,2,3] must not raise MarkupError."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit({
            "type": "tool_call_end",
            "tool": "read",
            "ok": True,
            "result": {"outputText": "tags: [1,2,3]"},
        })
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "tags:" in stream
        assert "[1,2,3]" in stream
        widget = app.query_one("#stream", Static)
        assert "[1,2,3]" in widget.content


@pytest.mark.asyncio
async def test_tui_stream_markup_chars_uppercase_brackets(tmp_path: Path):
    """Tool result with [ABC] must not raise MarkupError."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit({
            "type": "tool_call_end",
            "tool": "read",
            "ok": True,
            "result": {"outputText": "section [ABC] end"},
        })
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "section" in stream
        assert "[ABC]" in stream
        widget = app.query_one("#stream", Static)
        assert "[ABC]" in widget.content


@pytest.mark.asyncio
async def test_tui_stream_markup_chars_json_with_gt(tmp_path: Path):
    """Tool result with JSON containing [\"plan\": \">\"] must not raise MarkupError."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit({
            "type": "tool_call_end",
            "tool": "read",
            "ok": True,
            "result": {"outputText": '[{"plan": ">", "x": 1}]'},
        })
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert '"plan": ">' in stream
        assert '"x": 1' in stream
        widget = app.query_one("#stream", Static)
        assert '"plan": ">' in widget.content


@pytest.mark.asyncio
async def test_tui_sidebar_plan_markup_chars_dns_type(tmp_path: Path):
    """Sidebar Plan section with [type='CNAME'] must not raise MarkupError."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session._plan = "Record [type='CNAME'] points to example.com"
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        assert "Plan" in sidebar.content
        assert "[type='CNAME']" in sidebar.content
        assert "Record" in sidebar.content
        assert "example.com" in sidebar.content


@pytest.mark.asyncio
async def test_tui_sidebar_plan_markup_chars_bracket_list(tmp_path: Path):
    """Sidebar Plan section with [1,2,3] must not raise MarkupError."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session._plan = "tags: [1,2,3]"
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        assert "Plan" in sidebar.content
        assert "[1,2,3]" in sidebar.content


@pytest.mark.asyncio
async def test_tui_sidebar_plan_markup_chars_uppercase_brackets(tmp_path: Path):
    """Sidebar Plan section with [ABC] must not raise MarkupError."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session._plan = "section [ABC] end"
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        assert "Plan" in sidebar.content
        assert "[ABC]" in sidebar.content


@pytest.mark.asyncio
async def test_tui_sidebar_plan_markup_chars_json_with_gt(tmp_path: Path):
    """Sidebar Plan section with JSON containing [\"plan\": \">\"] must not raise MarkupError."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session._plan = '[{"plan": ">", "x": 1}]'
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        assert "Plan" in sidebar.content
        assert '"plan": ">' in sidebar.content


@pytest.mark.asyncio
async def test_tui_stream_spinner_markup_still_rendered(tmp_path: Path):
    """Intentional spinner markup (colour codes) must still render styled via Text.from_markup."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Directly add a thinking marker line to simulate spinner.
        app._stream_lines.append("")
        app._stream_lines.append(f"{_THINKING_MARK}[{app._theme.info}]{_THINKING_FRAMES[0]} Ctrl+C abort[/]")
        app._stream_lines.append("")
        app._render_stream()
        await pilot.pause()
        widget = app.query_one("#stream", Static)
        # The spinner text (without __MK__ prefix) must be present.
        assert "Ctrl+C abort" in widget.content
        # The raw thinking mark must NOT appear in rendered content.
        assert _THINKING_MARK not in widget.content


@pytest.mark.asyncio
async def test_tui_sidebar_info_markup_still_rendered(tmp_path: Path):
    """Intentional section header markup (colour codes) must still render styled via Text.from_markup."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        # Section headers must be present.
        assert "Info" in sidebar.content
        assert "MCP" in sidebar.content
        assert "Keys" in sidebar.content
        assert "Plan" not in sidebar.content  # no plan set


@pytest.mark.asyncio
async def test_tui_stream_complex_markup_content_no_crash(tmp_path: Path):
    """A single line with multiple bracket patterns must not raise MarkupError."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit({
            "type": "tool_call_end",
            "tool": "read",
            "ok": True,
            "result": {"outputText": "a=[b]c[/d] [1,2,3] [XYZ] {'key': 'val'} [type='CNAME']"},
        })
        await pilot.pause()
        widget = app.query_one("#stream", Static)
        # All bracket content must be present as literal text.
        assert "[b]c[/d]" in widget.content
        assert "[1,2,3]" in widget.content
        assert "[XYZ]" in widget.content
        assert "[type='CNAME']" in widget.content
