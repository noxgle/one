# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import json
import threading
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from one.core.agent_runtime_facade import AgentRuntimeFacade
from one.core.threaded_agent_runtime import RuntimeBootstrapSpec
from one.core.tui_runtime_host import TuiBootstrap, TuiRuntimeHost, create_tui_runtime
from one.modes.tui_mode import TuiSessionView


class _TuiAdapter:
    """A deliberately narrow stand-in for the production TUI adapter."""

    def __init__(self) -> None:
        self.owner_thread = threading.get_ident()

    async def dispatch(self, command: str, payload: dict[str, object]) -> dict[str, object]:
        if command == "snapshot" and not payload:
            return {"epoch": 0, "sidebar": {"model": "stub/model"}, "messages": []}
        if command == "prompt" and set(payload) == {"text"}:
            return {"accepted": True}
        raise ValueError("invalid TUI runtime command payload")


@pytest.mark.asyncio
async def test_tui_facade_worker_is_off_ui_thread_and_exposes_no_live_runtime() -> None:
    created: list[_TuiAdapter] = []

    async def factory(_spec: RuntimeBootstrapSpec) -> _TuiAdapter:
        adapter = _TuiAdapter()
        created.append(adapter)
        return adapter

    facade = AgentRuntimeFacade(
        RuntimeBootstrapSpec(cwd="/tmp", agent_dir="/tmp/agent"), factory,
        command_allowlist={"snapshot", "prompt"},
    )
    ui_thread = threading.get_ident()
    await facade.start()
    assert created[0].owner_thread != ui_thread
    assert not hasattr(facade, "session")
    assert not hasattr(facade, "runtime")
    assert (await facade.command("snapshot"))["result"]["sidebar"]["model"] == "stub/model"
    with pytest.raises(ValueError, match="invalid TUI runtime command payload"):
        await facade.command("prompt", {"unexpected": True})
    await facade.close()
    assert facade.finalize()


@pytest.mark.asyncio
async def test_detached_host_bridges_real_session_streaming_to_textual_before_prompt_completes(tmp_path) -> None:
    """The host bridge, not a test event injection, drives live Textual output."""
    from textual.widgets import Static

    from one.core.agent_session import AgentSession
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.settings_manager import SettingsManager
    from one.modes.tui_mode import TuiSessionView, _OneTextualApp
    from one.providers.base import ChatResult

    class Loader:
        cwd = str(tmp_path)

        def get_system_prompt(self, selected_tools=None):  # noqa: ARG002
            return "You are a test agent."

    class Provider:
        def __init__(self) -> None:
            self.calls = 0
            self.started = threading.Event()
            self.release = threading.Event()

        async def chat(self, *args, on_delta=None, on_thinking_delta=None, **kwargs):  # noqa: ANN002, ANN003
            self.calls += 1
            if self.calls == 1:
                assert on_thinking_delta is not None
                on_thinking_delta("working through the read")
                return ChatResult(
                    text="", raw={}, usage={}, stop_reason="tool_calls", had_native_tool_call=True,
                    native_tool_calls=[{"id": "read-1", "name": "read", "arguments": {"path": "input.txt"}}],
                )
            answer = "visible before completion and final answer " + ("with complete detail " * 16)
            if self.calls == 2:
                if on_delta is not None:
                    on_delta("visible before completion")
                self.started.set()
                await asyncio.to_thread(self.release.wait)
                if on_delta is not None:
                    on_delta(" and final answer " + ("with complete detail " * 16))
            return ChatResult(text=answer, raw={}, usage={}, stop_reason="stop")

    provider = Provider()

    async def factory(_spec):
        auth = AuthStorage.in_memory()
        auth.set_runtime_api_key("openai", "test-key")
        registry = ModelRegistry.create(auth, str(tmp_path / "models.json"))
        model = registry.find("openai", "gpt-4.1")
        assert model is not None
        session = AgentSession(
            SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory({"tools": {"maxSteps": 4}}),
            registry, Loader(), model, "medium", tools=["read"],
        )
        session.providers = {"openai": provider}
        # This deliberately exposes the session only to the runtime host, never
        # to the facade or Textual view.
        owner = SimpleNamespace(session=session, dispose=session.dispose)
        return TuiRuntimeHost(owner, None)

    facade = AgentRuntimeFacade(
        RuntimeBootstrapSpec(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), factory,
        command_allowlist={"snapshot", "prompt"},
    )
    await facade.start()
    view = TuiSessionView(facade)
    await view.refresh()
    app = _OneTextualApp(view)
    (tmp_path / "input.txt").write_text("tool payload", encoding="utf-8")
    try:
        async with app.run_test() as pilot:
            # Use the actual Textual input bindings; do not call the worker
            # event handler or submit helper directly.
            await pilot.press(*list("read it"), "enter")
            await asyncio.to_thread(provider.started.wait, 1)
            for _ in range(20):
                await pilot.pause(0.02)
                if "visible before completion" in "\n".join(app._stream_lines):
                    break
            stream = "\n".join(app._stream_lines)
            assert "working through the read" in stream
            assert "Tool:" in stream and "read" in stream
            assert "visible before completion" in stream
            assert app._turn_active is True  # spinner remains while provider is gated
            provider.release.set()
            for _ in range(50):
                await pilot.pause(0.02)
                if not app._turn_active:
                    break
            assert app._turn_active is False
            # turn_end is reduced before the frame-scheduled transcript paint.
            # Check the rendered widget after that paint rather than treating
            # the spinner transition as proof that presentation is complete.
            for _ in range(50):
                await pilot.pause(0.02)
                if "visible before completion and final answer" in str(app.query_one("#stream", Static).content):
                    break
            rendered = str(app.query_one("#stream", Static).content)
            assert "visible before completion and final answer" in "\n".join(app._stream_lines)
            assert "visible before completion and final answer" in rendered

            # A later turn must not discard the completed live block while its
            # own lifecycle events cross the detached bridge.
            await pilot.press(*list("again"), "enter")
            for _ in range(50):
                await pilot.pause(0.02)
                if not app._turn_active:
                    break
            for _ in range(50):
                await pilot.pause(0.02)
                rendered = str(app.query_one("#stream", Static).content)
                if rendered.count("visible before completion and final answer") >= 2:
                    break
            assert rendered.count("visible before completion and final answer") >= 2
    finally:
        provider.release.set()
        await facade.close()
        assert facade.finalize()


