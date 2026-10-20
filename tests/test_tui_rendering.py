# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from one.cli.args import parse_args
from one.modes.tui_mode import (
    BUILTIN_TUI_THEMES,
    TUI_SHORTCUTS,
    TuiSessionView,
    build_sidebar_snapshot,
    evaluate_waiting,
    format_tui_shortcuts,
    get_git_branch,
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


@pytest.mark.asyncio
async def test_tui_theme_applies_sidebar_colors_to_header(tmp_path: Path) -> None:
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        theme = BUILTIN_TUI_THEMES["light"]
        assert app._apply_theme(theme.name)

        header = app.query_one("#header", Static)
        assert header.styles.background.hex.lower() == theme.sidebar_bg
        assert header.styles.color.hex.lower() == theme.screen_fg
        assert all(border[1].hex.lower() == theme.panel_border for border in header.styles.border)
        assert app.query_one("#logo", Static).styles.color.hex.lower() == theme.screen_fg


def test_tui_shortcuts_are_single_source_of_truth() -> None:
    rendered = format_tui_shortcuts()

    expected = (
        ("Ctrl+K", "Select model"),
        ("Ctrl+P", "Commands"),
        ("Ctrl+C", "Abort"),
        ("Ctrl+L", "Clear output"),
        ("Ctrl+Q", "Quit"),
        ("Ctrl+Z", "Toggle cooperation"),
        ("Ctrl+S", "Toggle subagents"),
        ("Ctrl+O", "Toggle details"),
        ("Ctrl+V", "Paste clipboard"),
        ("Ctrl+Shift+V", "Paste terminal"),
        ("Ctrl+Alt+V", "Paste image"),
        ("Ctrl+R", "Retry mode"),
        ("Ctrl+← / →", "Adjust temperature"),
        ("Ctrl+↑ / ↓", "Input history"),
        ("Ctrl+F1", "Command help"),
        ("Esc", "Close"),
    )
    assert TUI_SHORTCUTS == expected
    assert rendered == "\n".join(f"{key} {description}" for key, description in expected)


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


def test_build_sidebar_snapshot_marks_hard_disabled_subagents_off() -> None:
    session = _DummySession()
    setattr(session, "subagents_hard_disabled", True)

    assert build_sidebar_snapshot(session)["subagents"] is False


def test_get_git_branch_handles_nested_repositories_and_fallbacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "sidebar-test", str(repo)], check=True)
    nested = repo / "nested" / "directory"
    nested.mkdir(parents=True)

    assert get_git_branch(nested) == "sidebar-test"
    assert get_git_branch(tmp_path / "not-a-repo") is None

    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "TUI test"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-qm", "initial"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--detach"], check=True)
    assert get_git_branch(nested) is None

    monkeypatch.setattr(
        "one.modes.tui_mode.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", ""),
    )
    assert get_git_branch(repo) is None


def test_get_git_branch_is_safe_for_subprocess_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr("one.modes.tui_mode.subprocess.run", raise_timeout)
    assert get_git_branch(tmp_path) is None

    def raise_oserror(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr("one.modes.tui_mode.subprocess.run", raise_oserror)
    assert get_git_branch(tmp_path) is None


@pytest.mark.asyncio
async def test_tui_sidebar_includes_attached_branch_only_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from textual.widgets import Static

    from one.modes import tui_mode
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path, settings_override={"tui": {"infoPanel": "sidebar"}})
    session.session_manager._cwd = "/safe/project"
    app = _OneTextualApp(session)
    monkeypatch.setattr(tui_mode, "get_git_branch", lambda cwd: "feature/test")
    async with app.run_test() as pilot:
        await pilot.pause()
        await asyncio.sleep(0.01)
        await pilot.pause()
        assert "Cwd: /safe/project (feature/test)" in app.query_one("#sidebar", Static).content

        monkeypatch.setattr(tui_mode, "get_git_branch", lambda cwd: None)
        app._git_branch_cache.clear()
        app._refresh_sidebar()
        await asyncio.sleep(0.01)
        await pilot.pause()
        assert "Cwd: /safe/project\n" in app.query_one("#sidebar", Static).content


