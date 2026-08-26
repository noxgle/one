from __future__ import annotations

import pytest

from one.core.settings_manager import SettingsManager


def test_get_default_mode_is_tui() -> None:
    """Default mode should be 'tui' when no custom value is set."""
    settings = SettingsManager.in_memory()
    assert settings.get_default_mode() == "tui"


def test_get_default_mode_override_cli() -> None:
    """When defaultMode is set to 'cli', get_default_mode returns 'cli'."""
    settings = SettingsManager.in_memory(initial={"defaultMode": "cli"})
    assert settings.get_default_mode() == "cli"


def test_get_default_mode_invalid_falls_back() -> None:
    """Invalid defaultMode values fall back to 'tui'."""
    settings = SettingsManager.in_memory(initial={"defaultMode": "nope"})
    assert settings.get_default_mode() == "tui"


def test_get_default_mode_case_insensitive() -> None:
    """defaultMode is case-insensitive: 'TUI' and 'CLI' work."""
    settings = SettingsManager.in_memory(initial={"defaultMode": "TUI"})
    assert settings.get_default_mode() == "tui"
    settings_cli = SettingsManager.in_memory(initial={"defaultMode": "CLI"})
    assert settings_cli.get_default_mode() == "cli"