@pytest.mark.asyncio
async def test_tui_session_view_keeps_only_snapshot_data_and_safe_tool_shape() -> None:
    class Runtime:
        def __init__(self) -> None:
            self.session = object()  # Must never become reachable from the view.
            self.listener = None

        def subscribe(self, listener):
            self.listener = listener
            return lambda: None

        async def dispatch(self, command, payload):
            if command == "snapshot":
                return {"epoch": 7, "sidebar": {"sessionId": "s", "model": "p/m"}, "messages": [{"role": "assistant", "content": "hi"}]}
            return {"accepted": True}

    created: list[Runtime] = []
    def factory(_spec):
        runtime = Runtime()
        created.append(runtime)
        return runtime

    facade = AgentRuntimeFacade(RuntimeBootstrapSpec(cwd="/tmp", agent_dir="/tmp/a"), factory, command_allowlist={"snapshot"})
    await facade.start()
    view = TuiSessionView(facade)
    await view.refresh()
    seen: list[dict] = []
    view.subscribe(seen.append)
    facade._on_worker_event({"type": "tool_call_end", "tool": "read", "ok": True, "result": {"outputText": "safe", "rawResult": {"secret": "no"}}})  # noqa: SLF001
    await __import__("asyncio").sleep(0)
    assert view.messages == [{"role": "assistant", "content": "hi"}]
    assert not hasattr(view, "session")
    assert seen[0]["outputText"] == "safe"
    assert "result" not in seen[0]
    await facade.close()
    assert facade.finalize()


@pytest.mark.asyncio
async def test_detached_model_pages_preserve_every_model_choice() -> None:
    models = [{"provider": "p", "id": f"model-{index}"} for index in range(251)]

    class Facade:
        async def command(self, command, payload=None):
            payload = payload or {}
            if command == "snapshot":
                return {"result": {"epoch": 3, "sidebar": {}, "messages": [], "models": {"count": len(models), "paged": True}}}
            assert command == "models_page"
            offset, limit = payload["offset"], payload["limit"]
            page = models[offset : offset + limit]
            return {"result": {"epoch": 3, "models": page, "nextOffset": offset + len(page) if offset + len(page) < len(models) else None}}

    view = TuiSessionView(Facade())
    await view.refresh()
    assert [model.id for model in await view.models()] == [item["id"] for item in models]


@pytest.mark.asyncio
async def test_full_tui_ingress_does_not_stall_input_and_resumes_facade_events() -> None:
    """A full app accumulator leaves facade events upstream, not blocking Textual."""
    from one.modes.tui_mode import _OneTextualApp

    facade = AgentRuntimeFacade(
        RuntimeBootstrapSpec(cwd="/tmp", agent_dir="/tmp/agent"), lambda _spec: _TuiAdapter(),
        event_capacity=8,
    )
    view = TuiSessionView(facade)
    app = _OneTextualApp(view)
    observed: list[str] = []
    async with app.run_test() as pilot:
        view.subscribe(lambda event: observed.append(event["type"]))
        with app._pending_ui_events_lock:  # noqa: SLF001
            app._pending_ui_events = [{"type": "warning", "warning": str(index)} for index in range(256)]  # noqa: SLF001
            app._pending_ui_event_bytes = sum(app._ui_event_size(event) for event in app._pending_ui_events)  # noqa: SLF001
        assert facade._listeners and facade._listeners[0][1] is not None  # noqa: SLF001
        assert not facade._listeners[0][1]({"type": "warning"})  # noqa: SLF001

        facade._on_worker_event({"type": "warning", "warning": "upstream"})  # noqa: SLF001
        facade._on_worker_event({"type": "agent_end", "ok": True})  # noqa: SLF001
        # Run the queued UI delivery explicitly before a periodic app timer can
        # drain the seeded ingress. It must leave both events upstream.
        facade._drain_on_ui_loop()  # noqa: SLF001
        assert [event[0]["type"] for event in facade._events] == ["warning", "agent_end"]  # noqa: SLF001

        # This executes a real Textual input callback while ingress is full.
        await pilot.press("x")
        assert app.query_one("#input").text == "x"

        app._flush_pending_ui_events()  # noqa: SLF001
        await pilot.pause()
        await asyncio.sleep(0.01)
        assert observed[-2:] == ["warning", "agent_end"]
        assert not facade._events  # noqa: SLF001

    await facade.close()


