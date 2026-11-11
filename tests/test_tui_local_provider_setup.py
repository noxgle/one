# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from textual.widgets import Input, Select, Static

from one.core.agent_runtime_facade import AgentRuntimeFacade
from one.core.settings_manager import SettingsManager
from one.core.threaded_agent_runtime import RuntimeBootstrapSpec
from one.core.tui_runtime_host import TUI_COMMANDS, TuiRuntimeHost
from one.modes.local_provider_setup import LocalProviderSetup
from one.modes.tui_mode import TuiSessionView, _OneTextualApp
from tests.support.tui import _mk_app_session


def configured_session(tmp_path):
    session = _mk_app_session(tmp_path)
    session.settings_manager = SettingsManager(str(tmp_path), str(tmp_path / "state"))
    # No endpoint metadata requests on switch; inspect the selected DTO instead.
    session._enrich_model_context = AsyncMock(side_effect=lambda model: model)
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["ollama", "llama.cpp"])
async def test_placeholder_opens_setup_offline_and_manual_save(tmp_path, monkeypatch, provider):
    session = configured_session(tmp_path)
    def no_network(*args, **kwargs):
        raise AssertionError("Opening or manually saving must not discover models")
    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    app = _OneTextualApp(session)
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.press("ctrl+k")
        search = app.screen.query_one("#model-picker-search", Input)
        search.value = f"{provider} local"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, LocalProviderSetup)
        assert app.screen._ready
        assert str(session.settings_manager._global_path) in str(app.screen.query_one("#local-provider-paths", Static).render())
        app.screen.query_one("#local-provider-model", Input).value = "manual-model"
        app.screen.query_one("#local-provider-url", Input).value = "http://example.test:123/v1"
        await pilot.click("#local-provider-save")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert session.model.id == "manual-model"
        assert session.model.base_url == ("http://example.test:123/v1" if provider == "ollama" else "http://example.test:123")
        assert json.loads(session.settings_manager._global_path.read_text())["defaultModel"] == "manual-model"


