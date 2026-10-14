# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
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
    auth.set_runtime_api_key("openai", "test-key")
    registry = ModelRegistry.create(auth, str(tmp_path / "models.json"))
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4}, "bash": {"showOutput": True}})
    sm = SessionManager.in_memory(str(tmp_path))
    return AgentSession(sm, settings, registry, _FakeLoader(), model, "medium")


def test_completed_assistant_pages_are_full_bounded_and_epoch_bound(tmp_path: Path) -> None:
    """Canonical finish prose is paged, never copied into a lifecycle DTO."""
    session = _mk_session(tmp_path)
    summary = "TUI_COMMANDS!  " + "界 trailing  \n" * 1099 + "tail  "
    message = {"role": "assistant", "content": [{"type": "text", "text": summary}], "timestamp": 7}
    session.messages.append(message)

    class Owner:
        def __init__(self, value):
            self.session = value

    host = TuiRuntimeHost(Owner(session), None)
    ref = host._completed_ref(message)
    assert ref is not None
    first = host.assistant_page(ref, 0, 96, 20, host._epoch)
    assert first["totalRows"] > 500
    assert first["rows"][0].startswith("TUI_COMMANDS!")
    assert len(str(first).encode("utf-8")) < 13 * 1024
    middle = host.assistant_page(ref, 500, 96, 20, host._epoch)
    assert middle["rows"]
    tail_offset = first["totalRows"] - 1
    tail = host.assistant_page(ref, tail_offset, 96, 20, host._epoch)
    assert tail["rows"][-1] == "tail  "
    with pytest.raises(ValueError):
        host.assistant_page(ref, 0, 96, 20, host._epoch + 1)


@pytest.mark.asyncio
async def test_real_worker_faked_finish_100kb_1200lines_pages_reflow_and_restore(tmp_path: Path) -> None:
    """A native finish travels through the worker, facade, view and real pager."""
    from textual.widgets import Static

    from one.modes.tui_mode import TuiSessionView
    from one.providers.base import ChatResult

    # CJK makes physical-cell wrapping observable while also exceeding the old
    # 8 KiB live-preview budget. The canonical native finish is 100+ KiB.
    summary = "\n".join(f"line {number:04d} 界" * 20 for number in range(1200))
    assert len(summary.encode("utf-8")) > 100 * 1024

    class Loader:
        cwd = str(tmp_path)

        def get_system_prompt(self, selected_tools=None):  # noqa: ARG002
            return "test"

    class Provider:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, *args, on_delta=None, **kwargs):  # noqa: ANN002, ANN003
            self.calls += 1
            if on_delta is not None:
                on_delta("preview " * 2000)
            text = summary if self.calls == 1 else "next prompt remains separate"
            return ChatResult(
                text="", raw={}, usage={}, stop_reason="tool_calls", had_native_tool_call=True,
                native_tool_calls=[{"id": f"finish-{self.calls}", "name": "finish", "arguments": {
                    "summary": text, "goal_success": True,
                }}],
            )

    provider = Provider()
    events: list[dict] = []

    async def factory(_spec):
        session = _mk_session(tmp_path)
        session.providers = {"openai": provider}
        host = TuiRuntimeHost(SimpleNamespace(session=session, dispose=session.dispose), None)
        host.subscribe(events.append)
        return host

    facade = AgentRuntimeFacade(
        RuntimeBootstrapSpec(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), factory,
        command_allowlist=TUI_COMMANDS,
    )
    await facade.start()
    view = TuiSessionView(facade)
    await view.refresh()
    app = _OneTextualApp(view)
    try:
        async with app.run_test() as pilot:
            await pilot.press(*list("first"), "enter")
            for _ in range(100):
                await pilot.pause(0.02)
                if not app._turn_active and app._completed_virtuals:
                    break

            assert any(event.get("completed") is True and event.get("completedRef", "").startswith("a:") for event in events), [
                (event.get("type"), event.get("completed"), event.get("completedRef"), event.get("message", {}).get("stopReason"), str(event.get("message", {}).get("content", ""))[:200]) for event in events
            ]
            assert app._turn_active is False
            assert "[display truncated; full text remains in session history]" not in "\n".join(app._stream_lines)
            virtual = next(iter(app._completed_virtuals.values()))
            assert virtual["total"] >= 1200
            assert virtual["rows"]
            assert virtual["width"] > 1

            stream_widget = app.query_one("#stream", Static)
            completed_index = next(index for index, line in enumerate(app._stream_lines) if line.startswith("\0completed:"))
            completed_start = stream_widget._offsets[completed_index]
            container = app.query_one("#stream_container")
            container.scroll_to(y=completed_start + 600, animate=False, force=True)
            await pilot.pause(0.02)
            middle = await view.assistant_page(virtual["ref"], 600, 1, virtual["width"])
            assert middle["rows"][0].startswith("line ")

            container.scroll_to(y=completed_start + virtual["total"] - 2, animate=False, force=True)
            stream_widget.render_line(completed_start + virtual["total"] - 2)
            tail = await view.assistant_page(virtual["ref"], virtual["total"] - 1, 1, virtual["width"])
            assert "line 1199" in tail["rows"][-1]

            old_width = virtual["width"]
            await pilot.resize_terminal(60, 24)
            for _ in range(30):
                await pilot.pause(0.02)
                if virtual["width"] != old_width and virtual["rows"]:
                    break
            assert virtual["width"] < old_width  # 60-column terminal; stream excludes its frame/sidebar.
            assert all("[Completed response unavailable.]" not in row for row in virtual["rows"].values())
            reflow = await view.assistant_page(virtual["ref"], 0, 1, virtual["width"])
            assert "界" in reflow["rows"][0]

            # A later prompt and a fresh detached snapshot retain old completed
            # placeholders and re-open the long canonical summary.
            await pilot.press(*list("second"), "enter")
            for _ in range(100):
                await pilot.pause(0.02)
                if not app._turn_active and len(app._completed_virtuals) == 2:
                    break
            assert len(app._completed_virtuals) == 2
            await view.refresh()
            app._rebuild_session_transcript()
            for _ in range(30):
                await pilot.pause(0.02)
                if any(value["total"] >= 1200 for value in app._completed_virtuals.values()):
                    break
            assert any(value["total"] >= 1200 and value["rows"] for value in app._completed_virtuals.values())
            assert "next prompt remains separate" in str(app.query_one("#stream", Static).content)
    finally:
        await facade.close()
        assert facade.finalize()