@pytest.mark.asyncio
async def test_large_tool_result_crosses_facade_view_and_app_without_double_counting() -> None:
    """A 32 KiB display preview must remain one DTO field at UI ingress."""
    from one.modes.tui_mode import _OneTextualApp

    facade = AgentRuntimeFacade(
        RuntimeBootstrapSpec(cwd="/tmp", agent_dir="/tmp/agent"), lambda _spec: _TuiAdapter(),
        event_capacity=4,
    )
    view = TuiSessionView(facade)
    app = _OneTextualApp(view)
    output = "web result " + ("x" * (32 * 1024 - 11))
    async with app.run_test() as pilot:
        facade._on_worker_event({  # noqa: SLF001
            "type": "tool_call_end", "tool": "web-search", "toolCallId": "tool-1",
            "ok": True, "result": {"outputText": output},
        })
        facade._on_worker_event({"type": "turn_end", "ok": True})  # noqa: SLF001
        await pilot.pause(0.1)
        assert app._turn_active is False  # noqa: SLF001
        assert any("web result" in line for line in app._stream_lines)  # noqa: SLF001
        with app._pending_ui_events_lock:  # noqa: SLF001
            assert app._pending_ui_event_bytes <= 64 * 1024  # noqa: SLF001
    await facade.close()


def test_default_mode_resolver_uses_project_precedence_without_settings_manager(tmp_path) -> None:
    from one.cli.main import _resolve_default_mode

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "settings.json").write_text('{"defaultMode":"tui"}', encoding="utf-8")
    project = tmp_path / ".one"
    project.mkdir()
    (project / "settings.json").write_text('{"defaultMode":"cli"}', encoding="utf-8")

    assert _resolve_default_mode(str(tmp_path), str(agent_dir)) == ("cli", [])


@pytest.mark.asyncio
async def test_bare_cli_uses_detached_tui_path_but_default_cli_uses_legacy_dispatch(tmp_path, monkeypatch) -> None:
    import one.cli.main as main

    agent_dir = tmp_path / "agent"
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(agent_dir))
    monkeypatch.chdir(tmp_path)
    entered: list[object] = []

    class Tui:
        def __init__(self, facade, _options):
            entered.append(facade)

        async def run(self):
            return None

    monkeypatch.setattr(main, "TuiMode", Tui)
    monkeypatch.setattr(main.SettingsManager, "create", lambda *_args: (_ for _ in ()).throw(AssertionError("legacy settings")))
    assert await main._run([]) == 0
    assert entered

    project = tmp_path / ".one"
    project.mkdir()
    (project / "settings.json").write_text('{"defaultMode":"cli"}', encoding="utf-8")
    with pytest.raises(AssertionError, match="legacy settings"):
        await main._run([])


@pytest.mark.asyncio
async def test_detached_view_toggle_uses_authoritative_snapshot_not_legacy_host_methods() -> None:
    """Facade actions cannot fall back to settings/registry/session-manager objects."""
    from one.modes.tui_mode import _OneTextualApp

    class Facade:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def command(self, command, payload=None):
            payload = payload or {}
            self.calls.append((command, payload))
            if command == "snapshot":
                return {"result": {"epoch": 3, "sidebar": {"theme": "default", "subagents": True, "bashOutput": True, "retry": "on", "cooperation": False}, "messages": []}}
            assert command == "ui"
            assert payload == {"operation": "set_bash_output", "payload": {"enabled": False}}
            return {"result": {"epoch": 3, "sidebar": {"theme": "default", "subagents": True, "bashOutput": False, "retry": "on", "cooperation": False}, "messages": []}}

        def subscribe(self, _listener):
            return lambda: None

    view = TuiSessionView(Facade())
    await view.refresh()
    app = _OneTextualApp(view)
    app._refresh_sidebar = lambda: None  # type: ignore[method-assign]
    app.action_toggle_bash_show()
    await __import__("asyncio").sleep(0)

    assert view.snapshot["sidebar"]["bashOutput"] is False
    assert app.session._facade.calls[-1][0] == "ui"  # noqa: SLF001
    for legacy in ("settings_manager", "model_registry", "session_manager", "approval_callback"):
        assert not hasattr(view, legacy)


@pytest.mark.asyncio
async def test_rebuild_session_transcript_uses_legacy_settings_or_detached_snapshot() -> None:
    """Legacy sessions have no snapshot; detached views have no settings manager."""
    from one.modes.tui_mode import _OneTextualApp

    message = {"role": "bashExecution", "command": "printf value", "output": "secret-output"}
    legacy = SimpleNamespace(messages=[message], settings_manager=SimpleNamespace(get_bash_show_output=lambda: False))
    legacy_app = _OneTextualApp(legacy)
    legacy_app._rebuild_session_transcript()
    assert "secret-output" not in "\n".join(legacy_app._stream_lines)

    class Facade:
        async def command(self, command, _payload=None):
            assert command == "snapshot"
            return {"result": {"epoch": 1, "sidebar": {"bashOutput": True}, "messages": [message]}}

        def subscribe(self, _listener):
            return lambda: None

    view = TuiSessionView(Facade())
    await view.refresh()
    app = _OneTextualApp(view)
    async with app.run_test() as pilot:
        app._rebuild_session_transcript()
        await pilot.pause()
        assert "secret-output" in "\n".join(app._stream_lines)


