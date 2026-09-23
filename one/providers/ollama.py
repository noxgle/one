"""Native Ollama Cloud chat adapter.

Ollama's native API accepts ``think`` and returns trace text separately in
``message.thinking``.  This is deliberately separate from the local Ollama
OpenAI-compatible adapter so llama.cpp and local Ollama wire behavior do not
change.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx

from .base import ChatResult, ProviderAdapter

# 120 s is the direct-call/default finite transport watchdog. AgentSession
# overrides it with a longer value for managed streams so its meaningful-token
# idle timeout remains authoritative.
_IDLE_SSE_TIMEOUT = 120


class OllamaCloudAdapter(ProviderAdapter):
    name = "ollama-cloud"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.endpoint = "/api/chat"

    @staticmethod
    def _think_value(thinking_level: str) -> bool | str:
        # Ollama supports low, medium, and high. xhigh uses high because max is
        # not accepted by models such as GPT-OSS.
        return {
            "off": False,
            "minimal": "low",
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "high",
        }.get(thinking_level, False)

    def _build_payload(
        self, model: str, messages: list[dict[str, Any]], thinking_level: str,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "think": self._think_value(thinking_level),
        }
        if max_tokens is not None:
            payload["options"] = {"num_predict": max_tokens}
        return payload

    async def chat(
        self, api_key: str, model: str, messages: list[dict[str, Any]],
        thinking_level: str, headers: dict[str, str] | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        max_tokens: int | None = None, images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
        stream_transport_timeout: float | None = None,
    ) -> ChatResult:
        if images:
            raise RuntimeError("ollama-cloud image input is not supported by the native chat adapter")
        payload = self._build_payload(model, messages, thinking_level, max_tokens)
        use_stream = callable(on_delta)
        transport_timeout = stream_transport_timeout or _IDLE_SSE_TIMEOUT
        payload["stream"] = use_stream
        req_headers = {"Content-Type": "application/json"}
        if api_key:
            req_headers["Authorization"] = f"Bearer {api_key}"
        if headers:
            req_headers.update(headers)
        url = f"{self.base_url}{self.endpoint}"

        if not use_stream:
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                response = await client.post(url, json=payload, headers=req_headers)
                if response.is_error:
                    raise RuntimeError(f"ollama-cloud API error {response.status_code}: {response.text[:1000]}")
                data = response.json()
            message = data.get("message") or {}
            thinking = message.get("thinking")
            if thinking and on_thinking_delta:
                on_thinking_delta(str(thinking))
            return ChatResult(
                text=str(message.get("content") or ""), raw=data,
                usage=data.get("usage") or {}, stop_reason="stop" if data.get("done") else None,
            )

        text_parts: list[str] = []
        raw_last: dict[str, Any] = {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(transport_timeout, connect=30, write=30), follow_redirects=True) as client:
            async with client.stream("POST", url, json=payload, headers=req_headers) as response:
                if response.is_error:
                    body = (await response.aread()).decode("utf-8", errors="ignore")[:1000]
                    raise RuntimeError(f"ollama-cloud API error {response.status_code}: {body}")
                lines = response.aiter_lines()
                while True:
                    try:
                        line = await asyncio.wait_for(lines.__anext__(), timeout=transport_timeout)
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        raise RuntimeError(f"Ollama stream idle for {transport_timeout:.0f}s")
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line.removeprefix("data:").strip())
                    except Exception:
                        continue
                    raw_last = chunk
                    message = chunk.get("message") or {}
                    thinking = message.get("thinking")
                    if thinking and on_thinking_delta:
                        on_thinking_delta(str(thinking))
                    content = message.get("content")
                    if content:
                        content_s = str(content)
                        text_parts.append(content_s)
                        on_delta(content_s)
                    if chunk.get("done"):
                        break
        return ChatResult(
            text="".join(text_parts), raw=raw_last, usage={},
            stop_reason="stop" if raw_last.get("done") else None,
        )

    async def list_models(self, api_key: str, headers: dict[str, str] | None = None) -> list[str] | None:
        req_headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        if headers:
            req_headers.update(headers)
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(f"{self.base_url}/api/tags", headers=req_headers)
            if response.is_error:
                raise RuntimeError(f"ollama-cloud API error {response.status_code}: {response.text[:1000]}")
            data = response.json()
        return [str(item["name"]) for item in data.get("models", []) if item.get("name")]