@pytest.mark.asyncio
@pytest.mark.parametrize("start,end,expected", [
    ((1, 1), (7, 2), "甲界e\u0301  \nwrappe"),
    ((7, 2), (1, 1), "甲界e\u0301  \nwrappe"),
])
async def test_transcript_mouse_drag_copies_unicode_wrapped_selection_after_scroll(
    tmp_path: Path, start: tuple[int, int], end: tuple[int, int], expected: str
) -> None:
    """Real Textual drag events select cached CJK/combining rows without cache mutation."""
    from textual import events

    from one.modes.tui_transcript import TranscriptWidget

    app = _OneTextualApp(_mk_session(tmp_path))
    copied: list[str] = []
    async with app.run_test(size=(100, 40)) as pilot:
        app._append_stream_lines(["skip", "甲界e\u0301  ", "wrapped tail", "after"])
        app._render_stream()
        await pilot.pause()
        stream = app.query_one("#stream", TranscriptWidget)
        assert stream.region.height >= 3, stream.region
        container = app.query_one("#stream_container")
        container.scroll_to(y=1, animate=False, force=True)
        await pilot.pause()
        # Populate and retain the immutable base row before selection paint.
        stream.render_line(1)
        base = stream._line_rows[stream._line_ids[1]][0]
        base_segments = tuple(base)
        app._copy_to_clipboard = lambda text: (copied.append(text) or ("copied", "test"))  # type: ignore[method-assign]

        await pilot.mouse_down(stream, offset=start)
        # Pilot has down/up helpers but no move helper; use its real event router
        # so coordinates remain widget-relative rather than assigning selections.
        await pilot._post_mouse_events([events.MouseMove], widget=stream, offset=end, button=1)  # noqa: SLF001
        await pilot.mouse_up(stream, offset=end)
        await pilot.pause()

        assert app.screen.selections, (stream.region, stream.allow_select)
        assert stream in app.screen.selections
        assert app.screen.get_selected_text() == expected
        assert copied == [expected]
        selected = stream.render_line(1)
        assert tuple(base) == base_segments
        assert selected != base