@pytest.mark.asyncio
async def test_tui_sidebar_git_probe_is_worker_cached_and_never_blocks_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from textual.widgets import Static, TextArea

    from one.modes import tui_mode
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path, settings_override={"tui": {"infoPanel": "sidebar"}})
    session.session_manager._cwd = "/slow/project"
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []
    ui_thread = threading.get_ident()

    def slow_branch(_cwd: str) -> str:
        calls.append(threading.get_ident())
        started.set()
        assert release.wait(2)
        return "feature/async"

    monkeypatch.setattr(tui_mode, "get_git_branch", slow_branch)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        # Calling paint repeatedly and typing remain prompt while the worker is blocked.
        began = time.monotonic()
        app._refresh_sidebar()
        app._refresh_sidebar()
        await pilot.press("x")
        assert time.monotonic() - began < 0.2
        assert app.query_one("#input", TextArea).text == "x"
        assert len(calls) == 1
        assert calls[0] != ui_thread

        release.set()
        for _ in range(20):
            await asyncio.sleep(0.01)
            if "feature/async" in str(app.query_one("#sidebar", Static).content):
                break
        assert "Cwd: /slow/project (feature/async)" in app.query_one("#sidebar", Static).content
        app._refresh_sidebar()
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_tui_sidebar_git_probe_does_not_repaint_after_session_cwd_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from textual.widgets import Static

    from one.modes import tui_mode
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path, settings_override={"tui": {"infoPanel": "sidebar"}})
    session.session_manager._cwd = "/old/project"
    old_started = threading.Event()
    release_old = threading.Event()

    def branch(cwd: str) -> str:
        if cwd == "/old/project":
            old_started.set()
            assert release_old.wait(2)
            return "old-branch"
        return "new-branch"

    monkeypatch.setattr(tui_mode, "get_git_branch", branch)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        for _ in range(100):
            if old_started.is_set():
                break
            await asyncio.sleep(0.01)
        assert old_started.is_set()
        session.session_manager._cwd = "/new/project"
        app._bind_session()
        app._refresh_sidebar()
        release_old.set()
        for _ in range(20):
            await asyncio.sleep(0.01)
            content = str(app.query_one("#sidebar", Static).content)
            if "new-branch" in content:
                break
        content = str(app.query_one("#sidebar", Static).content)
        assert "Cwd: /new/project (new-branch)" in content
        assert "old-branch" not in content


@pytest.mark.asyncio
async def test_tui_info_panel_layouts_are_mutually_exclusive(tmp_path: Path) -> None:
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    top = _OneTextualApp(_mk_app_session(tmp_path))
    async with top.run_test() as pilot:
        await pilot.pause()
        assert top.query_one("#sidebar", Static).styles.display == "none"
        header = top.query_one("#header", Static)
        assert header.styles.height.value == 3
        assert "\n" not in str(header.content)
        assert "██████╗" not in str(header.content)
        assert len(list(top.query("#logo"))) == 1

        await top._handle_command("/layout focus")
        assert top.query_one("#header", Static).styles.display == "none"
        assert len(list(top.query("#logo"))) == 1

    sidebar = _OneTextualApp(_mk_app_session(tmp_path, settings_override={"tui": {"infoPanel": "sidebar"}}))
    async with sidebar.run_test() as pilot:
        await pilot.pause()
        assert sidebar.query_one("#sidebar", Static).styles.display != "none"
        assert len(list(sidebar.query("#logo"))) == 1


@pytest.mark.asyncio
async def test_tui_temperature_mode_renders_in_compact_header_and_wide_sidebar(tmp_path: Path) -> None:
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    compact = _OneTextualApp(_mk_app_session(tmp_path))
    async with compact.run_test() as pilot:
        await pilot.pause()
        header = str(compact.query_one("#header", Static).content)
        assert "Mode: balanced (0.5)" in header
        assert "Think: medium" in header
        assert "Coop: off" in header
        assert "Status: ready" in header

    wide = _OneTextualApp(_mk_app_session(tmp_path, settings_override={"tui": {"infoPanel": "sidebar"}}))
    async with wide.run_test() as pilot:
        await pilot.pause()
        sidebar = str(wide.query_one("#sidebar", Static).content)
        assert "Mode: balanced (0.5)" in sidebar
        assert "Mode: balanced\n" not in sidebar
        assert "Temperature:" not in sidebar
        assert "Retry:" in sidebar
        assert "Status:" in sidebar
        assert "Delivery:" in sidebar


