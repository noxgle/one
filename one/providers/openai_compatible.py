from __future__ import annotations

from typing import Any

import httpx

from .base import ChatResult, ProviderAdapter


class OpenAICompatibleAdapter(ProviderAdapter):
    def __init__(self, name: str, base_url: str, endpoint: str = "/v1/chat/completions") -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
    ) -> ChatResult:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "reasoning_effort": "high" if thinking_level in {"high", "xhigh"} else "medium",
        }
        req_headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if headers:
            req_headers.update(headers)

        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.post(f"{self.base_url}{self.endpoint}", json=payload, headers=req_headers)
            resp.raise_for_status()
            data = resp.json()

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        text = msg.get("content") or ""
        usage = data.get("usage") or {}
        stop = choice.get("finish_reason")
        return ChatResult(text=text, raw=data, usage=usage, stop_reason=stop)
