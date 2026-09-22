from __future__ import annotations

from pathlib import Path

import pytest

from one.cli.args import parse_args
from one.modes.tui_mode import (
    BUILTIN_TUI_THEMES,
    TUI_SHORTCUTS,
    build_sidebar_snapshot,
    evaluate_waiting,
    format_tui_shortcuts,
    resolve_tui_theme,
)
from tests.support.tui import _mk_app_session


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


class _DummyModel:
    provider = "openrouter"
    id = "google/gemma-4-31b-it:free"


class _DummySessionManager:
    cwd = "/tmp/project"


def test_parse_args_accepts_tui_mode() -> None:
    parsed = parse_args(["--mode", "tui"])
    assert parsed.errors == []
    assert parsed.mode == "tui"


def test_resolve_tui_theme_includes_fallout() -> None:
    fallout = resolve_tui_theme("fallout")
    assert fallout.name == "fallout"
    assert "fallout" in BUILTIN_TUI_THEMES
    assert resolve_tui_theme("no-such-theme").name == "default"


def test_tui_shortcuts_are_single_source_of_truth() -> None:
    rendered = format_tui_shortcuts()

    assert ("Ctrl+P", "command palette") in TUI_SHORTCUTS
    assert "Ctrl+P command palette" in rendered
    assert "Ctrl+V paste text from the host/system clipboard" in rendered
    assert "Ctrl+Shift+V paste terminal text (SSH-safe)" in rendered
    assert "Ctrl+Alt+V paste image from the system clipboard" in rendered
    assert "Ctrl+F1 show slash-command help" in rendered


def test_build_sidebar_snapshot_contains_runtime_details() -> None:
    session = _DummySession()
    session.auto_retry_enabled = True
    snapshot = build_sidebar_snapshot(session)

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
    assert snapshot["retry"] == "on"


def test_build_sidebar_snapshot_lists_enabled_mcp_servers() -> None:
    class _FakeMcpManager:
        def server_status(self) -> list[dict]:
            return [
                {
                    "name": "demo",
                    "enabled": True,
                    "running": True,
                    "tools": ["a", "b", "c"],
                    "transport": "stdio",
                    "error": None,
                },
                {
                    "name": "web",
                    "enabled": True,
                    "running": False,
                    "tools": ["x"],
                    "transport": "http",
                    "error": "boom",
                },
                {"name": "old", "enabled": False, "running": False, "tools": [], "transport": "stdio", "error": None},
            ]

    from one.core.settings_manager import SettingsManager

    session = _DummySession()
    session._mcp_manager = _FakeMcpManager()
    session.settings_manager = SettingsManager.in_memory()
    session.settings_manager.set_retry_mode("off")
    snapshot = build_sidebar_snapshot(session)

    assert snapshot["mcpEnabled"] is True
    assert [s["name"] for s in snapshot["mcpServers"]] == ["demo", "web"]
    assert snapshot["mcpServers"][0]["toolCount"] == 3
    assert snapshot["mcpServers"][0]["transport"] == "stdio"
    assert snapshot["mcpServers"][1]["error"] == "boom"
    assert snapshot["retry"] == "off"


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
                {
                    "name": "demo",
                    "enabled": True,
                    "running": True,
                    "tools": ["a", "b", "c"],
                    "transport": "stdio",
                    "error": None,
                },
                {
                    "name": "web",
                    "enabled": True,
                    "running": False,
                    "tools": ["x"],
                    "transport": "http",
                    "error": "boom",
                },
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
        session.request_extension_ui(extension="err-ext", ui_type="widget", payload={"q": 1}, title="ErrorTest")
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
        assert input_widget.placeholder == "Answer for extension (JSON or text; empty = cancel)"

        # (d) stream includes error message
        stream = "\n".join(app._stream_lines)
        assert "[ExtUI] error responding" in stream
        assert "gone" in stream


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
        session._emit(
            {
                "type": "tool_call_end",
                "tool": "bash",
                "ok": False,
                "result": {
                    "error": "ls: cannot access '/nonexistent': No such file or directory\n\nCommand exited with code 2"
                },
            }
        )
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
        session._emit(
            {
                "type": "tool_call_end",
                "tool": "bash",
                "ok": False,
                "result": {
                    "error": "ls: cannot access '/nonexistent': No such file or directory\n\nCommand exited with code 2"
                },
            }
        )
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "tool err: bash" in stream
        assert "No such file" not in stream
        assert "'/nonexistent'" not in stream
        assert "Command exited with code 2" not in stream


