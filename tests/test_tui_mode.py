from __future__ import annotations

from one.cli.args import parse_args
from one.modes.tui_mode import BUILTIN_TUI_THEMES, build_sidebar_snapshot, resolve_tui_theme


class _DummyModel:
    provider = "openrouter"
    id = "google/gemma-4-31b-it:free"


class _DummySessionManager:
    cwd = "/tmp/project"


class _DummySession:
    def __init__(self) -> None:
        self.model = _DummyModel()
        self.thinking_level = "medium"
        self.session_manager = _DummySessionManager()
        self.session_id = "s-test"
        self.is_streaming = False
        self.is_compacting = False

    def get_context_usage(self) -> dict:
        return {"percent": 12.5}

    def get_pending_queues(self) -> dict:
        return {"steering": [1, 2], "followUp": [3]}

    def get_session_stats(self) -> dict:
        return {
            "tokens": {
                "input": 100,
                "output": 50,
                "cacheRead": 10,
                "cacheWrite": 5,
                "total": 165,
            },
            "cost": 0.0123,
        }


def test_parse_args_accepts_tui_mode() -> None:
    parsed = parse_args(["--mode", "tui"])
    assert parsed.errors == []
    assert parsed.mode == "tui"


def test_resolve_tui_theme_includes_fallout() -> None:
    fallout = resolve_tui_theme("fallout")
    assert fallout.name == "fallout"
    assert "fallout" in BUILTIN_TUI_THEMES
    assert resolve_tui_theme("no-such-theme").name == "default"


def test_build_sidebar_snapshot_contains_runtime_details() -> None:
    snapshot = build_sidebar_snapshot(
        _DummySession(),
        retry_state="retry-1",
        last_tool_error="bash: timeout",
        last_provider_error="400 Bad Request",
    )

    assert snapshot["model"] == "openrouter/google/gemma-4-31b-it:free"
    assert snapshot["thinking"] == "medium"
    assert snapshot["cwd"] == "/tmp/project"
    assert snapshot["contextPercent"] == 12.5
    assert snapshot["queueSteer"] == 2
    assert snapshot["queueFollow"] == 1
    assert snapshot["queueTotal"] == 3
    assert snapshot["tokenInput"] == 100
    assert snapshot["tokenOutput"] == 50
    assert snapshot["tokenTotal"] == 165
    assert snapshot["cost"] == 0.0123
    assert snapshot["lastToolError"] == "bash: timeout"
    assert snapshot["lastProviderError"] == "400 Bad Request"