@pytest.mark.asyncio
async def test_tui_compact_to_wide_aligns_conversation_and_sidebar_frames(tmp_path: Path) -> None:
    from textual.containers import Vertical, VerticalScroll
    from textual.widgets import Static, TextArea

    from one.modes.tui_mode import _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test(size=(120, 48)) as pilot:
        await pilot.pause()
        main = app.query_one("#main")
        header = app.query_one("#header", Static)
        stream = app.query_one("#stream_container", VerticalScroll)
        conversation = app.query_one("#conversation", Vertical)
        sidebar = app.query_one("#sidebar", Static)
        input_widget = app.query_one("#input", TextArea)
        logo = app.query_one("#logo", Static)

        assert not main.has_class("layout-wide")
        assert conversation.styles.margin.top == 1
        assert stream.region.y > header.region.bottom
        assert "██████╗" in str(logo.content)

        await app._handle_command("/layout wide")
        await pilot.pause()

        assert main.has_class("layout-wide")
        assert header.styles.display == "none"
        assert sidebar.styles.display != "none"
        assert conversation.styles.margin.top == 0
        assert main.region.y == sidebar.region.y
        assert main.region.height == sidebar.region.height
        assert conversation.region.y == sidebar.region.y
        assert input_widget.region.bottom == main.region.bottom
        assert "██████╗" in str(logo.content)

        await app._handle_command("/layout compact")
        await pilot.pause()

        assert not main.has_class("layout-wide")
        assert conversation.styles.margin.top == 1
        assert stream.region.y > header.region.bottom
        assert "██████╗" in str(logo.content)


@pytest.mark.asyncio
async def test_tui_narrow_compact_and_wide_frames_remain_aligned(tmp_path: Path) -> None:
    from textual.containers import Vertical, VerticalScroll
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        main = app.query_one("#main")
        header = app.query_one("#header", Static)
        stream = app.query_one("#stream_container", VerticalScroll)
        conversation = app.query_one("#conversation", Vertical)
        sidebar = app.query_one("#sidebar", Static)
        logo = app.query_one("#logo", Static)

        assert stream.region.y > header.region.bottom
        assert "██████╗" in str(logo.content)

        await app._handle_command("/layout wide")
        await pilot.pause()

        assert main.region.y == sidebar.region.y
        assert main.region.height == sidebar.region.height
        assert conversation.region.y == sidebar.region.y

        await app._handle_command("/layout compact")
        await pilot.pause()

        assert sidebar.styles.display == "none"
        assert stream.region.y > header.region.bottom


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
    assert snapshot["mcpServers"][0]["available"] is True
    assert snapshot["mcpServers"][1]["error"] == "boom"
    assert snapshot["mcpServers"][1]["available"] is False
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


def test_build_sidebar_snapshot_marks_problematic_mcp_runtime_states_unavailable() -> None:
    class _FakeMcpManager:
        def server_status(self) -> list[dict]:
            return [
                {
                    "name": state,
                    "enabled": True,
                    "running": True,
                    "tools": [],
                    "error": None,
                    "runtimeState": state,
                }
                for state in ("running", "failed", "retrying", "disabled")
            ]

    session = _DummySession()
    session._mcp_manager = _FakeMcpManager()

    snapshot = build_sidebar_snapshot(session)

    assert [server["available"] for server in snapshot["mcpServers"]] == [True, False, False, False]


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
    """Verify MCP availability markers and disabled-server filtering."""
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
        assert "[ok] demo" in s
        assert "[!] web" in s
        assert "old" not in s.splitlines()
        assert s.index("MCP") < s.index("Keys")