@pytest.mark.asyncio
async def test_detached_sessions_rename_uses_runtime_host_command(tmp_path) -> None:
    """A real detached Textual pilot must not reach legacy session mutation paths."""
    from one.modes import tui_mode
    from one.modes.tui_mode import _OneTextualApp

    target_path = str(tmp_path / "target.jsonl")
    calls: list[tuple[str, dict[str, object]]] = []

    class Facade:
        snapshot = {"epoch": 1, "sidebar": {"sessionId": "current", "theme": "default"}, "messages": []}

        async def command(self, command, payload=None):
            payload = payload or {}
            calls.append((command, payload))
            if command == "snapshot":
                return {"result": self.snapshot}
            if command == "session_list":
                return {"result": {"sessions": [{
                    "id": "target", "name": "Exact Name", "path": target_path,
                    "cwd": str(tmp_path), "modified": datetime.now(UTC).isoformat(), "messageCount": 1,
                }]}}
            assert command == "ui"
            assert payload == {"operation": "rename_session", "payload": {"path": target_path, "name": "Renamed"}}
            return {"result": self.snapshot}

        def subscribe(self, _listener):
            return lambda: None

    view = TuiSessionView(Facade())
    await view.refresh()
    app = _OneTextualApp(view)
    original_open = tui_mode.SessionManager.open
    tui_mode.SessionManager.open = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy open"))
    try:
        async with app.run_test() as pilot:
            await app._handle_command("/sessions")
            await app._handle_command("/sessions rename Exact Name Renamed")
            await pilot.pause()
    finally:
        tui_mode.SessionManager.open = original_open

    assert ("ui", {"operation": "rename_session", "payload": {"path": target_path, "name": "Renamed"}}) in calls


@pytest.mark.asyncio
async def test_question_and_approval_replies_return_authoritative_snapshots() -> None:
    answered: list[tuple[str, str]] = []
    session = SimpleNamespace(answer_question=lambda question_id, answer: answered.append((question_id, answer)))
    host = object.__new__(TuiRuntimeHost)
    host._epoch = 9
    host._session = lambda: session
    host._snapshot = lambda: {"epoch": 9, "sidebar": {"bashOutput": False}, "messages": []}

    answer = await host.answer_question("q-1", "yes", 9)
    assert answered == [("q-1", "yes")]
    assert answer == {"epoch": 9, "sidebar": {"bashOutput": False}, "messages": [], "accepted": True}
    with pytest.raises(ValueError, match="stale or invalid question response"):
        await host.answer_question("q-1", "late", 8)

    future = __import__("asyncio").get_running_loop().create_future()
    host._approval_active = {"id": "a-1", "future": future}
    host._approvals = __import__("collections").deque()
    host._publish_next_approval = lambda: None
    approval = await host.approval_resolve("a-1", False, "no", 9)
    assert future.result() == (False, "no")
    assert approval == {"epoch": 9, "sidebar": {"bashOutput": False}, "messages": [], "accepted": True}


@pytest.mark.asyncio
async def test_detached_approval_disable_dismisses_waiter_without_resolving_it() -> None:
    """A detached UI waiter has no local future and must only be dismissed."""
    from one.modes.tui_mode import _OneTextualApp

    class Facade:
        def __init__(self) -> None:
            self.listeners = []
            self.snapshot = {
                "epoch": 4,
                "sidebar": {"theme": "default", "cooperation": True, "model": "stub/model"},
                "messages": [],
            }
            self.commands: list[tuple[str, dict[str, object]]] = []

        async def command(self, command, payload=None):
            payload = payload or {}
            self.commands.append((command, payload))
            if command == "snapshot":
                return {"result": deepcopy(self.snapshot)}
            assert command == "ui"
            assert payload == {"operation": "set_cooperation", "payload": {"enabled": False}}
            self.snapshot["sidebar"]["cooperation"] = False
            return {"result": deepcopy(self.snapshot)}

        def subscribe(self, listener):
            self.listeners.append(listener)
            return lambda: self.listeners.remove(listener)

        def emit(self, event):
            for listener in tuple(self.listeners):
                listener(event)

    facade = Facade()
    view = TuiSessionView(facade)
    await view.refresh()
    app = _OneTextualApp(view)
    async with app.run_test() as pilot:
        facade.emit({
            "type": "tui_approval_request", "approvalId": "a-1", "tool": "bash",
            "origin": "main", "epoch": 4,
        })
        await pilot.pause()
        assert len(app._approval_waiters) == 1
        assert app.query_one("#approval_overlay").has_class("visible")

        app.action_toggle_cooperation()
        await pilot.pause()

        assert not app._approval_waiters
        assert app._approval_pending is None
        assert not app.query_one("#approval_overlay").has_class("visible")
        assert all(command != "approval_resolve" for command, _payload in facade.commands)


@pytest.mark.asyncio
async def test_detached_tui_monitor_refreshes_from_snapshots_and_rebinds_epoch() -> None:
    """Monitor data reaches Textual only as a snapshot, including after a rebind."""
    from one.modes.tui_mode import _OneTextualApp

    class Facade:
        def __init__(self) -> None:
            self.listeners = []
            self.snapshot = {
                "epoch": 4,
                "sidebar": {"sessionId": "old", "model": "stub/model"},
                "messages": [],
                "monitor": {"revision": 1, "children": [{"sessionId": "old-child", "status": "running"}]},
            }

        async def command(self, command, _payload=None):
            assert command == "snapshot"
            return {"result": deepcopy(self.snapshot)}

        def subscribe(self, listener):
            self.listeners.append(listener)
            return lambda: self.listeners.remove(listener)

        def emit(self, event):
            for listener in tuple(self.listeners):
                listener(event)

    facade = Facade()
    view = TuiSessionView(facade)
    await view.refresh()
    assert not hasattr(view, "subagent_monitor")
    app = _OneTextualApp(view)
    async with app.run_test() as pilot:
        assert app._monitor_child("old-child") is not None
        facade.snapshot["monitor"] = {
            "revision": 2,
            "children": [{"sessionId": "old-child", "status": "completed"}],
        }
        facade.emit({"type": "subagent_monitor_invalidated", "epoch": 4, "revision": 2})
        await pilot.pause(0.2)
        assert app._monitor_child("old-child")["status"] == "completed"

        # Session-changing commands replace the detached view's epoch.  Rebind
        # must discard the old monitor projection rather than retain its child.
        facade.snapshot = {
            "epoch": 5,
            "sidebar": {"sessionId": "new", "model": "stub/model"},
            "messages": [],
            "monitor": {"revision": 0, "children": [{"sessionId": "new-child", "status": "running"}]},
        }
        await view.refresh()
        app._bind_session()
        assert app._monitor_child("old-child") is None
        assert app._monitor_child("new-child") is not None


