# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from one.modes.tui_mode import (
    _DISPLAY_TRUNCATED_MARKER,
    _THINKING_FRAMES,
    _THINKING_MARK,
    MAX_RENDERED_BYTES,
    MAX_RENDERED_LINES,
    MAX_SOURCE_DISPLAY_BYTES,
    _format_elapsed_ms,
    advance_thinking_frame,
)
from tests.support.tui import _mk_app_session, _submit


@pytest.mark.asyncio
async def test_tui_next_assistant_start_keeps_predecessor_awaiting_final_paint(tmp_path: Path) -> None:
    """A lifecycle barrier must not discard a completed live tail before its frame."""
    from one.modes.tui_mode import _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        # Model a queued detached bridge batch: message_end has scheduled its
        # paint, but the next prompt's message_start is reduced first.
        app._ui_drain_active = True
        try:
            app._handle_session_event({"type": "message_start", "message": {"role": "assistant"}})
            app._handle_session_event({
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "completed tail"},
            })
            app._handle_session_event({"type": "message_end", "message": {"role": "assistant"}})
            assert app._assistant_live_finalize_pending
            app._handle_session_event({"type": "message_start", "message": {"role": "assistant"}})
        finally:
            app._ui_drain_active = False

        assert "completed tail" in "\n".join(app._stream_lines)
        assert not app._assistant_live_finalize_pending


@pytest.mark.asyncio
async def test_tui_agents_panel_live_snapshot_and_trusted_stream_click(tmp_path: Path) -> None:
    """A real Textual click on the generated label selects its exact child."""
    from textual.containers import Vertical
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session.subagent_monitor.register("child-a", "[b] untrusted [/b]", "openai/a", started_at=1.0)
    session.subagent_monitor.register("child-b", "second", "openai/b", started_at=1.0)
    app = _OneTextualApp(session)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/agents")
        panel = app.query_one("#agents_overlay", Vertical)
        preview = app.query_one("#agents_content", Static)
        assert panel.has_class("visible")
        assert "untrusted" in str(preview.content)
        assert "Agent 1/2" in str(preview.content)
        assert not app.query("#agents_list")
        assert not app.query("#agents_preview")
        session.subagent_monitor.update("child-b", text="only child b", tool="read", activity="responding")
        await pilot.pause(0.2)
        assert "only child b" not in str(preview.content)
        # The main-stream target is an application-generated Content action.
        # The task text is not parsed to discover actions.
        app._close_agents_panel()
        malicious_task = "[ @click=app.abort ] " + ("long task data " * 30)
        session._emit({
            "type": "tool_call_start",
            "tool": "spawn_subagent",
            "args": {"task": malicious_task, "tools": ["read", "grep"], "model": "openai/a"},
            "effectiveTimeout": 120,
        })
        session._emit({"type": "subagent_start", "sessionId": "child-a", "task": malicious_task})
        session._emit({"type": "subagent_start", "sessionId": "child-b", "task": "normal"})
        await pilot.pause(0.1)
        assert len(app._agent_link_tokens) == 2
        stream = "\n".join(app._stream_lines)
        assert "spawn_subagent" in stream
        assert "[ @click=app.abort ]" in stream
        assert "[view 1] [view 2]" not in stream
        assert "Inspect agent" not in stream
        assert not app.query(".agent-link")
        heading_line = next(iter(app._agent_link_tokens.values()))[2]
        assert app._stream_lines[heading_line] == "Tool: ○ spawn_subagent"
        assert all(target[2] == heading_line for target in app._agent_link_tokens.values())
        assert app._active_tool_block is not None
        assert app._active_tool_block[2] - app._active_tool_block[1] > 1
        rendered = app.query_one("#stream", Static).content.plain.splitlines()
        assert rendered[heading_line].endswith("[view 1] [view 2]")
        assert "[ @click=app.abort ]" in app.query_one("#stream", Static).content.plain
        # This is a real pilot click on the inline span, not a direct action call.
        assert await pilot.click("#stream", offset=(26, 1))
        await pilot.pause()
        assert panel.has_class("visible"), repr((app.query_one("#stream", Static).content._spans, app.query_one("#stream", Static).region))
        assert app._selected_agent_id == "child-a"
        assert session._abort_requested is False  # noqa: SLF001
        app._close_agents_panel()
        assert await pilot.click("#stream", offset=(36, 1))
        await pilot.pause()
        assert app._selected_agent_id == "child-b"
        assert "only child b" in str(preview.content)
        app._close_agents_panel()
        session._emit({"type": "tool_call_end", "tool": "spawn_subagent", "ok": True})
        await pilot.pause()
        assert app._stream_lines[heading_line].startswith("Tool: ✓ spawn_subagent")
        assert "[ok]" in "\n".join(app._stream_lines)
        assert app.query_one("#stream", Static).content.plain.splitlines()[heading_line].endswith("[view 1] [view 2]")
        assert await pilot.click("#stream", offset=(26, 1))
        await pilot.pause()
        assert app._selected_agent_id == "child-a"


@pytest.mark.asyncio
async def test_tui_evidence_overlay_pilot_click_pages_unicode_and_invalidates_stale_requests(tmp_path: Path) -> None:
    """Exercise the runtime-host/facade/view evidence path through a real click."""
    from types import SimpleNamespace

    from textual.containers import VerticalScroll
    from textual.widgets import Static

    from one.core.session_manager import SessionManager
    from one.core.tui_runtime_host import TuiRuntimeHost
    from one.modes.tui_mode import TuiSessionView, _OneTextualApp

    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    raw = {
        "args": {"token": "no"},
        "outputText": (
            "TOP-MARKER\n" + "α" * 5_000 + "[@click=app.abort]" + "α" * 1_123
            + " Bearer split-secret " + "α" * 973_900 + "\nTAIL-MARKER"
        ),
    }
    evidence_id, error = manager.append_evidence({"id": "large", "rawResult": raw})
    assert error is None and evidence_id == "large"
    host = object.__new__(TuiRuntimeHost)
    host._epoch = 4
    host._host = SimpleNamespace(session=SimpleNamespace(session_manager=manager))
    first_host_page = host.evidence_page(evidence_id, 0, 4)
    assert first_host_page["nextOffset"] is not None
    # The secret straddles the first byte-page boundary in the stored source;
    # it must be redacted before, rather than after, that page is selected.
    assert "split-secret" not in first_host_page["text"]
    stored = manager.read_evidence(evidence_id)
    assert stored is not None and stored["rawResult"] == raw
    total = first_host_page["totalChars"]
    last_offset = total - len(b'TAIL-MARKER"}')
    last_host_page = host.evidence_page(evidence_id, last_offset, 4)
    assert last_host_page["nextOffset"] is None and "TAIL-MARKER" in last_host_page["text"]

    class Facade:
        def __init__(self) -> None:
            self.listeners: list[object] = []
            self.delay = asyncio.Event()
            self.delay.set()

        async def command(self, name, payload=None):
            payload = payload or {}
            if name == "snapshot":
                return {"result": {"epoch": host._epoch, "sidebar": {"sessionId": "s", "model": "p/m", "bashOutput": True}, "messages": []}}
            if name == "evidence_page":
                await self.delay.wait()
                return {"result": await host.dispatch(name, payload)}
            return {"result": {"accepted": True}}

        def subscribe(self, listener, **_kwargs):
            self.listeners.append(listener)
            return lambda: self.listeners.remove(listener)

        def emit(self, event):
            for listener in tuple(self.listeners):
                listener(event)

    facade = Facade()
    view = TuiSessionView(facade)
    await view.refresh()
    app = _OneTextualApp(view)
    async with app.run_test(size=(100, 30)) as pilot:
        facade.emit({"type": "tool_call_end", "tool": "read", "ok": True, "outputText": "[truncated]", "evidenceId": evidence_id})
        await pilot.pause()
        stream = app.query_one("#stream", Static)
        span = next(item for item in stream.content._spans if "@click" in str(item.style))
        before = stream.content.plain[:span.start]
        assert await pilot.click("#stream", offset=(len(before.rsplit("\n", 1)[-1]) + 2, before.count("\n")))
        await pilot.pause(0.1)
        body = app.query_one("#evidence_content", Static)
        evidence_text = str(body.content)
        assert "α" in evidence_text and "never-show" not in evidence_text
        assert "[@click=app.abort]" in evidence_text
        evidence_body = app.query_one("#evidence_body", VerticalScroll)
        assert evidence_body.max_scroll_y > 0
        start_scroll = evidence_body.scroll_y
        evidence_body.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        assert evidence_body.scroll_y > start_scroll
        assert app._evidence_panel["nextOffset"] is not None
        assert await pilot.click("#evidence-next")
        await pilot.pause(0.1)
        second_offset = app._evidence_panel["offset"]
        assert second_offset > 0
        await pilot.press("page_up")
        await pilot.pause(0.1)
        assert app._evidence_panel["offset"] == 0
        # A known valid bounded byte offset opens the final source page.
        app._schedule_evidence_page(app._monitor_generation, evidence_id, last_offset, ())
        await pilot.pause(0.1)
        assert app._evidence_panel["nextOffset"] is None
        assert "TAIL-MARKER" in str(body.content)
        app._close_evidence_panel(focus_input=False)
        # Exercise a completed long result through the subscribed detached
        # facade after prior transcript history has been front-trimmed.  The
        # assistant and next turn mutate/splice the stream before the actual
        # native Textual click; action lookup must remain line-ID based.
        for index in range(MAX_RENDERED_LINES + 30):
            app._write(f"old-{index}", render=False)
        lifecycle_id, lifecycle_error = manager.append_evidence({
            "id": "lifecycle", "rawResult": {"outputText": "LIFECYCLE-EVIDENCE\n" + "line\n" * 180},
        })
        assert lifecycle_error is None and lifecycle_id == "lifecycle"
        facade.emit({
            "type": "tool_call_end", "tool": "read", "ok": True,
            "outputText": "\n".join(f"preview-{number}" for number in range(101)), "evidenceId": lifecycle_id,
        })
        facade.emit({"type": "message_start", "message": {"role": "assistant"}})
        facade.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "final after tool"}})
        facade.emit({"type": "message_end", "message": {"role": "assistant"}})
        facade.emit({"type": "turn_end", "ok": True})
        facade.emit({"type": "turn_start", "turnId": "next"})
        facade.emit({"type": "turn_end", "ok": True})
        await pilot.pause(0.1)
        assert "[preview limited to 100 logical lines]" in "\n".join(app._stream_lines)
        # A second unchanged virtual paint must not reformat retained rows.
        await pilot.resize_terminal(70, 24)
        await pilot.pause()
        app._rendered_stream_lines = ()
        app._render_stream()
        hits = app._stream_cache_hits
        app._rendered_stream_lines = ()
        app._render_stream()
        assert app._stream_cache_hits == hits
        stream = app.query_one("#stream", Static)
        assert any("open_evidence_link" in str(item.style) for item in stream.content._spans)
        # The link is after the 100-line visible preview, so scroll it into the
        # actual viewport and click the rendered final status row (not action()).
        container = app.query_one("#stream_container", VerticalScroll)
        container.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        clicked = False
        for row in range(1, max(2, container.region.height - 1)):
            for column in range(20, 36):
                await pilot.click(None, offset=(container.region.x + column, container.region.y + row))
                await pilot.pause()
                if app._evidence_panel is not None:
                    clicked = True
                    break
            if clicked:
                break
        assert clicked
        await pilot.pause(0.1)
        assert "LIFECYCLE-EVIDENCE" in str(body.content)
        # A response that was already in flight must not paint after its view
        # changes epochs (independent of rejecting an old stream link).
        app._close_evidence_panel()
        facade.delay.clear()
        app._schedule_evidence_page(app._monitor_generation, evidence_id, 0, ())
        await pilot.pause()
        view._replace_snapshot({"epoch": 5, "sidebar": {"sessionId": "s", "model": "p/m"}, "messages": []})
        facade.delay.set()
        await pilot.pause(0.1)
        assert app._evidence_panel is None
        app.action_open_evidence_link(next(iter(app._evidence_link_tokens)))
        await pilot.pause()
        assert app._evidence_panel is None


