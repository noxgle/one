# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from pathlib import Path

import pytest

from one.core.settings_manager import SettingsManager


def _manager(settings: dict) -> SettingsManager:
    return SettingsManager.in_memory(settings)


def test_prompt_cache_defaults_are_all_off() -> None:
    m = _manager({})
    s = m.get_prompt_cache_settings("chatgpt")
    assert s == {
        "enabled": False,
        "ttl": "30m",
        "stableBreakpoint": True,
        "toolResultBreakpoints": False,
        "key": "off",
        "prewarm": False,
    }
    assert m.get_prompt_cache_enabled("chatgpt") is False


def test_prompt_cache_enabled_profile() -> None:
    m = _manager({"providerProfiles": {"chatgpt": {"promptCache": {"enabled": True, "ttl": "12h"}}}})
    s = m.get_prompt_cache_settings("chatgpt")
    assert s["enabled"] is True
    assert s["ttl"] == "12h"
    assert m.get_prompt_cache_enabled("chatgpt") is True


def test_prompt_cache_24h_ttl_rejected_for_gpt62() -> None:
    m = _manager({"providerProfiles": {"chatgpt": {"promptCache": {"ttl": "24h"}}}})
    with pytest.raises(ValueError, match="24h"):
        m.get_prompt_cache_settings("chatgpt")


def test_prompt_cache_invalid_ttl_rejected() -> None:
    m = _manager({"providerProfiles": {"chatgpt": {"promptCache": {"ttl": "1h"}}}})
    with pytest.raises(ValueError, match="30m"):
        m.get_prompt_cache_settings("chatgpt")


def test_prompt_cache_invalid_key_rejected() -> None:
    m = _manager({"providerProfiles": {"chatgpt": {"promptCache": {"key": "session"}}}})
    with pytest.raises(ValueError, match="off"):
        m.get_prompt_cache_settings("chatgpt")


def test_prompt_cache_non_bool_flag_rejected() -> None:
    m = _manager({"providerProfiles": {"chatgpt": {"promptCache": {"prewarm": "yes"}}}})
    with pytest.raises(ValueError, match="boolean"):
        m.get_prompt_cache_settings("chatgpt")


def test_prompt_cache_other_provider_ignored() -> None:
    m = _manager({"providerProfiles": {"openai": {"promptCache": {"enabled": True}}}})
    # chatgpt profile is untouched
    assert m.get_prompt_cache_enabled("chatgpt") is False
    assert m.get_prompt_cache_enabled("openai") is True


def test_prompt_cache_survives_reload(tmp_path: Path) -> None:
    import json

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "settings.json").write_text(json.dumps(
        {"providerProfiles": {"chatgpt": {"promptCache": {"enabled": True, "ttl": "12h"}}}}
    ))
    m2 = SettingsManager(str(tmp_path), str(agent_dir))
    s = m2.get_prompt_cache_settings("chatgpt")
    assert s["enabled"] is True
    assert s["ttl"] == "12h"