@pytest.mark.asyncio
async def test_completed_page_queue_is_bounded_and_retains_rapid_scroll_rows(tmp_path: Path) -> None:
    """Demanded rows queue losslessly while only two requests per block run."""
    from one.modes.tui_mode import TuiSessionView

    session = _mk_session(tmp_path)
    message = {"role": "assistant", "content": [{"type": "text", "text": "\n".join(f"row-{i}" for i in range(1300))}]}
    session.messages.append(message)
    host = TuiRuntimeHost(SimpleNamespace(session=session, dispose=session.dispose), None)
    ref = host._completed_ref(message)
    assert ref is not None
    release = asyncio.Event()
    calls: list[int] = []

    class Facade:
        async def command(self, command, payload=None):
            payload = payload or {}
            if command == "snapshot":
                return {"result": {"epoch": 0, "sidebar": {"theme": "default"}, "messages": []}}
            assert command == "assistant_page"
            calls.append(payload["offset"])
            await release.wait()
            return {"result": host.assistant_page(**payload)}

        def subscribe(self, _listener):
            return lambda: None

    view = TuiSessionView(Facade())
    await view.refresh()
    app = _OneTextualApp(view)
    app._completed_virtuals[ref] = {"ref": ref, "total": 1300, "width": 20, "rows": {}, "pages": [], "pending": set(), "queued": set(), "request_generation": 0, "error": None}
    for row in range(1000):
        app._need_completed_virtual_row(ref, row)
    virtual = app._completed_virtuals[ref]
    assert len(virtual["pending"]) <= 2
    assert app._completed_page_inflight <= 4
    assert virtual["queued"]
    release.set()
    for _ in range(400):
        await asyncio.sleep(0.005)
        if not virtual["queued"] and not virtual["pending"]:
            break
    assert not virtual["queued"]
    assert virtual["rows"][999] == "row-999"
    assert len(calls) < 20


@pytest.mark.asyncio
async def test_completed_page_old_width_reply_cannot_replace_resized_rows(tmp_path: Path) -> None:
    """An in-flight old-width response is rejected after the virtual reflows."""
    from one.modes.tui_mode import TuiSessionView

    entered, release = asyncio.Event(), asyncio.Event()

    class Facade:
        async def command(self, command, payload=None):
            if command == "snapshot":
                return {"result": {"epoch": 0, "sidebar": {"theme": "default"}, "messages": []}}
            entered.set()
            await release.wait()
            return {"result": {"epoch": 0, "width": 20, "totalRows": 1, "rows": ["old-width"], "nextOffset": None}}

        def subscribe(self, _listener): return lambda: None

    view = TuiSessionView(Facade())
    await view.refresh()
    app = _OneTextualApp(view)
    app._completed_virtuals["a:1"] = {"ref": "a:1", "total": 1, "width": 20, "rows": {}, "pages": [], "pending": {0}, "queued": set(), "request_generation": 0, "error": None}
    app._completed_page_inflight = 1
    task = asyncio.create_task(app._fetch_completed_virtual_page("a:1", 0, 0))
    await entered.wait()
    app._completed_virtuals["a:1"].update({"width": 10, "request_generation": 1})
    release.set()
    await task
    assert app._completed_virtuals["a:1"]["rows"] == {}