@pytest.mark.asyncio
async def test_configure_button_fetch_select_failure_and_cancel(tmp_path, monkeypatch):
    session = configured_session(tmp_path)
    requests = []
    original = httpx.AsyncClient
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"models": [{"name": "downloaded"}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    app = _OneTextualApp(session)
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.press("ctrl+k")
        await pilot.click("#model-picker-ollama")
        await pilot.pause()
        assert isinstance(app.screen, LocalProviderSetup)
        assert not requests
        await pilot.click("#local-provider-fetch")
        await pilot.pause()
        assert len(requests) == 1 and requests[0].method == "GET"
        app.screen.query_one("#local-provider-models", Select).value = "downloaded"
        await pilot.pause()
        assert app.screen.query_one("#local-provider-model", Input).value == "downloaded"
        session.model_registry._models_path.write_text("broken")
        await pilot.click("#local-provider-save")
        await pilot.pause()
        assert isinstance(app.screen, LocalProviderSetup)
        assert "repair" in str(app.screen.query_one("#local-provider-message", Static).render())
        assert session.model.id == "gpt-4.1"
        await pilot.press("escape")
        assert len(app.screen_stack) == 2
        assert not session.settings_manager._global_path.exists()


@pytest.mark.asyncio
async def test_discovery_ignores_stale_url_and_session(tmp_path, monkeypatch):
    session = configured_session(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    async def discover(*args):
        started.set()
        await release.wait()
        return ["stale"]
    monkeypatch.setattr("one.core.local_provider_setup._discover", discover)
    app = _OneTextualApp(session)
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.press("ctrl+k")
        await pilot.click("#model-picker-ollama")
        await pilot.pause()
        screen = app.screen
        await pilot.click("#local-provider-fetch")
        await started.wait()
        screen.query_one("#local-provider-url", Input).value = "http://new.test/v1"
        release.set()
        await screen._operation_task
        assert "Found" not in str(screen.query_one("#local-provider-message", Static).render())
        # Changing the inline session invalidates future Save as well.
        app.session = configured_session(tmp_path / "other")
        await pilot.click("#local-provider-save")
        assert not session.settings_manager._global_path.exists()
        app.session = session


@pytest.mark.asyncio
async def test_threaded_wizard_uses_owner_loop_and_rejects_stale_dto(tmp_path, monkeypatch):
    owner_threads = []
    async def factory(_spec):
        session = configured_session(tmp_path)
        original = session.set_model
        async def switch(model, **kwargs):
            owner_threads.append(threading.get_ident())
            await original(model, **kwargs)
        session.set_model = switch
        return TuiRuntimeHost(SimpleNamespace(session=session, dispose=session.dispose), None)
    def no_network(*args, **kwargs):
        raise AssertionError("No real network in wizard tests")
    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    facade = AgentRuntimeFacade(
        RuntimeBootstrapSpec(cwd=str(tmp_path), agent_dir=str(tmp_path / "state")),
        factory, command_allowlist=TUI_COMMANDS,
    )
    await facade.start()
    view = TuiSessionView(facade)
    await view.refresh()
    app = _OneTextualApp(view)
    try:
        result = await view.command("ui", operation="local_provider_save", payload={
            "provider": "ollama", "url": "http://test/v1", "model": "m", "epoch": view.epoch + 1})
        assert not result["ok"] and "Session changed" in result["error"]
        async with app.run_test(size=(110, 42)) as pilot:
            await pilot.press("ctrl+k")
            await pilot.pause()
            await pilot.click("#model-picker-llama")
            await pilot.pause()
            assert isinstance(app.screen, LocalProviderSetup) and app.screen._ready
            app.screen.query_one("#local-provider-model", Input).value = "thread-model"
            await pilot.click("#local-provider-save")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert view.model.id == "thread-model"
            assert owner_threads and all(t != threading.get_ident() for t in owner_threads)
    finally:
        await facade.close()
        assert facade.finalize()


@pytest.mark.asyncio
async def test_busy_after_open_does_not_save(tmp_path, monkeypatch):
    session = configured_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.press("ctrl+k")
        await pilot.click("#model-picker-ollama")
        await pilot.pause()
        session._is_streaming = True
        app.screen.query_one("#local-provider-model", Input).value = "m"
        await pilot.click("#local-provider-save")
        await pilot.pause()
        assert "Wait" in str(app.screen.query_one("#local-provider-message", Static).render())
        assert not session.settings_manager._global_path.exists()
        session._is_streaming = False

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["ollama", "llama.cpp"])
@pytest.mark.parametrize("detached", [False, True])
async def test_configured_model_named_local_is_directly_selectable(tmp_path, provider, detached):
    from one.core.types import ModelInfo

    session = configured_session(tmp_path)
    model = ModelInfo(provider=provider, id="local", base_url="http://host", metadata_source="models.json")
    session.model_registry._models.append(model)
    facade = None
    if detached:
        async def factory(_spec):
            return TuiRuntimeHost(SimpleNamespace(session=session, dispose=session.dispose), None)
        facade = AgentRuntimeFacade(
            RuntimeBootstrapSpec(cwd=str(tmp_path), agent_dir=str(tmp_path / "state")),
            factory, command_allowlist=TUI_COMMANDS,
        )
        await facade.start()
        view = TuiSessionView(facade)
        await view.refresh()
        models = [m for m in await view.models() if m.provider == provider and m.id == "local"
                  and m.metadata_source == "models.json"]
        assert len(models) == 1
        app = _OneTextualApp(view)
    else:
        app = _OneTextualApp(session)
        models = [model]
    try:
        async with app.run_test(size=(110, 42)) as pilot:
            app._open_model_picker(models)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert app.session.model.provider == provider and app.session.model.id == "local"
    finally:
        if facade:
            await facade.close()
            assert facade.finalize()


@pytest.mark.asyncio
async def test_save_activation_is_offline_and_cannot_interleave_start_or_switch(tmp_path):
    from one.core.local_provider_setup import local_provider_operation

    session = configured_session(tmp_path)
    session._enrich_model_context = AsyncMock(side_effect=AssertionError("must not await endpoint enrichment"))
    competitor_ran = False

    async def start_or_switch():
        nonlocal competitor_ran
        competitor_ran = True
        session._is_streaming = True

    task = asyncio.create_task(start_or_switch())
    result = await local_provider_operation(session, "local_provider_save", {
        "provider": "ollama", "url": "http://host", "model": "offline"})
    assert result["ok"] and session.model.id == "offline"
    assert not competitor_ran
    session._enrich_model_context.assert_not_awaited()
    await task
    session._is_streaming = False


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (100, 30)])
@pytest.mark.parametrize("threaded", [False, True])
@pytest.mark.parametrize("submit", ["click", "enter"])
async def test_compact_manual_entry_real_keyboard_persists(tmp_path, monkeypatch, size, threaded, submit):
    from textual.widgets import Button

    def no_network(*args, **kwargs):
        raise AssertionError("Manual setup must be offline")
    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    session = configured_session(tmp_path)
    facade = None
    if threaded:
        async def factory(_spec):
            return TuiRuntimeHost(SimpleNamespace(session=session, dispose=session.dispose), None)
        facade = AgentRuntimeFacade(
            RuntimeBootstrapSpec(cwd=str(tmp_path), agent_dir=str(tmp_path / "state")),
            factory, command_allowlist=TUI_COMMANDS,
        )
        await facade.start()
        view = TuiSessionView(facade)
        await view.refresh()
    else:
        view = session
    app = _OneTextualApp(view)
    try:
        async with app.run_test(size=size) as pilot:
            await pilot.press("ctrl+k")
            await pilot.click("#model-picker-ollama")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, LocalProviderSetup) and screen._ready
            assert await pilot.click("#local-provider-url")
            await pilot.press("home", "shift+end", "backspace", *"http://server.test:11434", "enter")
            await pilot.pause()
            model = screen.query_one("#local-provider-model", Input)
            assert app.focused is model
            assert model.region in screen.query_one("#local-provider-form").scrollable_content_region
            await pilot.press(*"typed-model")
            message = screen.query_one("#local-provider-message", Static)
            save = screen.query_one("#local-provider-save", Button)
            assert message.region in screen.region
            assert save.region in screen.region
            assert save.styles.color != save.styles.background
            if submit == "click":
                assert await pilot.click("#local-provider-save")
            else:
                await pilot.press("enter")
            # Dispatch the button/input message before capturing its save task.
            await pilot.pause()
            assert screen._operation_task is not None
            await asyncio.wait_for(asyncio.shield(screen._operation_task), timeout=5)
            await pilot.pause()
            assert len(app.screen_stack) == 1, str(message.render())
            assert view.model.id == "typed-model"
            settings = json.loads((tmp_path / "state" / "settings.json").read_text())
            models = json.loads(session.model_registry._models_path.read_text())
            assert settings["defaultModel"] == "typed-model"
            assert any(m["id"] == "typed-model" and m["url"] == "http://server.test:11434/v1"
                       for m in models["providers"]["ollama"])
    finally:
        if facade:
            await facade.close()
            assert facade.finalize()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (100, 30)])
