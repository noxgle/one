# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import mock

import pytest

from one.core.agent_runtime_facade import AgentRuntimeFacade
from one.core.agent_session import AgentSession, approval_callback_origin
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.core.threaded_agent_runtime import RuntimeBootstrapSpec
from one.core.tui_runtime_host import TUI_COMMANDS, TuiRuntimeHost
from one.modes.tui_mode import (
    BUILTIN_TUI_THEMES,
    _OneTextualApp,
)


def _mk_session(tmp_path: Path) -> AgentSession:
    from tests.support.tui import _FakeLoader

    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth, str(tmp_path / "models.json"))
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4}, "bash": {"showOutput": True}})
    sm = SessionManager.in_memory(str(tmp_path))
    return AgentSession(sm, settings, registry, _FakeLoader(), model, "medium")


# ---------------------------------------------------------------------------
# Fix 1: Evidence overlay theme support — all built-in themes, full widget tree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_overlay_themed_in_all_builtin_themes(tmp_path: Path) -> None:
    """Every built-in theme must apply background/border/fg to the evidence overlay
    and its child widgets via _apply_theme."""
    from textual.color import Color

    for theme_name in BUILTIN_TUI_THEMES:
        session = _mk_session(tmp_path)
        app = _OneTextualApp(session)
        async with app.run_test() as pilot:
            await pilot.pause()
            result = app._apply_theme(theme_name)
            assert result is True

            evidence_overlay = app.query_one("#evidence_overlay")
            assert evidence_overlay.styles.background == Color.parse(BUILTIN_TUI_THEMES[theme_name].panel_bg)
            assert evidence_overlay.styles.color == Color.parse(BUILTIN_TUI_THEMES[theme_name].screen_fg)

            evidence_header = app.query_one("#evidence_header")
            assert evidence_header.styles.color == Color.parse(BUILTIN_TUI_THEMES[theme_name].screen_fg)

            evidence_body = app.query_one("#evidence_body")
            assert evidence_body.styles.background == Color.parse(BUILTIN_TUI_THEMES[theme_name].panel_bg)
            assert evidence_body.styles.color == Color.parse(BUILTIN_TUI_THEMES[theme_name].screen_fg)

            evidence_content = app.query_one("#evidence_content")
            assert evidence_content.styles.color == Color.parse(BUILTIN_TUI_THEMES[theme_name].screen_fg)

            evidence_actions = app.query_one("#evidence_actions")
            assert evidence_actions.styles.background == Color.parse(BUILTIN_TUI_THEMES[theme_name].panel_bg)

            prev = app.query_one("#evidence-prev")
            assert prev.styles.background == Color.parse(BUILTIN_TUI_THEMES[theme_name].user_bg)
            assert prev.styles.color == Color.parse(BUILTIN_TUI_THEMES[theme_name].user)

            nxt = app.query_one("#evidence-next")
            assert nxt.styles.background == Color.parse(BUILTIN_TUI_THEMES[theme_name].user_bg)
            assert nxt.styles.color == Color.parse(BUILTIN_TUI_THEMES[theme_name].user)

            close = app.query_one("#evidence-close")
            assert close.styles.background == Color.parse(BUILTIN_TUI_THEMES[theme_name].panel_bg)
            assert close.styles.color == Color.parse(BUILTIN_TUI_THEMES[theme_name].error)


@pytest.mark.asyncio
async def test_evidence_overlay_theme_change_while_viewer_open(tmp_path: Path) -> None:
    """When the evidence viewer is open and the theme changes, overlay colors update
    immediately without closing the viewer."""
    from textual.color import Color

    session = _mk_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Apply default theme explicitly (simulating on_mount).
        app._apply_theme("default")
        await pilot.pause()

        # Open the evidence viewer (simulate _show_evidence_panel).
        app._evidence_panel = {"text": "full result", "offset": 0, "evidenceId": "e1"}
        overlay = app.query_one("#evidence_overlay")
        overlay.add_class("visible")

        # Verify default theme colors are applied.
        default_bg = Color.parse(BUILTIN_TUI_THEMES["default"].panel_bg)
        assert overlay.styles.background == default_bg

        # Switch to a different theme while visible.
        app._apply_theme("hacker")
        await pilot.pause()

        # Colors must change but overlay stays visible.
        hacker_bg = Color.parse(BUILTIN_TUI_THEMES["hacker"].panel_bg)
        assert overlay.styles.background == hacker_bg
        assert overlay.has_class("visible")
        content = app.query_one("#evidence_content")
        assert content.styles.color == Color.parse(BUILTIN_TUI_THEMES["hacker"].screen_fg)


