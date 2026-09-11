from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh"]


@dataclass
class ModelInfo:
    provider: str
    id: str
    reasoning: bool = True
    context_window: int | None = None
    base_url: str | None = None
    tool_parser: list[dict[str, Any]] | None = None
    input_image: bool = False  # True when the model explicitly supports image input


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