@pytest.mark.parametrize("threaded", [False, True])
async def test_compact_fetch_progress_error_retry_and_keyboard_selection(tmp_path, monkeypatch, size, threaded):
    from textual.widgets import Button

    started, release = threading.Event(), threading.Event()
    attempts = 0
    original = httpx.AsyncClient

    async def handler(request):
        nonlocal attempts
        attempts += 1
        assert request.method == "GET" and request.url.path == "/api/tags"
        started.set()
        await asyncio.to_thread(release.wait)
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"models": [{"name": "server-model"}]})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    session = configured_session(tmp_path)
    facade = None
    if threaded:
        async def factory(_spec):
            return TuiRuntimeHost(SimpleNamespace(session=session, dispose=session.dispose), None)
        facade = AgentRuntimeFacade(
            RuntimeBootstrapSpec(cwd=str(tmp_path), agent_dir=str(tmp_path / "state")),
            factory, command_allowlist=TUI_COMMANDS,
        )
        await facade.start()
        view = TuiSessionView(facade)
        await view.refresh()
    else:
        view = session
    app = _OneTextualApp(view)
    try:
        async with app.run_test(size=size) as pilot:
            await pilot.press("ctrl+k")
            await pilot.click("#model-picker-ollama")
            await pilot.pause()
            screen = app.screen
            assert await pilot.click("#local-provider-fetch")
            await asyncio.to_thread(started.wait)
            await pilot.pause()
            message = screen.query_one("#local-provider-message", Static)
            assert "Fetching" in str(message.render()) and message.region in screen.region
            assert screen.query_one("#local-provider-fetch", Button).disabled
            assert screen.query_one("#local-provider-save", Button).disabled
            release.set()
            await screen._operation_task
            await pilot.pause()
            assert "Could not discover" in str(message.render())
            assert message.has_class("error") and message.region in screen.region
            for ident in ("fetch", "save"):
                button = screen.query_one(f"#local-provider-{ident}", Button)
                assert not button.disabled and button.styles.color != button.styles.background
            # Tab navigation scrolls the form back to the fetch control.
            screen.query_one("#local-provider-url", Input).focus()
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            assert await pilot.click("#local-provider-fetch")
            await pilot.pause()
            assert attempts == 2 and "Found 1" in str(message.render())
            assert not message.has_class("error")
            assert app.focused is screen.query_one("#local-provider-models", Select)
            await pilot.press("enter", "down", "enter")
            await pilot.pause()
            assert screen.query_one("#local-provider-model", Input).value == "server-model"
            assert not screen.query_one("#local-provider-fetch", Button).disabled
            assert not screen.query_one("#local-provider-save", Button).disabled
    finally:
        release.set()
        if facade:
            await facade.close()
            assert facade.finalize()