@pytest.mark.asyncio
async def test_virtual_selection_copies_evicted_1800_rows_once_through_facade(tmp_path: Path) -> None:
    """One explicit drag retains its own rows while the display cache stays bounded."""
    from textual.geometry import Offset
    from textual.selection import Selection

    from one.modes.tui_mode import TuiSessionView
    from one.modes.tui_transcript import TranscriptWidget

    lines = [f"TUI_COMMANDS row-{number:04d} trailing  " for number in range(1800)]
    session = _mk_session(tmp_path)
    message = {"role": "assistant", "content": [{"type": "text", "text": "\n".join(lines)}]}
    session.messages.append(message)
    host = TuiRuntimeHost(SimpleNamespace(session=session, dispose=session.dispose), None)
    ref = host._completed_ref(message)
    assert ref is not None

    async def factory(_spec):
        return host

    runtime = AgentRuntimeFacade(
        RuntimeBootstrapSpec(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), factory,
        command_allowlist=TUI_COMMANDS,
    )
    await runtime.start()
    calls: list[int] = []

    class CountingFacade:
        async def command(self, name, payload=None):  # noqa: ANN001
            if name == "assistant_page":
                calls.append(payload["offset"])
            return await runtime.command(name, payload)

        def subscribe(self, listener, **kwargs):  # noqa: ANN001
            return runtime.subscribe(listener, **kwargs)

    view = TuiSessionView(CountingFacade())
    await view.refresh()
    app = _OneTextualApp(view)
    copied: list[str] = []
    try:
        async with app.run_test(size=(120, 35)) as pilot:
            stream = app.query_one("#stream", TranscriptWidget)
            width = max(1, stream.region.width)
            app._completed_virtuals[ref] = {
                "ref": ref, "total": len(lines), "width": width, "rows": {}, "pages": [],
                "pending": set(), "queued": set(), "request_generation": 0, "error": None,
            }
            app._append_stream_lines(["\0completed:" + ref])
            app._render_stream()
            for _ in range(100):
                await pilot.pause(0.01)
                virtual = app._completed_virtuals[ref]
                if (
                    virtual["total"] == len(lines)
                    and virtual["width"] == stream.region.width
                    and stream._offsets[-1] == len(lines)
                ):
                    break
            assert app._completed_virtuals[ref]["total"] == len(lines)
            assert stream._offsets[-1] == len(lines)
            selection = Selection(Offset(0, 0), Offset(len(lines[-1]), len(lines) - 1))
            app.screen.selections[stream] = selection
            app._copy_to_clipboard = lambda text: (copied.append(text) or ("copied", "test"))  # type: ignore[method-assign]
            app._copy_transcript_selection(stream, selection)
            # Closing an unrelated overlay is not a transcript-selection change.
            app._evidence_panel = {"text": "unrelated", "offset": 0}
            app._close_evidence_panel(focus_input=False)
            for _ in range(450):
                await pilot.pause(0.01)
                if copied:
                    break

            virtual = app._completed_virtuals[ref]
            assert copied == ["\n".join(lines)]
            assert len(calls) <= 96  # bounded display-cache/viewport overlap; no unbounded retry loop.
            assert len(virtual["pending"]) <= 2
            assert app._completed_page_inflight <= 4
            assert len(virtual["rows"]) <= 12 * 96
            # The final repaint may legitimately re-demand its visible first
            # row after page-cache eviction; selection fetching itself never
            # leaves an unbounded all-rows queue behind.
            assert len(virtual["queued"]) <= 96
    finally:
        await runtime.close()
        assert runtime.finalize()


@pytest.mark.asyncio
async def test_virtual_selection_clear_or_epoch_change_cancels_waiting_copy(tmp_path: Path) -> None:
    """A waiting virtual drag cannot copy after its selection or session goes stale."""
    from textual.geometry import Offset
    from textual.selection import Selection

    from one.modes.tui_mode import TuiSessionView
    from one.modes.tui_transcript import TranscriptWidget

    entered, release = asyncio.Event(), asyncio.Event()

    class Facade:
        async def command(self, name, payload=None):  # noqa: ANN001
            if name == "snapshot":
                return {"result": {"epoch": 0, "sidebar": {"theme": "default"}, "messages": []}}
            entered.set()
            await release.wait()
            return {"result": {"epoch": 0, "width": 80, "totalRows": 1, "rows": ["late"], "nextOffset": None}}

        def subscribe(self, _listener, **_kwargs):
            return lambda: None

    view = TuiSessionView(Facade())
    await view.refresh()
    app = _OneTextualApp(view)
    copied: list[str] = []
    async with app.run_test() as pilot:
        stream = app.query_one("#stream", TranscriptWidget)
        app._completed_virtuals["a:1"] = {
            "ref": "a:1", "total": 1, "width": max(1, stream.region.width), "rows": {}, "pages": [],
            "pending": set(), "queued": set(), "request_generation": 0, "error": None,
        }
        app._append_stream_lines(["\0completed:a:1"])
        app._render_stream()
        await pilot.pause()
        selection = Selection(Offset(0, 0), Offset(1, 0))
        app.screen.selections[stream] = selection
        app._copy_to_clipboard = lambda text: (copied.append(text) or ("copied", "test"))  # type: ignore[method-assign]
        app._copy_transcript_selection(stream, selection)
        await entered.wait()
        app.screen.selections.pop(stream)
        release.set()
        await pilot.pause(0.05)
        assert copied == []

        # A new stale epoch has the same fail-closed result even if the old
        # physical selection mapping still exists.
        entered.clear()
        release.clear()
        app._completed_virtuals["a:1"].update({"rows": {}, "pages": [], "pending": set(), "queued": set(), "error": None})
        app.screen.selections[stream] = selection
        app._copy_transcript_selection(stream, selection)
        await entered.wait()
        view._snapshot["epoch"] = 1
        release.set()
        await pilot.pause(0.05)
        assert copied == []


