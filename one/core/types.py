# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ThinkingLevel = Literal["off", "on", "minimal", "low", "medium", "high", "xhigh"]


@dataclass
class ModelInfo:
    provider: str
    id: str
    reasoning: bool = True
    context_window: int | None = None
    base_url: str | None = None
    tool_parser: list[dict[str, Any]] | None = None
    input_image: bool = False  # True when the model explicitly supports image input
    # Limits are deliberately separate.  ``context_window`` remains the legacy
    # total-context field; it is never repurposed as an input budget.
    input_limit: int | None = None
    output_limit: int | None = None
    effective_input_budget: int | None = None
    max_context_window: int | None = None
    server_context_window: int | None = None
    metadata_source: str | None = None
    metadata_updated_at: int | None = None
    default_context_window: int | None = None
    default_input_budget: int | None = None
    # Per-field provenance keeps a catalogue refresh from erasing a user or
    # endpoint value when only some capacity fields came from the catalogue.
    manual_fields: frozenset[str] = field(default_factory=frozenset)
    catalog_fields: frozenset[str] = field(default_factory=frozenset)
    catalog_baseline: dict[str, int | None] = field(default_factory=dict)
    catalog_values: dict[str, int] = field(default_factory=dict)
    endpoint_fields: frozenset[str] = field(default_factory=frozenset)


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class ToolResult:
    tool_call_id: str
    content: list[dict[str, Any]]
    is_error: bool = False
    details: Any = None


@dataclass
class AgentMessage:
    role: str
    content: Any
    provider: str | None = None
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str | None = None
    timestamp: int | None = None
