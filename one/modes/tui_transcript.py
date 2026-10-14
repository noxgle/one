"""Virtual, line-at-a-time transcript presentation for the Textual TUI.

The transcript deliberately keeps its model outside the widget.  ``set_transcript``
only invalidates changed stable line IDs; Textual asks ``render_line`` for the rows
currently on screen, at which point that line's Content is wrapped and cached.
"""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence

from rich.segment import Segment
from textual import events
from textual.content import Content
from textual.geometry import Size
from textual.strip import Strip
from textual.style import Style
from textual.visual import Visual, visualize
from textual.widgets import Static


class TranscriptWidget(Static):
    """A Static-compatible virtual transcript with the Textual line API.

    Subclassing Static retains the public ``content`` surface used by older
    consumers, but normal transcript painting never calls ``Static.update``.
    """

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._line_ids: list[int] = []
        self._line_values: list[str] = []
        self._line_revisions: list[object] = []
        self._content_for_line: Callable[[int], Content] | None = None
        self._line_rows: dict[int, list[Strip]] = {}
        self._line_heights: list[int] = []
        self._offsets: list[int] = [0]
        self._wrap_width = -1
        # Repeated tool preview rows occur under distinct retained IDs. Share
        # only fully styled/action-qualified strips, never an ID-only cache.
        self._shared_rows: OrderedDict[tuple[object, ...], tuple[list[Strip], int]] = OrderedDict()
        self._shared_row_bytes = 0
        self._virtual_blocks: dict[int, tuple[dict[int, str], Callable[[int], None]]] = {}
        self._virtual_strips: OrderedDict[tuple[int, int], tuple[Strip, int]] = OrderedDict()
        self._virtual_strip_bytes = 0
        self._app_size: Size | None = None

    @property
    def _width(self) -> int:
        # Keep the committed wrap width stable while VerticalScroll adds/removes
        # its scrollbar during layout. Explicit resize invalidation resets it.
        if self._wrap_width > 1:
            return self._wrap_width
        return max(1, self.region.width or self.size.width)

    def set_transcript(
        self,
        line_ids: Sequence[int],
        line_values: Sequence[str],
        line_revisions: Sequence[object],
        content_for_line: Callable[[int], Content],
        *,
        virtual_blocks: dict[int, tuple[int, dict[int, str], Callable[[int], None]]] | None = None,
    ) -> None:
        """Apply a logical splice without aggregating or visualizing all lines."""
        ids, values, revisions = list(line_ids), list(line_values), list(line_revisions)
        virtual_blocks = virtual_blocks or {}
        self._virtual_blocks = {
            index: (loaded, need) for index, (_total, loaded, need) in virtual_blocks.items()
        }
        if len(ids) != len(values) or len(ids) != len(revisions) or len(set(ids)) != len(ids):
            raise ValueError("transcript IDs, values, and revisions must be aligned and unique")
        old_by_id = {
            line_id: (self._line_values[index], self._line_revisions[index], self._line_heights[index])
            for index, line_id in enumerate(self._line_ids)
        }
        self._line_ids, self._line_values, self._line_revisions = ids, values, revisions
        heights: list[int] = []
        width_changed = self._wrap_width != self._width
        if width_changed:
            # Completed pages are pre-wrapped by the runtime host. Their cache
            # key is a physical row, so retaining strips across a width change
            # would paint old-width CJK/long rows over newly fetched rows.
            self._virtual_strips.clear()
            self._virtual_strip_bytes = 0
        for index, (line_id, value, revision) in enumerate(zip(ids, values, revisions)):
            previous = old_by_id.get(line_id)
            changed = width_changed or previous is None or previous[:2] != (value, revision)
            if changed:
                self._line_rows.pop(line_id, None)
                if index in virtual_blocks:
                    # A completed block is one sparse logical source. Its rows
                    # have no IDs/values/strips until a visible page arrives.
                    rows = []
                else:
                    rows = self._wrap_line(index, content_for_line)
                heights.append(len(rows))
            else:
                heights.append(previous[2])
            if index in virtual_blocks:
                heights[-1] = virtual_blocks[index][0]
        retained = set(ids)
        self._line_rows = {line_id: rows for line_id, rows in self._line_rows.items() if line_id in retained}
        self._line_heights = heights
        self._content_for_line = content_for_line
        self._wrap_width = self._width
        self._reindex()
        self.refresh(layout=True)
        # The initial mount has width zero/one. Re-layout once its parent has a
        # real width; normal events do not cause this extra pass.
        if self._width <= 1:
            self.call_after_refresh(lambda: self.app._render_stream())

    @property
    def needs_reflow(self) -> bool:
        return self._wrap_width != self._width

    @property
    def content(self) -> Content:
        """Compatibility inspection view, intentionally excluded from paint paths."""
        content = Content()
        for index in range(len(self._line_ids)):
            part = (
                Content("")
                if index in self._virtual_blocks
                else self._content_for_line(index) if self._content_for_line else Content(self._line_values[index])
            )
            content = content.append(part)
        return content

    def invalidate_wrap(self) -> None:
        """Invalidate rows after a width/theme change; reflow remains demand-led."""
        self._line_rows.clear()
        self._wrap_width = -1
        self._wrap_width = self._width
        self._line_heights = [
            self._line_heights[index] if index in self._virtual_blocks else len(self._wrap_line(index))
            for index in range(len(self._line_ids))
        ]
        self._reindex()
        self.refresh(layout=True)

    def on_resize(self, event: events.Resize) -> None:
        """Reflow only when the initial mounted width becomes available.

        A ``VerticalScroll`` child also receives resize events whenever its
        scrollbar appears or disappears. Those must not invalidate the
        committed wrap width: changing a completed virtual block from its
        provisional height would otherwise make the scrollbar and page width
        oscillate. The app explicitly invalidates on a terminal resize.
        """
        app_size = self.app.size
        app_resized = app_size != self._app_size
        self._app_size = app_size
        if self._line_ids and event.size.width != self._wrap_width and (self._wrap_width <= 1 or app_resized):
            self.invalidate_wrap()

    def _reindex(self) -> None:
        total = 0
        self._offsets = [0]
        for height in self._line_heights:
            total += height
            self._offsets.append(total)
        self.virtual_size = Size(self._width, total)
        # A child of VerticalScroll needs a layout height as well as virtual_size.
        self.styles.height = total

    def _wrap_line(self, index: int, content_for_line: Callable[[int], Content] | None = None) -> list[Strip]:
        line_id = self._line_ids[index]
        width = self._width
        rows = self._line_rows.get(line_id)
        if rows is None:
            getter = content_for_line or self._content_for_line
            content = getter(index) if getter else Content(self._line_values[index])
            # Each logical entry owns its row boundary.  Retained Content was
            # historically assembled with a trailing newline for Static.
            if content.plain.endswith("\n"):
                content = content.right_crop(1)
            signature = (
                content.plain,
                tuple((span.start, span.end, str(span.style)) for span in content._spans),
                width,
                self._line_revisions[index],
            )
            shared = self._shared_rows.get(signature)
            if shared is not None:
                self._shared_rows.move_to_end(signature)
                rows = shared[0]
                self._line_rows[line_id] = rows
                return rows
            visual = visualize(self, content, markup=False)
            # Do not bake Textual's current selection in to a shared row cache.
            # A row can be used by several retained entries and selection changes
            # without changing its content revision.  Selection is applied below,
            # at paint time, against physical-row metadata.
            rows = Visual.to_strips(
                self, visual, width, None, self.visual_style, apply_selection=False
            )
            if not rows:
                rows = [Strip.blank(width, self.visual_style.rich_style)]
            self._line_rows[line_id] = rows
            row_bytes = sum(len(row.text.encode("utf-8")) for row in rows)
            self._shared_rows[signature] = (rows, row_bytes)
            self._shared_row_bytes += row_bytes
            while len(self._shared_rows) > 1024 or self._shared_row_bytes > 128 * 1024:
                _key, (_rows, evicted_bytes) = self._shared_rows.popitem(last=False)
                self._shared_row_bytes -= evicted_bytes
        return rows

    def render_line(self, y: int) -> Strip:
        """Render only the requested physical row, preserving Content actions."""
        from bisect import bisect_right

        if y < 0 or not self._line_ids:
            return Strip.blank(self.size.width, self.visual_style.rich_style)
        index = bisect_right(self._offsets, y) - 1
        if not 0 <= index < len(self._line_ids):
            return Strip.blank(self.size.width, self.visual_style.rich_style)
        rows = self._line_rows.get(self._line_ids[index])
        virtual = self._virtual_blocks.get(index)
        if virtual is not None:
            row = y - self._offsets[index]
            loaded, need = virtual
            value = loaded.get(row)
            if value is None:
                need(row)
                return Strip.blank(self.size.width, self.visual_style.rich_style)
            key = (self._line_ids[index], row)
            cached = self._virtual_strips.get(key)
            if cached is None:
                visual = visualize(self, Content(value), markup=False)
                strips = Visual.to_strips(self, visual, self._width, None, self.visual_style, apply_selection=False)
                strip = strips[0] if strips else Strip.blank(self._width, self.visual_style.rich_style)
                size = len(value.encode("utf-8"))
                self._virtual_strips[key] = (strip, size)
                self._virtual_strip_bytes += size
                while len(self._virtual_strips) > 1024 or self._virtual_strip_bytes > 128 * 1024:
                    _old, (_strip, old_size) = self._virtual_strips.popitem(last=False)
                    self._virtual_strip_bytes -= old_size
            else:
                strip = cached[0]
                self._virtual_strips.move_to_end(key)
            return self._selection_strip(strip, y)
        if rows is None:
            # Layout is prepared by set_transcript/invalidate_wrap. A blank is
            # safer than mutating virtual geometry in Textual's paint request.
            return Strip.blank(self.size.width, self.visual_style.rich_style)
        row = y - self._offsets[index]
        if not 0 <= row < len(rows):
            return Strip.blank(self.size.width, self.visual_style.rich_style)
        # Textual mouse selection is expressed in character offsets, while a
        # terminal row is measured in cells. apply_offsets records the former
        # for Textual (including wide CJK and combining characters) without
        # changing the cached base Strip.
        base = rows[row]
        return self._selection_strip(base, y)

    def _selection_strip(self, base: Strip, y: int) -> Strip:
        """Return a paint-only selected strip, retaining base cache immutability."""
        selection = self.text_selection
        if selection is None:
            return base.apply_offsets(0, y)
        span = selection.get_span(y)
        if span is None:
            return base.apply_offsets(0, y)
        start, end = span
        text_length = len(base.text)
        start = max(0, min(start, text_length))
        end = text_length if end < 0 else max(start, min(end, text_length))
        if start == end:
            return base.apply_offsets(0, y)
        selection_style = Style.from_styles(
            self.screen.get_component_styles("screen--selection")
        ).rich_style
        segments: list[Segment] = []
        offset = 0
        for segment in base:
            text, style, control = segment
            segment_end = offset + len(text)
            selected_start = max(start, offset)
            selected_end = min(end, segment_end)
            if selected_start >= selected_end:
                segments.append(segment)
            else:
                before = text[: selected_start - offset]
                chosen = text[selected_start - offset : selected_end - offset]
                after = text[selected_end - offset :]
                if before:
                    segments.append(Segment(before, style, control))
                if chosen:
                    segments.append(
                        Segment(chosen, (style + selection_style) if style else selection_style, control)
                    )
                if after:
                    segments.append(Segment(after, style, control))
            offset = segment_end
        # Offset metadata must be generated after splitting. Reusing an offset
        # attached to the original segment makes every split piece claim the
        # same character origin and breaks mouse selection in its middle.
        return Strip(segments, base.cell_length).apply_offsets(0, y)

    def get_selection(self, selection):  # type: ignore[no-untyped-def,override]
        """Selection is extracted from virtual rows, not Static's compatibility visual."""
        if not self._line_ids:
            return "", "\n"
        start = selection.start.y if selection.start is not None else 0
        end = selection.end.y if selection.end is not None else self._offsets[-1] - 1
        if start > end:
            start, end = end, start
        start, end = max(0, start), min(self._offsets[-1] - 1, end)
        if end < start:
            return "", "\n"
        # Selection coordinates name physical rows. Read exactly that span;
        # render_line is intentionally pure and cannot change offsets here.
        rows = "\n".join(self.render_line(row).text for row in range(start, end + 1))
        from textual.geometry import Offset
        from textual.selection import Selection

        # Coordinates supplied by Textual are character offsets; do not turn
        # them into terminal cells. That distinction matters for CJK, emoji and
        # combining characters. split("\n") deliberately preserves whitespace.
        row_text = rows.split("\n")
        start_x = selection.start.x if selection.start else 0
        end_x = selection.end.x if selection.end else len(row_text[-1])
        if selection.start and selection.end and (selection.start.y, start_x) > (selection.end.y, end_x):
            start_x, end_x = end_x, start_x
        local = Selection(Offset(start_x, 0), Offset(end_x, end - start))
        return local.extract(rows), "\n"

    async def on_click(self, event: events.Click) -> None:
        """Dispatch a trusted Content action from a virtual Strip.

        Static's aggregate visual is intentionally absent, so broker lookup
        cannot use it for line-API strips on Textual 8.2.
        """
        strip = self.render_line(event.y)
        cell = 0
        for segment in strip:
            end = cell + segment.cell_length
            if cell <= event.x < end:
                action = (segment.style.meta or {}).get("@click") if segment.style else None
                if isinstance(action, str) and action.startswith("app.open_"):
                    await self.run_action(action)
                return
            cell = end

    def on_mouse_up(self, event: events.MouseUp) -> None:  # noqa: ARG002
        """Let the owning app copy a real drag after Textual updates selection."""
        copy_selection = getattr(self.app, "_copy_transcript_selection", None)
        if callable(copy_selection):
            self.call_after_refresh(lambda: copy_selection(self, self.text_selection))
