from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ChatResult:
    text: str
    raw: dict[str, Any]
    usage: dict[str, Any]
    stop_reason: str | None = None


class ProviderAdapter:
    name: str

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
    ) -> ChatResult:
        raise NotImplementedError