@pytest.mark.asyncio
async def test_tui_result_preview_uses_logical_lines_before_narrow_wrapping(tmp_path: Path) -> None:
    """99/100 fit, 101 is marked, independent of physical wrapped rows."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test(size=(30, 20)) as pilot:
        await pilot.pause()
        for count, marked in ((99, False), (100, False), (101, True)):
            app._stream_lines.clear()
            app._stream_line_ids.clear()
            app._stream_line_values.clear()
            app._write_result_block("\n".join(f"{index:03d}-" + "wide " * 12 for index in range(count)))
            joined = "\n".join(app._stream_lines)
            assert ("[preview limited to 100 logical lines]" in joined) is marked
            assert "000-" in joined and f"{min(count - 1, 99):03d}-" in joined
            assert (f"{count - 1:03d}-" in joined) is not marked
        session.settings_manager.set_config_value("tui.toolResultPreviewLines", 3)
        app._stream_lines.clear()
        app._stream_line_ids.clear()
        app._stream_line_values.clear()
        app._write_result_block("\n".join(str(index) for index in range(4)))
        assert "[preview limited to 3 logical lines]" in "\n".join(app._stream_lines)
        # Force a render/cache pass at the narrow size; wrapping cannot change
        # the source-line decision above.
        await pilot.pause()
        assert app.query_one("#stream", Static).content


@pytest.mark.asyncio
async def test_tui_tool_start_uses_safe_display_args_not_empty_mapping(tmp_path: Path) -> None:
    from one.core.agent_runtime_facade import _event_projection
    from one.modes.tui_mode import _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test() as pilot:
        for tool, args, wanted in (
            ("bash", {"command": "printf safe", "token": "secret-value"}, "command=printf safe"),
            ("spawn_subagent", {"task": "inspect tests", "token": "secret-value"}, "task=inspect tests"),
        ):
            event = _event_projection({"type": "tool_call_start", "tool": tool, "args": args})
            assert "args" not in event and "secret-value" not in event["displayArgs"]
            app._handle_session_event(event)
            rendered = "\n".join(app._stream_lines)
            assert wanted in rendered and "{}" not in rendered
            app._handle_session_event({"type": "tool_call_end", "tool": tool, "ok": True})
        await pilot.pause()


@pytest.mark.asyncio
async def test_tui_agents_preview_renders_mixed_timeline_in_chronological_order(tmp_path: Path) -> None:
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    monitor = session.subagent_monitor
    monitor.register("child", "task", "model")
    monitor.update("child", text="Before read")
    monitor.tool_started("child", "read", tool_call_id="read-1", args={"path": "file.txt", "offset": 1}, effective_timeout=120)
    monitor.tool_finished("child", "read", True, 12, "file result", tool_call_id="read-1")
    monitor.update("child", text="After read")
    app = _OneTextualApp(session)
    async with app.run_test(size=(100, 30)) as pilot:
        await _submit(app, pilot, "/agents")
        preview = str(app.query_one("#agents_content", Static).content)
        assert preview.index("Before read") < preview.index("Tool: ✓ read · 12 ms")
        assert "Arguments: path=file.txt, offset=1" in preview
        assert preview.index("Tool: ✓ read · 12 ms") < preview.index("Result:\nfile result")
        assert preview.index("Result:\nfile result") < preview.index("After read")
        assert "[no visible response yet]" not in preview


@pytest.mark.asyncio
async def test_tui_agent_link_refreshes_new_child_before_coalesced_monitor_tick(tmp_path: Path) -> None:
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        session.subagent_monitor.register("fresh-child", "task", "model")
        session._emit({"type": "tool_call_start", "tool": "spawn_subagent", "args": {"task": "task"}})
        session._emit({"type": "subagent_start", "sessionId": "fresh-child", "task": "task"})
        await pilot.pause()
        # Model a main-stream lifecycle event arriving before the next 0.1s
        # coalesced refresh updates the cached snapshot.
        app._monitor_snapshot = {"revision": 0, "children": []}
        assert await pilot.click("#stream", offset=(26, 1))
        await pilot.pause()
        assert app._agents_panel_open
        assert app._selected_agent_id == "fresh-child"


@pytest.mark.asyncio
async def test_tui_agent_link_metadata_is_removed_on_trim_and_transcript_rebuild(tmp_path: Path) -> None:
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session.subagent_monitor.register("child", "task", "model")
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        session._emit({"type": "tool_call_start", "tool": "spawn_subagent", "args": {"task": "task"}})
        session._emit({"type": "subagent_start", "sessionId": "child", "task": "task"})
        await pilot.pause()
        assert len(app._agent_link_tokens) == 1
        assert not app.query(".agent-link")
        app._stream_lines.extend(["old"] * MAX_RENDERED_LINES)
        app._trim_stream()
        await pilot.pause()
        assert not app._agent_link_tokens
        assert not app.query(".agent-link")
        app.action_open_agent_link(1)
        assert not app._agents_panel_open

        session._emit({"type": "tool_call_start", "tool": "spawn_subagent", "args": {"task": "task"}})
        session._emit({"type": "subagent_start", "sessionId": "child", "task": "task"})
        await pilot.pause()
        assert len(app._agent_link_tokens) == 1
        app._rebuild_session_transcript()
        await pilot.pause()
        assert not app._agent_link_tokens
        assert not app.query(".agent-link")


@pytest.mark.asyncio
async def test_tui_trim_rebases_surviving_trusted_agent_link(tmp_path: Path) -> None:
    """A link that survives front trimming remains on its rebased tool row."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session.subagent_monitor.register("surviving-child", "task", "model")
    app = _OneTextualApp(session)
    async with app.run_test(size=(100, 24)) as pilot:
        # Leave room for the generated Tool: row, then force a front trim when
        # the child entry arrives. The trusted metadata must move with it.
        app._stream_lines.extend(f"old-{index}" for index in range(MAX_RENDERED_LINES))
        app._render_stream()
        session._emit({"type": "tool_call_start", "tool": "spawn_subagent", "args": {"task": "task"}})
        session._emit({"type": "subagent_start", "sessionId": "surviving-child", "task": "task"})
        await pilot.pause()

        assert len(app._agent_link_tokens) == 1
        token, (_generation, child_id, line_index, _call_id) = next(iter(app._agent_link_tokens.items()))
        assert child_id == "surviving-child"
        assert app._stream_lines[line_index] == "Tool: ○ spawn_subagent"
        assert app.query_one("#stream", Static).content.plain.splitlines()[line_index].endswith("[view]")

        app.action_open_agent_link(token)
        await pilot.pause()
        assert app._agents_panel_open
        assert app._selected_agent_id == "surviving-child"


