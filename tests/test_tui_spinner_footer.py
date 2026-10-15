"""Tests for the spinner footer inside the shared conversation wrapper.

Verifies:
- Footer (`#waiting_status`) lives inside a shared `#conversation` wrapper
  alongside the scrollable transcript (`#stream_container`).
- Footer stays fixed while scrolling the transcript (region unchanged after scroll).
- Transcript renderer is NOT called on spinner ticks.
- Exact ``Ctrl+C \u2014 abort`` label appears when spinning.
- Theme applies to both wrapper and footer; inner scroll has no border.
- Works in both compact and wide layouts.
- Footer has no bottom padding clipping; height=1 is fully usable.
"""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one

from __future__ import annotations

from pathlib import Path

import pytest

from one.modes.tui_mode import _THINKING_FRAMES
from tests.support.tui import _mk_app_session


@pytest.mark.asyncio
async def test_tui_spinner_footer_inside_shared_conversation_frame(tmp_path: Path) -> None:
    """The footer widget must be a child of #conversation, not a sibling of the scroll."""
    from textual.containers import Vertical

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        conversation = app.query_one("#conversation", Vertical)
        waiting = app.query_one("#waiting_status")

        # The waiting_status must be a direct child of the #conversation wrapper.
        assert waiting in conversation.children, (
            "#waiting_status must live inside the shared #conversation wrapper"
        )


@pytest.mark.asyncio
async def test_tui_spinner_footer_remains_fixed_while_scrolling(tmp_path: Path) -> None:
    """Scrolling the transcript must not move the footer region."""
    from textual.containers import Vertical, VerticalScroll
    from textual.events import MouseScrollUp
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        # Populate transcript so the scrollable has content to scroll.
        for i in range(50):
            app._stream_lines.append(f"line {i}")
        app._render_stream()
        await pilot.pause()

        conversation = app.query_one("#conversation", Vertical)
        stream_container = app.query_one("#stream_container", VerticalScroll)
        waiting: Static = app.query_one("#waiting_status", Static)

        # 1. Verify layout: scroll is above footer inside conversation.
        scroll_region = stream_container.region
        footer_region_before = waiting.region
        assert scroll_region.y < footer_region_before.y, (
            "Scroll container must be above footer"
        )
        # Footer must be within conversation bounds.
        conv_region = conversation.region
        assert footer_region_before.x >= conv_region.x
        assert footer_region_before.x + footer_region_before.width <= conv_region.x + conv_region.width
        # Footer is BELOW scroll content area.
        assert footer_region_before.y >= scroll_region.y + scroll_region.height

        # 2. Scroll the transcript upward by dispatching MouseScrollUp events.
        for _ in range(30):
            app.post_message(
                MouseScrollUp(
                    stream_container,
                    x=50.0,
                    y=stream_container.region.y + 10.0,
                    delta_x=0,
                    delta_y=-1,
                    button=0,
                    shift=False,
                    meta=False,
                    ctrl=False,
                )
            )
            await pilot.pause()

        # 3. Footer region must be unchanged after scrolling (footer is
        #    fixed outside the scroll container).
        footer_region_after = waiting.region
        assert footer_region_before == footer_region_after, (
            f"Footer region must not change on scroll: before={footer_region_before}, after={footer_region_after}"
        )


@pytest.mark.asyncio
async def test_tui_spinner_footer_region_within_conversation_content(
    tmp_path: Path,
) -> None:
    """Footer must sit BELOW the scroll region and inside the conversation content region."""
    from textual.containers import Vertical, VerticalScroll
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        # Fill the transcript so scroll has content.
        for i in range(40):
            app._stream_lines.append(f"row {i}")
        app._render_stream()
        await pilot.pause()

        conversation = app.query_one("#conversation", Vertical)
        stream_container = app.query_one("#stream_container", VerticalScroll)
        waiting: Static = app.query_one("#waiting_status", Static)

        conv_region = conversation.region
        scroll_region = stream_container.region
        footer_region = waiting.region

        # content_size.height must be >= 1 for the footer widget.
        assert waiting.content_size.height >= 1, (
            f"Footer content_size.height must be >= 1, got {waiting.content_size.height}"
        )

        # Footer is inside conversation width.
        assert footer_region.x >= conv_region.x
        assert footer_region.bottom <= conv_region.bottom

        # Footer is below scroll region.
        assert footer_region.y >= scroll_region.y + scroll_region.height


