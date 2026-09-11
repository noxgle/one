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

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "tui"

# ---------------------------------------------------------------------------
# Deterministic fake session / model helpers (replicate what
# ``tests/test_tui_mode.py`` _DummySession / _DummyModel provide plus
# the extra attributes/_OneTextualApp._refresh_sidebar_ and
# ``build_sidebar_snapshot`` read).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DummyModel:
    provider: str = "openai"
    id: str = "gpt-4.1"


class _DummySessionManager:
    cwd: str = "/tmp/project"


class _DummySettingsManager:
    def get_theme(self) -> str:
        return "default"

    def get_subagents_enabled(self) -> bool:
        return True

    def get_bash_show_output(self) -> bool:
        return True


class _DummySession:
    """Minimal deterministic session for snapshot tests."""

    def __init__(self) -> None:
        self.model = _DummyModel()
        self.thinking_level: str = "medium"
        self.session_manager = _DummySessionManager()
        self.session_id: str = "snap-test-001"
        self.is_streaming: bool = False
        self.is_compacting: bool = False
        self.approval_callback: Any = None
        self.settings_manager = _DummySettingsManager()
        self._subscribers: list[Any] = []
        self._unsubscribe: Any = None
        self.auto_retry_enabled: bool = True

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


# ---------------------------------------------------------------------------
# Golden-file snapshot helper
# ---------------------------------------------------------------------------

GOLDEN_EXT = ".txt"  # .txt is the committed extension (contains SVG text).


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

    existing = golden.read_text(encoding="utf-8")
    if content != existing:
        pytest.fail(
            f"Snapshot mismatch for '{name}' ({golden.name}):\n"
            f"  golden bytes: {len(existing.encode('utf-8'))}\n"
            f"  captured bytes: {len(content.encode('utf-8'))}\n"
            f"Generate with: ONE_UPDATE_SNAPSHOTS=1 pytest -q {__file__}"
        )


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

    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.pause()

        # Deterministic assistant + tool stream content.
        for line in _STREAM_LINES:
            app._stream_lines.append(line)
        app._render_stream()
        app._refresh_sidebar()
        await pilot.pause()

        # Capture SVG screenshot; fall back to text snapshot if needed.
        content = _capture_content(app)
        _check_snapshot("base", content, update=UPDATE)


@pytest.mark.asyncio
async def test_tui_snapshot_widget_panel() -> None:
    """Base scenario plus an extension widget panel rendered."""
    from one.modes.tui_mode import _OneTextualApp

    session = _DummySession()
    app = _OneTextualApp(session)

    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.pause()

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
        await pilot.pause()

        content = _capture_content(app)
        _check_snapshot("widget_panel", content, update=UPDATE)


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
        app._render_stream()
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
        await pilot.pause()

        content = _capture_content(app)
        _check_snapshot("overlay", content, update=UPDATE)


# ---------------------------------------------------------------------------
# Capture helper — tries export_screenshot(), falls back to text snapshot
# ---------------------------------------------------------------------------


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

    Captures: ``#stream``, ``#sidebar``, ``#input`` placeholder,
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