def test_tui_host_ignores_inflight_old_monitor_notification_after_epoch_rebind() -> None:
    class Monitor:
        def __init__(self) -> None:
            self.listener = None

        def subscribe(self, listener):
            self.listener = listener
            return lambda: None

    class Session:
        def __init__(self) -> None:
            self.subagent_monitor = Monitor()
            self.events = []

        def _emit(self, event):
            self.events.append(event)

    old, new = Session(), Session()
    runtime_host = SimpleNamespace(session=old)
    host = TuiRuntimeHost(runtime_host, None)
    old_callback = old.subagent_monitor.listener
    runtime_host.session = new
    host._epoch += 1  # Match the runtime transaction before monitor rebinding.
    host._bind_monitor()

    old_callback({"type": "updated", "sessionId": "old-child", "revision": 1})
    new.subagent_monitor.listener({"type": "updated", "sessionId": "new-child", "revision": 1})

    assert old.events == []
    assert new.events == [{
        "type": "subagent_monitor_invalidated", "sessionId": "new-child",
        "revision": 1, "changeType": "updated",
    }]


@pytest.mark.asyncio
async def test_tui_host_dispatch_rebinds_session_bridge_for_all_replacements(monkeypatch) -> None:
    """Every session-changing dispatch keeps exactly one current bridge alive."""
    from one.core.session_manager import SessionManager

    class Session:
        def __init__(self, session_id):
            self.session_id = session_id
            self.listeners = []
            self.subagent_monitor = None
            self.session_manager = SimpleNamespace(cwd="/tmp", session_dir="/tmp/sessions")

        def subscribe(self, listener):
            self.listeners.append(listener)

            def off():
                self.listeners.remove(listener)

            return off

        def emit(self, event):
            for listener in tuple(self.listeners):
                listener(event)

        async def navigate_tree(self, _entry_id, _options):
            return {"ok": True}

    class Owner:
        def __init__(self, sessions):
            self.sessions = iter(sessions)
            self.session = next(self.sessions)
            self.fail_switch = False
            self.noop_fork = False

        def _replace(self):
            self.session = next(self.sessions)

        async def new_session(self, _options):
            self._replace()

        async def switch_session(self, _path):
            if self.fail_switch:
                raise RuntimeError("switch failed")
            self._replace()

        async def fork(self, _entry_id):
            if self.noop_fork:
                return {"ok": True}
            self._replace()
            return {"ok": True}

        async def dispose(self):
            return None

    sessions = [Session(name) for name in ("old", "new", "switched", "forked", "navigated")]
    owner = Owner(sessions)
    host = TuiRuntimeHost(owner, None)
    host._snapshot = lambda: {"epoch": host._epoch, "sidebar": {}, "messages": []}  # type: ignore[method-assign]
    seen = []
    host.subscribe(seen.append)

    def assert_current(session, epoch):
        session.emit({"type": "thinking_delta", "delta": session.session_id})
        assert seen[-1] == {
            "type": "thinking_delta", "delta": session.session_id,
            "epoch": epoch, "sourceSessionId": session.session_id,
        }
        assert len(session.listeners) == 1

    def assert_rejected(callback, label):
        count = len(seen)
        callback({"type": "thinking_delta", "delta": label})
        assert len(seen) == count

    # A failed switch must not detach the live current-session listener.
    owner.fail_switch = True
    monkeypatch.setattr(
        SessionManager, "list", staticmethod(lambda *_args: [SimpleNamespace(path="/tmp/target", id="target")]),
    )
    with pytest.raises(RuntimeError, match="switch failed"):
        await host.dispatch("switch_session", {"path": "/tmp/target", "id": "target"})
    assert_current(sessions[0], 0)

    old_callback = sessions[0].listeners[0]
    await host.dispatch("new_session", {})
    assert host._epoch == 1
    assert_rejected(old_callback, "late-new")
    assert_current(sessions[1], 1)
    assert sessions[0].listeners == []

    old_callback = sessions[1].listeners[0]
    owner.fail_switch = False
    await host.dispatch("switch_session", {"path": "/tmp/target", "id": "target"})
    assert host._epoch == 2
    assert_rejected(old_callback, "late-switch")
    assert_current(sessions[2], 2)

    # AgentSessionRuntimeHost.fork may have no branch path to replace with.
    # That is a successful no-op, not a new epoch or a listener rebind.
    owner.noop_fork = True
    current_callback = sessions[2].listeners[0]
    await host.dispatch("ui", {"operation": "fork", "payload": {"entryId": "entry"}})
    assert host._epoch == 2
    assert sessions[2].listeners == [current_callback]
    assert_current(sessions[2], 2)

    old_callback = sessions[2].listeners[0]
    owner.noop_fork = False
    await host.dispatch("ui", {"operation": "fork", "payload": {"entryId": "entry"}})
    assert host._epoch == 3
    assert_rejected(old_callback, "late-fork")
    assert_current(sessions[3], 3)

    async def navigate_tree(_entry_id, _options):
        owner._replace()
        return {"ok": True}

    sessions[3].navigate_tree = navigate_tree
    old_callback = sessions[3].listeners[0]
    await host.dispatch("ui", {
        "operation": "navigate",
        "payload": {"entryId": "entry", "summarize": False, "customInstructions": None},
    })
    assert host._epoch == 4
    assert_rejected(old_callback, "late-navigate")
    assert_current(sessions[4], 4)

    await host.dispose()
    assert sessions[4].listeners == []