@pytest.mark.asyncio
async def test_detached_tui_sidebar_normalizes_and_renders_mcp_statuses(tmp_path: Path) -> None:
    """Detached snapshots receive status records, not the legacy derived DTO."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    class _Facade:
        def subscribe(self, _listener, **_kwargs):
            return lambda: None

        async def command(self, command: str, _payload: object = None) -> dict:
            assert command == "snapshot"
            return {
                "result": {
                    "epoch": 1,
                    "sidebar": {
                        "model": "stub/model",
                        "mcpEnabled": True,
                        "mcpServers": [
                            {"name": "running", "enabled": True, "running": True, "tools": ["a"]},
                            {
                                "name": "failed", "enabled": True, "running": True,
                                "runtimeState": "failed", "tools": [],
                            },
                            {"name": "disabled", "enabled": False, "running": True, "tools": []},
                        ],
                    },
                    "messages": [],
                },
            }

    view = TuiSessionView(_Facade())
    await view.refresh()
    snapshot = build_sidebar_snapshot(view)
    assert snapshot["contextWindow"] is None
    assert [server["name"] for server in snapshot["mcpServers"]] == ["running", "failed"]
    assert [server["available"] for server in snapshot["mcpServers"]] == [True, False]

    app = _OneTextualApp(view)
    async with app.run_test() as pilot:
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        rendered = str(sidebar.content)
        assert "[ok] running" in rendered
        assert "[!] failed" in rendered
        assert "disabled" not in rendered


@pytest.mark.asyncio
async def test_tui_unmount_approval_cleanup_does_not_refresh_sidebar(tmp_path: Path) -> None:
    from one.modes.tui_mode import _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    future: asyncio.Future[tuple[bool, str]] = asyncio.get_running_loop().create_future()
    app._approval_waiters.append({"future": future})
    sidebar_refreshes: list[None] = []
    app._refresh_sidebar = lambda: sidebar_refreshes.append(None)  # type: ignore[method-assign]

    app.on_unmount()

    assert future.result() == (False, "TUI closed")
    assert not app._approval_waiters
    assert sidebar_refreshes == []


def test_evaluate_waiting_rule() -> None:
    now = 100.0
    # No active turn -> never waiting.
    assert evaluate_waiting(False, now - 10.0, now) is False
    # Active turn remains visible even while prose is streaming.
    assert evaluate_waiting(True, now - 0.2, now) is True
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
    session._plan = [{"step": "my plan text", "status": "in_progress"}]
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        assert "Plan" in sidebar.content
        assert "[>] my plan text" in sidebar.content


@pytest.mark.asyncio
async def test_tui_sidebar_plan_window_is_literal_and_bounds_long_rows(tmp_path: Path):
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session._plan = [
        {"step": "completed", "status": "completed"},
        {"step": "[b]current[/] " + "very long label " * 12, "status": "in_progress"},
        {"step": "next", "status": "pending"},
        {"step": "outside window", "status": "pending"},
    ]
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static).content
        assert "[>] [b]current[/]" in sidebar
        assert "…" in sidebar
        assert "[ ] next" in sidebar
        assert "outside window" not in sidebar


@pytest.mark.asyncio
async def test_tui_sidebar_renders_all_structured_plan_statuses(tmp_path: Path):
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session._plan = [
        {"step": "queued", "status": "pending"},
        {"step": "running", "status": "in_progress"},
        {"step": "done", "status": "completed"},
        {"step": "waiting", "status": "blocked"},
    ]
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._refresh_sidebar()
        await pilot.pause()
        sidebar = app.query_one("#sidebar", Static)
        # The narrow sidebar retains the focused three-item window, not all
        # four rows in a character-truncated plan string.
        for marker in ("[ ] queued", "[>] running", "[x] done"):
            assert marker in sidebar.content
        assert "[!] waiting" not in sidebar.content


@pytest.mark.parametrize(
    ("active_index", "expected"),
    [
        (0, ["one", "two", "three"]),
        (1, ["one", "two", "three"]),
        (2, ["two", "three", "four"]),
        (3, ["three", "four", "five"]),
        (4, ["four", "five", "six"]),
        (5, ["four", "five", "six"]),
    ],
)
def test_sidebar_plan_window_tracks_each_active_item(active_index: int, expected: list[str]):
    from one.modes.tui_mode import _sidebar_plan_window

    plan = [
        {"step": step, "status": "in_progress" if index == active_index else "pending"}
        for index, step in enumerate(("one", "two", "three", "four", "five", "six"))
    ]

    assert _sidebar_plan_window(plan).splitlines() == [
        f"{'[>]' if step == plan[active_index]['step'] else '[ ]'} {step}" for step in expected
    ]
    assert plan[active_index]["status"] == "in_progress"


@pytest.mark.parametrize("count", [1, 2, 3])
def test_sidebar_plan_window_keeps_short_structured_plans(count: int):
    from one.modes.tui_mode import _sidebar_plan_window

    plan = [{"step": str(index), "status": "pending"} for index in range(count)]
    assert _sidebar_plan_window(plan).splitlines() == [f"[ ] {index}" for index in range(count)]


def test_sidebar_plan_window_focuses_first_unfinished_or_final_completed_items():
    from one.modes.tui_mode import _sidebar_plan_window

    unfinished = [
        {"step": "one", "status": "completed"},
        {"step": "two", "status": "completed"},
        {"step": "three", "status": "blocked"},
        {"step": "four", "status": "pending"},
        {"step": "five", "status": "pending"},
        {"step": "six", "status": "pending"},
    ]
    assert _sidebar_plan_window(unfinished).splitlines() == ["[x] two", "[!] three", "[ ] four"]

    completed = [{"step": str(index), "status": "completed"} for index in range(6)]
    assert _sidebar_plan_window(completed).splitlines() == ["[x] 3", "[x] 4", "[x] 5"]


def test_sidebar_plan_window_handles_legacy_and_compacts_each_structured_item():
    from one.modes.tui_mode import _sidebar_plan_window

    assert _sidebar_plan_window("[x] one\n[>] two\n[ ] three\n[ ] four").splitlines() == [
        "[x] one",
        "[>] two",
        "[ ] three",
    ]
    assert _sidebar_plan_window("unmarked legacy plan") == "unmarked legacy plan"
    long_plan = [{"step": "long\n\nlabel " * 20, "status": "blocked"}]
    row = _sidebar_plan_window(long_plan, max_item_chars=24)
    assert row.startswith("[!] long label")
    assert row.endswith("…")
    assert len(row) <= 24


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
        assert "Tool: × bash" in stream
        assert "Result:" in stream
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
        assert "Tool: × bash" in stream
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
        assert "Tool: ✓ ls" in stream
        assert "Result:" in stream
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
        assert "Tool: ✓ finish" in stream
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
        assert "Tool: ✓ ls" in stream
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
async def test_tui_welcome_logo_is_a_dedicated_non_transcript_widget(tmp_path: Path):
    """Startup artwork is visible initially, visual-only, and separate from the header."""
    from textual.widgets import Static

    from one.modes.tui_mode import _load_tui_logo_lines, _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        assert app._stream_lines == []
        header = str(app.query_one("#header", Static).content)
        logo = str(app.query_one("#logo", Static).content)
        logo_lines = _load_tui_logo_lines()
        assert logo_lines[0] in logo
        assert logo_lines[-1] in logo
        assert logo_lines[0] not in header
        assert all(logo_lines[0] not in str(message) for message in session.messages)
        assert header.splitlines() == [header]
        assert "v" in header
        assert "openai/" in header
        assert "Think: medium" in header
        assert "Coop: off" in header
        assert "Status: ready" in header

        # Initial conversation rendering follows the shared stream container;
        # startup art is not a permanently fixed header.
        app._stream_lines.extend(f"conversation line {index}" for index in range(60))
        app._render_stream()
        await pilot.pause()
        stream = app.query_one("#stream_container")
        assert stream.scroll_y == stream.max_scroll_y

        # A normal update continues following only while already at the tail.
        # The render is frame-scheduled; wait for actual commit and deferred
        # viewport reconcile to clear before asserting tail position.
        app._write("conversation delta")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if (
                app._rendered_stream_revision == app._stream_render_revision
                and not app._stream_viewport_callback_pending
                and app._deferred_stream_viewport is None
            ):
                break
            await pilot.pause(0.01)
        # Don't mask a real lost-tail: assert scroll is actually at the tail.
        assert stream.scroll_y == stream.max_scroll_y


@pytest.mark.asyncio
async def test_tui_main_stream_preserves_scrolled_history_during_agent_activity(tmp_path: Path):
    """Incoming assistant, tool, and child activity must not steal the viewport."""
    from textual.containers import VerticalScroll
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        app._stream_lines.extend(f"history-{index}" for index in range(120))
        app._render_stream()
        await pilot.pause()
        container = app.query_one("#stream_container", VerticalScroll)
        transcript = app.query_one("#stream", Static)
        assert container.max_scroll_y > 10
        container.scroll_to(y=5, animate=False)
        await pilot.pause()
        preserved_y = container.scroll_y
        visible_before = transcript.render_line(int(preserved_y)).text
        assert "history-" in visible_before

        # Use the normal event path for live assistant/tool activity and keep
        # the session marked active while a child-status line arrives.
        app._turn_active = True
        session._emit({"type": "message_start", "message": {"role": "assistant", "content": ""}})
        session._emit(
            {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "delta"}}
        )
        session._emit({"type": "tool_call_start", "tool": "read", "args": {"path": "notes.txt"}})
        app._flush_pending_ui_events()
        app._write("Subagent: child still working")
        await pilot.pause()

        assert container.scroll_y == preserved_y
        assert container.scroll_y < container.max_scroll_y
        assert transcript.render_line(int(container.scroll_y)).text == visible_before

        # Returning to the tail explicitly re-enables follow for the next delta.
        container.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        session._emit(
            {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": " again"}}
        )
        app._flush_pending_ui_events()
        await pilot.pause()
        assert container.scroll_y == container.max_scroll_y


@pytest.mark.asyncio
async def test_tui_main_stream_trimming_preserves_scrolled_viewport(tmp_path: Path):
    """Front trimming retains the reader's visible history anchor when possible."""
    from textual.containers import VerticalScroll
    from textual.widgets import Static

    from one.modes.tui_mode import MAX_RENDERED_LINES, _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        app._stream_lines.extend(f"history-{index}" for index in range(MAX_RENDERED_LINES))
        app._render_stream()
        await pilot.pause()
        container = app.query_one("#stream_container", VerticalScroll)
        container.scroll_to(y=50, animate=False)
        await pilot.pause()
        transcript = app.query_one("#stream", Static)
        content_origin = transcript.region.y - container.region.y + container.scroll_y
        visible_before = transcript.render_line(int(container.scroll_y - content_origin)).text
        assert "history-" in visible_before

        for index in range(12):
            app._write(f"new-{index}")
        await pilot.pause()

        assert 0 <= container.scroll_y <= container.max_scroll_y
        assert container.scroll_y < container.max_scroll_y
        content_origin = transcript.region.y - container.region.y + container.scroll_y
        assert transcript.render_line(int(container.scroll_y - content_origin)).text == visible_before


