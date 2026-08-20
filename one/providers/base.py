from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


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
        on_delta: Callable[[str], None] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        raise NotImplementedError

    async def list_models(self, api_key: str, headers: dict[str, str] | None = None) -> list[str] | None:
        """Return the provider's model ids, or None when no list endpoint exists.

        Providers without a public models-list endpoint (e.g. Anthropic)
        return None; callers may fall back to a minimal-chat validation.
        """
        raise NotImplementedError
