from __future__ import annotations

import json
from typing import Any, Callable

import httpx

from .base import ChatResult, ProviderAdapter


class OpenAICompatibleAdapter(ProviderAdapter):
    def __init__(
        self,
        name: str,
        base_url: str,
        endpoint: str = "/v1/chat/completions",
        *,
        supports_reasoning_effort: bool = True,
        default_temperature: float | None = 0.1,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint
        self.supports_reasoning_effort = supports_reasoning_effort
        self.default_temperature = default_temperature

    def _build_payload(self, model: str, messages: list[dict[str, Any]], thinking_level: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if self.default_temperature is not None:
            payload["temperature"] = self.default_temperature
        if self.supports_reasoning_effort:
            payload["reasoning_effort"] = "high" if thinking_level in {"high", "xhigh"} else "medium"
        return payload

    def with_base_url(self, base_url: str) -> "OpenAICompatibleAdapter":
        return OpenAICompatibleAdapter(
            self.name,
            base_url,
            endpoint=self.endpoint,
            supports_reasoning_effort=self.supports_reasoning_effort,
            default_temperature=self.default_temperature,
        )

    def _build_headers(self, api_key: str, headers: dict[str, str] | None = None) -> dict[str, str]:
        req_headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            req_headers["Authorization"] = f"Bearer {api_key}"
        if headers:
            req_headers.update(headers)
        return req_headers

    def _models_url(self) -> str:
        """OpenAI-compatible models endpoint.

        Bases that already include ``/v1`` (e.g. Ollama local
        ``http://localhost:11434/v1``) append ``/models``; bases without it
        (e.g. OpenRouter ``https://openrouter.ai/api``) use ``/v1/models``.
        """
        base = self.base_url
        if base.endswith("/v1"):
            return f"{base}/models"
        return f"{base}/v1/models"

    async def _fetch_models_payload(self, api_key: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        req_headers = self._build_headers(api_key, headers)
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(self._models_url(), headers=req_headers)
            if resp.is_error:
                body = ""
                try:
                    parsed = resp.json()
                    body = str(parsed.get("error") or parsed)
                except Exception:
                    body = resp.text[:1000]
                raise RuntimeError(f"{self.name} API error {resp.status_code}: {body}")
            return resp.json()

    async def list_models(self, api_key: str, headers: dict[str, str] | None = None) -> list[str] | None:
        data = await self._fetch_models_payload(api_key, headers)
        return [m.get("id") for m in data.get("data", []) if m.get("id")]

    async def list_models_detailed(self, api_key: str, headers: dict[str, str] | None = None) -> list[dict[str, Any]] | None:
        """Map entries to {"id", "contextWindow"}; OpenRouter exposes context_length."""
        data = await self._fetch_models_payload(api_key, headers)
        out: list[dict[str, Any]] = []
        for m in data.get("data", []):
            mid = m.get("id")
            if not mid:
                continue
            ctx = m.get("context_length")
            out.append({"id": mid, "contextWindow": ctx if isinstance(ctx, int) and ctx > 0 else None})
        return out

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
    ) -> ChatResult:
        payload = self._build_payload(model, messages, thinking_level)
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        use_stream = callable(on_delta)
        if use_stream:
            payload["stream"] = True
        req_headers = self._build_headers(api_key, headers)
        if not use_stream:
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                resp = await client.post(f"{self.base_url}{self.endpoint}", json=payload, headers=req_headers)
                if resp.is_error:
                    body = ""
                    try:
                        parsed = resp.json()
                        body = str(parsed.get("error") or parsed)
                    except Exception:
                        body = resp.text[:1000]
                    raise RuntimeError(f"{self.name} API error {resp.status_code}: {body}")
                data = resp.json()

            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message", {})
            thinking = msg.get("reasoning_content") or ""
            content = msg.get("content") or ""
            text = (thinking + "\n\n" + content) if thinking else content
            usage = data.get("usage") or {}
            stop = choice.get("finish_reason")
            return ChatResult(text=text, raw=data, usage=usage, stop_reason=stop)

        text_parts: list[str] = []
        usage: dict[str, Any] = {}
        stop_reason: str | None = None
        raw_last: dict[str, Any] = {}

        async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
            async with client.stream("POST", f"{self.base_url}{self.endpoint}", json=payload, headers=req_headers) as resp:
                if resp.is_error:
                    body = (await resp.aread()).decode("utf-8", errors="ignore")[:1000]
                    raise RuntimeError(f"{self.name} API error {resp.status_code}: {body}")

                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except Exception:
                        continue
                    raw_last = chunk
                    if isinstance(chunk.get("usage"), dict):
                        usage = chunk["usage"]
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        text_parts.append(str(piece))
                        try:
                            on_delta(str(piece))
                        except Exception:
                            pass
                    piece_reasoning = delta.get("reasoning_content")
                    if piece_reasoning:
                        text_parts.append(str(piece_reasoning))
                        try:
                            reasoning_str = str(piece_reasoning)
                            # Add leading space if token doesn't start with whitespace (llama.cpp tokenization)
                            if reasoning_str and not reasoning_str[0].isspace() and len(text_parts) > 1:
                                reasoning_str = " " + reasoning_str
                            if on_thinking_delta:
                                on_thinking_delta(reasoning_str)
                            elif on_delta:
                                on_delta(reasoning_str)
                        except Exception:
                            pass
                    if choice.get("finish_reason"):
                        stop_reason = choice.get("finish_reason")

        full_text = "".join(text_parts)
        return ChatResult(text=full_text, raw=raw_last, usage=usage, stop_reason=stop_reason)