@pytest.mark.asyncio
async def test_tui_resize_reflows_stream_cache_and_preserves_trim_anchor(tmp_path: Path):
    """A resize invalidates cached lines and a heavily wrapped trim keeps the reader's row."""
    from textual.containers import VerticalScroll
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        # Direct setup deliberately exceeds the later physical-row cap so the
        # following normal write removes more than the 256-row anchor search.
        app._stream_lines.extend(
            " ".join(f"anchor-{index:03d}-part-{part} " + "x" * 5 for part in range(12))
            for index in range(1_000)
        )
        app._render_stream()
        await pilot.pause()
        old_width = app._stream_cache_width
        old_height = app.query_one("#stream", Static).virtual_size.height

        await pilot.resize_terminal(60, 24)
        await pilot.pause()
        transcript = app.query_one("#stream", Static)
        assert app._stream_cache_width != old_width
        assert transcript.virtual_size.height > old_height
        assert app._apply_theme("light")
        # Resize and theme invalidation share the frame-capped renderer.  Wait
        # for the scheduled render that owns the cache transition rather than
        # assuming it completes in an arbitrary sleep interval.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (
                app._stream_cache_theme == "light"
                and app._rendered_stream_revision == app._stream_render_revision
                and not app._stream_render_dirty
            ):
                break
            await pilot.pause(0.01)
        assert app._stream_cache_theme == "light"
        assert app._rendered_stream_revision == app._stream_render_revision
        assert not app._stream_render_dirty
        assert app._stream_content_cache

        container = app.query_one("#stream_container", VerticalScroll)
        container.scroll_to(y=5_400, animate=False, immediate=True)
        await pilot.pause()
        content_origin = transcript.region.y - container.region.y + container.scroll_y
        visible_before = transcript.render_line(int(container.scroll_y - content_origin)).text
        assert "anchor-" in visible_before

        # The trim drops thousands of physical rows, rather than just the
        # logical-line count. The reader's selected row remains retained and
        # is therefore a valid anchor candidate for reconciliation.
        app._stream_lines.append("new tail " + "y" * 180)
        app._trim_stream(render=False)
        assert app._stream_front_trim_pending > 256
        assert visible_before.split()[0].rsplit("-part", 1)[0] in "\n".join(app._stream_lines)
        app._render_stream()
        await pilot.pause()