@pytest.mark.asyncio
async def test_tui_ls_output_shown_when_enabled(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit(
            {
                "type": "tool_call_end",
                "tool": "ls",
                "ok": True,
                "result": {"outputText": "file1.txt\nfile2.txt"},
            }
        )
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
        session._emit(
            {
                "type": "tool_call_end",
                "tool": "finish",
                "ok": True,
                "result": {"outputText": "THE-FINAL-SUMMARY"},
            }
        )
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
        session._emit(
            {
                "type": "tool_call_end",
                "tool": "ls",
                "ok": True,
                "result": {"outputText": "file1.txt\nfile2.txt"},
            }
        )
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "tool ok: ls" in stream
        assert "file1.txt" not in stream
        assert "file2.txt" not in stream


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

        req = session.request_extension_ui(
            extension="ext", ui_type="overlay", payload={"mode": "modal"}, title="Overlay"
        )
        await pilot.pause()

        assert overlay.has_class("visible")
        content = str(overlay.content)
        assert "Overlay" in content
        assert '"mode": "modal"' in content

        session.respond_extension_ui(request_id=req["id"], payload={"ok": True})
        await pilot.pause()
        assert not overlay.has_class("visible")


@pytest.mark.asyncio
async def test_tui_sidebar_plan_with_markup_chars_no_crash(tmp_path: Path):
    """Plan text containing [ ], <, > must not raise MarkupError in the sidebar.

    Phase 8: plan data lines are appended as literal Text (not parsed by
    Textual's markup parser), so raw brackets appear as-is in the output.
    """
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session._plan = '[b]bold[/] and [{"plan": ">", "x": 1}]'
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
        assert "Plan" not in sidebar.content


@pytest.mark.asyncio
async def test_tui_welcome_includes_logo(tmp_path: Path):
    """TUI mount must write ASCII logo lines containing '██╗' anchors."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "██╗" in stream
        assert "one TUI v2 ready. /help" in stream


@pytest.mark.asyncio
async def test_tui_three_segments_two_tools_strict_chronology(tmp_path: Path):
    """Three thinking segments / two tools: exactly one turn_start, strict chronological order."""
    from one.modes.tui_mode import _THINKING_TEXT_MARK, _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Exactly one turn_start before A, then A→tool1→B→tool2→C→answer
        session._emit({"type": "turn_start"})
        await pilot.pause()

        # Segment A
        for ch in "A-seg":
            session._emit({"type": "thinking_delta", "delta": ch})
            await pilot.pause()

        # Tool 1: read
        session._emit({"type": "tool_call_start", "tool": "read", "args": {"pattern": "x"}})
        await pilot.pause()
        session._emit({"type": "tool_call_end", "tool": "read", "ok": True, "result": {"outputText": "out1"}})
        await pilot.pause()

        # Segment B
        for ch in "B-seg":
            session._emit({"type": "thinking_delta", "delta": ch})
            await pilot.pause()

        # Tool 2: grep
        session._emit({"type": "tool_call_start", "tool": "grep", "args": {"pattern": "x"}})
        await pilot.pause()
        session._emit({"type": "tool_call_end", "tool": "grep", "ok": True, "result": {"outputText": "out2"}})
        await pilot.pause()

        # Segment C
        for ch in "C-seg":
            session._emit({"type": "thinking_delta", "delta": ch})
            await pilot.pause()

        # Final answer
        session._emit({"type": "message_start", "message": {"role": "assistant", "content": ""}})
        await pilot.pause()
        session._emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "FINAL"}})
        await pilot.pause()
        session._emit({"type": "message_end", "message": {"role": "assistant", "content": "FINAL"}})
        await pilot.pause()
        session._emit({"type": "turn_end", "ok": True})
        await pilot.pause()

        thinking_text_lines = [l for l in app._stream_lines if l.startswith(_THINKING_TEXT_MARK)]
        assert len(thinking_text_lines) == 3, f"Expected 3 _THINKING_TEXT_MARK lines, got {len(thinking_text_lines)}"

        # Strict chronological order: A < tool1_start < tool1_end < B < tool2_start < tool2_end < C < final
        a_idx = next(i for i, l in enumerate(app._stream_lines) if l.startswith(_THINKING_TEXT_MARK) and "A-seg" in l)
        tool1_start = next(i for i, l in enumerate(app._stream_lines) if "tool:" in l and "read" in l)
        tool1_end = next(i for i, l in enumerate(app._stream_lines) if "out1" in l)
        b_idx = next(i for i, l in enumerate(app._stream_lines) if l.startswith(_THINKING_TEXT_MARK) and "B-seg" in l)
        tool2_start = next(i for i, l in enumerate(app._stream_lines) if "tool:" in l and "grep" in l)
        tool2_end = next(i for i, l in enumerate(app._stream_lines) if "out2" in l)
        c_idx = next(i for i, l in enumerate(app._stream_lines) if l.startswith(_THINKING_TEXT_MARK) and "C-seg" in l)
        final_idx = next(i for i, l in enumerate(app._stream_lines) if "FINAL" in l)

        assert "[ok]" in "\n".join(app._stream_lines[tool1_start:tool1_end])
        assert "[ok]" in "\n".join(app._stream_lines[tool2_start:tool2_end])
        assert a_idx < tool1_start < tool1_end < b_idx < tool2_start < tool2_end < c_idx < final_idx


@pytest.mark.asyncio
async def test_tui_literal_chunks_no_markup_error(tmp_path: Path):
    """Chunks with brackets, code, bold markers render literal without MarkupError."""
    from textual.widgets import Static

    from one.modes.tui_mode import _THINKING_TEXT_MARK, _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        session._emit({"type": "turn_start"})
        await pilot.pause()

        # Literal chunks with markup-breaking characters
        chunks = ["rea", "son", ",", " ", "can", "'", "t", " ", "[", "bold", "]", " ", "x", "[/]"]
        for ch in chunks:
            session._emit({"type": "thinking_delta", "delta": ch})
            await pilot.pause()

        session._emit({"type": "message_start", "message": {"role": "assistant", "content": ""}})
        await pilot.pause()
        session._emit({"type": "message_end", "message": {"role": "assistant", "content": "reason, can't [bold] x[/]"}})
        await pilot.pause()
        session._emit({"type": "turn_end", "ok": True})
        await pilot.pause()

        # No MarkupError raised; inspect rendered Static content
        widget = app.query_one("#stream", Static)
        rendered = str(widget.content)
        # Exact literal joined chunk string — brackets not interpreted as markup
        expected_literal = "reason, can't [bold] x[/]"
        assert expected_literal in rendered, f"Expected {expected_literal!r} in content, got: {rendered!r}"

        # One _THINKING_TEXT_MARK line
        thinking_text_lines = [l for l in app._stream_lines if l.startswith(_THINKING_TEXT_MARK)]
        assert len(thinking_text_lines) == 1


@pytest.mark.asyncio
async def test_tui_tool_call_nudge_start_boundary(tmp_path: Path):
    """A, nudge_start, B => two thinking blocks; no new event schema."""
    from one.modes.tui_mode import _THINKING_TEXT_MARK, _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        session._emit({"type": "turn_start"})
        await pilot.pause()
        session._emit({"type": "thinking_delta", "delta": "A"})
        await pilot.pause()

        # nudge_start resets thinking block
        session._emit({"type": "tool_call_nudge_start"})
        await pilot.pause()

        session._emit({"type": "thinking_delta", "delta": "B"})
        await pilot.pause()

        session._emit({"type": "message_start", "message": {"role": "assistant", "content": ""}})
        await pilot.pause()
        session._emit({"type": "message_end", "message": {"role": "assistant", "content": ""}})
        await pilot.pause()
        session._emit({"type": "turn_end", "ok": True})
        await pilot.pause()

        thinking_text_lines = [l for l in app._stream_lines if l.startswith(_THINKING_TEXT_MARK)]
        assert len(thinking_text_lines) == 2, f"Expected 2 _THINKING_TEXT_MARK lines, got {len(thinking_text_lines)}"

        # A and B in separate lines
        a_idx = next(i for i, l in enumerate(thinking_text_lines) if "A" in l)
        b_idx = next(i for i, l in enumerate(thinking_text_lines) if "B" in l)
        assert a_idx != b_idx


@pytest.mark.asyncio
async def test_tui_message_end_after_tool_call_no_duplicate(tmp_path: Path):
    """message_end following tool_call_start must not create an extra block
    — the state reset by tool_call_start ensures idempotent cleanup."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        session._emit({"type": "message_start", "message": {"role": "assistant", "content": ""}})
        await pilot.pause()
        session._emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": '{"tool":"ls","args":{}}'}})
        await pilot.pause()
        session._emit({"type": "tool_call_start", "tool": "ls", "args": {}})
        await pilot.pause()
        session._emit({"type": "message_end", "message": {"role": "assistant", "content": ""}})
        await pilot.pause()

        stream = "\n".join(app._stream_lines)
        # The tool block must be present exactly once.
        assert sum(1 for l in stream.split("\n") if "tool:" in l and "ls" in l) == 1
        # No assistant delta block should remain (JSON was removed by tool_call_start).
        assert app._assistant_has_live_delta is False
        assert app._assistant_live_start_idx == -1


@pytest.mark.asyncio
async def test_tui_sidebar_renders_version_line(tmp_path: Path):
    """Sidebar info block must include a Version line with the package VERSION."""
    from textual.widgets import Static

    from one.config import VERSION
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        content = str(sidebar.content)
        assert f"Version: {VERSION}" in content


@pytest.mark.asyncio
async def test_tui_tool_call_start_with_effective_timeout(tmp_path: Path):
    """A tool_call_start event that carries a positive effectiveTimeout must
    render it in the block: ``tool start (timeout 30s): bash {...}``."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        session._emit(
            {
                "type": "tool_call_start",
                "tool": "bash",
                "args": {"command": "echo hi"},
                "effectiveTimeout": 30,
            }
        )
        await pilot.pause()

        stream = "\n".join(app._stream_lines)
        # The timeout and tool name must both appear in the stream.
        assert "tool (timeout 30s):" in stream
        assert '"command": "echo hi"' in stream


@pytest.mark.asyncio
async def test_tui_tool_status_uses_literal_segmented_bold_styles(tmp_path: Path):
    """Tool headings/statuses are bold while JSON remains literal and plain."""
    from rich.console import Console
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        args = {"x": "[b]"}
        session._emit({"type": "tool_call_start", "tool": "read", "args": args, "effectiveTimeout": 30})
        await pilot.pause()
        session._emit({"type": "tool_call_end", "tool": "read", "ok": True, "result": {}})
        await pilot.pause()
        session._emit({"type": "tool_call_start", "tool": "read", "args": {"x": "bad"}})
        await pilot.pause()
        session._emit({"type": "tool_call_end", "tool": "read", "ok": False, "result": {}})
        await pilot.pause()

        content = app.query_one("#stream", Static).content
        heading = next(line for line in app._stream_lines if line.startswith("tool (timeout 30s): read"))
        args_line = next(line for line in app._stream_lines if "[b]" in line)
        status_line = next(line for line in app._stream_lines if line.endswith(" [ok]"))
        error_line = next(line for line in app._stream_lines if line.endswith("[err]"))
        heading_start = content.plain.index(heading)
        args_start = content.plain.index(args_line)
        status_start = content.plain.index(status_line) + status_line.rindex(" [ok]")
        error_start = content.plain.index(error_line) + error_line.rindex("[err]")

        console = Console()
        assert content.get_style_at_offset(console, heading_start).bold is True
        assert not content.get_style_at_offset(console, args_start).bold
        assert not content.get_style_at_offset(console, args_start + args_line.index("[b]")).bold
        assert content.get_style_at_offset(console, status_start + 1).bold is True
        assert content.get_style_at_offset(console, error_start + 1).bold is True


@pytest.mark.asyncio
async def test_tui_lifecycle_separators_are_ordered_once_and_narrow_safe(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test(size=(10, 20)) as pilot:
        await pilot.pause()
        session._emit({"type": "compaction_start", "reason": "manual"})
        await pilot.pause()
        session._emit({"type": "tool_call_start", "tool": "finish", "args": {"summary": "done"}})
        await pilot.pause()

        separators = [i for i, line in enumerate(app._stream_lines) if set(line) == {"─"}]
        assert len(separators) == 2
        assert all(len(app._stream_lines[index]) >= 1 for index in separators)
        finish = next(i for i, line in enumerate(app._stream_lines) if line.startswith("tool: finish"))
        assert separators[0] < separators[1] < finish


@pytest.mark.asyncio
async def test_tui_tool_call_start_per_call_override_shows_override(tmp_path: Path):
    """When the agent overrides the timeout per-call, the override value must
    appear in the TUI line."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        session._emit(
            {
                "type": "tool_call_start",
                "tool": "bash",
                "args": {"command": "sleep 10"},
                "effectiveTimeout": 5,
            }
        )
        await pilot.pause()

        stream = "\n".join(app._stream_lines)
        assert "tool (timeout 5s):" in stream
        assert '"command": "sleep 10"' in stream