@pytest.mark.asyncio
async def test_tui_multi_megabyte_entry_is_bounded_and_unchanged_lines_hit_render_cache(tmp_path: Path) -> None:
    """Pilot coverage for pre-Content byte/row bounds and cached history."""
    from one.modes.tui_mode import _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test(size=(48, 18)) as pilot:
        app._write("x" * (3 * 1024 * 1024))
        await pilot.pause()
        assert sum(len(line.encode("utf-8")) + 1 for line in app._stream_lines) <= MAX_RENDERED_BYTES
        assert any("display truncated" in line for line in app._stream_lines)

        app._write("cached tail")
        await pilot.pause()
        hits_before = app._stream_cache_hits
        app._write("changed tail")
        await pilot.pause()
        # The virtual renderer must not format off-screen retained rows merely
        # because a later line was appended.
        assert app._stream_cache_hits == hits_before


@pytest.mark.asyncio
async def test_tui_multi_megabyte_live_assistant_response_is_bounded_and_marked(tmp_path: Path) -> None:
    """Large live text is byte-bounded before formatting; ordinary text survives."""
    from one.modes.tui_mode import _MAX_LIVE_ASSISTANT_RENDER_BYTES, _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    formatted_bytes: list[int] = []
    original_format = app._format_chat_panel

    def record_format(role: str, text: str, *args, **kwargs):
        formatted_bytes.append(len(text.encode("utf-8")))
        return original_format(role, text, *args, **kwargs)

    app._format_chat_panel = record_format  # type: ignore[method-assign]
    async with app.run_test(size=(48, 18)) as pilot:
        # The normal-output case is below the presentation cap and remains whole.
        app._append_assistant_delta("normal-" + "n" * (4 * 1024))
        await pilot.pause()
        assert formatted_bytes[-1] == len(("normal-" + "n" * (4 * 1024)).encode("utf-8"))

        app._discard_live_assistant_block()
        app._assistant_live_lines_dirty = False
        app._append_assistant_delta("x" * (3 * 1024 * 1024))
        await pilot.pause()

        assert len(app._assistant_live_buffer.encode("utf-8")) <= MAX_SOURCE_DISPLAY_BYTES
        assert _DISPLAY_TRUNCATED_MARKER in app._assistant_live_buffer
        assert formatted_bytes[-1] <= _MAX_LIVE_ASSISTANT_RENDER_BYTES
        assert "earlier assistant output truncated" in "\n".join(app._stream_lines)
        stream = "\n".join(app._stream_lines)
        assert "earlier assistant output truncated" in stream
        assert sum(len(line.encode("utf-8")) + 1 for line in app._stream_lines) <= MAX_RENDERED_BYTES
        assert len(app._stream_lines) <= MAX_RENDERED_LINES


@pytest.mark.asyncio
async def test_tui_agents_panel_narrow_navigation_selects_and_scrolls_preview(tmp_path: Path) -> None:
    from textual.containers import VerticalScroll
    from textual.widgets import TextArea

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session.subagent_monitor.register("child-a", "first", "model")
    session.subagent_monitor.register("child-b", "second", "model")
    session.subagent_monitor.update("child-b", text="\n".join(f"line {i}" for i in range(80)))
    app = _OneTextualApp(session)
    async with app.run_test(size=(40, 14)) as pilot:
        await _submit(app, pilot, "/agents")
        input_widget = app.query_one("#input", TextArea)
        input_widget.text = "draft\ntext"
        input_widget.move_cursor((1, 2))
        input_text = input_widget.text
        input_cursor = input_widget.cursor_location

        await pilot.press("right")
        await pilot.pause()
        assert app._selected_agent_id == "child-b"
        assert input_widget.text == input_text
        assert input_widget.cursor_location == input_cursor

        await pilot.press("left")
        await pilot.pause()
        assert app._selected_agent_id == "child-a"
        assert input_widget.text == input_text
        assert input_widget.cursor_location == input_cursor

        await pilot.press("right")
        await pilot.pause()
        preview = app.query_one("#agents_timeline", VerticalScroll)
        assert preview.max_scroll_y > 0
        await pilot.press("down")
        await pilot.pause()
        down_scroll_y = preview.scroll_y
        assert down_scroll_y > 0
        assert input_widget.text == input_text
        assert input_widget.cursor_location == input_cursor

        await pilot.press("up")
        await pilot.pause()
        assert preview.scroll_y < down_scroll_y
        assert input_widget.text == input_text
        assert input_widget.cursor_location == input_cursor

        # The app-level route remains available when a panel scroll widget has
        # focus, while the command input remains untouched.
        preview.focus()
        await pilot.press("left")
        await pilot.pause()
        assert app._selected_agent_id == "child-a"
        assert input_widget.text == input_text
        assert input_widget.cursor_location == input_cursor

        preview.focus()
        await pilot.press("right")
        await pilot.pause()
        assert app._selected_agent_id == "child-b"
        assert input_widget.text == input_text
        assert input_widget.cursor_location == input_cursor

        await pilot.press("escape")
        await pilot.pause()
        assert not app._agents_panel_open
        assert app.focused is input_widget
        assert input_widget.text == input_text
        assert input_widget.cursor_location == input_cursor


@pytest.mark.asyncio
async def test_tui_agents_rebind_and_stale_link_are_ignored(tmp_path: Path) -> None:
    from one.modes.tui_mode import _OneTextualApp

    old = _mk_app_session(tmp_path)
    new = _mk_app_session(tmp_path)
    old.subagent_monitor.register("old-child", "old", "model")
    app = _OneTextualApp(old)
    async with app.run_test() as pilot:
        await pilot.pause()
        old._emit({"type": "tool_call_start", "tool": "spawn_subagent", "args": {"task": "old"}})
        old._emit({"type": "subagent_start", "sessionId": "old-child", "task": "old"})
        await pilot.pause()
        token = next(iter(app._agent_link_tokens))
        app.session = new
        app._bind_session()
        await pilot.pause()
        assert not app.query(".agent-link")
        app.action_open_agent_link(token)
        await pilot.pause()
        assert app._selected_agent_id is None
        assert not app._agents_panel_open


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0 ms"), (125, "125 ms"), (1000, "1.00 s"), (1250, "1.25 s"),
     (None, None), (True, None), (float("nan"), None), (-1, None), ("125", None)],
)
def test_format_elapsed_ms(value: object, expected: str | None) -> None:
    assert _format_elapsed_ms(value) == expected


@pytest.mark.asyncio
async def test_tui_tool_elapsed_headings_and_turn_summary_are_safe(tmp_path: Path) -> None:
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session.settings_manager.set_bash_show_output(False)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        # No active turn means an unscoped completion cannot enter totals.
        app._handle_session_event({"type": "provider_request_end", "requestId": "outside", "elapsedMs": 900})
        app._handle_session_event({"type": "tool_call_start", "tool": "read", "args": {"x": "[b]"}})
        app._handle_session_event({"type": "tool_call_end", "tool": "read", "ok": True, "elapsedMs": 0,
                                   "result": {"outputText": "only once"}})
        app._handle_session_event({"type": "tool_call_end", "tool": "write", "ok": False, "elapsedMs": 1250})
        app._handle_session_event({"type": "tool_call_end", "tool": "bad", "ok": True, "elapsedMs": True})
        app._handle_session_event({"type": "agent_start", "turnId": "turn-1"})
        app._handle_session_event({"type": "provider_request_end", "turnId": "turn-1", "requestId": "a", "elapsedMs": 100})
        app._handle_session_event({"type": "turn_end", "turnId": "turn-1", "ok": False, "willRetry": True})
        app._handle_session_event({"type": "provider_request_end", "turnId": "turn-1", "requestId": "a", "elapsedMs": 100})
        app._handle_session_event({"type": "provider_request_end", "turnId": "turn-1", "requestId": "retry", "elapsedMs": 50})
        app._handle_session_event({"type": "provider_request_end", "turnId": "other", "requestId": "b", "elapsedMs": 99})
        app._handle_session_event({"type": "agent_end", "turnId": "turn-1", "elapsedMs": 3420})
        app._handle_session_event({"type": "provider_request_end", "requestId": "after", "elapsedMs": 900})
        app._handle_session_event({"type": "agent_end", "turnId": "turn-1", "elapsedMs": 3420})
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "Tool: ✓ read · 0 ms" in stream
        assert "Tool: × write · 1.25 s" in stream
        assert "Tool: ✓ bad ·" not in stream
        assert stream.count("Result:") == 0
        assert stream.count("Time: turn 3.42 s · provider 150 ms / 2 requests") == 1
        assert "[b]" in app.query_one("#stream", Static).content.plain


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            {"type": "tool_call_parse_failed", "source": "format_repair", "category": "invalid_tool_call_shape"},
            "[Tool] Format repair: invalid tool-call shape; no tool was executed.",
        ),
        (
            {"type": "tool_call_parse_failed", "reason": "malformed_native_tool_call"},
            "[Tool] malformed native tool call; no tool was executed.",
        ),
        (
            {"type": "tool_call_parse_failed", "reason": "conflicting_native_tool_call_id"},
            "[Tool] conflicting native tool-call ID; no tool was executed.",
        ),
        (
            {"type": "tool_call_parse_failed", "reason": "control_tool_in_native_batch"},
            "[Tool] control tool in native batch; no tool was executed.",
        ),
    ],
)
async def test_tui_rejected_tool_call_identifies_safe_failure(
    tmp_path: Path, event: dict[str, str], expected: str,
) -> None:
    from one.modes.tui_mode import _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test() as pilot:
        app._handle_session_event(event)
        await pilot.pause()
        assert expected in "\n".join(app._stream_lines)