@pytest.mark.asyncio
async def test_tui_session_rebind_invalidates_pending_viewport_callback(tmp_path: Path):
    """An old session's queued post-layout viewport restore cannot run after rebind."""
    from textual.containers import VerticalScroll

    from one.modes.tui_mode import _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        app._stream_lines.extend(f"history-{index}" for index in range(120))
        app._render_stream()
        container = app.query_one("#stream_container", VerticalScroll)
        app._queue_stream_viewport_reconcile(
            container,
            follow=True,
            previous_scroll_y=container.scroll_y,
            previous_max_scroll_y=container.max_scroll_y,
            previous_scroll_target_y=container.scroll_target_y,
            anchor=None,
            front_trim_count=0,
        )
        callback_token = app._stream_viewport_callback_token
        app.session = _mk_app_session(tmp_path)
        app._bind_session()
        assert app._deferred_stream_viewport is None
        assert callback_token != app._stream_viewport_callback_token
        app._reconcile_stream_viewport(callback_token)
        await pilot.pause()
        assert app._deferred_stream_viewport is None


@pytest.mark.asyncio
async def test_tui_deferred_tail_follow_coalesces_and_respects_user_scroll(tmp_path: Path):
    """A queued tail callback must not override a later wheel/scrollbar intent."""
    from textual import events
    from textual.containers import VerticalScroll

    from one.modes.tui_mode import _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        app._stream_lines.extend(f"history-{index}" for index in range(120))
        app._render_stream()
        await pilot.pause()
        container = app.query_one("#stream_container", VerticalScroll)
        container.scroll_end(animate=False, immediate=True)
        await pilot.pause()

        # Render schedules tail follow, then a real wheel event changes the
        # user's target before Textual's deferred layout callback can run.
        app._write("queued delta")
        container.post_message(events.MouseScrollUp(container, 1, 1, 0, -1, 0, False, False, False))
        await pilot.pause()
        assert container.scroll_y < container.max_scroll_y

        # Two frame-capped paints can complete before Static's wrapped height
        # settles. The latest pending tail intent must follow the final layout,
        # not an intermediate max_scroll_y.
        container.scroll_end(animate=False, immediate=True)
        app._write("first coalesced delta")
        await asyncio.sleep(1 / 30 * 1.1)
        app._write("second coalesced delta")
        # One barrier applies each scheduled paint; extra refresh barriers are
        # intentional because the deferred viewport reconcile waits for the
        # final wrapped layout without issuing another transcript paint.
        for _ in range(4):
            await pilot.pause()
        assert container.scroll_y == container.max_scroll_y


