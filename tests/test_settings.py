from __future__ import annotations

import pytest

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


# ── Default settings: effective getter values ───────────────────────────────


def test_default_retry_enabled_is_true() -> None:
    """retry.enabled defaults to True in DEFAULT_SETTINGS and via getter."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["retry"]["enabled"] is True
    settings = SettingsManager.in_memory()
    assert settings.get_retry_enabled() is True


def test_default_retry_max_retries_is_zero() -> None:
    """retry.maxRetries defaults to 0 (no retry attempts)."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["retry"]["maxRetries"] == 0
    settings = SettingsManager.in_memory()
    assert settings.get_retry_settings()["maxRetries"] == 0


def test_default_retry_mode_is_on() -> None:
    """retry.mode defaults to 'on'."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["retry"]["mode"] == "on"
    settings = SettingsManager.in_memory()
    assert settings.get_retry_mode() == "on"


def test_default_retry_base_delay_ms() -> None:
    """retry.baseDelayMs defaults to 1500."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["retry"]["baseDelayMs"] == 1500
    settings = SettingsManager.in_memory()
    assert settings.get_retry_settings()["baseDelayMs"] == 1500


def test_default_retry_max_delay_ms() -> None:
    """retry.maxDelayMs defaults to 20000."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["retry"]["maxDelayMs"] == 20000
    settings = SettingsManager.in_memory()
    assert settings.get_retry_settings()["maxDelayMs"] == 20000


def test_default_image_auto_resize_is_true() -> None:
    """image.autoResize defaults to True."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["image"]["autoResize"] is True
    settings = SettingsManager.in_memory()
    assert settings.get_image_auto_resize() is True


def test_default_image_block_images_is_false() -> None:
    """image.blockImages defaults to False."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["image"]["blockImages"] is False
    settings = SettingsManager.in_memory()
    assert settings.get_block_images() is False


def test_default_theme_is_hacker() -> None:
    """theme defaults to 'hacker'."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["theme"] == "hacker"
    settings = SettingsManager.in_memory()
    assert settings.get_theme() == "hacker"


def test_default_quiet_startup_is_false() -> None:
    """quietStartup defaults to False."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["quietStartup"] is False
    settings = SettingsManager.in_memory()
    assert settings.get_quiet_startup() is False


def test_default_steering_mode_is_interrupt() -> None:
    """steeringMode defaults to 'interrupt'."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["steeringMode"] == "interrupt"
    settings = SettingsManager.in_memory()
    assert settings.get_steering_mode() == "interrupt"


def test_default_follow_up_mode_is_queue() -> None:
    """followUpMode defaults to 'queue'."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["followUpMode"] == "queue"
    settings = SettingsManager.in_memory()
    assert settings.get_follow_up_mode() == "queue"


def test_default_transport_is_http() -> None:
    """transport defaults to 'http'."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["transport"] == "http"
    settings = SettingsManager.in_memory()
    assert settings.get_transport() == "http"


def test_default_thinking_budgets_is_empty_dict() -> None:
    """thinkingBudgets defaults to empty dict."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["thinkingBudgets"] == {}
    settings = SettingsManager.in_memory()
    assert settings.get_thinking_budgets() == {}


def test_default_shell_command_prefix_is_none() -> None:
    """shellCommandPrefix defaults to None."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["shellCommandPrefix"] is None
    settings = SettingsManager.in_memory()
    assert settings.get_shell_command_prefix() is None


def test_default_packages_is_empty_list() -> None:
    """packages defaults to empty list."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["packages"] == []
    settings = SettingsManager.in_memory()
    assert settings.get_packages() == []


def test_default_tools_max_steps_is_100() -> None:
    """tools.maxSteps defaults to 100."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["tools"]["maxSteps"] == 100
    settings = SettingsManager.in_memory()
    assert settings.get_tool_max_steps() == 100


def test_default_tools_timeout_sec_preserved() -> None:
    """tools.timeoutSec stays at 30."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["tools"]["timeoutSec"] == 30
    settings = SettingsManager.in_memory()
    assert settings.get_tool_timeout_sec() == 30


def test_default_tools_approval_false() -> None:
    """tools.approval defaults to False."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["tools"]["approval"] is False
    settings = SettingsManager.in_memory()
    assert settings.get_tool_approval() is False