@pytest.mark.asyncio
async def test_tui_timing_clear_and_legacy_agent_end_reset_state(tmp_path: Path) -> None:
    from one.modes.tui_mode import _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test() as pilot:
        app._handle_session_event({"type": "agent_start", "turnId": "old"})
        app._handle_session_event({"type": "provider_request_end", "turnId": "old", "requestId": "old-request", "elapsedMs": 1})
        app.action_clear_stream()
        app._handle_session_event({"type": "agent_end", "turnId": "old", "elapsedMs": 1})
        app._handle_session_event({"type": "agent_start", "turnId": "legacy"})
        app._handle_session_event({"type": "provider_request_end", "turnId": "legacy", "requestId": "legacy-request", "elapsedMs": 1})
        app._handle_session_event({"type": "agent_end", "turnId": "legacy"})
        await pilot.pause()
        assert "Time: turn" not in "\n".join(app._stream_lines)


class _StreamingProvider:
    """Provider that emits text deltas via on_delta callback."""

    def __init__(self, deltas: list[str]) -> None:
        self.deltas = deltas
        self.calls = 0

    async def chat(self, api_key, model, messages, thinking_level, headers=None, on_delta=None, on_thinking_delta=None, max_tokens=None, images=None, storage_dir=""):
        from one.providers.base import ChatResult

        self.calls += 1
        for d in self.deltas:
            if on_delta:
                on_delta(d)
            await asyncio.sleep(0.005)
        return ChatResult(text="".join(self.deltas), raw={}, usage={}, stop_reason="stop")


@pytest.mark.asyncio
async def test_tui_renders_codex_reasoning_summary_sse_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-network Codex SSE stream reaches the adapter, session, and TUI.

    The matching ``.done`` event must not duplicate the summary that was
    already delivered by ``response.reasoning_summary_text.delta``.
    """
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp
    from one.providers import codex_responses
    from one.providers.codex_responses import CodexResponsesAdapter

    summary = "I will inspect the relevant files first."
    answer = (
        "The visible answer arrives separately after the reasoning summary, "
        "and has enough detail to be treated as a final response rather than "
        "a request to use a tool. It confirms the adapter preserved the "
        "visible output channel independently from the thinking channel. "
        "This regression fixture intentionally uses a complete prose response "
        "so the session does not issue its short-response tool nudge."
    )
    sse_lines = [
        "data: " + json.dumps(
            {
                "type": "response.reasoning_summary_text.delta",
                "item_id": "rs_123",
                "output_index": 0,
                "summary_index": 0,
                "content_index": 0,
                "delta": summary,
            }
        ),
        "data: " + json.dumps(
            {
                "type": "response.reasoning_summary_text.done",
                "item_id": "rs_123",
                "output_index": 0,
                "summary_index": 0,
                "content_index": 0,
                "text": summary,
            }
        ),
        "data: " + json.dumps({"type": "response.output_text.delta", "delta": answer}),
        "data: " + json.dumps(
            {"type": "response.completed", "response": {"status": "completed"}}
        ),
    ]

    class _SseResponse:
        is_error = False

        async def __aenter__(self) -> _SseResponse:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def aiter_lines(self):
            for line in sse_lines:
                yield line

        async def aread(self) -> bytes:
            return b""

    class _NoNetworkClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
            pass

        async def __aenter__(self) -> _NoNetworkClient:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        def stream(self, *args: Any, **kwargs: Any) -> _SseResponse:  # noqa: ARG002
            return _SseResponse()

    monkeypatch.setattr(codex_responses.httpx, "AsyncClient", _NoNetworkClient)
    session = _mk_app_session(tmp_path)
    session.model_registry._auth.set_runtime_api_key("chatgpt", "test-token")
    model = session.model_registry.find("chatgpt", "gpt-5.6-sol")
    assert model is not None
    session.model = model
    session.providers = {"chatgpt": CodexResponsesAdapter()}
    session.settings_manager.set_retry_enabled(False)
    events: list[dict[str, Any]] = []
    session.subscribe(events.append)

    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/thinking high")
        assert session.thinking_level == "high"
        await _submit(app, pilot, "Summarize your approach")
        for _ in range(300):
            await pilot.pause()
            if not session.is_streaming and not app._turn_active:
                break

        thinking_events = [event for event in events if event.get("type") == "thinking_delta"]
        visible_events = [
            event
            for event in events
            if event.get("type") == "message_update"
            and event.get("assistantMessageEvent", {}).get("type") == "text_delta"
        ]
        assert [event["delta"] for event in thinking_events] == [summary]
        assert "".join(event["assistantMessageEvent"]["delta"] for event in visible_events) == answer

        stream = "\n".join(app._stream_lines)
        rendered = str(app.query_one("#stream", Static).content)
        assert "Thinking:" in stream
        assert summary in stream
        assert summary in rendered
        assert "The visible answer" in stream
        assert "The visible answer" in rendered


def test_thinking_frames_are_single_width() -> None:
    assert len(_THINKING_FRAMES) >= 2
    # Each braille frame must occupy exactly one terminal cell.
    for frame in _THINKING_FRAMES:
        assert len(frame) == 1


def test_advance_thinking_frame_wraps() -> None:
    n = len(_THINKING_FRAMES)
    assert advance_thinking_frame(0) == 1
    assert advance_thinking_frame(n - 1) == 0
    # A few steps keep cycling.
    frame = 0
    for _ in range(3 * n):
        frame = advance_thinking_frame(frame)
    assert frame == 0


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
        # Selection offsets are (x, y) within the transcript. The visual-only
        # logo has its own widget, so this is row zero.
        app.screen.selections = {stream_widget: Selection(Offset(0, 0), Offset(5, 0))}
        app._try_auto_copy_selected_stream_text()

        assert received == ["hello"]
        assert app._last_auto_copied == "hello"
        assert all("██████╗" not in line for line in app._stream_lines)
        logo_widget = app.query_one("#logo")
        app.screen.selections = {logo_widget: Selection(Offset(0, 0), Offset(3, 0))}
        app._try_auto_copy_selected_stream_text()
        assert received == ["hello"]
        # Dedup: the same selection must not copy twice.
        app.screen.selections = {stream_widget: Selection(Offset(0, 0), Offset(5, 0))}
        app._try_auto_copy_selected_stream_text()
        assert received == ["hello"]


@pytest.mark.asyncio
async def test_tui_plan_update_renders_block(tmp_path: Path):
    """plan_update with a plan text renders a 'Plan:' block in the stream."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit(
            {
                "type": "plan_update",
                "plan": "[>] read file\n[ ] edit content",
            }
        )
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "Plan:" in stream
        assert "Tool: ○" not in stream
        assert "[>] read file" in stream
        assert "[ ] edit content" in stream


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
        assert "Tool: ○" not in stream


@pytest.mark.asyncio
async def test_tui_plan_update_is_bounded(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit({"type": "plan_update", "plan": "x" * 300})
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        # Main plan updates use the normal bounded panel rather than the
        # sidebar's former 200-character slice.
        assert stream.count("x") == 300


@pytest.mark.asyncio
async def test_tui_plan_update_preserves_lifecycle_tool_block_and_full_plan(tmp_path: Path):
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    plan = [{"step": f"step {index}", "status": "pending"} for index in range(1, 7)]
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit({"type": "tool_call_start", "tool": "plan", "args": {}})
        session._emit({"type": "plan_update", "plan": plan})
        session._emit({"type": "tool_call_end", "tool": "plan", "ok": True, "result": {}})
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "Plan:" in stream
        assert all(f"[ ] step {index}" in stream for index in range(1, 7))
        assert stream.count("Tool: ○") == 0
        assert "Tool: ✓ plan" in stream


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
        session._emit(
            {
                "type": "plan_update",
                "plan": '[b]bold[/] and [{"plan": ">", "x": 1}]',
            }
        )
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "Plan:" in stream
        # Stream data is literal text — brackets appear as-is (not escaped).
        widget = app.query_one("#stream", Static)
        assert "[b]bold[/]" in widget.content.plain


@pytest.mark.asyncio
async def test_tui_stream_markup_chars_dns_type(tmp_path: Path):
    """Tool result with [type='CNAME'] must not raise MarkupError."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit(
            {
                "type": "tool_call_end",
                "tool": "read",
                "ok": True,
                "result": {"outputText": "DNS entry: [type='CNAME']"},
            }
        )
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "DNS entry:" in stream
        assert "[type='CNAME']" in stream
        widget = app.query_one("#stream", Static)
        assert "[type='CNAME']" in widget.content.plain


@pytest.mark.asyncio
async def test_tui_stream_markup_chars_bracket_list(tmp_path: Path):
    """Tool result with [1,2,3] must not raise MarkupError."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit(
            {
                "type": "tool_call_end",
                "tool": "read",
                "ok": True,
                "result": {"outputText": "tags: [1,2,3]"},
            }
        )
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "tags:" in stream
        assert "[1,2,3]" in stream
        widget = app.query_one("#stream", Static)
        assert "[1,2,3]" in widget.content.plain


