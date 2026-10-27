# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from textual.widgets import Button, Input, Static, TextArea

from one.core.tui_runtime_host import TuiRuntimeHost
from one.modes.tui_mode import _CommandCenter, _McpBrowser, _McpDetails, _OneTextualApp
from tests.support.tui import _mk_app_session


class _Settings:
    def __init__(self) -> None:
        self.servers = {"alpha": {"command": "safe-command", "env": {"TOKEN": "secret"}}}
        self.persisted: list[tuple[str, bool]] = []

    def get_mcp_servers(self) -> dict:
        return self.servers

    def set_mcp_server_enabled(self, name: str, enabled: bool) -> None:
        self.persisted.append((name, enabled))
        self.servers[name]["enabled"] = enabled


class _McpManager:
    def __init__(self) -> None:
        self.statuses = [{
            "name": "alpha",
            "enabled": False,
            "running": False,
            "transport": "stdio",
            "runtimeState": "disabled",
            "tools": ["read_file", "x" * 80],
            "command": "safe-command --token secret",
            "args": ["--secret", "secret"],
            "env": {"TOKEN": "secret"},
            "url": "https://token:secret@example.test",
            "error": "secret diagnostic",
            "restartAttempts": 2,
            "maxRestartAttempts": 3,
        }]
        self.enabled: list[str] = []
        self.disabled: list[str] = []

    def server_status(self) -> list[dict]:
        return [dict(item) for item in self.statuses]

    async def enable_server(self, name, command, args, env, *, url=None) -> list[str]:  # noqa: ANN001
        self.enabled.append(name)
        self.statuses[0].update(enabled=True, running=True, runtimeState="running")
        return list(self.statuses[0]["tools"])

    async def disable_server(self, name: str) -> list[str]:
        self.disabled.append(name)
        self.statuses[0].update(enabled=False, running=False, runtimeState="disabled")
        return list(self.statuses[0]["tools"])


class _HostSession:
    def __init__(self) -> None:
        self.is_streaming = False
        self._is_readonly = False
        self.settings_manager = _Settings()
        self.sync_count = 0

    def sync_mcp_tools(self) -> None:
        self.sync_count += 1


def _host() -> tuple[TuiRuntimeHost, _HostSession, _McpManager]:
    session = _HostSession()
    manager = _McpManager()
    return TuiRuntimeHost(SimpleNamespace(session=session), manager), session, manager


@pytest.mark.asyncio
async def test_mcp_browser_host_safe_projection_persistence_and_rejections() -> None:
    host, session, manager = _host()

    listing = await host.ui_operation("mcp_browser_list", {"offset": 0, "limit": 100})
    assert listing["mutable"] is True
    assert listing["blockedReason"] == ""
    assert listing["servers"] == [{
        "name": "alpha", "enabled": False, "running": False, "transport": "stdio",
        "runtimeState": "disabled", "toolCount": 2, "tools": ["read_file", "x" * 80],
        "toolsTruncated": False, "restartAttempts": 2, "maxRestartAttempts": 3, "hasError": True,
    }]
    rendered = str(listing)
    for secret in ("safe-command", "TOKEN", "https://", "diagnostic", "secret"):
        assert secret not in rendered

    assert await host.ui_operation("mcp_browser_set", {"name": "alpha", "enabled": True}) == {"ok": True}
    assert manager.enabled == ["alpha"]
    assert session.settings_manager.persisted == [("alpha", True)]
    assert session.sync_count == 1

    session.is_streaming = True
    with pytest.raises(ValueError, match="Stop or wait"):
        await host.ui_operation("mcp_browser_set", {"name": "alpha", "enabled": False})
    session.is_streaming = False
    session._is_readonly = True
    with pytest.raises(ValueError, match="Readonly"):
        await host.ui_operation("mcp_browser_set", {"name": "alpha", "enabled": False})
    session._is_readonly = False
    with pytest.raises(ValueError, match="no longer available"):
        await host.ui_operation("mcp_browser_set", {"name": "missing", "enabled": True})


async def _open_mcp(app: _OneTextualApp, pilot) -> tuple[_CommandCenter, _McpBrowser]:
    await pilot.press("ctrl+p")
    await pilot.pause()
    center = app.screen
    assert isinstance(center, _CommandCenter)
    center.query_one("#command-search", Input).value = "mcp"
    await pilot.pause()
    await pilot.press("enter")
    for _ in range(10):
        await pilot.pause()
        if isinstance(app.screen, _McpBrowser):
            break
    assert isinstance(app.screen, _McpBrowser)
    return center, app.screen


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 40), (60, 20)])
async def test_ctrl_p_mcp_details_back_toggle_error_and_compact_layout(tmp_path: Path, size: tuple[int, int]) -> None:
    app = _OneTextualApp(_mk_app_session(tmp_path))
    rows = [{
        "name": "alpha", "enabled": False, "running": False, "transport": "stdio",
        "runtimeState": "disabled", "toolCount": 1, "tools": ["read_file"],
        "toolsTruncated": False, "restartAttempts": 0, "maxRestartAttempts": 0, "hasError": False,
    }]
    calls: list[tuple[str, dict]] = []

    async def command(operation: str, payload: dict) -> dict:
        calls.append((operation, dict(payload)))
        if operation == "mcp_browser_set":
            rows[0]["enabled"] = payload["enabled"]
            rows[0]["running"] = payload["enabled"]
            return {"ok": True}
        return {"servers": [dict(row) for row in rows], "blockedReason": "", "nextOffset": None}

    with patch.object(app, "_mcp_browser_command", side_effect=command):
        async with app.run_test(size=size) as pilot:
            app.query_one("#input", TextArea).text = "retain draft"
            center, browser = await _open_mcp(app, pilot)
            assert browser.has_class("compact") is (size[0] < 72 or size[1] < 27)
            assert browser.query_one("#mcp-actions").region.bottom <= size[1]

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, _McpDetails)
            assert not [call for call in calls if call[0] == "mcp_browser_set"]
            await pilot.press("escape")
            assert app.screen is browser

            await pilot.click("#mcp-toggle")
            await pilot.pause()
            assert ("mcp_browser_set", {"name": "alpha", "enabled": True}) in calls
            assert browser.query_one("#mcp-toggle", Button).label == "Disable server"

            await pilot.press("escape")
            assert app.screen is center
            assert center.query_one("#command-search", Input).value == "mcp"
            await pilot.press("escape")
            assert app.query_one("#input", TextArea).text == "retain draft"

    async def failed_command(operation: str, payload: dict) -> dict:  # noqa: ARG001
        if operation == "mcp_browser_set":
            raise RuntimeError("backend failed")
        return {"servers": [dict(row) for row in rows], "blockedReason": "", "nextOffset": None}

    error_app = _OneTextualApp(_mk_app_session(tmp_path))
    with patch.object(error_app, "_mcp_browser_command", side_effect=failed_command):
        async with error_app.run_test(size=size) as pilot:
            _, browser = await _open_mcp(error_app, pilot)
            await pilot.click("#mcp-toggle")
            await pilot.pause()
            assert "MCP operation failed" in str(browser.query_one("#mcp-notice", Static).content)
