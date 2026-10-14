"""P1-9: snapshot/regression tests for TUI rendering.

Captures a deterministic full TUI render (Textual SVG screenshot) and
compares it against committed golden files in ``tests/snapshots/tui/``.

Usage
-----
Generate golden files::

    ONE_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest -q tests/test_tui_snapshots.py

Run regression::

    .venv/bin/python -m pytest -q tests/test_tui_snapshots.py
"""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from typing import Any

import pytest

SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "tui"

pytestmark = pytest.mark.snapshot

# ---------------------------------------------------------------------------
# Self-contained deterministic fake session / model helpers, including the
# attributes read by ``_OneTextualApp._refresh_sidebar_`` and
# ``build_sidebar_snapshot``.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DummyModel:
    provider: str = "openai"
    id: str = "gpt-4.1"


class _DummySessionManager:
    cwd: str = "/tmp/project"


class _DummySettingsManager:
    def __init__(self, info_panel: str = "top") -> None:
        self.info_panel = info_panel

    def get_theme(self) -> str:
        return "default"

    def get_tui_info_panel(self) -> str:
        return self.info_panel

    def get_subagents_enabled(self) -> bool:
        return True

    def get_bash_show_output(self) -> bool:
        return True


class _DummySession:
    """Minimal deterministic session for snapshot tests."""

    def __init__(self, *, info_panel: str = "top") -> None:
        self.model = _DummyModel()
        self.thinking_level: str = "medium"
        self.session_manager = _DummySessionManager()
        self.session_id: str = "snap-test-001"
        self.is_streaming: bool = False
        self.is_compacting: bool = False
        self.approval_callback: Any = None
        self.settings_manager = _DummySettingsManager(info_panel)
        self._subscribers: list[Any] = []
        self._unsubscribe: Any = None
        self.auto_retry_enabled: bool = True
        self.subagent_monitor: Any = None

    def subscribe(self, fn: Any) -> Any:
        """Register a listener; return a callable that unsubscribes."""
        self._subscribers.append(fn)

        def unsubscribe() -> None:
            try:
                self._subscribers.remove(fn)
            except ValueError:
                pass

        self._unsubscribe = unsubscribe
        return unsubscribe

    def get_context_usage(self) -> dict:
        return {"percent": 33.7}

    def get_pending_queues(self) -> dict:
        return {"steering": ["s1"], "followUp": ["f1", "f2"]}

    def get_session_stats(self) -> dict:
        return {
            "tokens": {
                "input": 250,
                "output": 120,
                "cacheRead": 50,
                "cacheWrite": 10,
                "total": 430,
            },
            "cost": 0.0087,
        }

    # Additional attributes _OneTextualApp accesses during mount.
    # (Not used by build_sidebar_snapshot but accessed by other paths.)
    pending_message_count: int = 0
    active_tools: list[str] = []

    @property
    def session_file(self) -> str:
        return "/tmp/project/snap-test-001.jsonl"


class _DummyMonitor:
    """Static monitor fixture for the deterministic agents-panel snapshot."""

    def subscribe(self, _listener: Any) -> Any:
        return lambda: None

    def snapshot(self) -> dict[str, Any]:
        return {
            "revision": 1,
            "children": [
                {
                    "sessionId": "snap-child-001",
                    "task": "Inspect the deterministic preview",
                    "model": "openai/gpt-4.1",
                    "status": "completed",
                    "startedMonotonic": 10.0,
                    "completedAt": 12.5,
                    "activity": "completed",
                    "currentTool": None,
                    "outputTruncated": False,
                    "timeline": [
                        {"kind": "response", "text": "A bounded child response preview."},
                        {
                            "kind": "tool",
                            "tool": "read",
                            "toolCallId": "snap-read-1",
                            "args": "path=preview.txt, offset=1, limit=20",
                            "effectiveTimeout": 120,
                            "state": "completed",
                            "ok": True,
                            "elapsedMs": 125,
                            "output": "deterministic safe result text",
                        },
                    ],
                }
            ],
        }


# ---------------------------------------------------------------------------
# Golden-file snapshot helper
# ---------------------------------------------------------------------------