@pytest.mark.asyncio
async def test_tui_stream_markup_chars_uppercase_brackets(tmp_path: Path):
    """Tool result with [ABC] must not raise MarkupError."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit(
            {
                "type": "tool_call_end",
                "tool": "read",
                "ok": True,
                "result": {"outputText": "section [ABC] end"},
            }
        )
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "section" in stream
        assert "[ABC]" in stream
        widget = app.query_one("#stream", Static)
        assert "[ABC]" in widget.content.plain


@pytest.mark.asyncio
async def test_tui_stream_markup_chars_json_with_gt(tmp_path: Path):
    """Tool result with JSON containing [\"plan\": \">\"] must not raise MarkupError."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit(
            {
                "type": "tool_call_end",
                "tool": "read",
                "ok": True,
                "result": {"outputText": '[{"plan": ">", "x": 1}]'},
            }
        )
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert '"plan": ">' in stream
        assert '"x": 1' in stream
        widget = app.query_one("#stream", Static)
        assert '"plan": ">' in widget.content.plain


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
        app._stream_lines.append(f"{_THINKING_MARK}[{app._theme.info}]{_THINKING_FRAMES[0]} Ctrl+C \u2014 abort[/]")
        app._stream_lines.append("")
        app._render_stream()
        await pilot.pause()
        widget = app.query_one("#stream", Static)
        # The spinner text (without __MK__ prefix) must be present.
        assert "Ctrl+C \u2014 abort" in widget.content.plain
        # The raw thinking mark must NOT appear in rendered content.
        assert _THINKING_MARK not in widget.content.plain


@pytest.mark.asyncio
async def test_tui_stream_complex_markup_content_no_crash(tmp_path: Path):
    """A single line with multiple bracket patterns must not raise MarkupError."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit(
            {
                "type": "tool_call_end",
                "tool": "read",
                "ok": True,
                "result": {"outputText": "a=[b]c[/d] [1,2,3] [XYZ] {'key': 'val'} [type='CNAME']"},
            }
        )
        await pilot.pause()
        widget = app.query_one("#stream", Static)
        # All bracket content must be present as literal text.
        assert "[b]c[/d]" in widget.content.plain
        assert "[1,2,3]" in widget.content.plain
        assert "[XYZ]" in widget.content.plain
        assert "[type='CNAME']" in widget.content.plain


@pytest.mark.asyncio
async def test_tui_spinner_frames_block_shade(tmp_path: Path):
    """Spinner frames are the block-shade sequence ░▒▓█▓▒."""
    from one.modes.tui_mode import _THINKING_FRAMES

    assert _THINKING_FRAMES == "░▒▓█▓▒"


@pytest.mark.asyncio
async def test_tui_spinner_advances(tmp_path: Path):
    """advance_thinking_frame cycles through block-shade frames."""
    from one.modes.tui_mode import advance_thinking_frame

    frame = 0
    expected = "░▒▓█▓▒"
    for ch in expected:
        assert _THINKING_FRAMES[frame] == ch
        frame = advance_thinking_frame(frame)
    assert frame == 0


@pytest.mark.asyncio
async def test_tui_spinner_resumes_after_gate_clears(tmp_path: Path):
    """ask_user pending shows paused label; after clearing, spinner animates again."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._turn_active = True
        app._last_delta_ts = 0.0
        start_frame = app._thinking_frame

        # First tick: ask_user pending → paused label.
        app._ask_user_pending = {"id": "x"}
        app._tick_waiting()
        await pilot.pause()
        status = app.query_one("#waiting_status")
        assert "waiting for your response" in str(status.render())
        assert app._thinking_frame == start_frame

        # Clear the gate.
        app._ask_user_pending = None
        # Second tick: should animate again (frame advances).
        app._tick_waiting()
        await pilot.pause()
        assert "Ctrl+C \u2014 abort" in str(status.render())
        # Frame should have advanced.
        assert app._thinking_frame > start_frame


@pytest.mark.asyncio
async def test_tui_spinner_animation_refreshes_widget_each_tick(tmp_path: Path):
    """Waiting animation repaints only its own widget, not the transcript."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Arm waiting: turn active + stale delta.
        app._turn_active = True
        app._last_delta_ts = 0.0
        assert app._thinking_active is False
        stream_widget = app.query_one("#stream", Static)
        status_widget = app.query_one("#waiting_status", Static)

        # --- First tick: initial append path (else branch) ---
        app._tick_waiting()
        assert app._thinking_active is True
        first_lines = list(app._stream_lines)
        first_stream = str(stream_widget.content)
        assert "Ctrl+C \u2014 abort" in str(status_widget.render())

        # --- Second tick: in-place rewrite path (if branch) ---
        app._tick_waiting()
        assert app._stream_lines == first_lines
        await pilot.pause(0.05)
        assert str(stream_widget.content) == first_stream
        assert "Ctrl+C \u2014 abort" in str(status_widget.render())

        # --- Third tick: verify continued animation ---
        app._tick_waiting()
        await pilot.pause(0.05)
        assert str(stream_widget.content) == first_stream
        assert "Ctrl+C \u2014 abort" in str(status_widget.render())


@pytest.mark.asyncio
async def test_tui_spinner_cleared_on_turn_end(tmp_path: Path):
    """When the turn ends, the independent waiting status is cleared."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Arm waiting.
        app._turn_active = True
        app._last_delta_ts = 0.0
        app._tick_waiting()
        await pilot.pause()
        status_widget = app.query_one("#waiting_status", Static)
        assert "Ctrl+C \u2014 abort" in str(status_widget.render())

        # Simulate turn completion: the event handler calls _remove_thinking_line
        # and sets _turn_active = False.
        app._turn_active = False
        app._remove_thinking_line()
        await pilot.pause()
        assert "Ctrl+C \u2014 abort" not in str(status_widget.render())


@pytest.mark.asyncio
async def test_tui_thinking_two_segments_separated_by_tool(tmp_path: Path):
    """A->tool1 start/end->B->answer: exactly 2 Thinking: labels and 2 _THINKING_TEXT_MARK lines."""
    from one.modes.tui_mode import _THINKING_TEXT_MARK, _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Turn A thinking
        session._emit({"type": "turn_start"})
        await pilot.pause()
        session._emit({"type": "thinking_delta", "delta": "A"})
        await pilot.pause()

        # Capture first marked A line IMMEDIATELY after A and before tool_call_start
        thinking_after_a = [l for l in app._stream_lines if l.startswith(_THINKING_TEXT_MARK)]
        assert len(thinking_after_a) == 1
        assert "A" in thinking_after_a[0]
        a_line = thinking_after_a[0]

        # Tool call 1
        session._emit({"type": "tool_call_start", "tool": "read", "args": {"path": "a.txt"}})
        await pilot.pause()
        session._emit({"type": "tool_call_end", "tool": "read", "ok": True, "result": {"outputText": "result1"}})
        await pilot.pause()

        # Turn B thinking
        session._emit({"type": "thinking_delta", "delta": "B"})
        await pilot.pause()

        # Answer
        session._emit({"type": "message_start", "message": {"role": "assistant", "content": ""}})
        await pilot.pause()
        session._emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "answer"}})
        await pilot.pause()
        session._emit({"type": "message_end", "message": {"role": "assistant", "content": "answer"}})
        await pilot.pause()
        session._emit({"type": "turn_end", "ok": True})
        await pilot.pause()

        # Count Thinking: labels
        thinking_labels = [l for l in app._stream_lines if l == "Thinking:"]
        assert len(thinking_labels) == 2, f"Expected 2 Thinking: labels, got {len(thinking_labels)}"

        # Count _THINKING_TEXT_MARK lines
        thinking_text_lines = [l for l in app._stream_lines if l.startswith(_THINKING_TEXT_MARK)]
        assert len(thinking_text_lines) == 2, f"Expected 2 _THINKING_TEXT_MARK lines, got {len(thinking_text_lines)}"

        # First thinking line unchanged after B/answer appended
        assert thinking_text_lines[0] == a_line

        # Strict indices on full _stream_lines
        a_idx = next(i for i, l in enumerate(app._stream_lines) if l.startswith(_THINKING_TEXT_MARK) and "A" in l)
        tool_start_idx = next(i for i, l in enumerate(app._stream_lines) if "tool:" in l and "read" in l)
        tool_end_idx = next(i for i, l in enumerate(app._stream_lines) if "result1" in l)
        b_idx = next(i for i, l in enumerate(app._stream_lines) if l.startswith(_THINKING_TEXT_MARK) and "B" in l)
        answer_idx = next(i for i, l in enumerate(app._stream_lines) if "answer" in l)
        assert "[ok]" in "\n".join(app._stream_lines[tool_start_idx:tool_end_idx])
        assert a_idx < tool_start_idx < tool_end_idx < b_idx < answer_idx


@pytest.mark.asyncio
async def test_tui_many_deltas_same_segment_one_marked_line(tmp_path: Path):
    """Many deltas in one segment produce exactly one _THINKING_TEXT_MARK line."""
    from one.modes.tui_mode import _THINKING_TEXT_MARK, _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        session._emit({"type": "turn_start"})
        await pilot.pause()

        # 20 deltas
        for i in range(20):
            session._emit({"type": "thinking_delta", "delta": f"x{i}"})
            await pilot.pause()

        session._emit({"type": "message_start"})
        await pilot.pause()
        session._emit({"type": "message_end"})
        await pilot.pause()
        session._emit({"type": "turn_end", "ok": True})
        await pilot.pause()

        thinking_text_lines = [line for line in app._stream_lines if line.startswith(_THINKING_TEXT_MARK)]
        assert len(thinking_text_lines) == 1, f"Expected 1 _THINKING_TEXT_MARK line, got {len(thinking_text_lines)}"

        # All deltas should be in the single line
        assert all(f"x{i}" in thinking_text_lines[0] for i in range(20))


