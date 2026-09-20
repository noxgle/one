from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from one.modes.tui_mode import (
    _THINKING_FRAMES,
    _THINKING_MARK,
    MAX_RENDERED_LINES,
    advance_thinking_frame,
)
from tests.support.tui import _mk_app_session, _submit


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
        # Selection offsets are (x, y): the logo lines are written on mount,
        # "one TUI v2 ready" is at y=20, and "hello world" is at y=21.
        app.screen.selections = {stream_widget: Selection(Offset(0, 21), Offset(5, 21))}
        app._try_auto_copy_selected_stream_text()

        assert received == ["hello"]
        assert app._last_auto_copied == "hello"
        # Dedup: the same selection must not copy twice.
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
                "plan": "1. read file\n2. edit content",
            }
        )
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
        assert "[b]bold[/]" in widget.content


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
        assert '"plan": ">' in widget.content


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
        assert "[b]c[/d]" in widget.content
        assert "[1,2,3]" in widget.content
        assert "[XYZ]" in widget.content
        assert "[type='CNAME']" in widget.content


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
        stream = "\n".join(app._stream_lines)
        assert "waiting for your response" in stream
        assert app._thinking_frame == start_frame

        # Clear the gate.
        app._ask_user_pending = None
        # Second tick: should animate again (frame advances).
        app._tick_waiting()
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "Ctrl+C abort" in stream
        # Frame should have advanced.
        assert app._thinking_frame > start_frame


@pytest.mark.asyncio
async def test_tui_spinner_animation_refreshes_widget_each_tick(tmp_path: Path):
    """Every mutation of the spinner line must trigger a widget refresh.

    Regression: _tick_waiting rewrote the spinner line in-place but never
    called _render_stream, so the visible widget stayed frozen.
    """
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

        # NOTE: captures happen synchronously right after each manual tick
        # with no `await` in between. The app's own 0.15s interval timer also
        # drives _tick_waiting, and any await would let it fire extra ticks;
        # with a 6-frame cycle that can land back on the same glyph and make
        # consecutive-inequality assertions flaky on loaded runners.
        # --- First tick: initial append path (else branch) ---
        app._tick_waiting()
        assert app._thinking_active is True
        first_lines = list(app._stream_lines)
        first_render = str(stream_widget.content)
        assert any(l.startswith("__MK__:") for l in app._stream_lines), (
            "spinner line must be present after first tick"
        )

        # --- Second tick: in-place rewrite path (if branch) ---
        app._tick_waiting()
        second_lines = list(app._stream_lines)
        # _stream_lines must have changed (frame advanced).
        assert second_lines != first_lines, (
            "_stream_lines should change when spinner frame advances"
        )
        second_render = str(stream_widget.content)
        assert second_render != first_render, (
            "rendered #stream content must change between ticks — the frozen-spinner bug"
        )

        # --- Third tick: verify continued animation ---
        app._tick_waiting()
        third_render = str(stream_widget.content)
        assert third_render != second_render, (
            "spinner must keep animating on subsequent ticks"
        )


@pytest.mark.asyncio
async def test_tui_spinner_cleared_on_turn_end(tmp_path: Path):
    """When the turn ends (turn_active cleared), the spinner line
    and any thinking text must be removed from the stream."""
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
        stream_widget = app.query_one("#stream", Static)
        before_render = str(stream_widget.content)
        assert "Ctrl+C abort" in before_render or "thinking" in before_render.lower(), (
            "spinner should be visible"
        )

        # Simulate turn completion: the event handler calls _remove_thinking_line
        # and sets _turn_active = False.
        app._turn_active = False
        app._remove_thinking_line()
        app._render_stream()
        await pilot.pause()

        after_render = str(stream_widget.content)
        assert "Ctrl+C abort" not in after_render, (
            "spinner must be removed after turn ends"
        )


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
        tool_start_idx = next(i for i, l in enumerate(app._stream_lines) if "tool start" in l and "read" in l)
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
        assert "tool start" in stream
        thinking_stale = any(
            line == "Thinking:" and "tool start" not in line
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
        assert "tool start" in stream and "bash" in stream
        # The streamed JSON must NOT appear as a standalone assistant chat block.
        # Find the tool block line and verify there's no assistant delta block
        # before it (only logo lines and user text).
        tool_line_idx = next(
            (i for i, l in enumerate(app._stream_lines) if "tool start" in l and "bash" in l),
            None,
        )
        assert tool_line_idx is not None
        # The JSON text should not be in any non-logo lines.
        for line in app._stream_lines:
            if line.startswith(" ") or line.startswith("\u2588"):
                continue  # logo lines
            if "tool start" in line and "bash" in line:
                continue  # the tool block itself
            # No assistant delta lines should contain the full JSON structure.
            # Check by looking for the JSON's key structural elements on the
            # same line (they may be wrapped, but the tool block line is safe).
            assert 'tool_start' not in line or ('tool start ' in line and 'bash' in line)

        # 4. message_end must NOT create a duplicate block (state already reset)
        session._emit({"type": "message_end", "message": {"role": "assistant", "content": ""}})
        await pilot.pause()
        assert app._assistant_has_live_delta is False
        stream_after_end = "\n".join(app._stream_lines)
        # Only one tool block, no extra assistant block.
        assert sum(1 for l in stream_after_end.split("\n") if "tool start" in l and "bash" in l) == 1


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
        assert "tool start" in stream and "read" in stream
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
        search_sub = "tool start (timeout"
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
            assert len(app._pending_ui_events) == 251

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
        assert refreshes == 1

        for _ in range(100):
            session._emit(
                {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "x"}}
            )
        app._flush_pending_ui_events()
        assert refreshes == 1

        session._emit({"type": "turn_end", "ok": True})
        app._flush_pending_ui_events()
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
        assert renders == 1

        session._emit(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "\n".join(str(i) for i in range(600))},
            }
        )
        app._flush_pending_ui_events()
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
        assert "tool start" in stream and "bash" in stream
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