GOLDEN_EXT = ".txt"  # .txt is the committed extension (contains SVG text).
_SVG_TERMINAL_ID = re.compile(
    r'(?P<prefix>\.terminal-|id="terminal-|class="terminal-|url\(#terminal-)\d+(?=-)'
)
_STABLE_TERMINAL_ID_SUFFIX = "SNAPSHOT"


def _normalize_svg_terminal_ids(content: str) -> str:
    """Replace Rich's process-dependent SVG terminal ID prefix.

    Text fallback snapshots are deliberately left untouched.
    """
    if not content.lstrip().startswith("<svg"):
        return content
    return _SVG_TERMINAL_ID.sub(rf"\g<prefix>{_STABLE_TERMINAL_ID_SUFFIX}", content)


def _normalize_svg_whitespace_only_lines(content: str) -> str:
    """Remove indentation from otherwise empty SVG lines."""
    if not content.lstrip().startswith("<svg"):
        return content
    return re.sub(r"(?m)^[ \t]+$", "", content)


def _capture_snapshot_mismatch(name: str, golden: str, captured: str) -> Path | None:
    """Save the exact mismatch outside the checkout for flaky-render diagnosis."""
    artifact_dir = Path("/tmp/opencode")
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        stem = f"tui-snapshot-{name}-{os.getpid()}"
        (artifact_dir / f"{stem}.golden.svg").write_text(golden, encoding="utf-8")
        (artifact_dir / f"{stem}.captured.svg").write_text(captured, encoding="utf-8")
        (artifact_dir / f"{stem}.diff").write_text(
            "".join(
                unified_diff(
                    golden.splitlines(keepends=True),
                    captured.splitlines(keepends=True),
                    fromfile=f"{name}.golden.svg",
                    tofile=f"{name}.captured.svg",
                )
            ),
            encoding="utf-8",
        )
    except OSError:
        return None
    return artifact_dir / f"{stem}.diff"


def _check_snapshot(
    name: str,
    content: str,
    *,
    update: bool = False,
) -> None:
    """Compare *content* against the golden file for *name*.

    When ``update`` is True the golden file is created/overwritten.
    Otherwise the test fails on missing or differing content.
    """
    content = _normalize_svg_terminal_ids(content)
    golden = SNAPSHOT_DIR / f"{name}{GOLDEN_EXT}"
    if update:
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(content, encoding="utf-8")
        return

    if not golden.exists():
        pytest.fail(
            f"Missing golden file: {golden}\n"
            f"Generate it with: ONE_UPDATE_SNAPSHOTS=1 pytest -q {__file__}"
        )

    existing = _normalize_svg_terminal_ids(golden.read_text(encoding="utf-8"))
    if content != existing:
        artifact = _capture_snapshot_mismatch(name, existing, content)
        artifact_message = f"\n  exact diff: {artifact}\n" if artifact else ""
        pytest.fail(
            f"Snapshot mismatch for '{name}' ({golden.name}):\n"
            f"  golden bytes: {len(existing.encode('utf-8'))}\n"
            f"  captured bytes: {len(content.encode('utf-8'))}\n"
            f"{artifact_message}"
            f"Generate with: ONE_UPDATE_SNAPSHOTS=1 pytest -q {__file__}"
        )


def test_normalize_svg_terminal_ids() -> None:
    svg = (
        '<svg><style>.terminal-123-r1 {}</style>'
        '<g class="terminal-123-r1" id="terminal-123-line-0" '
        'clip-path="url(#terminal-123-line-0)">terminal-123-r1</g></svg>'
    )

    assert _normalize_svg_terminal_ids(svg) == (
        '<svg><style>.terminal-SNAPSHOT-r1 {}</style>'
        '<g class="terminal-SNAPSHOT-r1" id="terminal-SNAPSHOT-line-0" '
        'clip-path="url(#terminal-SNAPSHOT-line-0)">terminal-123-r1</g></svg>'
    )
    assert _normalize_svg_terminal_ids("terminal-123-r1") == "terminal-123-r1"