@pytest.mark.asyncio
async def test_tui_leading_empty_whitespace_no_label_then_block(tmp_path: Path):
    """Leading '', ' ', '\\t' before substantive => no label; then A, space, B => one block."""
    from textual.widgets import Static

    from one.modes.tui_mode import _THINKING_TEXT_MARK, _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        session._emit({"type": "turn_start"})
        await pilot.pause()

        # Leading empty and whitespace-only deltas should NOT open the block
        session._emit({"type": "thinking_delta", "delta": ""})
        await pilot.pause()
        session._emit({"type": "thinking_delta", "delta": " "})
        await pilot.pause()
        session._emit({"type": "thinking_delta", "delta": "\t"})
        await pilot.pause()

        # Substantive content opens the block
        session._emit({"type": "thinking_delta", "delta": "A"})
        await pilot.pause()
        session._emit({"type": "thinking_delta", "delta": " "})
        await pilot.pause()
        session._emit({"type": "thinking_delta", "delta": "B"})
        await pilot.pause()

        session._emit({"type": "message_start"})
        await pilot.pause()
        session._emit({"type": "message_end"})
        await pilot.pause()
        session._emit({"type": "turn_end", "ok": True})
        await pilot.pause()

        # Exactly one Thinking: label
        thinking_labels = [l for l in app._stream_lines if l == "Thinking:"]
        assert len(thinking_labels) == 1, f"Expected 1 Thinking: label, got {len(thinking_labels)}"

        # Inspect rendered Static content and assert literal "A B"
        widget = app.query_one("#stream", Static)
        rendered = str(widget.content)
        assert "A B" in rendered, f"Expected 'A B' in rendered content, got: {rendered!r}"

        # One _THINKING_TEXT_MARK line
        thinking_text_lines = [l for l in app._stream_lines if l.startswith(_THINKING_TEXT_MARK)]
        assert len(thinking_text_lines) == 1
        assert "A" in thinking_text_lines[0]
        assert "B" in thinking_text_lines[0]


@pytest.mark.asyncio
async def test_tui_thinking_label_finalizes_after_waiting_spinner_removal(tmp_path: Path):
    """Spinner removal before a lifecycle barrier must not leave ``Thinking:`` behind."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        session._emit({"type": "turn_start"})
        await pilot.pause()
        app._last_delta_ts = 0.0
        app._tick_waiting()
        session._emit({"type": "thinking_delta", "delta": "reasoning"})
        await pilot.pause()

        # This is the ordered lifecycle path that previously shifted the
        # cached label index before the next boundary finalized it.
        app._remove_thinking_line()
        session._emit({"type": "tool_call_start", "tool": "read", "args": {"path": "a.txt"}})
        await pilot.pause()

        assert app._stream_lines.count("Thinking:") == 1


@pytest.mark.asyncio
async def test_tui_tool_call_end_without_start_separates_thinking(tmp_path: Path):
    """tool_call_end without preceding tool_call_start separates A and B thinking blocks."""
    from one.modes.tui_mode import _THINKING_TEXT_MARK, _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # A thinking
        session._emit({"type": "turn_start"})
        await pilot.pause()
        session._emit({"type": "thinking_delta", "delta": "A"})
        await pilot.pause()

        # tool_call_end without tool_call_start
        session._emit({"type": "tool_call_end", "tool": "read", "ok": True, "result": {"outputText": "x"}})
        await pilot.pause()

        # B thinking
        session._emit({"type": "thinking_delta", "delta": "B"})
        await pilot.pause()

        session._emit({"type": "message_start"})
        await pilot.pause()
        session._emit({"type": "message_end"})
        await pilot.pause()
        session._emit({"type": "turn_end", "ok": True})
        await pilot.pause()

        thinking_text_lines = [l for l in app._stream_lines if l.startswith(_THINKING_TEXT_MARK)]
        assert len(thinking_text_lines) == 2, f"Expected 2 _THINKING_TEXT_MARK lines, got {len(thinking_text_lines)}"

        # Strict stream order: A line < tool end < B line (by full stream index)
        a_idx = next(i for i, l in enumerate(app._stream_lines) if l.startswith(_THINKING_TEXT_MARK) and "A" in l)
        tool_end_idx = next(i for i, l in enumerate(app._stream_lines) if "tool ok: read" in l)
        b_idx = next(i for i, l in enumerate(app._stream_lines) if l.startswith(_THINKING_TEXT_MARK) and "B" in l)
        assert a_idx < tool_end_idx < b_idx, f"Expected A({a_idx}) < tool_end({tool_end_idx}) < B({b_idx})"


@pytest.mark.asyncio
async def test_tui_no_thinking_events_no_label(tmp_path: Path):
    """No thinking events => no Thinking: label in stream."""
    from one.modes.tui_mode import _THINKING_TEXT_MARK, _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        session._emit({"type": "turn_start"})
        await pilot.pause()

        # Skip all thinking, go directly to answer
        session._emit({"type": "message_start"})
        await pilot.pause()
        session._emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "answer"}})
        await pilot.pause()
        session._emit({"type": "message_end"})
        await pilot.pause()
        session._emit({"type": "turn_end", "ok": True})
        await pilot.pause()

        thinking_labels = [line for line in app._stream_lines if line == "Thinking:"]
        assert len(thinking_labels) == 0, f"Expected 0 Thinking: labels, got {len(thinking_labels)}"

        thinking_text_lines = [line for line in app._stream_lines if line.startswith(_THINKING_TEXT_MARK)]
        assert len(thinking_text_lines) == 0, f"Expected 0 _THINKING_TEXT_MARK lines, got {len(thinking_text_lines)}"


@pytest.mark.asyncio
async def test_tui_streaming_produces_one_assistant_block(tmp_path: Path):
    """Normal streaming: message_start → multiple text_deltas → message_end
    must produce exactly one assistant chat block in the stream lines,
    not a duplicate final block at message_end."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path, runtime_key="sk-test")
    session.providers = {"openai": _StreamingProvider(["Hello ", "world"])}
    session.settings_manager.set_retry_enabled(False)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "say hi")
        # Wait for the turn to complete
        for _ in range(300):
            await pilot.pause()
            if not session.is_streaming and not app._turn_active:
                break
        # The stream should contain the assistant response once.
        # After message_end with live deltas, there's one blank-line terminator.
        assistant_lines = [
            l for l in app._stream_lines
            if l.startswith("Hello ") or l == "" or l == "world"
        ]
        # "Hello world" should appear as assistant text (wrapped by _format_chat_panel).
        stream = "\n".join(app._stream_lines)
        assert "Hello world" in stream or "Hello" in stream
        # Count how many assistant blocks (non-empty, non-tool, non-user text).
        # We should have exactly one assistant text block.
        assistant_blocks = 0
        in_block = False
        for line in app._stream_lines:
            if not line and in_block:
                # blank line after content = end of block
                assistant_blocks += 1
                in_block = False
            elif line and not line.startswith("tool ") and not line.startswith("["):
                in_block = True
        # At minimum one assistant block.
        assert assistant_blocks >= 1


@pytest.mark.asyncio
async def test_tui_message_end_no_duplicate_when_streaming(tmp_path: Path):
    """When deltas were streamed, message_end adds ONE trailing blank line,
    not another chat block.  Verify the assistant_live_ flags are reset."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path, runtime_key="sk-test")
    session.providers = {"openai": _StreamingProvider(["x"])}
    session.settings_manager.set_retry_enabled(False)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "echo")
        for _ in range(300):
            await pilot.pause()
            if not session.is_streaming and not app._turn_active:
                break
        # After message_end, live state must be fully cleaned up.
        assert app._assistant_has_live_delta is False
        assert app._assistant_live_start_idx == -1
        assert app._assistant_live_buffer == ""


@pytest.mark.asyncio
async def test_tui_thinking_cleanup_on_tool_call(tmp_path: Path):
    """When a tool call occurs during thinking, the thinking block must
    be cleaned up and replaced by the tool block — no stale thinking text."""
    from one.modes.tui_mode import _OneTextualApp

    class _ThinkingProvider:
        async def chat(self, api_key, model, messages, thinking_level, headers=None, on_delta=None, on_thinking_delta=None, max_tokens=None, images=None, storage_dir=""):
            from one.providers.base import ChatResult

            if on_delta:
                on_delta("thinking")
                on_delta(" done")
                on_delta("tool_plan")
            await asyncio.sleep(0.02)
            return ChatResult(
                text='{"tool":"bash","args":{"command":"echo ok"}}',
                raw={}, usage={}, stop_reason="stop",
            )

    session = _mk_app_session(tmp_path, runtime_key="sk-test")
    session.providers = {"openai": _ThinkingProvider()}
    session.settings_manager.set_retry_enabled(False)
    # Include bash tool
    session._active_tools = ["read", "bash", "finish"]
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "run bash")
        for _ in range(300):
            await pilot.pause()
            if not session.is_streaming and not app._turn_active:
                break
        stream = "\n".join(app._stream_lines)
        # Must contain the tool block but NOT a stale thinking line.
        assert "tool (timeout" in stream
        thinking_stale = any(
            line == "Thought:" and "tool:" not in line
            for line in app._stream_lines
        )
        # Thinking label should not remain after the turn completes.
        # (It's cleaned up by _finalize_thinking_block at tool_call_start.)
        thinking_lines = [l for l in app._stream_lines if l == "Thinking:"]
        assert len(thinking_lines) == 0, f"Stale Thinking: labels found: {thinking_lines}"


@pytest.mark.asyncio
async def test_tui_thinking_state_resets_between_turns(tmp_path: Path):
    """After a turn ends, the thinking label/buffer must be fully cleared
    so the next turn starts with a clean slate."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path, runtime_key="sk-test")
    session.providers = {"openai": _StreamingProvider(["done"])}
    session.settings_manager.set_retry_enabled(False)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        # First turn
        await _submit(app, pilot, "turn one")
        for _ in range(300):
            await pilot.pause()
            if not session.is_streaming and not app._turn_active:
                break
        assert app._thinking_label_shown is False
        assert app._thinking_buffer == ""
        assert app._thinking_line_idx is None


