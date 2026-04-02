from __future__ import annotations

from typing import Any

import httpx

from .base import ChatResult, ProviderAdapter


class AnthropicAdapter(ProviderAdapter):
    name = "anthropic"

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
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
            "max_tokens": 4096,
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

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post("https://api.anthropic.com/v1/messages", json=payload, headers=req_headers)
            resp.raise_for_status()
            data = resp.json()

        text = "".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
        usage = data.get("usage") or {}
        stop_reason = data.get("stop_reason")
        return ChatResult(text=text, raw=data, usage=usage, stop_reason=stop_reason)