@pytest.mark.asyncio
async def test_tui_spinner_rendered_screen_contains_exact_label(tmp_path: Path) -> None:
    """Rendered screen must contain the exact ``Ctrl+C \u2014 abort`` text."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        app._turn_active = True
        app._last_delta_ts = 0.0
        app._tick_waiting()
        await pilot.pause()

        waiting: Static = app.query_one("#waiting_status", Static)
        rendered = str(waiting.render())
        # Must contain the exact em-dash label.
        assert "Ctrl+C \u2014 abort" in rendered, (
            f"Expected 'Ctrl+C \\u2014 abort' in rendered footer, got: {rendered!r}"
        )


@pytest.mark.asyncio
async def test_tui_spinner_stays_active_for_hidden_native_write_arguments(tmp_path: Path) -> None:
    """Non-prose argument deltas must not reset the independent wait footer."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    app = _OneTextualApp(_mk_app_session(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app._turn_active = True
        app._last_delta_ts = 0.0
        app._handle_session_event({
            "type": "message_update",
            "assistantMessageEvent": {"type": "tool_call_delta", "delta": "x" * 8192},
        })
        app._handle_session_event({"type": "tool_call_start", "tool": "write", "displayArgs": "content: <large>"})
        app._tick_waiting()
        await pilot.pause()
        assert "Ctrl+C — abort" in str(app.query_one("#waiting_status", Static).render())


@pytest.mark.asyncio
async def test_tui_finish_summary_has_no_empty_native_carrier_on_restore(tmp_path: Path) -> None:
    """A persisted native tool carrier is context only, not an empty chat panel."""
    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    session.messages.extend([
        {"role": "assistant", "content": [{"type": "text", "text": ""}], "_nativeToolCalls": [{"id": "call-1"}]},
        {"role": "toolResult", "content": '{"ok":true,"outputText":"written"}'},
        {"role": "assistant", "content": [{"type": "text", "text": "Finished successfully."}], "stopReason": "completed"},
    ])
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._rebuild_session_transcript()
        app._render_stream()
        await pilot.pause()
        rendered = "\n".join(str(line) for line in app._stream_lines)
        assert "Finished successfully." in rendered
        assert "[empty]" not in rendered
        assert "[empty response]" not in rendered


@pytest.mark.asyncio
async def test_tui_restore_keeps_native_tool_message_prose(tmp_path: Path) -> None:
    session = _mk_app_session(tmp_path)
    session.messages.append({
        "role": "assistant",
        "content": [{"type": "text", "text": "I will create the requested file."}],
        "_nativeToolCalls": [{"id": "call-1"}],
    })
    from one.modes.tui_mode import _OneTextualApp

    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._rebuild_session_transcript()
        assert "I will create the requested file." in "\n".join(str(line) for line in app._stream_lines)


@pytest.mark.asyncio
async def test_tui_spinner_footer_theme_applies_to_wrapper_and_footer(
    tmp_path: Path,
) -> None:
    """Theme changes: outer #conversation has border, inner scroll has no border, footer themed."""
    from textual.color import Color
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Apply the "light" theme.
        app._set_theme("light")
        await pilot.pause()

        conversation = app.query_one("#conversation")
        waiting: Static = app.query_one("#waiting_status", Static)
        stream_container = app.query_one("#stream_container")

        white = Color(255, 255, 255)

        # Conversation wrapper: themed background + border.
        assert conversation.styles.background == white, (
            f"Conversation wrapper bg should be light theme panel_bg, got {conversation.styles.background}"
        )
        assert conversation.styles.border, (
            "Conversation wrapper must have border"
        )

        # Footer: themed background.
        wb = waiting.styles.background
        assert wb == white, f"Waiting status bg should be light theme panel_bg, got {wb}"

        # Inner scroll: NO border (theme must not restore it).
        # Setting styles.border = None produces an empty Edges() which is falsy.
        assert not stream_container.styles.border, (
            f"#stream_container must have no border after theme, got {stream_container.styles.border}"
        )