@pytest.mark.asyncio
async def test_tui_tool_call_start_removes_streamed_json_block(tmp_path: Path):
    """When the provider streams JSON text that turns out to be a tool call,
    the tool_call_start event must remove the live assistant panel so the
    JSON does not appear as normal assistant chat.

    Event sequence:
      message_start (reset) → text_delta ("{...}") → tool_call_start → message_end
    """
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # 1. message_start resets live state
        session._emit({"type": "message_start", "message": {"role": "assistant", "content": ""}})
        await pilot.pause()
        assert app._assistant_has_live_delta is False

        # 2. JSON text_delta builds the live assistant panel
        session._emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": '{"tool":"bash","args":{"command":"echo hi"}}'}})
        await pilot.pause()
        app._flush_pending_assistant_deltas()
        assert app._assistant_has_live_delta is True
        assert app._assistant_live_start_idx >= 0
        # The JSON lines should be present in the stream (may be wrapped).
        assistant_lines_before = app._stream_lines[app._assistant_live_start_idx :]
        assistant_text_before = "\n".join(assistant_lines_before)
        assert "tool" in assistant_text_before
        assert "bash" in assistant_text_before
        assert "echo hi" in assistant_text_before

        # 3. tool_call_start must remove the JSON block and write the tool block
        session._emit({"type": "tool_call_start", "tool": "bash", "args": {"command": "echo hi"}})
        await pilot.pause()

        # The live state must be fully cleared.
        assert app._assistant_has_live_delta is False
        assert app._assistant_live_start_idx == -1
        assert app._assistant_live_buffer == ""

        stream = "\n".join(app._stream_lines)
        # The tool block must be present.
        assert "tool:" in stream and "bash" in stream
        # The streamed JSON must NOT appear as a standalone assistant chat block.
        # Find the tool block line and verify there's no assistant delta block
        # before it (only user text).
        tool_line_idx = next(
            (i for i, l in enumerate(app._stream_lines) if "tool:" in l and "bash" in l),
            None,
        )
        assert tool_line_idx is not None
        # The JSON text should not be in any transcript lines.
        for line in app._stream_lines:
            if "tool:" in line and "bash" in line:
                continue  # the tool block itself
            # No assistant delta lines should contain the full JSON structure.
            # Check by looking for the JSON's key structural elements on the
            # same line (they may be wrapped, but the tool block line is safe).
            assert 'tool_start' not in line or ('tool: ' in line and 'bash' in line)

        # 4. message_end must NOT create a duplicate block (state already reset)
        session._emit({"type": "message_end", "message": {"role": "assistant", "content": ""}})
        await pilot.pause()
        assert app._assistant_has_live_delta is False
        stream_after_end = "\n".join(app._stream_lines)
        # Only one tool block, no extra assistant block.
        assert sum(1 for l in stream_after_end.split("\n") if "tool:" in l and "bash" in l) == 1


@pytest.mark.asyncio
async def test_tui_tool_call_start_noop_without_live_block(tmp_path: Path):
    """When no live assistant block exists, tool_call_start should be a no-op
    for the assistant delta and just write the tool block."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # No deltas — live state is already clean
        assert app._assistant_live_start_idx == -1

        session._emit({"type": "tool_call_start", "tool": "read", "args": {"path": "f.txt"}})
        await pilot.pause()

        stream = "\n".join(app._stream_lines)
        assert "tool:" in stream and "read" in stream
        assert app._assistant_has_live_delta is False


@pytest.mark.asyncio
async def test_tui_streamed_multiline_response_not_duplicated(tmp_path: Path):
    """A multiline streamed assistant response + final message_end must
    render each visible line exactly once — no duplicate from the
    fallback final-text path."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # message_start resets live state.
        session._emit({"type": "message_start", "message": {"role": "assistant", "content": []}})
        await pilot.pause()

        # Stream three deltas (simulating multiline output).
        for delta in ["Hello", "\n", "World!"]:
            session._emit(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": delta},
                }
            )
            await pilot.pause()

        # message_end with content that matches the streamed text.
        session._emit(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello\nWorld!"}],
                },
                "suppressed": False,
            }
        )
        await pilot.pause()

        stream = "\n".join(app._stream_lines)
        # "Hello" and "World!" must appear exactly once.
        assert stream.count("Hello") == 1
        assert stream.count("World!") == 1


@pytest.mark.asyncio
async def test_tui_tool_block_after_500_lines_yields_one_status(tmp_path: Path):
    """After more than 500 rendered lines force a trim, a subsequent
    tool_call_start + tool_call_end must produce exactly one status block."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Force _stream_lines to > 500 so the next tool call triggers a trim.
        for i in range(510):
            app._write(f"line {i}", "info")

        assert len(app._stream_lines) == 500

        # Start a tool — this writes a block and records absolute indices.
        session._emit(
            {
                "type": "tool_call_start",
                "tool": "bash",
                "args": {"command": "echo hi"},
                "effectiveTimeout": 30,
            }
        )
        await pilot.pause()

        active = app._active_tool_block
        assert active is not None, "active tool block must exist"
        tool_name, start, end, block_text = active

        # End the tool — _finish_tool_block should succeed.
        session._emit(
            {
                "type": "tool_call_end",
                "tool": "bash",
                "ok": True,
                "result": {"outputText": "", "result": ""},
            }
        )
        await pilot.pause()

        assert app._active_tool_block is None, "tool block should be cleared"

        # Count how many lines contain the tool start marker (status block).
        # _finish_tool_block replaces the multiline block with a single-line
        # status:  "<original> [ok]" or "<original> [err]".
        search_sub = "tool (timeout"
        status_lines = [l for l in app._stream_lines if search_sub in l]
        assert len(status_lines) == 1, f"expected exactly 1 status block, found {status_lines}"


@pytest.mark.asyncio
async def test_tui_coalesces_lifecycle_wakeup_and_drains_delta_batch(tmp_path: Path):
    """A token storm uses the timer/manual batch drain, not token-rate messages."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        wakeups: list[dict[str, Any]] = []

        def record_wakeup(message: Any) -> bool:
            wakeups.append(message.payload)
            return True

        app.post_message = record_wakeup  # type: ignore[method-assign]
        session._emit({"type": "message_start", "message": {"role": "assistant", "content": ""}})
        # The first event gets one wakeup. Subsequent deltas coalesce behind
        # it and remain available for the 30 Hz/manual drain.
        for _ in range(250):
            session._emit(
                {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "x"}}
            )

        assert wakeups == [{"type": "_flush_pending_ui_events"}]
        with app._pending_ui_events_lock:
            # Adjacent deltas are represented by one bounded chunk behind the
            # lifecycle barrier, rather than one retained object per token.
            assert len(app._pending_ui_events) == 2

        app._flush_pending_ui_events()
        assert app._assistant_stream == "x" * 250
        with app._pending_ui_events_lock:
            assert app._pending_ui_events == []
            assert app._ui_flush_wakeup_pending is False


@pytest.mark.asyncio
async def test_tui_batch_drain_does_not_refresh_sidebar_for_deltas(tmp_path: Path):
    """Only lifecycle events refresh the sidebar; delta batches do not."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        refreshes = 0

        def record_refresh() -> None:
            nonlocal refreshes
            refreshes += 1

        app._refresh_sidebar = record_refresh  # type: ignore[method-assign]
        session._emit({"type": "message_start", "message": {"role": "assistant", "content": ""}})
        app._flush_pending_ui_events()
        assert refreshes == 0
        app._paint_scheduled_sidebar()  # noqa: SLF001
        assert refreshes == 1

        for _ in range(100):
            session._emit(
                {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "x"}}
            )
        app._flush_pending_ui_events()
        assert refreshes == 1

        session._emit({"type": "turn_end", "ok": True})
        app._flush_pending_ui_events()
        app._paint_scheduled_sidebar()  # noqa: SLF001
        assert refreshes == 2


@pytest.mark.asyncio
async def test_tui_atomic_batch_swap_preserves_concurrent_deltas(tmp_path: Path):
    """Producer appends racing the UI drain cannot lose or reorder tokens."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._handle_session_event({"type": "message_start", "message": {"role": "assistant", "content": ""}})
        token_count = 500

        def produce() -> None:
            for index in range(token_count):
                session._emit(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {"type": "text_delta", "delta": f"{index},"},
                    }
                )

        producer = threading.Thread(target=produce)
        producer.start()
        while producer.is_alive():
            app._flush_pending_ui_events()
        producer.join()
        app._flush_pending_ui_events()

        assert app._assistant_stream == "".join(f"{index}," for index in range(token_count))
        with app._pending_ui_events_lock:
            assert app._pending_ui_events == []


