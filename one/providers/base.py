# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict


class NativeToolCall(TypedDict):
    """Provider-neutral function call.

    ``arguments`` is a decoded JSON object when the provider supplied valid
    JSON, otherwise the original argument string.  Keeping malformed input
    explicit lets the session reject it without guessing or executing it.
    """

    id: str
    name: str
    arguments: dict[str, Any] | str


NativeToolCallKey = tuple[int, int]


def native_tool_replay_pairs(
    messages: list[dict[str, Any]],
) -> tuple[dict[NativeToolCallKey, int], dict[int, NativeToolCallKey]]:
    """Pair durable native calls and outputs in transcript order.

    A call is eligible only when it has the complete normalized shape needed by
    every native protocol.  Repeated ids intentionally consume in FIFO order.
    This lets adapters leave malformed calls and orphaned outputs as ordinary
    conversation, rather than creating invalid provider protocol items.
    """
    pending: dict[str, list[NativeToolCallKey]] = {}
    calls_to_outputs: dict[NativeToolCallKey, int] = {}
    outputs_to_calls: dict[int, NativeToolCallKey] = {}
    for message_index, message in enumerate(messages):
        calls = message.get("_nativeToolCalls")
        if message.get("role") == "assistant" and isinstance(calls, list):
            for call_index, call in enumerate(calls):
                if not isinstance(call, dict):
                    continue
                call_id = call.get("id")
                name = call.get("name")
                if (
                    isinstance(call_id, str)
                    and call_id.strip()
                    and isinstance(name, str)
                    and name.strip()
                    and isinstance(call.get("arguments"), dict)
                ):
                    pending.setdefault(call_id, []).append((message_index, call_index))

        output_id = message.get("_nativeToolCallId")
        if not isinstance(output_id, str) or not output_id.strip():
            continue
        candidates = pending.get(output_id)
        if not candidates:
            continue
        call_key = candidates.pop(0)
        calls_to_outputs[call_key] = message_index
        outputs_to_calls[message_index] = call_key
    return calls_to_outputs, outputs_to_calls


@dataclass
class ChatResult:
    text: str
    raw: dict[str, Any]
    usage: dict[str, Any]
    stop_reason: str | None = None
    # Additive response classification. Reasoning remains separate from text.
    had_thinking: bool = False
    # Indicates that the provider response contained a native tool call.
    had_native_tool_call: bool = False
    native_tool_calls: list[NativeToolCall] | None = None


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
        on_thinking_delta: Callable[[str], None] | None = None,
        max_tokens: int | None = None,
        images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        raise NotImplementedError

    async def list_models(self, api_key: str, headers: dict[str, str] | None = None) -> list[str] | None:
        """Return the provider's model ids, or None when no list endpoint exists.

        Providers without a public models-list endpoint (e.g. Anthropic)
        return None; callers may fall back to a minimal-chat validation.
        """
        raise NotImplementedError

    async def list_models_detailed(self, api_key: str, headers: dict[str, str] | None = None) -> list[dict[str, Any]] | None:
        """Return model entries as ``{"id": str, "contextWindow": int | None}``.

        Default implementation delegates to :meth:`list_models` with unknown
        context windows; adapters whose list endpoints expose a context length
        (OpenRouter ``context_length``, Gemini ``inputTokenLimit``) override it.
        """
        models = await self.list_models(api_key, headers)
        if models is None:
            return None
        return [{"id": m, "contextWindow": None} for m in models]
