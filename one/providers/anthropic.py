from __future__ import annotations

import json
from typing import Any, Callable

import httpx

from .base import ChatResult, ProviderAdapter


class AnthropicAdapter(ProviderAdapter):
    name = "anthropic"

    async def list_models(self, api_key: str, headers: dict[str, str] | None = None) -> list[str] | None:
        # Anthropic has no public models-list endpoint; callers fall back to
        # minimal-chat validation.
        return None

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
        content_messages = []
        system = None
        for m in messages:
            if m.get("role") == "system":
                system = m.get("content", "")
                continue
            content_messages.append({"role": m.get("role"), "content": m.get("content", "")})

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or 4096,
            "messages": content_messages,
        }
        if system:
            payload["system"] = system
        req_headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if headers:
            req_headers.update(headers)

        use_stream = callable(on_delta)
        if not use_stream:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post("https://api.anthropic.com/v1/messages", json=payload, headers=req_headers)
                resp.raise_for_status()
                data = resp.json()

            text = "".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
            usage = data.get("usage") or {}
            stop_reason = data.get("stop_reason")
            return ChatResult(text=text, raw=data, usage=usage, stop_reason=stop_reason)

        payload["stream"] = True
        text_parts: list[str] = []
        usage: dict[str, Any] = {}
        stop_reason: str | None = None
        raw_last: dict[str, Any] = {}

        async with httpx.AsyncClient(timeout=180) as client:
            async with client.stream("POST", "https://api.anthropic.com/v1/messages", json=payload, headers=req_headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str or data_str == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data_str)
                    except Exception:
                        continue
                    raw_last = chunk
                    ctype = chunk.get("type")
                    if ctype == "content_block_delta":
                        delta = (chunk.get("delta") or {}).get("text")
                        if delta:
                            delta_s = str(delta)
                            text_parts.append(delta_s)
                            try:
                                on_delta(delta_s)
                            except Exception:
                                pass
                    if ctype == "message_start":
                        msg_usage = (chunk.get("message") or {}).get("usage")
                        if isinstance(msg_usage, dict):
                            usage.update(msg_usage)
                    if ctype == "message_delta":
                        delta_obj = chunk.get("delta") or {}
                        if isinstance(delta_obj, dict) and delta_obj.get("stop_reason"):
                            stop_reason = delta_obj.get("stop_reason")
                        msg_usage = chunk.get("usage")
                        if isinstance(msg_usage, dict):
                            usage.update(msg_usage)

        return ChatResult(text="".join(text_parts), raw=raw_last, usage=usage, stop_reason=stop_reason)