@pytest.mark.asyncio
async def test_config_load_error_can_retry_without_reopening(tmp_path, monkeypatch):
    from textual.widgets import Button

    from one.modes import local_provider_setup as wizard

    original = wizard.local_provider_operation
    attempts = 0

    async def operation(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {"ok": False, "error": "Temporary configuration failure; retry."}
        return await original(*args, **kwargs)

    monkeypatch.setattr(wizard, "local_provider_operation", operation)
    app = _OneTextualApp(configured_session(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(LocalProviderSetup("ollama"))
        await pilot.pause()
        screen = app.screen
        message = screen.query_one("#local-provider-message", Static)
        retry = screen.query_one("#local-provider-fetch", Button)
        assert "Temporary" in str(message.render()) and message.region in screen.region
        assert not retry.disabled and str(retry.label) == "Retry setup"
        assert screen.query_one("#local-provider-save", Button).disabled
        assert await pilot.click("#local-provider-fetch")
        await pilot.pause()
        assert attempts == 2 and screen._ready
        assert not screen.query_one("#local-provider-save", Button).disabled
        assert not message.has_class("error")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["local_provider_config", "local_provider_models"])
@pytest.mark.parametrize("shutdown", [False, True])
@pytest.mark.parametrize("late_result", [False, True])
async def test_close_pending_setup_cancels_task_without_stale_mutation(
    tmp_path, monkeypatch, operation, shutdown, late_result,
):
    from one.modes import local_provider_setup as wizard

    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = wizard.local_provider_operation

    async def pending(session, name, payload, **kwargs):
        if name != operation:
            return await original(session, name, payload, **kwargs)
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            if not late_result:
                raise
            # Model a completion already in flight when the consumer closes.
            await release.wait()
        return {"ok": True, "url": "http://stale.test", "model": "stale",
                "models": ["stale"], "settingsPath": "stale", "modelsPath": "stale"}

    monkeypatch.setattr(wizard, "local_provider_operation", pending)
    session = configured_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        screen = LocalProviderSetup("ollama")
        app.push_screen(screen)
        await pilot.pause()
        if operation == "local_provider_models":
            await asyncio.wait_for(asyncio.shield(screen._operation_task), timeout=5)
            assert await pilot.click("#local-provider-fetch")
        await asyncio.wait_for(started.wait(), timeout=5)
        task = screen._operation_task
        model = screen.query_one("#local-provider-model", Input)
        message = screen.query_one("#local-provider-message", Static)
        previous = (model.value, str(message.render()), session.model.id)
        if not shutdown:
            app.pop_screen()
            await pilot.pause()
    # Both explicit removal and app shutdown must cancel the owned task.
    await asyncio.wait_for(cancelled.wait(), timeout=5)
    assert screen._setup_closing and not screen._valid()
    release.set()
    if late_result:
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
    else:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
    assert not screen._busy
    assert (model.value, str(message.render()), session.model.id) == previous
    assert not session.settings_manager._global_path.exists()


@pytest.mark.asyncio
async def test_pending_save_cannot_be_cancelled_while_open(tmp_path, monkeypatch):
    from one.modes import local_provider_setup as wizard

    started, release = asyncio.Event(), asyncio.Event()
    original = wizard.local_provider_operation

    async def pending(session, operation, payload, **kwargs):
        if operation == "local_provider_save":
            started.set()
            await release.wait()
        return await original(session, operation, payload, **kwargs)

    monkeypatch.setattr(wizard, "local_provider_operation", pending)
    session = configured_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        screen = LocalProviderSetup("ollama")
        app.push_screen(screen)
        await pilot.pause()
        await asyncio.wait_for(asyncio.shield(screen._operation_task), timeout=5)
        screen.query_one("#local-provider-model", Input).value = "saved"
        await pilot.click("#local-provider-save")
        await asyncio.wait_for(started.wait(), timeout=5)
        task = screen._operation_task
        await pilot.press("escape")
        await pilot.click("#local-provider-cancel")
        assert app.screen is screen and not screen._setup_closing
        assert not task.done()
        release.set()
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        await pilot.pause()
        assert len(app.screen_stack) == 1 and session.model.id == "saved"


@pytest.mark.asyncio
@pytest.mark.parametrize("threaded", [False, True])
async def test_initialization_waits_for_active_mount_and_reopens(tmp_path, monkeypatch, threaded):
    """Yield inside Mount, before Textual sets _is_mounted (not pilot timing)."""
    from textual.widgets import Button

    original_mount = LocalProviderSetup.on_mount
    observations = []

    async def slow_mount(screen):
        original_mount(screen)
        # Force tasks created by on_mount to execute before Mount returns.
        for _ in range(5):
            await asyncio.sleep(0)
        observations.append((screen.is_mounted, screen._operation_task))

    monkeypatch.setattr(LocalProviderSetup, "on_mount", slow_mount)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: pytest.fail("No network expected"))
    session = configured_session(tmp_path)
    facade = None
    if threaded:
        async def factory(_spec):
            return TuiRuntimeHost(SimpleNamespace(session=session, dispose=session.dispose), None)
        facade = AgentRuntimeFacade(
            RuntimeBootstrapSpec(cwd=str(tmp_path), agent_dir=str(tmp_path / "state")),
            factory, command_allowlist=TUI_COMMANDS,
        )
        await facade.start()
        view = TuiSessionView(facade)
        await view.refresh()
    else:
        view = session
    app = _OneTextualApp(view)
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            for _ in range(2):
                screen = LocalProviderSetup("ollama")
                await app.push_screen(screen)
                await pilot.pause()
                assert screen._ready, str(screen.query_one("#local-provider-message", Static).render())
                assert not screen.query_one("#local-provider-save", Button).disabled
                assert observations[-1] == (False, None)
                task = screen._operation_task
                # Duplicate refresh/resume callbacks must not reload configuration.
                screen._initialize()
                assert screen._operation_task is task
                await pilot.press("escape")
                await pilot.pause()
                assert screen._setup_closing
    finally:
        if facade:
            await facade.close()
            assert facade.finalize()


@pytest.mark.asyncio
@pytest.mark.parametrize("threaded", [False, True])
async def test_config_timeout_retry_and_save_has_no_load_deadline(tmp_path, monkeypatch, threaded):
    from textual.widgets import Button

    from one.core import local_provider_setup as core
    from one.modes import local_provider_setup as wizard

    original = core.local_provider_operation
    attempts = 0
    config_started, save_started, release_save = threading.Event(), threading.Event(), threading.Event()

    async def operation(session, name, payload, **kwargs):
        nonlocal attempts
        if name == "local_provider_config":
            attempts += 1
            if attempts == 1:
                config_started.set()
                await asyncio.Event().wait()
        if name == "local_provider_save":
            save_started.set()
            while not release_save.is_set():
                await asyncio.sleep(0.005)
        return await original(session, name, payload, **kwargs)

    monkeypatch.setattr(core, "local_provider_operation", operation)
    monkeypatch.setattr(wizard, "local_provider_operation", operation)
    monkeypatch.setattr(LocalProviderSetup, "CONFIG_TIMEOUT", 0.1)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: pytest.fail("No network expected"))
    session = configured_session(tmp_path)
    facade = None
    if threaded:
        async def factory(_spec):
            return TuiRuntimeHost(SimpleNamespace(session=session, dispose=session.dispose), None)
        facade = AgentRuntimeFacade(
            RuntimeBootstrapSpec(cwd=str(tmp_path), agent_dir=str(tmp_path / "state")),
            factory, command_allowlist=TUI_COMMANDS,
        )
        await facade.start()
        view = TuiSessionView(facade)
        await view.refresh()
    else:
        view = session
    app = _OneTextualApp(view)
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            screen = LocalProviderSetup("ollama")
            await app.push_screen(screen)
            await pilot.pause()
            assert config_started.is_set()
            await asyncio.wait_for(asyncio.shield(screen._operation_task), 2)
            assert "timed out" in str(screen.query_one("#local-provider-message", Static).render())
            retry = screen.query_one("#local-provider-fetch", Button)
            assert not retry.disabled and str(retry.label) == "Retry setup"
            assert screen.query_one("#local-provider-save", Button).disabled
            await pilot.click("#local-provider-fetch")
            await pilot.pause()
            assert attempts == 2 and screen._ready
            screen.query_one("#local-provider-model", Input).value = "after-timeout"
            await pilot.click("#local-provider-save")
            assert save_started.is_set()
            await asyncio.sleep(0.2)  # Deliberately exceed the configuration deadline.
            assert not screen._operation_task.done() and screen._busy
            release_save.set()
            await asyncio.wait_for(asyncio.shield(screen._operation_task), 2)
            await pilot.pause()
            assert view.model.id == "after-timeout"
            assert len(app.screen_stack) == 1
    finally:
        release_save.set()
        if facade:
            await facade.close()
            assert facade.finalize()


@pytest.mark.asyncio
async def test_inactive_initialization_resumes_once_and_stale_session_can_cancel(tmp_path, monkeypatch):
    from textual.screen import ModalScreen

    original = LocalProviderSetup._initialize
    calls = []

    def covered_initialize(screen):
        calls.append(screen._active())
        original(screen)

    monkeypatch.setattr(LocalProviderSetup, "_initialize", covered_initialize)
    app = _OneTextualApp(configured_session(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        screen = LocalProviderSetup("ollama")
        await app.push_screen(screen)
        # Cover it before the post-refresh callback is dispatched.
        await app.push_screen(ModalScreen())
        await pilot.pause()
        assert not screen._initialized and screen._operation_task is None
        # Textual may defer callbacks on suspended screens; force a stale one too.
        screen._initialize()
        assert False in calls
        app.pop_screen()
        await pilot.pause()
        assert screen._ready and screen._initialized
        task = screen._operation_task
        screen._initialize()
        assert screen._operation_task is task
        app.session = configured_session(tmp_path / "replacement")
        await pilot.click("#local-provider-fetch")
        assert "Session changed" in str(screen.query_one("#local-provider-message", Static).render())
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1 and screen._setup_closing