@pytest.mark.asyncio
async def test_tui_host_uses_explicit_detached_session_and_control_commands() -> None:
    """The host, rather than Textual, owns session and settings mutations."""
    calls: list[tuple[str, object]] = []

    class Settings:
        def set_theme(self, value): calls.append(("theme", value))
        def set_retry_mode(self, value): calls.append(("retry", value))
        def set_bash_show_output(self, value): calls.append(("bash", value))
        def set_default_provider(self, value): calls.append(("provider", value))
        def set_default_model(self, value): calls.append(("model", value))

    class Session:
        settings_manager = Settings()
        def set_thinking_level(self, value): calls.append(("thinking", value))
        def set_auto_retry_enabled(self, value): calls.append(("autoRetry", value))
        async def set_model(self, value): calls.append(("setModel", value.id))
        model_registry = SimpleNamespace(resolve=lambda provider, model, allow_dynamic: SimpleNamespace(provider=provider, id=model))

    session = Session()
    runtime_host = SimpleNamespace(session=session)
    host = TuiRuntimeHost(runtime_host, None)
    async def snapshot():
        return {"epoch": host._epoch, "sidebar": {}}
    host.snapshot = snapshot  # type: ignore[method-assign]

    await host.dispatch("ui", {"operation": "set_theme", "payload": {"theme": "ocean"}})
    await host.dispatch("ui", {"operation": "set_retry", "payload": {"mode": "unlimited"}})
    await host.dispatch("ui", {"operation": "set_model", "payload": {"provider": "stub", "model": "m"}})
    with pytest.raises(ValueError, match="invalid TUI operation payload"):
        await host.dispatch("ui", {"operation": "set_theme", "payload": {"theme": "ocean", "extra": True}})
    with pytest.raises(ValueError, match="invalid TUI runtime command payload"):
        await host.dispatch("switch_session", {"path": "/tmp/session"})

    assert calls == [
        ("theme", "ocean"), ("retry", "unlimited"), ("autoRetry", True),
        ("setModel", "m"), ("provider", "stub"), ("model", "m"),
    ]


def test_tui_snapshot_projection_stays_below_command_result_cap() -> None:
    """A 500-entry reload must not be rejected by the facade result cap."""
    session = type("Session", (), {
        "messages": [{"role": "assistant", "content": "x" * 32_768} for _ in range(500)],
    })()
    host = object.__new__(TuiRuntimeHost)
    host._epoch = 4
    host._session = lambda: session
    host._sidebar = lambda: {"model": "fake/model"}

    snapshot = host._snapshot()

    assert len(snapshot["messages"]) <= 500
    assert any("omitted" in row["content"] for row in snapshot["messages"])
    assert len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")) < 64 * 1024


def test_tui_snapshot_whole_document_is_bounded_and_keeps_latest_control_state() -> None:
    """Combined registry/MCP/monitor/history pressure cannot evict controls."""
    class Registry:
        def get_available(self): return [object()] * 20_000
    session = type("Session", (), {
        "messages": [{"role": "assistant", "content": f"{index}:" + "x" * 6000} for index in range(500)],
        "model_registry": Registry(),
        "subagent_monitor": SimpleNamespace(snapshot=lambda: {"revision": 1, "children": [
            {"sessionId": str(i), "task": "y" * 20_000} for i in range(100)
        ]}),
        "get_pending_questions": lambda self: [{"id": "q", "question": "z" * 20_000}],
    })()
    host = object.__new__(TuiRuntimeHost)
    host._epoch = 9
    host._session = lambda: session
    host._sidebar = lambda: {"sessionId": "s", "model": "p/m", "huge": "x" * 100_000}
    host._mcp = SimpleNamespace(server_status=lambda: [{"name": "m", "schema": "s" * 100_000}])
    host._approval_active = {"id": "a", "tool": "bash", "origin": "main", "displayArgs": "x" * 20_000}
    host._startup = {"mcpErrors": ["x" * 20_000] * 100, "resourceDiagnostics": {"x" * 500: ["y" * 20_000]}}

    snapshot = host._snapshot()

    assert len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")) < 60 * 1024
    assert snapshot["epoch"] == 9
    assert snapshot["approval"]["id"] == "a"
    assert snapshot["questions"][0]["id"] == "q"
    assert any("499:" in row["content"] for row in snapshot["messages"])


def test_tui_snapshot_keeps_large_tool_evidence_identity_and_monitor_details() -> None:
    evidence_id = "evidence-1"
    session = SimpleNamespace(
        messages=[{"role": "toolResult", "content": json.dumps({"evidenceId": evidence_id, "outputText": "x" * 10_000})}],
        model_registry=SimpleNamespace(get_available=lambda: []),
        subagent_monitor=SimpleNamespace(snapshot=lambda: {"revision": 1, "children": [{
            "sessionId": "child", "outputTail": "tail", "timeline": [{"kind": "result", "text": "detail"}],
        }]}),
        get_pending_questions=lambda: [],
    )
    host = object.__new__(TuiRuntimeHost)
    host._epoch, host._session, host._sidebar, host._mcp, host._approval_active, host._startup = 1, lambda: session, lambda: {}, None, None, {}
    snapshot = host._snapshot()
    assert snapshot["messages"][0]["evidenceId"] == evidence_id
    assert snapshot["monitor"]["children"][0]["timeline"][0]["text"] == "detail"