# ---------------------------------------------------------------------------
# Deterministic assistant / tool stream content (no timestamps, IDs, etc.)
# ---------------------------------------------------------------------------

_STREAM_LINES = [
    "> hello there",
    "Sure! Here's a quick demo.",
    "tool start (timeout 30s): bash {\"command\": \"echo hi\"}",
    "tool ok: bash",
    "Done with the demo.",
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

UPDATE = os.environ.get("ONE_UPDATE_SNAPSHOTS", "").lower() in {"1", "true", "yes"}


@pytest.mark.asyncio
async def test_tui_snapshot_base() -> None:
    """Mount the TUI, write deterministic stream content, refresh sidebar."""
    from one.modes.tui_mode import _OneTextualApp

    session = _DummySession()
    app = _OneTextualApp(session)

    async with app.run_test(size=(120, 48)) as pilot:
        await _settle_snapshot(pilot, app)

        # Deterministic assistant + tool stream content.
        for line in _STREAM_LINES:
            app._stream_lines.append(line)
        app._render_stream()
        app._refresh_sidebar()
        await _settle_snapshot(pilot, app)

        # This snapshot covers the full visual-only logo rather than its
        # short-screen fallback.
        assert "██████╗" in str(app.query_one("#logo").content)

        # Capture SVG screenshot; fall back to text snapshot if needed.
        content = _capture_content(app)
        _check_snapshot("base", content, update=UPDATE)


@pytest.mark.asyncio
async def test_tui_snapshot_widget_panel() -> None:
    """Base scenario plus an extension widget panel rendered."""
    from one.modes.tui_mode import _OneTextualApp

    # This fixture explicitly uses the sidebar layout: the widget-panel golden
    # is intended to cover the sidebar and the extension widget together.
    session = _DummySession(info_panel="sidebar")
    app = _OneTextualApp(session)

    async with app.run_test(size=(100, 28)) as pilot:
        await _settle_snapshot(pilot, app)

        for line in _STREAM_LINES:
            app._stream_lines.append(line)
        app._render_stream()
        app._refresh_sidebar()

        # Show a widget extension panel.
        app._show_extension_panel(
            {
                "extension": "ext",
                "uiType": "widget",
                "title": "Wizard",
                "payload": {"mode": "quick"},
            }
        )
        await _settle_snapshot(pilot, app)

        content = _capture_content(app)
        _check_snapshot("widget_panel", content, update=UPDATE)


@pytest.mark.asyncio
async def test_tui_snapshot_agents() -> None:
    """Current-session direct-child monitor with a selected preview."""
    from one.modes.tui_mode import _OneTextualApp

    session = _DummySession()
    session.subagent_monitor = _DummyMonitor()
    app = _OneTextualApp(session)

    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.pause()
        app._open_agents_panel()
        await pilot.pause()
        app.query_one("#agents_timeline").scroll_end(animate=False)
        await _settle_snapshot(pilot, app)

        content = _normalize_svg_whitespace_only_lines(_capture_content(app))
        assert "Result:" in content
        assert "deterministic" in content
        _check_snapshot("agents", content, update=UPDATE)


@pytest.mark.asyncio
async def test_tui_snapshot_overlay() -> None:
    """Base scenario plus an extension overlay rendered."""
    from one.modes.tui_mode import _OneTextualApp

    session = _DummySession()
    app = _OneTextualApp(session)

    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.pause()

        for line in _STREAM_LINES:
            app._stream_lines.append(line)
        # Exercise the mounted application's deferred render path rather than
        # bypassing it with a synchronous test-only paint.
        app._schedule_stream_render()
        assert "stream frame" in _snapshot_pending_work(app)
        app._refresh_sidebar()

        # Show an overlay extension panel.
        app._show_extension_panel(
            {
                "extension": "ext",
                "uiType": "overlay",
                "title": "Confirm",
                "payload": {"step": 2},
            }
        )
        await _settle_snapshot(pilot, app)
        assert not _snapshot_pending_work(app)

        content = _capture_content(app)
        delayed_captures = [content]
        for _ in range(2):
            # Cross later frame boundaries after the asynchronous stream paint;
            # a settled overlay must export the same strict SVG each time.
            await pilot.pause(0.04)
            await _settle_snapshot(pilot, app)
            delayed_captures.append(_capture_content(app))
        assert delayed_captures.count(content) == len(delayed_captures)
        _check_snapshot("overlay", content, update=UPDATE)


# ---------------------------------------------------------------------------
# Capture helper — tries export_screenshot(), falls back to text snapshot
# ---------------------------------------------------------------------------


async def _settle_snapshot(pilot: Any, app: Any) -> None:
    """Wait until queued frame, layout, and viewport work has painted."""
    for _ in range(4):
        await pilot.pause()
        await pilot.wait_for_scheduled_animations()
        await pilot.pause()
        pending = _snapshot_pending_work(app)
        if not pending:
            return
        # This is a conditional wait for Textual's capped next paint, not a
        # blind screenshot delay. It permits a queued 30 Hz paint to run.
        await pilot.pause(0.04)
    pytest.fail(f"Snapshot did not settle: {', '.join(_snapshot_pending_work(app))}")


def _snapshot_pending_work(app: Any) -> list[str]:
    """Return all observable work that can still change an SVG export."""
    pending: list[str] = []
    if app._stream_viewport_callback_pending:
        pending.append("stream viewport callback")
    if app._deferred_stream_viewport is not None:
        pending.append("deferred stream viewport")
    if app._stream_render_dirty or app._stream_render_timer is not None:
        pending.append("stream frame")
    if app._rendered_stream_revision != app._stream_render_revision:
        pending.append("stream revision")
    if app._sidebar_refresh_dirty or app._sidebar_refresh_timer is not None:
        pending.append("sidebar frame")
    screen = app.screen
    if screen._compositor._dirty_regions:
        pending.append("compositor dirty regions")
    if app._next_callbacks or screen._next_callbacks or screen._callbacks:
        pending.append("post-refresh callbacks")
    return pending


def _capture_content(app: Any) -> str:
    """Return a deterministic full-layout snapshot string.

    Tries ``app.export_screenshot()`` (Textual SVG) first.  If the result
    is empty or the method is missing it falls back to composing a text
    snapshot from the widgets that form the layout.

    Returns the SVG text (which is deterministic and non-ANSI).
    """
    try:
        svg = app.export_screenshot()
    except Exception:
        svg = ""

    if svg:
        return svg

    # Fallback: compose a text snapshot from widget content/classes.
    return _compose_text_snapshot(app)


def _compose_text_snapshot(app: Any) -> str:
    """Compose a deterministic text snapshot from widget content and classes.

    Captures: ``#header``, ``#logo``, ``#stream``, ``#sidebar``, ``#input`` placeholder,
    ``#ext_panel``, ``#ext_overlay`` — each separated by a horizontal rule.
    """
    from textual.widgets import Static, TextArea

    parts: list[str] = []

    def _widget_text(widget_id: str) -> str:
        try:
            w = app.query_one(widget_id, Static)
            cls = " ".join(w.classes) if hasattr(w, "classes") else ""
            return f"[{cls}]\n{w.content}\n"
        except Exception:
            return f"[{widget_id}]\n[not found]\n"

    def _input_text() -> str:
        try:
            w = app.query_one("#input", TextArea)
            return f"[TextArea]\n{str(w.placeholder or '')}\n"
        except Exception:
            return "[TextArea]\n[not found]\n"

    parts.append("### HEADER")
    parts.append(_widget_text("#header"))
    parts.append("### LOGO")
    parts.append(_widget_text("#logo"))
    parts.append("### STREAM")
    parts.append(_widget_text("#stream"))
    parts.append("### SIDEBAR")
    parts.append(_widget_text("#sidebar"))
    parts.append("### INPUT")
    parts.append(_input_text())
    parts.append("### EXT_PANEL")
    parts.append(_widget_text("#ext_panel"))
    parts.append("### EXT_OVERLAY")
    parts.append(_widget_text("#ext_overlay"))

    return "\n---\n".join(parts)