@pytest.mark.asyncio
async def test_virtual_selection_page_error_is_finite_and_never_copies_partial(tmp_path: Path) -> None:
    """A failed page produces safe feedback once, rather than an endless partial copy."""
    from textual.geometry import Offset
    from textual.selection import Selection

    from one.modes.tui_mode import TuiSessionView
    from one.modes.tui_transcript import TranscriptWidget

    calls = 0

    class Facade:
        async def command(self, name, payload=None):  # noqa: ANN001
            nonlocal calls
            if name == "snapshot":
                return {"result": {"epoch": 0, "sidebar": {"theme": "default"}, "messages": []}}
            calls += 1
            raise RuntimeError("offline page failure")

        def subscribe(self, _listener, **_kwargs):
            return lambda: None

    view = TuiSessionView(Facade())
    await view.refresh()
    app = _OneTextualApp(view)
    copied: list[str] = []
    notices: list[str] = []
    async with app.run_test() as pilot:
        stream = app.query_one("#stream", TranscriptWidget)
        app._completed_virtuals["a:1"] = {
            "ref": "a:1", "total": 2, "width": max(1, stream.region.width), "rows": {}, "pages": [],
            "pending": set(), "queued": set(), "request_generation": 0, "error": None,
        }
        app._append_stream_lines(["\0completed:a:1"])
        app._render_stream()
        await pilot.pause()
        selection = Selection(Offset(0, 0), Offset(1, 1))
        app.screen.selections[stream] = selection
        app._copy_to_clipboard = lambda text: (copied.append(text) or ("copied", "test"))  # type: ignore[method-assign]
        app._toast = lambda message, **_kwargs: notices.append(message)  # type: ignore[method-assign]
        app._copy_transcript_selection(stream, selection)
        for _ in range(50):
            await pilot.pause(0.01)
            if notices:
                break
        assert copied == []
        assert calls <= 2
        assert notices == ["Copy unavailable: completed response page could not be loaded."]


@pytest.mark.asyncio
async def test_virtual_selection_beyond_provisional_geometry_never_copies_partial(tmp_path: Path) -> None:
    """An old/out-of-range physical drag fails closed rather than being clamped."""
    from textual.geometry import Offset
    from textual.selection import Selection

    from one.modes.tui_mode import TuiSessionView
    from one.modes.tui_transcript import TranscriptWidget

    class Facade:
        async def command(self, name, payload=None):  # noqa: ANN001
            if name == "snapshot":
                return {"result": {"epoch": 0, "sidebar": {"theme": "default"}, "messages": []}}
            return {"result": {"epoch": 0, "width": payload["width"], "totalRows": 1, "rows": ["only"], "nextOffset": None}}

        def subscribe(self, _listener, **_kwargs):
            return lambda: None

    view = TuiSessionView(Facade())
    await view.refresh()
    app = _OneTextualApp(view)
    copied: list[str] = []
    notices: list[str] = []
    async with app.run_test() as pilot:
        stream = app.query_one("#stream", TranscriptWidget)
        app._completed_virtuals["a:1"] = {
            "ref": "a:1", "total": 1, "width": max(1, stream.region.width), "rows": {}, "pages": [],
            "pending": set(), "queued": set(), "request_generation": 0, "error": None, "geometry_loaded": False,
        }
        app._append_stream_lines(["\0completed:a:1"])
        app._render_stream()
        await pilot.pause()
        selection = Selection(Offset(0, 0), Offset(1, 9))
        app.screen.selections[stream] = selection
        app._copy_to_clipboard = lambda text: (copied.append(text) or ("copied", "test"))  # type: ignore[method-assign]
        app._toast = lambda message, **_kwargs: notices.append(message)  # type: ignore[method-assign]
        app._copy_transcript_selection(stream, selection)
        for _ in range(50):
            await pilot.pause(0.01)
            if notices:
                break
        assert copied == []
        assert notices == ["Copy unavailable: selection geometry changed."]



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