@pytest.mark.asyncio
async def test_evidence_overlay_themed_borders_all_themes(tmp_path: Path) -> None:
    """Borders must follow theme panel_border for every built-in theme."""
    from textual.color import Color

    for theme_name in BUILTIN_TUI_THEMES:
        session = _mk_session(tmp_path)
        app = _OneTextualApp(session)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._apply_theme(theme_name)

            evidence_overlay = app.query_one("#evidence_overlay")
            expected_border = Color.parse(BUILTIN_TUI_THEMES[theme_name].panel_border)
            assert evidence_overlay.styles.border_top[1] == expected_border


@pytest.mark.asyncio
async def test_evidence_panel_render_and_theme_change(tmp_path: Path) -> None:
    """Render an actual evidence panel via _evidence_panel setter + _render_evidence_panel,
    then change theme."""
    from textual.color import Color
    from textual.widgets import Static

    session = _mk_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._apply_theme("default")
        await pilot.pause()

        # Simulate opening the evidence panel with real content.
        evidence_text = "Full tool result: {'key': 'value', 'count': 42}"
        app._evidence_panel = {"text": evidence_text, "offset": 0, "evidenceId": "e1"}
        app._render_evidence_panel()
        await pilot.pause()

        # Verify panel is open and content visible.
        assert app._evidence_panel is not None
        assert app._evidence_panel["text"] == evidence_text
        overlay = app.query_one("#evidence_overlay")
        assert overlay.has_class("visible")
        content_widget = app.query_one("#evidence_content", Static)
        assert "Full tool result" in str(content_widget.content)

        # Change theme — content stays, colors update.
        app._apply_theme("hacker")
        await pilot.pause()
        assert overlay.has_class("visible")
        hacker_fg = Color.parse(BUILTIN_TUI_THEMES["hacker"].screen_fg)
        assert content_widget.styles.color == hacker_fg
        assert "Full tool result" in str(content_widget.content)