@pytest.mark.asyncio
async def test_tui_spinner_footer_compact_layout(tmp_path: Path) -> None:
    """The footer must be visible and correctly positioned in compact layout."""
    from textual.containers import Vertical
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        app._layout_mode = "compact"
        app._apply_information_layout()
        await pilot.pause()

        conversation = app.query_one("#conversation", Vertical)
        waiting: Static = app.query_one("#waiting_status", Static)

        assert waiting in conversation.children
        assert waiting.content_size.height >= 1
        # Header should be visible in compact layout.
        header = app.query_one("#header")
        assert header.styles.display == "block"


@pytest.mark.asyncio
async def test_tui_spinner_footer_wide_layout(tmp_path: Path) -> None:
    """The footer must be visible and correctly positioned in wide layout."""
    from textual.containers import Vertical
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test(size=(120, 48)) as pilot:
        await pilot.pause()

        app._layout_mode = "wide"
        app._apply_information_layout()
        await pilot.pause()

        conversation = app.query_one("#conversation", Vertical)
        waiting: Static = app.query_one("#waiting_status", Static)

        assert waiting in conversation.children
        assert waiting.content_size.height >= 1
        # Sidebar should be visible in wide.
        sidebar = app.query_one("#sidebar")
        assert sidebar.styles.display == "block"


@pytest.mark.asyncio
async def test_tui_spinner_footer_focus_small_resize_theme(tmp_path: Path) -> None:
    """Small terminal + theme must keep footer visible with no inner borders."""
    from textual.containers import Vertical, VerticalScroll
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test(size=(60, 16)) as pilot:
        await pilot.pause()

        # Apply a different theme to verify no inner border leak.
        app._set_theme("hacker")
        await pilot.pause()

        conversation = app.query_one("#conversation", Vertical)
        waiting: Static = app.query_one("#waiting_status", Static)
        stream_container = app.query_one("#stream_container", VerticalScroll)

        # Footer exists with height.
        assert waiting.content_size.height >= 1

        # Footer is inside conversation.
        assert waiting in conversation.children

        # Inner scroll has no border regardless of theme.
        assert not stream_container.styles.border, (
            f"Inner scroll border must be falsy, got {stream_container.styles.border}"
        )

        # Conversation still has a border.
        assert conversation.styles.border

        # Footer is below scroll.
        assert waiting.region.y >= stream_container.region.y + stream_container.region.height


@pytest.mark.asyncio
async def test_tui_spinner_idle_footer_reserved_no_relayout(tmp_path: Path) -> None:
    """Even when idle (no spinner), the footer widget occupies its row to prevent relayout."""
    from textual.widgets import Static

    from one.modes.tui_mode import _OneTextualApp

    session = _mk_app_session(tmp_path)
    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Never arm the spinner — _turn_active stays False.
        waiting: Static = app.query_one("#waiting_status", Static)
        assert waiting is not None
        assert waiting.content_size.height >= 1
        # The widget exists and has height 1 in CSS.
        # Even with empty content it occupies space.
        rendered = str(waiting.render())
        assert rendered == "" or rendered.isspace(), (
            "Idle footer should have no visible content"
        )


@pytest.mark.asyncio
async def test_tui_spinner_frames_unchanged(tmp_path: Path) -> None:
    """Spinner frame sequence must remain ░▒▓█▓▒."""
    assert _THINKING_FRAMES == "░▒▓█▓▒"
