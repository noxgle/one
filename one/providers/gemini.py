from __future__ import annotations

from typing import Any

import httpx

from .base import ChatResult, ProviderAdapter


class GeminiAdapter(ProviderAdapter):
    name = "gemini"

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
    ) -> ChatResult:
        contents = []
        for m in messages:
            if m.get("role") == "system":
                continue
            role = "user" if m.get("role") == "user" else "model"
            contents.append({"role": role, "parts": [{"text": str(m.get("content", ""))}]})

        payload = {
            "contents": contents,
            "generationConfig": {"temperature": 0.1},
        }

        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        )
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, json=payload, headers=h)
            resp.raise_for_status()
            data = resp.json()

        candidates = data.get("candidates") or []
        text = ""
        if candidates:
            for part in (candidates[0].get("content", {}).get("parts") or []):
                text += part.get("text", "")
        usage = data.get("usageMetadata") or {}
        stop = candidates[0].get("finishReason") if candidates else None
        return ChatResult(text=text, raw=data, usage=usage, stop_reason=stop)