def test_evidence_page_projects_only_sanitized_result_and_uses_byte_pages() -> None:
    secret = "Bearer split-secret"
    raw = {"outputText": "a" * 16_000 + secret + "\\\"\x00", "args": {"command": "do-not-show"}, "token": "nope"}
    session = SimpleNamespace(session_manager=SimpleNamespace(read_evidence=lambda _id: {"rawResult": raw}))
    host = object.__new__(TuiRuntimeHost)
    host._epoch, host._session = 2, lambda: session
    first = host.evidence_page("id", 0, 2)
    assert len(json.dumps(first, ensure_ascii=False).encode()) < 64 * 1024
    assert "split-secret" not in first["text"] and "do-not-show" not in first["text"]
    assert first["nextOffset"] is not None
    last = host.evidence_page("id", first["nextOffset"], 2)
    assert "split-secret" not in last["text"]
    with pytest.raises(ValueError, match="stale"):
        host.evidence_page("id", 0, 1)


def test_evidence_page_rejects_unicode_mid_codepoint_offsets() -> None:
    session = SimpleNamespace(session_manager=SimpleNamespace(
        read_evidence=lambda _id: {"rawResult": {"outputText": "é" * 8_000}}
    ))
    host = object.__new__(TuiRuntimeHost)
    host._epoch, host._session = 2, lambda: session
    first = host.evidence_page("id", 0, 2)
    assert first["nextOffset"] is not None
    assert first["text"].endswith("é")
    mid_codepoint = len(b'{"outputText":"') + 1
    with pytest.raises(ValueError, match="invalid evidence offset"):
        host.evidence_page("id", mid_codepoint, 2)


def _bootstrap(**overrides: object) -> TuiBootstrap:
    values: dict[str, object] = {
        "cwd": "/work", "agent_dir": "/agent", "session_path": None,
        "session_dir": None, "provider": None, "model": None, "api_key": None,
        "thinking": None, "temperature": None, "temperature_mode": None, "tools": (),
    }
    values.update(overrides)
    return TuiBootstrap(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tools", "no_tools", "expected_tools"),
    [((), False, None), ((), True, []), (("read",), False, ["read"])],
)
async def test_tui_worker_bootstrap_reloads_before_runtime_and_preserves_tool_selection(
    monkeypatch: pytest.MonkeyPatch, tools: tuple[str, ...], no_tools: bool, expected_tools: list[str] | None,
) -> None:
    import one.core.tui_runtime_host as module

    calls: list[str] = []
    captured: dict[str, object] = {}

    class Settings:
        @classmethod
        def create(cls, *_args): return cls()
        def get_default_provider(self): return "configured"
        def get_default_model(self): return "configured-model"
        def get_default_thinking_level(self): return "off"
        def get_default_temperature(self): return 0.5
        def get_session_dir(self): return None
        def get_tool_approval(self): return False

    class Loader:
        def __init__(self, **_kwargs): pass
        async def reload(self): calls.append("reload")
        def get_skills(self): return {"diagnostics": ["bad skill"]}
        def get_extensions(self): return {"diagnostics": []}
        def get_prompts(self): return {"diagnostics": []}
        def get_themes(self): return {"diagnostics": []}

    class Registry:
        @classmethod
        def create(cls, *_args): return cls()
        def select_default(self, provider, model):
            calls.append(f"default:{provider}/{model}")
            return "selected-default"
        def all(self): return []

    class Auth:
        @classmethod
        def create(cls, *_args): return cls()

    class Managers:
        @staticmethod
        def create(cwd, session_dir): return SimpleNamespace(cwd=cwd, session_dir=session_dir)

    async def make_runtime(bootstrap, _options):
        assert calls[0] == "reload"
        captured.update(bootstrap)
        return SimpleNamespace(session=SimpleNamespace(approval_callback=None))

    monkeypatch.setattr(module, "SettingsManager", Settings)
    monkeypatch.setattr(module, "DefaultResourceLoader", Loader)
    monkeypatch.setattr(module, "ModelRegistry", Registry)
    monkeypatch.setattr(module, "AuthStorage", Auth)
    monkeypatch.setattr(module, "SessionManager", Managers)
    monkeypatch.setattr(module, "get_default_session_dir", lambda *_args: "/fallback")
    monkeypatch.setattr(module, "create_agent_session_runtime", make_runtime)

    host = await create_tui_runtime(_bootstrap(tools=tools, no_tools=no_tools, no_mcp=True))

    assert captured["model"] == "selected-default"
    assert calls[1] == "default:configured/configured-model"
    selected = [tool.name for tool in captured["tools"]]
    assert selected == (expected_tools if expected_tools is not None else list(module.DEFAULT_TOOL_NAMES))
    assert host._startup == {"mcpErrors": [], "resourceDiagnostics": {"skills": ["bad skill"]}}  # noqa: SLF001


