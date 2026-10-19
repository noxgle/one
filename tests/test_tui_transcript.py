# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_transcript_prepares_unicode_rows_and_keeps_render_geometry_pure() -> None:
    """CJK/emoji/combining text is laid out before, never during, paint."""
    from textual.app import App, ComposeResult
    from textual.content import Content
    from textual.geometry import Offset
    from textual.selection import Selection

    from one.modes.tui_transcript import TranscriptWidget

    class TranscriptApp(App[None]):
        def compose(self) -> ComposeResult:
            yield TranscriptWidget(id="stream")

    app = TranscriptApp()
    async with app.run_test(size=(12, 8)):
        stream = app.query_one("#stream", TranscriptWidget)
        values = ["表🙂e\u0301  ", "same", "same"]
        revisions = [(0,), (1,), (2,)]
        stream.set_transcript([101, 102, 103], values, revisions, lambda index: Content(values[index]))
        height = stream.virtual_size.height
        rows = [stream.render_line(row).text for row in range(height)]
        assert stream.virtual_size.height == height
        assert rows[0].endswith("  ")
        assert stream.get_selection(Selection(Offset(0, 0), Offset(3, 0)))[0]

        # Metadata-only replacement of one stable ID must invalidate its row,
        # while identical text under distinct IDs remains independent.
        stream.set_transcript([101, 102, 103], values, [(0,), ("new-action",), (2,)], lambda index: Content(values[index]))
        assert stream._line_revisions[1] == ("new-action",)


@pytest.mark.asyncio
async def test_main_transcript_wraps_inside_padding_and_stable_scrollbar() -> None:
    """No physical row may extend beneath padding or the parent scrollbar."""
    from textual.content import Content

    from one.modes.tui_mode import _OneTextualApp
    from one.modes.tui_transcript import TranscriptWidget
    from tests.test_tui_snapshots import _DummySession

    app = _OneTextualApp(_DummySession())
    async with app.run_test(size=(100, 32)) as pilot:
        stream = app.query_one("#stream", TranscriptWidget)
        container = app.query_one("#stream_container")
        app.query_one("#logo").display = False
        sentence = (
            "W skrócie: najbardziej opłaca się przy dużych lub potencjalnie wrażliwych "
            "na limity wynikach, których nie trzeba zawsze w całości umieszczać w "
            "kontekście. Dla małych wyników zwykła odpowiedź narzędzia jest prostsza."
        )
        for size in ((100, 32), (72, 28), (132, 40)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
            widths = []
            # Cross the scrollbar threshold in both directions, without resizing.
            for count in (1, 60, 1):
                app._stream_lines = [sentence] * count
                app._render_stream()
                await pilot.pause()
                widths.append(stream._width)
                assert stream._width == stream.content_size.width
                assert container.show_vertical_scrollbar == (count == 60)
                for index in range(count):
                    rows = stream._line_rows[stream._line_ids[index]]
                    assert all(row.cell_length <= stream.content_size.width for row in rows)
                    # Wrapping may remove separator spaces, never letters.
                    assert "".join(row.text for row in rows).replace(" ", "") == sentence.replace(" ", "")
                # Also exercise the prewrapped completed-response paint path.
                value = "界" * (stream._width // 2)
                stream.set_transcript(
                    [9999], [""], [0], lambda _: Content(""),
                    virtual_blocks={0: (1, {0: value}, lambda _: None)},
                )
                assert stream.render_line(0).text == value
                assert stream.render_line(0).cell_length <= stream.content_size.width
            assert len(set(widths)) == 1