@pytest.mark.asyncio
async def test_tui_deferred_history_restore_respects_later_user_scroll(tmp_path: Path):
    """A queued trim anchor must not restore an older reading position."""
    from textual import events
    from textual.containers import VerticalScroll

    from one.modes.tui_mode import MAX_RENDERED_LINES, _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        app._stream_lines.extend(f"history-{index}" for index in range(MAX_RENDERED_LINES))
        app._render_stream()
        await pilot.pause()
        container = app.query_one("#stream_container", VerticalScroll)
        container.scroll_to(y=80, animate=False, immediate=True)
        await pilot.pause()

        # This update trims the front and queues a non-follow anchor restore.
        app._write("trimmed update")
        # A later reader movement must invalidate that stale restore.
        container.post_message(events.MouseScrollUp(container, 1, 1, 0, -1, 0, False, False, False))
        await pilot.pause()
        assert container.scroll_y < 80


def test_tui_stream_anchor_search_is_bounded_and_rejects_duplicates() -> None:
    """Long wrapped output cannot turn one refresh callback into a full scan."""
    from one.modes.tui_mode import _STREAM_ANCHOR_SEARCH_ROWS, _OneTextualApp

    class _Line:
        def __init__(self, text: str) -> None:
            self.text = text

    class _Stream:
        class virtual_size:  # noqa: N801 - mirrors Textual's public attribute
            height = 100_000

        def __init__(self) -> None:
            self.calls: list[int] = []

        def render_line(self, row: int) -> _Line:
            self.calls.append(row)
            return _Line("duplicate" if row in {49_999, 50_001} else "other")

    stream = _Stream()
    assert _OneTextualApp._find_stream_anchor_row(stream, "duplicate", 50_000) is None  # type: ignore[arg-type]
    assert len(stream.calls) <= 2 * _STREAM_ANCHOR_SEARCH_ROWS + 1


