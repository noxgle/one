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