# ---------------------------------------------------------------------------
# Fix 2: Real runtime bridge approval test — host._approval_callback → facade → Textual
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_bridge_approval_displayArgs_safe_args_redacted(tmp_path: Path) -> None:
    """host._approval_callback('bash', {'command':'echo safe', 'password':'secret'})
    must project sanitized displayArgs through the true session→host→facade→view→Textual path.
    Visible safe args present, secret absent. FIFO main/child requests preserved."""
    from one.modes.tui_mode import TuiSessionView

    session = _mk_session(tmp_path)

    class Owner:
        def __init__(self, session):
            self.session = session

        async def dispose(self):
            pass

    owner = Owner(session)
    host = TuiRuntimeHost(owner, None)
    host._snapshot = lambda: {"epoch": host._epoch, "sidebar": {}, "messages": []}

    # Track emitted events to verify they contain sanitized displayArgs.
    emitted_events = []

    def capture_events(event):
        emitted_events.append(dict(event))

    session.subscribe(capture_events)

    def factory(_spec):
        return host

    facade = AgentRuntimeFacade(
        RuntimeBootstrapSpec(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")),
        factory,
        command_allowlist=TUI_COMMANDS,
    )
    await facade.start()

    view = TuiSessionView(facade)
    await view.refresh()
    app = _OneTextualApp(view)

    try:
        async with app.run_test() as pilot:
            await pilot.pause()

            # Start approval in a background task — it blocks until resolved.
            async def trigger_approval():
                token = approval_callback_origin.set(session)
                try:
                    return await host._approval_callback("bash", {
                        "command": "echo safe",
                        "password": "secret",
                        "api_key": "sk-123",
                    })
                finally:
                    approval_callback_origin.reset(token)

            approval_task = asyncio.create_task(trigger_approval())

            # Wait until the TUI has received the approval.
            for _ in range(30):
                await pilot.pause(0.02)
                if app._approval_pending is not None:
                    break

            # Verify the emitted event contains sanitized displayArgs.
            assert len(emitted_events) >= 1
            approval_event = emitted_events[-1]
            assert approval_event["type"] == "tui_approval_request"
            assert approval_event["tool"] == "bash"

            # displayArgs should contain safe command but NOT secret/api_key.
            display_args = approval_event.get("displayArgs", "")
            assert "echo safe" in display_args
            assert "secret" not in display_args
            assert "sk-123" not in display_args

            # Verify the TUI received the approval and populated pending.
            assert app._approval_pending is not None
            pending = app._approval_pending
            assert pending["tool"] == "bash"
            assert "echo safe" in str(pending.get("displayArgs", ""))

            # Verify _approval_summary shows safe args, not secrets.
            summary_text = str(app._approval_summary(pending))
            assert "echo safe" in summary_text
            assert "secret" not in summary_text.lower()
            assert "sk-123" not in summary_text

            # Resolve the approval via the TUI to unblock the host.
            app.action_approve_tool()
            await pilot.pause()

            # The host's approval future should now be resolved.
            result = await approval_task
            assert result == (True, "")

    finally:
        await facade.close()
        assert facade.finalize()


@pytest.mark.asyncio
async def test_runtime_bridge_approval_fifo_main_child(tmp_path: Path) -> None:
    """Multiple host._approval_callback calls must queue in FIFO order.
    Main request first, child request second. Resolved sequentially."""
    from one.modes.tui_mode import TuiSessionView

    session = _mk_session(tmp_path)

    class Owner:
        def __init__(self, session):
            self.session = session

        async def dispose(self):
            pass

    owner = Owner(session)
    host = TuiRuntimeHost(owner, None)
    host._snapshot = lambda: {"epoch": host._epoch, "sidebar": {}, "messages": []}

    session.subscribe(lambda e: None)  # dummy listener to keep session happy

    def factory(_spec):
        return host

    facade = AgentRuntimeFacade(
        RuntimeBootstrapSpec(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")),
        factory,
        command_allowlist=TUI_COMMANDS,
    )
    await facade.start()

    view = TuiSessionView(facade)
    await view.refresh()
    app = _OneTextualApp(view)

    emitted_events = []
    session.subscribe(emitted_events.append)

    try:
        async with app.run_test() as pilot:
            await pilot.pause()

            # Queue main approval.
            async def trigger_main():
                token = approval_callback_origin.set(session)
                try:
                    return await host._approval_callback("bash", {"command": "main-cmd"})
                finally:
                    approval_callback_origin.reset(token)

            main_task = asyncio.create_task(trigger_main())

            # Wait until main approval is queued and emitted.
            for _ in range(30):
                await pilot.pause(0.02)
                if len(app._approval_waiters) >= 1:
                    break

            assert len(app._approval_waiters) == 1
            assert app._approval_pending.get("tool") == "bash"

            # Approve main.
            app.action_approve_tool()
            await pilot.pause()
            main_result = await main_task
            assert main_result == (True, "")

            # After main is resolved, child approval should be emitted.
            # Trigger child approval.
            async def trigger_child():
                token = approval_callback_origin.set(session)
                try:
                    return await host._approval_callback("write", {"path": "child.txt"})
                finally:
                    approval_callback_origin.reset(token)

            child_task = asyncio.create_task(trigger_child())

            for _ in range(30):
                await pilot.pause(0.02)
                if len(app._approval_waiters) >= 1:
                    break

            # Child should now be pending.
            assert app._approval_pending.get("tool") == "write"

            # Approve child.
            app.action_approve_tool()
            await pilot.pause()
            child_result = await child_task
            assert child_result == (True, "")

    finally:
        await facade.close()
        assert facade.finalize()


@pytest.mark.asyncio
async def test_runtime_bridge_approval_resolved_via_facade(tmp_path: Path) -> None:
    """Approval resolved via facade approval_resolve must clear pending and clean up epoch."""
    from one.modes.tui_mode import TuiSessionView

    session = _mk_session(tmp_path)

    class Owner:
        def __init__(self, session):
            self.session = session

        async def dispose(self):
            pass

    owner = Owner(session)
    host = TuiRuntimeHost(owner, None)
    host._snapshot = lambda: {"epoch": host._epoch, "sidebar": {}, "messages": []}

    session.subscribe(lambda e: None)

    def factory(_spec):
        return host

    facade = AgentRuntimeFacade(
        RuntimeBootstrapSpec(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")),
        factory,
        command_allowlist=TUI_COMMANDS,
    )
    await facade.start()

    view = TuiSessionView(facade)
    await view.refresh()
    app = _OneTextualApp(view)

    try:
        async with app.run_test() as pilot:
            await pilot.pause()

            # Trigger approval.
            async def trigger_approval():
                token = approval_callback_origin.set(session)
                try:
                    return await host._approval_callback("bash", {"command": "test"})
                finally:
                    approval_callback_origin.reset(token)

            task = asyncio.create_task(trigger_approval())
            await pilot.pause()
            await pilot.pause()

            assert app._approval_pending is not None
            approval_id = app._approval_pending.get("id")
            epoch = app._approval_pending.get("epoch")

            # Resolve via facade command with correct param names.
            await facade.command("approval_resolve", {
                "approval_id": approval_id,
                "approved": True,
                "reason": "",
                "epoch": epoch,
            })
            await pilot.pause()

            # The approval future should be resolved.
            result = await task
            assert result == (True, "")

    finally:
        await facade.close()
        assert facade.finalize()


# ---------------------------------------------------------------------------
# Fix 3: Sidebar consolidation — parametrized on/off × count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sidebar_subagent_running_consolidated(tmp_path: Path) -> None:
    """Sidebar must show 'Subagents: on (N running)' not separate lines."""
    session = _mk_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Mock build_sidebar_snapshot to return desired sidebar data.
        mock_sidebar = {
            "subagents": True, "coop": "on", "bashOutput": False,
            "model": "test/model", "thinking": "medium", "temperature": 0.5,
            "temperatureMode": "balanced", "cwd": str(tmp_path), "sessionId": "test",
            "streaming": False, "compacting": False, "retry": "on",
            "contextPercent": 0.0, "contextKnown": True, "deliveryMode": "idle",
            "mcpEnabled": False, "mcpServers": [],
        }
        app._monitor_snapshot = {"children": []}
        with mock.patch("one.modes.tui_mode.build_sidebar_snapshot", return_value=mock_sidebar):
            app._refresh_sidebar()
            await pilot.pause()

        # Check sidebar widget contains the consolidated subagent line.
        sidebar_text = str(app.query_one("#sidebar").content)
        subagent_line = [l for l in sidebar_text.split("\n") if "Subagents:" in l]
        assert len(subagent_line) == 1
        assert "(0 running)" in subagent_line[0]
        assert "Agents:" not in sidebar_text


@pytest.mark.asyncio
async def test_sidebar_subagent_running_count_1(tmp_path: Path) -> None:
    """Sidebar must show correct running count (1)."""
    session = _mk_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        mock_sidebar = {
            "subagents": True, "coop": "on", "bashOutput": False,
            "model": "test/model", "thinking": "medium", "temperature": 0.5,
            "temperatureMode": "balanced", "cwd": str(tmp_path), "sessionId": "test",
            "streaming": False, "compacting": False, "retry": "on",
            "contextPercent": 0.0, "contextKnown": True, "deliveryMode": "idle",
            "mcpEnabled": False, "mcpServers": [],
        }
        app._monitor_snapshot = {"children": [{"status": "running", "sessionId": "c1"}]}
        with mock.patch("one.modes.tui_mode.build_sidebar_snapshot", return_value=mock_sidebar):
            app._refresh_sidebar()
            await pilot.pause()

        sidebar_text = str(app.query_one("#sidebar").content)
        subagent_line = [l for l in sidebar_text.split("\n") if "Subagents:" in l]
        assert len(subagent_line) == 1
        assert "(1 running)" in subagent_line[0]


@pytest.mark.asyncio
async def test_sidebar_subagent_running_count_2(tmp_path: Path) -> None:
    """Sidebar must show correct running count (2) and exclude non-running."""
    session = _mk_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        mock_sidebar = {
            "subagents": True, "coop": "on", "bashOutput": False,
            "model": "test/model", "thinking": "medium", "temperature": 0.5,
            "temperatureMode": "balanced", "cwd": str(tmp_path), "sessionId": "test",
            "streaming": False, "compacting": False, "retry": "on",
            "contextPercent": 0.0, "contextKnown": True, "deliveryMode": "idle",
            "mcpEnabled": False, "mcpServers": [],
        }
        app._monitor_snapshot = {
            "children": [
                {"status": "running", "sessionId": "c1"},
                {"status": "completed", "sessionId": "c2"},
                {"status": "running", "sessionId": "c3"},
            ]
        }
        with mock.patch("one.modes.tui_mode.build_sidebar_snapshot", return_value=mock_sidebar):
            app._refresh_sidebar()
            await pilot.pause()

        sidebar_text = str(app.query_one("#sidebar").content)
        subagent_line = [l for l in sidebar_text.split("\n") if "Subagents:" in l]
        assert len(subagent_line) == 1
        assert "(2 running)" in subagent_line[0]
        assert "Agents:" not in sidebar_text


@pytest.mark.asyncio
async def test_sidebar_subagent_off(tmp_path: Path) -> None:
    """Sidebar must show 'Subagents: off (N running)' when disabled."""
    session = _mk_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        mock_sidebar = {
            "subagents": False, "coop": "on", "bashOutput": False,
            "model": "test/model", "thinking": "medium", "temperature": 0.5,
            "temperatureMode": "balanced", "cwd": str(tmp_path), "sessionId": "test",
            "streaming": False, "compacting": False, "retry": "on",
            "contextPercent": 0.0, "contextKnown": True, "deliveryMode": "idle",
            "mcpEnabled": False, "mcpServers": [],
        }
        app._monitor_snapshot = {"children": []}
        with mock.patch("one.modes.tui_mode.build_sidebar_snapshot", return_value=mock_sidebar):
            app._refresh_sidebar()
            await pilot.pause()

        sidebar_text = str(app.query_one("#sidebar").content)
        subagent_line = [l for l in sidebar_text.split("\n") if "Subagents:" in l]
        assert len(subagent_line) == 1
        assert "Subagents: off (0 running)" in subagent_line[0]


# ---------------------------------------------------------------------------
# Parametrized sidebar tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("subagents_on,count", [
    (True, 0), (True, 1), (True, 2),
    (False, 0), (False, 1), (False, 2),
])
async def test_sidebar_parametrized_subagent_status(tmp_path: Path, subagents_on: bool, count: int) -> None:
    """Sidebar must show correct Subagents: on/off (N running) for all combinations."""
    session = _mk_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        children = [{"status": "running", "sessionId": f"c{i}"} for i in range(count)]
        app._monitor_snapshot = {"children": children}
        mock_sidebar = {
            "subagents": subagents_on, "coop": "on", "bashOutput": False,
            "model": "test/model", "thinking": "medium", "temperature": 0.5,
            "temperatureMode": "balanced", "cwd": str(tmp_path), "sessionId": "test",
            "streaming": False, "compacting": False, "retry": "on",
            "contextPercent": 0.0, "contextKnown": True, "deliveryMode": "idle",
            "mcpEnabled": False, "mcpServers": [],
        }
        with mock.patch("one.modes.tui_mode.build_sidebar_snapshot", return_value=mock_sidebar):
            app._refresh_sidebar()
            await pilot.pause()

        sidebar_text = str(app.query_one("#sidebar").content)
        subagent_line = [l for l in sidebar_text.split("\n") if "Subagents:" in l]
        assert len(subagent_line) == 1
        expected_status = "on" if subagents_on else "off"
        assert f"Subagents: {expected_status} ({count} running)" in subagent_line[0]


# ---------------------------------------------------------------------------
# Approval displayArgs edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_displayArgs_redaction_and_fallback(tmp_path: Path) -> None:
    """displayArgs longer than 512 chars must be truncated.  Missing displayArgs
    must fall back to args (legacy path).  FIFO ordering is unchanged."""
    from one.modes.tui_mode import SessionEvent

    session = _mk_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Oversized displayArgs — must be bounded to 512.
        long_args = "x" * 1024
        await app.on_session_event(
            SessionEvent(
                {
                    "type": "tui_approval_request",
                    "approvalId": "a-overflow",
                    "tool": "write",
                    "origin": None,
                    "epoch": 1,
                    "displayArgs": long_args,
                }
            )
        )
        await pilot.pause()

        assert app._approval_pending is not None
        assert len(app._approval_pending.get("displayArgs", "")) <= 512

        # No displayArgs — fallback to args{}.
        app._approval_pending = None
        app._approval_waiters.clear()
        await app.on_session_event(
            SessionEvent(
                {
                    "type": "tui_approval_request",
                    "approvalId": "a-legacy",
                    "tool": "write",
                    "args": {"path": "secret.txt", "content": "password123"},
                    "origin": None,
                    "epoch": 2,
                }
            )
        )
        await pilot.pause()

        assert app._approval_pending is not None
        assert app._approval_pending.get("displayArgs") is None
        summary_text = str(app._approval_summary(app._approval_pending))
        assert "{}" in summary_text or "path" in summary_text


@pytest.mark.asyncio
async def test_approval_fifo_unchanged(tmp_path: Path) -> None:
    """Multiple tui_approval_request events must be queued in FIFO order."""
    from one.modes.tui_mode import SessionEvent

    session = _mk_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        for i in range(3):
            await app.on_session_event(
                SessionEvent(
                    {
                        "type": "tui_approval_request",
                        "approvalId": f"a-{i}",
                        "tool": "bash",
                        "origin": None,
                        "epoch": i,
                        "displayArgs": f'{{"command": "cmd{i}"}}',
                    }
                )
            )
            await pilot.pause()

        assert len(app._approval_waiters) == 3
        assert app._approval_pending is not None
        assert app._approval_pending["id"] == "a-0"
        assert app._approval_waiters[1]["id"] == "a-1"
        assert app._approval_waiters[2]["id"] == "a-2"
