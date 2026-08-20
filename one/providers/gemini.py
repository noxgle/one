from __future__ import annotations

import json
from typing import Any, Callable

import httpx

from .base import ChatResult, ProviderAdapter


class GeminiAdapter(ProviderAdapter):
    name = "gemini"

    async def list_models(self, api_key: str, headers: dict[str, str] | None = None) -> list[str] | None:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            if resp.is_error:
                body = resp.text[:1000]
                raise RuntimeError(f"gemini API error {resp.status_code}: {body}")
            data = resp.json()
        out: list[str] = []
        for m in data.get("models", []):
            name = m.get("name", "")
            methods = m.get("supportedGenerationMethods") or []
            if name and "generateContent" in methods:
                out.append(name.removeprefix("models/"))
        return out

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
        contents = []
        for m in messages:
            if m.get("role") == "system":
                continue
            role = "user" if m.get("role") == "user" else "model"
            contents.append({"role": role, "parts": [{"text": str(m.get("content", ""))}]})

        generation_config: dict[str, Any] = {"temperature": 0.1}
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
        payload = {
            "contents": contents,
            "generationConfig": generation_config,
        }

        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)

        use_stream = callable(on_delta)
        if not use_stream:
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

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse&key={api_key}"
        )
        text_parts: list[str] = []
        usage: dict[str, Any] = {}
        stop_reason: str | None = None
        raw_last: dict[str, Any] = {}
        async with httpx.AsyncClient(timeout=180) as client:
            async with client.stream("POST", url, json=payload, headers=h) as resp:
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
                    if isinstance(chunk.get("usageMetadata"), dict):
                        usage = chunk["usageMetadata"]
                    candidates = chunk.get("candidates") or []
                    if not candidates:
                        continue
                    c0 = candidates[0]
                    parts = (c0.get("content") or {}).get("parts") or []
                    piece = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))
                    if piece:
                        text_parts.append(piece)
                        try:
                            on_delta(piece)
                        except Exception:
                            pass
                    if c0.get("finishReason"):
                        stop_reason = c0.get("finishReason")

        return ChatResult(text="".join(text_parts), raw=raw_last, usage=usage, stop_reason=stop_reason)
