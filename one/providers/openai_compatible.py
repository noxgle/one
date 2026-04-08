from __future__ import annotations

from typing import Any

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

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
    ) -> ChatResult:
        payload = self._build_payload(model, messages, thinking_level)
        req_headers = self._build_headers(api_key, headers)

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
        text = msg.get("content") or ""
        usage = data.get("usage") or {}
        stop = choice.get("finish_reason")
        return ChatResult(text=text, raw=data, usage=usage, stop_reason=stop)