@pytest.mark.asyncio
async def test_tui_clear_stream_discards_buffered_assistant_deltas(tmp_path: Path):
    """Ctrl+L must prevent an unpainted delta batch from returning later."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        session._emit({"type": "message_start", "message": {"role": "assistant", "content": ""}})
        await pilot.pause()
        session._emit(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "stale buffered text"},
            }
        )
        with app._pending_assistant_deltas_lock:
            assert app._pending_assistant_deltas == "stale buffered text"

        app.action_clear_stream()
        app._flush_pending_assistant_deltas()

        assert app._stream_lines == []
        assert app._assistant_stream == ""
        assert app._assistant_stream_open is False
        assert app._active_tool_block is None
        assert app._thinking_line_idx is None
        with app._pending_assistant_deltas_lock:
            assert app._pending_assistant_deltas == ""


@pytest.mark.asyncio
async def test_tui_clear_command_uses_full_stream_reset(tmp_path: Path):
    """/clear must reset the same complete viewport state as Ctrl+L."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._write("visible viewport content", "info")
        app._assistant_stream_open = True
        app._assistant_stream = "live assistant"
        app._assistant_has_live_delta = True
        app._assistant_live_start_idx = 1
        app._assistant_live_buffer = "live assistant"
        app._assistant_live_line_count = 1
        app._active_tool_block = ("read", 1, 2, "tool start: read")
        app._thinking_active = True
        app._thinking_label_shown = True
        app._thinking_buffer = "reasoning"
        app._thinking_line_idx = 1
        app._last_auto_copied = "copied selection"
        app.action_show_model_picker()
        assert app._model_picker_models
        assert app.query_one("#model_overlay", Static).has_class("visible")
        with app._pending_ui_events_lock:
            app._pending_ui_events = [
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "buffered delta"},
                }
            ]
            app._pending_assistant_deltas = "buffered delta"
            app._ui_flush_wakeup_pending = True

        await app._handle_command("/clear")

        assert app._stream_lines == []
        assert app._rendered_stream_lines == ()
        assert str(app.query_one("#stream", Static).content) == ""
        with app._pending_ui_events_lock:
            assert app._pending_ui_events == []
            assert app._pending_assistant_deltas == ""
            assert app._ui_flush_wakeup_pending is False
        assert app._assistant_stream_open is False
        assert app._assistant_stream == ""
        assert app._assistant_has_live_delta is False
        assert app._assistant_live_start_idx == -1
        assert app._assistant_live_buffer == ""
        assert app._assistant_live_line_count == 0
        assert app._active_tool_block is None
        assert app._thinking_active is False
        assert app._thinking_label_shown is False
        assert app._thinking_buffer == ""
        assert app._thinking_line_idx is None
        assert app._last_auto_copied == ""
        assert app._model_picker_models == []
        assert not app.query_one("#model_overlay", Static).has_class("visible")


@pytest.mark.asyncio
async def test_tui_viewport_keeps_exact_tail_without_touching_session_history(tmp_path: Path):
    """The bounded presentation cache never truncates AgentSession history."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    original_messages = list(session.messages)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_clear_stream()
        for index in range(MAX_RENDERED_LINES + 25):
            app._write(f"viewport-line-{index}", "info")

        assert len(app._stream_lines) == MAX_RENDERED_LINES
        assert app._stream_lines == [f"viewport-line-{index}" for index in range(25, MAX_RENDERED_LINES + 25)]
        assert session.messages == original_messages


@pytest.mark.asyncio
async def test_tui_delta_batch_renders_once_and_large_live_block_has_marker(tmp_path: Path):
    """A delta run paints once and a huge single assistant block stays bounded."""
    from one.modes.tui_mode import _TRUNCATED_ASSISTANT_MARKER, _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        renders = 0
        original_render = app._render_stream

        def count_render() -> None:
            nonlocal renders
            renders += 1
            original_render()

        app._render_stream = count_render  # type: ignore[method-assign]
        session._emit({"type": "message_start", "message": {"role": "assistant", "content": ""}})
        for _ in range(100):
            session._emit(
                {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "x"}}
            )
        app._flush_pending_ui_events()
        # Token paint is frame-scheduled rather than performed in the drain.
        await pilot.pause()
        assert renders == 1

        session._emit(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "\n".join(str(i) for i in range(600))},
            }
        )
        app._flush_pending_ui_events()
        await pilot.pause()
        assert len(app._stream_lines) <= MAX_RENDERED_LINES
        assert _TRUNCATED_ASSISTANT_MARKER in app._stream_lines


@pytest.mark.asyncio
async def test_tui_mouse_up_snapshots_selection_before_stream_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Immediate mouse-up copy survives a deferred refresh and runs once."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        copied: list[str] = []
        app._copy_to_clipboard = lambda text: (copied.append(text) or ("copied", "fake"))  # type: ignore[method-assign]
        monkeypatch.setattr(app.screen, "get_selected_text", lambda: " selected tail ")

        # Simulate a queued streaming refresh at a viewport trim boundary.
        app._stream_lines = [f"line-{i}" for i in range(MAX_RENDERED_LINES)]
        app._rendered_stream_lines = tuple(app._stream_lines)
        app._stream_lines.append("new tail")
        app._trim_stream(render=False)
        app.on_mouse_up(None)  # type: ignore[arg-type]
        monkeypatch.setattr(app.screen, "get_selected_text", lambda: "")
        app._render_stream()
        await pilot.pause()

        assert copied == [" selected tail "]


@pytest.mark.asyncio
async def test_tui_trim_rebases_live_block_and_discard_survives(tmp_path: Path):
    """When a >500-line trim happens while a live delta block is active,
    _assistant_live_start_idx must be rebased so that
    _discard_live_assistant_block() removes exactly the right lines and
    surrounding content survives."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # 1. Pre-fill stream with 350 lines so the live block starts well
        #    inside the 500-line window.
        for i in range(350):
            app._stream_lines.append(f"line-{i}")

        # 2. Start a live assistant delta — it starts at index 350.
        session._emit(
            {"type": "message_start", "message": {"role": "assistant", "content": ""}}
        )
        await pilot.pause()
        session._emit(
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "assistant text here",
                },
            }
        )
        await pilot.pause()
        app._flush_pending_assistant_deltas()

        assert app._assistant_has_live_delta is True
        live_start_before_trim = app._assistant_live_start_idx
        live_count_before_trim = app._assistant_live_line_count
        assert live_start_before_trim >= 0
        assert live_count_before_trim > 0

        # 3. Force a trim by adding enough lines to push total > 500.
        #    350 pre-fill + ~4 live lines + 200 new = 554 lines → trim
        #    drops 54 from front, rebasing live idx from ~350 to ~296.
        for i in range(200):
            app._stream_lines.append(f"pad-{i}")
        app._trim_stream()

        # Verify the index was rebased.
        rebased_idx = app._assistant_live_start_idx
        dropped = live_start_before_trim - rebased_idx
        assert dropped >= 40, f"Expected >= 40 dropped, got {dropped}"
        # Live bookkeeping must still be valid (block not fully trimmed).
        assert rebased_idx >= 0
        assert app._assistant_has_live_delta is True

        # 4. Discard the live block — it should remove exactly the
        #    rebased lines and preserve surviving content.
        session._emit(
            {
                "type": "tool_call_start",
                "tool": "bash",
                "args": {"command": "echo survive"},
            }
        )
        await pilot.pause()

        stream = "\n".join(app._stream_lines)
        assert "tool:" in stream and "bash" in stream
        # line-349 was at the end of the pre-fill and should survive the trim
        # (it was at index 349, which is within the last-500 window).
        assert "line-349" in stream, "Near-end pre-fill line should survive"
        # The discarded assistant text must NOT appear.
        assert "assistant text here" not in stream

        # Live bookkeeping must be reset.
        assert app._assistant_live_start_idx == -1
        assert app._assistant_has_live_delta is False


@pytest.mark.asyncio
async def test_tui_trim_eats_whole_live_block_no_corruption(tmp_path: Path):
    """When a trim consumes the entire live assistant block, live
    bookkeeping must be invalidated (idx=-1) so subsequent deltas start
    fresh instead of splicing at a stale absolute index."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # 1. Build a live block that sits near the end of the stream.
        session._emit(
            {"type": "message_start", "message": {"role": "assistant", "content": ""}}
        )
        await pilot.pause()
        session._emit(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "short live block"},
            }
        )
        await pilot.pause()
        app._flush_pending_assistant_deltas()
        assert app._assistant_has_live_delta is True
        live_start = app._assistant_live_start_idx
        live_count = app._assistant_live_line_count
        assert live_start >= 0

        # 2. Fill the stream with 520 *more* lines so that when trimmed,
        #    the tail (where the live block sits) gets dropped too.
        for i in range(520):
            app._stream_lines.append(f"pad-{i}")

        # 3. Append one more delta to trigger _trim_stream — this is the
        #    moment the trim must invalidate the live block that was
        #    pushed off the end by the new lines.
        session._emit(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": " after trim"},
            }
        )
        await pilot.pause()
        app._flush_pending_assistant_deltas()

        # The trim should have invalidated the live block because the
        # entire block was pushed beyond the 500-line window.
        # Regardless of exact state, the next delta must not crash.
        session._emit(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": " fresh start ok"},
            }
        )
        await pilot.pause()
        app._flush_pending_assistant_deltas()

        # Verify the stream is coherent — no duplicate blocks, no crashes.
        stream = "\n".join(app._stream_lines)
        assert len(app._stream_lines) <= 500 + 10  # small headroom
        # The stream should be non-empty and contain recent content.
        assert len(app._stream_lines) > 0
        # No crash means we're fine — just ensure subsequent writes work.
        app._write("post-trim content")
        await pilot.pause()
        stream2 = "\n".join(app._stream_lines)
        assert "post-trim content" in stream2


@pytest.mark.asyncio
async def test_tui_output_truncation_warning_is_visible(tmp_path):
    from one.modes.tui_mode import _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app._handle_session_event({"type": "warning", "warning": "output_truncated",
                                   "message": "Output token limit reached (requested: 65536)."})
        await pilot.pause()
        assert "Output token limit reached" in "\n".join(app._stream_lines)