def test_default_bash_show_output_is_false() -> None:
    """bash.showOutput defaults to False."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["bash"]["showOutput"] is False
    settings = SettingsManager.in_memory()
    assert settings.get_bash_show_output() is False


def test_default_getters_fall_back_when_nested_settings_are_null() -> None:
    """Malformed nested settings use the current defaults rather than stale literals."""
    settings = SettingsManager.in_memory(
        {
            "compaction": None,
            "theme": None,
            "tools": None,
            "bash": None,
        }
    )

    assert settings.get_compaction_threshold_percent() == 80
    assert settings.get_theme() == "hacker"
    assert settings.get_tool_max_steps() == 100
    assert settings.get_bash_show_output() is False


# ── Retry enabled/maxRetries semantics ─────────────────────────────────────


def test_retry_enabled_true_with_max_retries_zero_no_retries() -> None:
    """retry.enabled=True AND maxRetries=0 → get_retry_enabled is True but
    effective retry loop will not retry because attempt(0) < maxRetries(0) is False."""
    settings = SettingsManager.in_memory()
    assert settings.get_retry_enabled() is True
    assert settings.get_retry_settings()["maxRetries"] == 0
    # Simulate the retry-loop guard: retries_enabled and attempt < max_retries
    retries_enabled = settings.get_retry_settings().get("enabled", True)
    max_retries = settings.get_retry_settings().get("maxRetries", 3)
    will_retry = retries_enabled and 0 < max_retries  # attempt 0
    assert will_retry is False  # 0 < 0 is False


def test_retry_enabled_false_no_retries_even_with_max_retries() -> None:
    """retry.enabled=False → no retries regardless of maxRetries value."""
    settings = SettingsManager.in_memory(initial={"retry": {"enabled": False, "maxRetries": 3}})
    assert settings.get_retry_settings().get("enabled", True) is False
    retries_enabled = settings.get_retry_settings().get("enabled", True)
    max_retries = settings.get_retry_settings().get("maxRetries", 3)
    will_retry = retries_enabled and 0 < max_retries
    assert will_retry is False


def test_retry_enabled_true_with_positive_max_retries_allows_retries() -> None:
    """retry.enabled=True AND maxRetries=3 → retries allowed up to 3 attempts."""
    settings = SettingsManager.in_memory(initial={"retry": {"enabled": True, "maxRetries": 3}})
    retries_enabled = settings.get_retry_settings().get("enabled", True)
    max_retries = settings.get_retry_settings().get("maxRetries", 3)
    # attempt 0 < 3 → retry
    assert (retries_enabled and 0 < max_retries) is True
    # attempt 2 < 3 → retry
    assert (retries_enabled and 2 < max_retries) is True
    # attempt 3 < 3 → no more retries
    assert (retries_enabled and 3 < max_retries) is False


def test_retry_mode_on_gets_enabled_true() -> None:
    """Default mode 'on' → get_retry_enabled() is True."""
    settings = SettingsManager.in_memory()
    assert settings.get_retry_mode() == "on"
    assert settings.get_retry_enabled() is True


def test_retry_mode_off_gets_enabled_false() -> None:
    """Mode 'off' → get_retry_enabled() is False."""
    settings = SettingsManager.in_memory(initial={"retry": {"mode": "off"}})
    assert settings.get_retry_mode() == "off"
    assert settings.get_retry_enabled() is False


def test_retry_mode_unlimited_gets_enabled_true() -> None:
    """Mode 'unlimited' → get_retry_enabled() is True."""
    settings = SettingsManager.in_memory(initial={"retry": {"mode": "unlimited"}})
    assert settings.get_retry_mode() == "unlimited"
    assert settings.get_retry_enabled() is True


@pytest.mark.parametrize(
    ("retry", "expected"),
    [
        ({}, True),  # Older configurations omitted retry.enabled.
        ({"enabled": True, "mode": "on"}, True),
        ({"enabled": True, "mode": "unlimited"}, True),
        ({"enabled": False, "mode": "on"}, False),
        ({"enabled": True, "mode": "off"}, False),
        (None, True),
    ],
)
def test_retry_enabled_honors_legacy_flag_and_mode(retry: dict[str, object] | None, expected: bool) -> None:
    settings = SettingsManager.in_memory(initial={"retry": retry})
    assert settings.get_retry_enabled() is expected


def test_retry_disabled_via_enabled_field_no_retry_in_loop() -> None:
    """Explicit retry.enabled=False prevents retries even with positive maxRetries."""
    settings = SettingsManager.in_memory(
        initial={"retry": {"enabled": False, "maxRetries": 5, "mode": "on"}}
    )
    retries_enabled = settings.get_retry_settings().get("enabled", True)
    max_retries = settings.get_retry_settings().get("maxRetries", 3)
    will_retry = retries_enabled and 0 < max_retries
    assert will_retry is False


def test_user_settings_override_defaults_deep_merge() -> None:
    """User/project settings override defaults through deep merge."""
    settings = SettingsManager.in_memory(
        initial={
            "theme": "dracula",
            "bash": {"showOutput": True},
            "tools": {"maxSteps": 50},
            "retry": {"enabled": False, "maxRetries": 5},
            "image": {"autoResize": False},
        }
    )
    assert settings.get_theme() == "dracula"
    assert settings.get_bash_show_output() is True
    assert settings.get_tool_max_steps() == 50
    assert settings.get_retry_settings()["enabled"] is False
    assert settings.get_retry_settings()["maxRetries"] == 5
    assert settings.get_image_auto_resize() is False
    # Unchanged defaults still present
    assert settings.get_retry_mode() == "on"
    assert settings.get_block_images() is False
    assert settings.get_tool_timeout_sec() == 30


def test_compaction_defaults_preserved() -> None:
    """Compaction defaults are unchanged: enabled=True, threshold=80."""
    from one.core.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["compaction"]["enabled"] is True
    assert DEFAULT_SETTINGS["compaction"]["thresholdPercent"] == 80
    settings = SettingsManager.in_memory()
    assert settings.get_compaction_settings()["enabled"] is True
    assert settings.get_compaction_threshold_percent() == 80


def test_get_default_tool_max_steps_default_is_hundred() -> None:
    """get_tool_max_steps() returns 100 with default settings."""
    settings = SettingsManager.in_memory()
    assert settings.get_tool_max_steps() == 100


def test_get_tool_max_steps_custom_override() -> None:
    """Custom maxSteps from initial settings is respected."""
    settings = SettingsManager.in_memory(initial={"tools": {"maxSteps": 25}})
    assert settings.get_tool_max_steps() == 25


def test_get_tool_max_steps_zero_means_unlimited() -> None:
    """maxSteps=0 is valid and means unlimited."""
    settings = SettingsManager.in_memory(initial={"tools": {"maxSteps": 0}})
    assert settings.get_tool_max_steps() == 0