@pytest.mark.asyncio
async def test_tui_full_logo_lines_are_visible_in_large_rendered_viewport(tmp_path: Path):
    """A viewport large enough for the artwork renders both its first and last lines."""
    from textual.widgets import Static

    from one.modes.tui_mode import _load_tui_logo_lines, _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        logo = app.query_one("#logo", Static)
        visible_logo = "\n".join(logo.render_line(y).text for y in range(logo.size.height))
        logo_lines = _load_tui_logo_lines()
        assert logo_lines[0] in visible_logo
        assert logo_lines[-1] in visible_logo


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
        args_start = content.plain.index('{"x": "[b]"}')
        status_start = content.plain.index(status_line) + status_line.rindex(" [ok]")
        error_start = content.plain.index(error_line) + error_line.rindex("[err]")

        assert content.get_style_at_offset(heading_start).bold is True
        assert not content.get_style_at_offset(args_start).bold
        assert not content.get_style_at_offset(args_start + args_line.index("[b]")).bold
        assert content.get_style_at_offset(status_start + 1).bold is True
        assert content.get_style_at_offset(error_start + 1).bold is True


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
        assert all(index == 0 or app._stream_lines[index - 1] == "" for index in separators)


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
async def test_tui_tool_lifecycle_and_result_panels_have_distinct_headings(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit({"type": "tool_call_start", "tool": "read", "args": {"path": "a.txt"}})
        await pilot.pause()
        assert "Tool: ○" in "\n".join(app._stream_lines)

        session._emit(
            {"type": "tool_call_end", "tool": "read", "ok": True, "result": {"outputText": "file body"}}
        )
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "Tool: ✓ read" in stream
        assert "Result:" in stream
        assert "Result: ○" not in stream


@pytest.mark.asyncio
async def test_tui_persisted_tool_result_and_manual_bash_output_use_local_panels(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session.messages.extend([
        {"role": "toolResult", "content": json.dumps({"outputText": "saved output", "args": {"secret": "hidden"}})},
        {"role": "bashExecution", "command": "cat legacy.txt", "output": "\x1b[31mlegacy [b] output\x1b[0m", "exitCode": 0},
    ])
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._rebuild_session_transcript()
        rebuilt = "\n".join(app._stream_lines)
        assert "Result:" in rebuilt
        assert "saved output" in rebuilt and '"args"' not in rebuilt and "hidden" not in rebuilt
        assert "Command:" in rebuilt
        assert "/bash cat legacy.txt" in rebuilt
        assert "legacy [b] output" in rebuilt
        assert "\x1b" not in rebuilt

        async def execute_tui_command(command: str) -> dict[str, object]:
            assert command == "echo result"
            return {"output": "bash output", "content": [{"type": "text", "text": "bash output"}], "exitCode": 0}

        session.execute_tui_command = execute_tui_command  # type: ignore[method-assign]
        await app._handle_command("/bash echo result")
        stream = "\n".join(app._stream_lines)
        assert stream.count("Result:") == 3
        assert "bash output" in stream
        app._write_manual_result(
            "\n".join("[b]" for _ in range(45)),
            exit_code=0,
            timed_out=False,
            cancelled=False,
            truncated=False,
            full_output_path=None,
            stored=True,
        )
        stream = "\n".join(app._stream_lines)
        assert "Output display truncated; full output remains in current session history" in stream
        assert "evidence" not in stream.lower()
        session.settings_manager.set_bash_show_output(False)
        app._rebuild_session_transcript()
        hidden = "\n".join(app._stream_lines)
        assert "Command:" in hidden and "/bash cat legacy.txt" in hidden
        assert "exitCode=0" in hidden
        assert "legacy [b] output" not in hidden

        app._stream_lines = []
        app._write_manual_result(
            "\n".join("[b]" for _ in range(45)),
            exit_code=0,
            timed_out=False,
            cancelled=False,
            truncated=False,
            full_output_path=None,
            stored=False,
        )
        unstored = "\n".join(app._stream_lines)
        assert "Output display truncated" in unstored
        assert "session history" not in unstored and "evidence" not in unstored
        app._stream_lines = []
        app._write_manual_result(
            "\n".join("[b]" for _ in range(45)),
            exit_code=7,
            timed_out=False,
            cancelled=False,
            truncated=True,
            full_output_path="/missing/manual.log",
            stored=True,
        )
        replay = "\n".join(app._stream_lines)
        assert "recorded raw output log: /missing/manual.log" in replay
        assert "exitCode=7" in replay
        app._stream_lines = []
        crafted_path = "\x1b[31m/missing/[b]\nlog\x1b[0m" + "x" * 300
        app._write_manual_result(
            "\n".join("line" for _ in range(45)),
            exit_code=0,
            timed_out=False,
            cancelled=False,
            truncated=True,
            full_output_path=crafted_path,
            stored=True,
        )
        crafted = "\n".join(app._stream_lines)
        log_notice = next(line for line in app._stream_lines if "recorded raw output log:" in line)
        assert "\x1b" not in crafted
        assert "\n" not in log_notice
        assert "[b] log" in log_notice
        assert len(log_notice.split("recorded raw output log: ", 1)[1].rstrip("]")) <= 200


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
