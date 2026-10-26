# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from one.modes.tui_commands import CommandAction, command_actions, search_actions


def test_command_action_is_frozen() -> None:
    action = CommandAction("help", "Help", "Show help.", "Help")

    with pytest.raises(FrozenInstanceError):
        action.title = "Pomoc"  # type: ignore[misc]


def test_catalog_contains_ordered_actions_and_categories() -> None:
    actions = command_actions({})

    assert [action.id for action in actions] == [
        "sessions", "new", "stats", "history", "model", "preset", "thinking", "temperature", "agents",
        "cooperation", "subagents", "details", "mcp", "theme", "layout", "sidebar", "shortcuts", "help",
        "clear", "quit",
    ]
    assert [action.category for action in actions] == sorted(
        (action.category for action in actions),
        key=("Session", "Model & agent", "Tools & access", "Appearance", "Help").index,
    )
    assert {action.category for action in actions} == {"Session", "Model & agent", "Tools & access", "Appearance", "Help"}
    assert {action.command for action in actions if action.command} == {
        "/sessions", "/new", "/stats", "/history", "/model", "/thinking", "/temperature", "/cooperation", "/mcp", "/layout", "/sidebar", "/help",
    }


def test_catalog_state_controls_disabled_and_toggle_actions() -> None:
    actions = {action.id: action for action in command_actions({
        "streaming": True,
        "cooperation": True,
        "subagents": True,
        "subagents_hard_disabled": True,
        "details": True,
        "supports_sessions": False,
    })}

    for action_id in ("new", "model", "preset"):
        assert actions[action_id].enabled is False
        assert actions[action_id].disabled_reason == "Unavailable while the agent is streaming."
    assert actions["sessions"].enabled is False
    assert actions["sessions"].disabled_reason == "Sessions are unavailable in this mode."
    assert actions["subagents"].enabled is False
    assert "--no-subagents" in actions["subagents"].disabled_reason
    assert (actions["cooperation"].title, actions["cooperation"].shortcut) == ("Disable cooperation", "ON")
    assert (actions["subagents"].title, actions["subagents"].shortcut) == ("Disable subagents", "ON")
    assert (actions["details"].title, actions["details"].shortcut) == ("Disable details", "ON")


def test_search_preserves_catalog_for_blank_query_and_matches_polish_aliases() -> None:
    actions = command_actions({})

    assert search_actions(actions, "  ") == actions
    assert [action.id for action in search_actions(actions, "sesje")] == ["sessions"]
    assert [action.id for action in search_actions(actions, "motyw")] == ["theme"]


def test_search_normalizes_shortcuts_and_ranks_exact_then_prefix_then_stable() -> None:
    actions = [
        CommandAction("description", "Alpha", "foo ctrl k", "Help"),
        CommandAction("shortcut", "Beta", "Other", "Help", "Ctrl+K"),
        CommandAction("title", "Ctrl K chooser", "Other", "Help"),
        CommandAction("later", "Control key", "Other", "Help"),
    ]

    assert [action.id for action in search_actions(actions, "control-k")] == [
        "shortcut", "title", "later", "description"
    ]
    assert [action.id for action in search_actions(actions, "foo")] == ["description"]
    assert search_actions(actions, "missing") == []
