# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Input, Static, TextArea

from one.core.agent_runtime_facade import AgentRuntimeFacade
from one.core.agent_session import AgentSession
from one.core.agent_session_runtime import AgentSessionRuntime, AgentSessionRuntimeHost
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.core.threaded_agent_runtime import RuntimeBootstrapSpec
from one.core.tui_runtime_host import TUI_COMMANDS, TuiRuntimeHost
from one.modes.tui_mode import TuiSessionView, _CommandCenter, _OneTextualApp, _SessionBrowser


class _Loader:
    def __init__(self, cwd: Path) -> None:
        self.cwd = str(cwd)

    def get_system_prompt(self, selected_tools=None):  # noqa: ARG002
        return "You are a test agent."


async def _make_runtime_app(tmp_path: Path) -> tuple[_OneTextualApp, AgentRuntimeFacade, SessionManager]:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    active = SessionManager.create(str(tmp_path), str(session_dir))
    active.append_message({"role": "assistant", "content": "active transcript"})

    async def factory(_spec):
        auth = AuthStorage.in_memory()
        registry = ModelRegistry.create(auth, str(tmp_path / "models.json"))
        model = registry.find("openai", "gpt-4.1")
        assert model is not None
        session = AgentSession(
            active,
            SettingsManager.in_memory({"tools": {"maxSteps": 1}, "bash": {"showOutput": True}}),
            registry,
            _Loader(tmp_path),
            model,
            "medium",
            tools=[],
        )
        runtime = AgentSessionRuntime(session, None, str(tmp_path), active, _Loader(tmp_path))
        bootstrap = {
            "authStorage": auth,
            "modelRegistry": registry,
            "settingsManager": session.settings_manager,
            "resourceLoader": runtime.resource_loader,
            "resourceLoaderAlreadyLoaded": True,
            "model": model,
            "thinking": "medium",
            "tools": [],
            "mcpManager": None,
        }
        return TuiRuntimeHost(AgentSessionRuntimeHost(bootstrap, runtime), None)

    facade = AgentRuntimeFacade(
        RuntimeBootstrapSpec(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")),
        factory,
        command_allowlist=TUI_COMMANDS,
    )
    await facade.start()
    view = TuiSessionView(facade)
    await view.refresh()
    return _OneTextualApp(view), facade, active


def _persist_target(tmp_path: Path) -> SessionManager:
    target = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    target.set_session_name("Persisted scratch target")
    target.append_message({"role": "user", "content": "persisted user"})
    target.append_message({"role": "assistant", "content": "persisted assistant"})
    return target


async def _open_browser(app: _OneTextualApp, pilot) -> _SessionBrowser:
    await pilot.press("ctrl+p")
    await pilot.pause()
    assert isinstance(app.screen, _CommandCenter)
    app.screen.query_one("#command-search", Input).value = "sessions"
    await pilot.pause()
    await pilot.press("enter")
    for _ in range(20):
        await pilot.pause(0.02)
        if isinstance(app.screen, _SessionBrowser) and app.screen._infos:
            break
    assert isinstance(app.screen, _SessionBrowser)
    assert app.screen._infos
    return app.screen


@pytest.mark.asyncio
async def test_runtime_browser_loads_persisted_session_via_paged_facade_and_keeps_draft(tmp_path: Path) -> None:
    app, facade, active = await _make_runtime_app(tmp_path)
    target = _persist_target(tmp_path)
    try:
        async with app.run_test() as pilot:
            input_box = app.query_one("#input", TextArea)
            input_box.text = "unsent draft"
            browser = await _open_browser(app, pilot)
            assert any(info.id == target.session_id for info in browser._infos)

            browser.query_one("#session-search", Input).value = target.session_id
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert "Keep your draft" in str(browser.query_one("#session-notice", Static).content)
            assert app.session.session_id == active.session_id

            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.02)
                if app.session.session_id == target.session_id:
                    break
            assert app.session.session_id == target.session_id
            assert input_box.text == "unsent draft"
            transcript = "\n".join(app._stream_lines)
            assert "persisted user" in transcript
            assert "persisted assistant" in transcript
            assert "active transcript" not in transcript
    finally:
        await facade.close()


@pytest.mark.asyncio
async def test_runtime_browser_blocks_load_while_streaming_and_surfaces_list_and_load_errors(tmp_path: Path, monkeypatch) -> None:
    app, facade, _active = await _make_runtime_app(tmp_path)
    target = _persist_target(tmp_path)
    try:
        async with app.run_test() as pilot:
            browser = await _open_browser(app, pilot)
            browser.query_one("#session-search", Input).value = target.session_id
            await pilot.pause()

            app.session._snapshot["sidebar"]["streaming"] = True
            await pilot.press("enter")
            await pilot.pause()
            assert "Stop or wait" in str(browser.query_one("#session-notice", Static).content)
            assert app.session.session_id != target.session_id

            app.session._snapshot["sidebar"]["streaming"] = False
            async def list_error() -> list:
                raise RuntimeError("list failure")

            monkeypatch.setattr(app, "_browser_session_infos", list_error)
            await browser._refresh()
            assert "Unable to list sessions" in str(browser.query_one("#session-notice", Static).content)

            async def load_error(_info) -> None:
                raise RuntimeError("load failure")

            monkeypatch.setattr(app, "_browser_session_infos", lambda: asyncio.sleep(0, result=[target_info for target_info in browser._infos if target_info.id == target.session_id]))
            monkeypatch.setattr(app, "_load_browser_session", load_error)
            browser._confirmed = (target.session_id, app.query_one("#input", TextArea).text)
            await browser._load()
            assert "Unable to load this session" in str(browser.query_one("#session-notice", Static).content)
    finally:
        await facade.close()