def test_tui_worker_session_dir_falls_back_when_configured_dir_is_not_writable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import one.core.tui_runtime_host as module

    settings = SimpleNamespace(get_session_dir=lambda: "/unwritable")
    monkeypatch.setattr(module, "ensure_private_dir", lambda _path: None)
    monkeypatch.setattr(module.Path, "write_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("nope")))
    monkeypatch.setattr(module, "get_default_session_dir", lambda *_args: "/fallback")

    assert module._session_dir_with_fallback(_bootstrap(), settings) == "/fallback"


def test_tui_worker_startup_data_copies_mcp_errors_without_printing() -> None:
    import one.core.tui_runtime_host as module

    mcp = SimpleNamespace(errors=lambda: ["MCP server 'stub': unavailable"])
    loader = SimpleNamespace(
        get_skills=lambda: {"diagnostics": ["invalid skill"]},
        get_extensions=lambda: {"diagnostics": []},
        get_prompts=lambda: {"diagnostics": []},
        get_themes=lambda: {"diagnostics": []},
    )

    assert module._safe_startup_data(mcp, loader) == {
        "mcpErrors": ["MCP server 'stub': unavailable"],
        "resourceDiagnostics": {"skills": ["invalid skill"]},
    }


@pytest.mark.asyncio
async def test_tui_worker_bootstrap_keeps_mcp_errors_detached_and_cleans_partial_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import one.core.tui_runtime_host as module

    closed: list[str] = []

    class Settings:
        @classmethod
        def create(cls, *_args): return cls()
        def get_default_provider(self): return "p"
        def get_default_model(self): return "m"
        def get_default_thinking_level(self): return "off"
        def get_default_temperature(self): return 0.5
        def get_session_dir(self): return None
        def get_tool_approval(self): return False

    class Loader:
        def __init__(self, **_kwargs): pass
        async def reload(self): pass
        async def close(self): closed.append("loader")
        def get_skills(self): return {"diagnostics": []}
        def get_extensions(self): return {"diagnostics": []}
        def get_prompts(self): return {"diagnostics": []}
        def get_themes(self): return {"diagnostics": []}

    class Mcp:
        @classmethod
        def create(cls, _settings): return cls()
        async def start(self): pass
        async def close(self): closed.append("mcp")
        def errors(self): return ["MCP server 'stub': unavailable"]

    class Auth:
        @classmethod
        def create(cls, *_args): return cls()

    class Registry:
        @classmethod
        def create(cls, *_args): return cls()
        def select_default(self, *_args): return None

    class Managers:
        @staticmethod
        def create(cwd, session_dir): return SimpleNamespace(cwd=cwd, session_dir=session_dir)

    runtime = SimpleNamespace(session=SimpleNamespace(approval_callback=None))
    async def make_runtime(*_args): return runtime
    async def close_runtime(): closed.append("runtime")
    runtime.dispose = close_runtime

    monkeypatch.setattr(module, "SettingsManager", Settings)
    monkeypatch.setattr(module, "DefaultResourceLoader", Loader)
    monkeypatch.setattr(module, "McpManager", Mcp)
    monkeypatch.setattr(module, "AuthStorage", Auth)
    monkeypatch.setattr(module, "ModelRegistry", Registry)
    monkeypatch.setattr(module, "SessionManager", Managers)
    monkeypatch.setattr(module, "get_default_session_dir", lambda *_args: "/fallback")
    monkeypatch.setattr(module, "create_agent_session_runtime", make_runtime)
    monkeypatch.setattr(module, "_safe_startup_data", lambda *_args: (_ for _ in ()).throw(RuntimeError("later failure")))

    with pytest.raises(RuntimeError, match="later failure"):
        await create_tui_runtime(_bootstrap())

    assert closed == ["runtime", "mcp", "loader"]


@pytest.mark.asyncio
async def test_tui_worker_bootstrap_attempts_all_cleanup_after_disposer_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import one.core.tui_runtime_host as module

    closed: list[str] = []

    class Settings:
        @classmethod
        def create(cls, *_args): return cls()
        def get_default_provider(self): return "p"
        def get_default_model(self): return "m"
        def get_default_thinking_level(self): return "off"
        def get_default_temperature(self): return 0.5
        def get_session_dir(self): return None
        def get_tool_approval(self): return False

    class Loader:
        def __init__(self, **_kwargs): pass
        async def reload(self): pass
        async def close(self): closed.append("loader")
        def get_skills(self): return {"diagnostics": []}
        def get_extensions(self): return {"diagnostics": []}
        def get_prompts(self): return {"diagnostics": []}
        def get_themes(self): return {"diagnostics": []}

    class Mcp:
        @classmethod
        def create(cls, _settings): return cls()
        async def start(self): pass
        async def close(self):
            closed.append("mcp")
            raise RuntimeError("MCP cleanup failed")

    class Auth:
        @classmethod
        def create(cls, *_args): return cls()

    class Registry:
        @classmethod
        def create(cls, *_args): return cls()
        def select_default(self, *_args): return None

    class Managers:
        @staticmethod
        def create(cwd, session_dir): return SimpleNamespace(cwd=cwd, session_dir=session_dir)

    runtime = SimpleNamespace(session=SimpleNamespace(approval_callback=None))
    async def make_runtime(*_args): return runtime
    async def dispose_runtime():
        closed.append("runtime")
        raise RuntimeError("runtime cleanup failed")
    runtime.dispose = dispose_runtime

    monkeypatch.setattr(module, "SettingsManager", Settings)
    monkeypatch.setattr(module, "DefaultResourceLoader", Loader)
    monkeypatch.setattr(module, "McpManager", Mcp)
    monkeypatch.setattr(module, "AuthStorage", Auth)
    monkeypatch.setattr(module, "ModelRegistry", Registry)
    monkeypatch.setattr(module, "SessionManager", Managers)
    monkeypatch.setattr(module, "get_default_session_dir", lambda *_args: "/fallback")
    monkeypatch.setattr(module, "create_agent_session_runtime", make_runtime)
    monkeypatch.setattr(module, "_safe_startup_data", lambda *_args: (_ for _ in ()).throw(ValueError("construction failure")))

    with pytest.raises(ValueError, match="construction failure"):
        await create_tui_runtime(_bootstrap())

    assert closed == ["runtime", "mcp", "loader"]