@pytest.mark.asyncio
async def test_tui_tool_call_start_without_effective_timeout_plain_format(tmp_path: Path):
    """A tool_call_start event without effectiveTimeout (or with None / 0)
    must render the old plain format without a timeout suffix."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # No effectiveTimeout key at all
        session._emit(
            {
                "type": "tool_call_start",
                "tool": "read",
                "args": {"path": "a.txt"},
            }
        )
        await pilot.pause()

        stream = "\n".join(app._stream_lines)
        # Must use the plain "tool: read" format — no "(timeout ..." suffix.
        assert "tool:" in stream
        assert "tool (timeout" not in stream
        assert "a.txt" in stream


@pytest.mark.asyncio
async def test_tui_tool_call_start_subagent_timeout(tmp_path: Path):
    """spawn_subagent now uses subagents.timeoutSec as its effective timeout.

    The TUI must render the subagents timeout (1800 s default) in the
    tool_call_start event line."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Simulate what agent_session emits for spawn_subagent:
        # effectiveTimeout = subagents.timeoutSec (default 1800)
        session._emit(
            {
                "type": "tool_call_start",
                "tool": "spawn_subagent",
                "args": {"task": "do something"},
                "effectiveTimeout": 1800,
            }
        )
        await pilot.pause()

        stream = "\n".join(app._stream_lines)
        assert "tool (timeout 1800s):" in stream


