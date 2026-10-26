"""Pure command-palette action catalog for the TUI."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandAction:
    """An action presented by a command palette, independent of any UI toolkit."""

    id: str
    title: str
    description: str
    category: str
    shortcut: str = ""
    command: str = ""
    aliases: tuple[str, ...] = ()
    enabled: bool = True
    disabled_reason: str = ""


def command_actions(state: dict) -> list[CommandAction]:
    """Return the ordered palette actions for the supplied session state."""
    streaming = bool(state.get("streaming", False))
    cooperation = bool(state.get("cooperation", False))
    subagents = bool(state.get("subagents", False))
    subagents_hard_disabled = bool(state.get("subagents_hard_disabled", False))
    details = bool(state.get("details", False))
    supports_sessions = bool(state.get("supports_sessions", True))

    streaming_reason = "Unavailable while the agent is streaming."
    sessions_reason = "Sessions are unavailable in this mode."
    subagents_reason = "Subagents are unavailable for this session because it was started with --no-subagents."

    return [
        CommandAction("sessions", "Sessions", "Browse and switch sessions.", "Session", "", "/sessions", ("sesje",), supports_sessions, sessions_reason if not supports_sessions else ""),
        CommandAction("new", "New session", "Start a new session.", "Session", "", "/new", ("nowa", "nowy"), not streaming, streaming_reason if streaming else ""),
        CommandAction("stats", "Session statistics", "Show statistics for this session.", "Session", "", "/stats", ("statystyki",)),
        CommandAction("history", "Session history", "Show this session's history.", "Session", "", "/history", ("historia",)),
        CommandAction("model", "Select model", "Choose the model used by the agent.", "Model & agent", "Ctrl+K", "/model", ("model",), not streaming, streaming_reason if streaming else ""),
        CommandAction("preset", "Select preset", "Choose the active capability preset.", "Model & agent", "Shift+Tab", "", ("preset", "profil"), not streaming, streaming_reason if streaming else ""),
        CommandAction("thinking", "Thinking level", "Set the agent thinking level.", "Model & agent", "", "/thinking", ("myslenie", "myślenie")),
        CommandAction("temperature", "Temperature", "Set the response temperature.", "Model & agent", "", "/temperature", ("temperatura",)),
        CommandAction("agents", "Agent status", "Inspect the agent and its current state.", "Model & agent", "", "", ("agenci",)),
        CommandAction(
            "cooperation",
            "Disable cooperation" if cooperation else "Enable cooperation",
            "Turn cooperation OFF." if cooperation else "Turn cooperation ON.",
            "Model & agent",
            "ON" if cooperation else "OFF",
            "/cooperation",
            ("wspolpraca", "współpraca"),
        ),
        CommandAction(
            "subagents",
            "Disable subagents" if subagents else "Enable subagents",
            "Turn subagents OFF." if subagents else "Turn subagents ON.",
            "Model & agent",
            "ON" if subagents else "OFF",
            "",
            ("podagenci",),
            not subagents_hard_disabled,
            subagents_reason if subagents_hard_disabled else "",
        ),
        CommandAction(
            "details",
            "Disable details" if details else "Enable details",
            "Turn details OFF." if details else "Turn details ON.",
            "Tools & access",
            "ON" if details else "OFF",
            "",
            ("szczegoly", "szczegóły"),
        ),
        CommandAction("mcp", "MCP servers", "Manage MCP server connections.", "Tools & access", "", "/mcp", ("mcp", "serwery")),
        CommandAction("theme", "Theme", "Choose the color theme.", "Appearance", "", "", ("motyw", "theme")),
        CommandAction("layout", "Layout", "Choose the TUI layout.", "Appearance", "", "/layout", ("uklad", "układ")),
        CommandAction("sidebar", "Sidebar", "Show or hide the sidebar.", "Appearance", "", "/sidebar", ("panel", "boczny")),
        CommandAction("shortcuts", "Keyboard shortcuts", "Show all keyboard shortcuts.", "Help", "", "", ("skroty", "skróty")),
        CommandAction("help", "Help", "Show command help.", "Help", "Ctrl+F1", "/help", ("pomoc",)),
        CommandAction("clear", "Clear conversation", "Clear the visible conversation.", "Help", "Ctrl+L", "", ("wyczysc", "wyczyść")),
        CommandAction("quit", "Quit", "Exit ONE.", "Help", "Ctrl+Q", "", ("wyjscie", "wyjście", "zakoncz", "zakończ")),
    ]


def search_actions(actions: list[CommandAction], query: str) -> list[CommandAction]:
    """Filter actions by normalized tokens and rank exact and prefix matches first."""
    normalized_query = _normalize(query)
    if not normalized_query:
        return list(actions)

    query_tokens = normalized_query.split()
    matches: list[tuple[int, int, CommandAction]] = []
    for index, action in enumerate(actions):
        fields = (action.title, action.description, action.command, action.shortcut, *action.aliases)
        normalized_fields = tuple(_normalize(field) for field in fields if field)
        field_tokens = tuple(token for field in normalized_fields for token in field.split())
        if not all(any(candidate.startswith(token) for candidate in field_tokens) for token in query_tokens):
            continue
        exact = normalized_query in (_normalize(action.command), _normalize(action.title))
        prefix = any(field.startswith(normalized_query) for field in normalized_fields)
        matches.append((0 if exact else 1 if prefix else 2, index, action))

    matches.sort(key=lambda match: (match[0], match[1]))
    return [action for _, _, action in matches]


def _normalize(value: str) -> str:
    """Case-fold text and make Ctrl+K, ctrl k, and control-k equivalent."""
    value = value.casefold().replace("control", "ctrl")
    return " ".join(re.findall(r"[\w/]+", value))


__all__ = ["CommandAction", "command_actions", "search_actions"]
