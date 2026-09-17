from __future__ import annotations

from one.core.settings_manager import SettingsManager


def test_default_subagents_has_timeout_sec_1800() -> None:
    """DEFAULT_SETTINGS subagents dict includes timeoutSec=1800."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert "subagents" in DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["subagents"]["timeoutSec"] == 1800


def test_get_subagents_timeout_sec_default() -> None:
    """With default settings, get_subagents_timeout_sec returns 1800."""
    settings = SettingsManager.in_memory()
    assert settings.get_subagents_timeout_sec() == 1800


def test_get_subagents_timeout_sec_custom() -> None:
    """Custom timeoutSec in subagents is respected."""
    settings = SettingsManager.in_memory(initial={"subagents": {"timeoutSec": 60}})
    assert settings.get_subagents_timeout_sec() == 60


def test_get_subagents_timeout_sec_invalid_fallback() -> None:
    """Invalid timeoutSec values fall back to 1800."""
    settings = SettingsManager.in_memory(initial={"subagents": {"timeoutSec": -1}})
    assert settings.get_subagents_timeout_sec() == 1800
    settings2 = SettingsManager.in_memory(initial={"subagents": {"timeoutSec": "bad"}})
    assert settings2.get_subagents_timeout_sec() == 1800


def test_tool_output_pruning_defaults_and_opt_out() -> None:
    settings = SettingsManager.in_memory()
    assert settings.get_tool_output_pruning_enabled() is True
    assert settings.get_tool_output_pruning_recent_tokens() == 8192
    assert settings.get_tool_output_pruning_min_result_tokens() == 2048

    disabled = SettingsManager.in_memory({"toolOutputPruning": {"enabled": False}})
    assert disabled.get_tool_output_pruning_enabled() is False
    invalid = SettingsManager.in_memory({"toolOutputPruning": None})
    assert invalid.get_tool_output_pruning_enabled() is True