@pytest.mark.asyncio
async def test_tui_separate_tool_output_appears_once(tmp_path: Path):
    """A tool_call_start followed by tool_call_end with real output text
    must render exactly one status block AND exactly one output block."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        session._emit(
            {
                "type": "tool_call_start",
                "tool": "bash",
                "args": {"command": "echo hello"},
                "effectiveTimeout": 5,
            }
        )
        await pilot.pause()

        session._emit(
            {
                "type": "tool_call_end",
                "tool": "bash",
                "ok": True,
                "result": {"outputText": "hello output\n", "result": "hello output\n"},
            }
        )
        await pilot.pause()

        stream = "\n".join(app._stream_lines)
        # Status block appears once (with [ok] suffix).
        # Note: _format_chat_panel wraps long lines, so search for the unique prefix.
        assert stream.count("tool (timeout 5s)") == 1
        # Output block appears exactly once.
        assert stream.count("hello output") == 1


@pytest.mark.asyncio
async def test_tui_ctrlv_key_inserts_once(tmp_path: Path, monkeypatch):
    """Pressing ctrl+v via pilot must insert exactly once (via the ctrl+v
    binding which calls action_paste)."""
    from textual.widgets import TextArea

    from one.modes import tui_mode
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    monkeypatch.setattr(tui_mode, "_paste_from_system_clipboard", lambda: "via-ctrlv-key")
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget: TextArea = app.query_one("#input", TextArea)
        await pilot.press("ctrl+v")
        await pilot.pause()
        # Should have inserted once via the binding → action_paste path.
        assert input_widget.text == "via-ctrlv-key"
