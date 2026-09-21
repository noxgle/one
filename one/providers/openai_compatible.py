from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from typing import Any

import httpx

from one.core.attachments import AttachmentStorageError

from .base import ChatResult, ProviderAdapter

# Idle timeout for per-line SSE reads — aborts a stalled stream after 120 s
# of no data. Must stay < the outer 300 s provider deadline
# (get_provider_timeout_sec), otherwise the outer deadline fires first.
_IDLE_SSE_TIMEOUT = 120

# Chat Completions reasoning effort supports minimal through high.  ``xhigh``
# has no equivalent there, so it deliberately uses the strongest supported
# effort rather than silently collapsing every non-high selection to medium.
_REASONING_EFFORT_BY_LEVEL = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
}


class MissingBlobError(AttachmentStorageError):
    """Raised when a required image blob is missing or corrupt before HTTP."""


class OpenAICompatibleAdapter(ProviderAdapter):
    def __init__(
        self,
        name: str,
        base_url: str,
        endpoint: str = "/v1/chat/completions",
        *,
        supports_reasoning_effort: bool = True,
        default_temperature: float | None = 0.1,
        reasoning_mode: str = "openai",
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint
        self.supports_reasoning_effort = supports_reasoning_effort
        self.default_temperature = default_temperature
        # OpenRouter uses the OpenAI wire format for chat, but its reasoning
        # control and trace fields are provider-specific.  Keep this opt-in so
        # local llama.cpp and every other compatible endpoint retain behavior.
        self.reasoning_mode = reasoning_mode

    def _build_image_part(self, image_ref: dict[str, Any], storage_dir: str) -> dict[str, Any]:
        """Build an ``image_url`` part from an image reference.

        *storage_dir* is resolved from the session context — never from the
        image_ref itself so that the persisted JSONL contains no internal paths.

        Raises ``MissingBlobError`` when the blob is missing, corrupt, or
        unreadable — the provider never silently drops an image.
        """
        blob_hash = image_ref.get("blobHash") or image_ref.get("blob_hash")
        mime = image_ref.get("mime", "image/png")
        if not blob_hash or not storage_dir:
            raise MissingBlobError(
                f"image reference missing blob_hash/storage_dir: {image_ref}"
            )
        raw = self._read_blob(storage_dir, blob_hash)
        if raw is None:
            raise MissingBlobError(
                f"required image blob missing or corrupt: {blob_hash}"
            )
        b64 = base64.b64encode(raw).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}

    def _read_blob(self, storage_dir: str, blob_hash: str) -> bytes | None:
        """Read a blob from the local store (inline import to avoid circular deps)."""
        try:
            from one.core.attachments import read_blob_bytes
            return read_blob_bytes(storage_dir, blob_hash)
        except Exception:
            return None

    def _resolve_message_content(
        self, role: str, content: Any, images: list[dict[str, Any]] | None,
        storage_dir: str,
    ) -> list[dict[str, Any]] | str:
        """Expand message content with inline image parts for OpenAI format.

        Note: _build_image_part now raises ``MissingBlobError`` on failure,
        so this method propagates that exception when blobs are corrupt.
        """
        if role != "user":
            return content

        if images:
            parts: list[dict[str, Any]] = []
            # Text content (may be a string or list of text parts)
            if isinstance(content, str) and content.strip():
                parts.append({"type": "text", "text": content.strip()})
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(part)
            # Add image parts (may raise MissingBlobError)
            for img in images:
                part = self._build_image_part(img, storage_dir)
                parts.append(part)
            return parts if parts else content

        return content

    def _build_payload(self, model: str, messages: list[dict[str, Any]], thinking_level: str,
                       images: list[dict[str, Any]] | None = None, storage_dir: str = "") -> dict[str, Any]:
        # Validate all image blobs are present BEFORE building payload / making HTTP.
        if images:
            for img in images:
                blob_hash = img.get("blobHash") or img.get("blob_hash")
                if not blob_hash:
                    raise MissingBlobError(
                        f"image reference missing blob_hash: {img}"
                    )
                raw = self._read_blob(storage_dir, blob_hash)
                if raw is None:
                    raise MissingBlobError(
                        f"required image blob missing or corrupt before HTTP: {blob_hash}"
                    )
        # Attach images to the last user message (current turn), so tool-loaded
        # images follow the tool result instead of rewriting the first prompt.
        if images:
            last_user = -1
            for i, m in enumerate(messages):
                if m.get("role", "") == "user":
                    last_user = i
            expanded: list[dict[str, Any]] = []
            for i, m in enumerate(messages):
                role = m.get("role", "")
                content = m.get("content", "")
                if role == "user" and i == last_user:
                    content = self._resolve_message_content(role, content, images, storage_dir)
                expanded.append({"role": role, "content": content})
            messages = expanded

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if self.default_temperature is not None:
            payload["temperature"] = self.default_temperature
        if self.reasoning_mode == "openrouter":
            effort = _REASONING_EFFORT_BY_LEVEL.get(thinking_level)
            if effort:
                payload["reasoning"] = {"effort": effort}
        elif self.supports_reasoning_effort:
            effort = _REASONING_EFFORT_BY_LEVEL.get(thinking_level)
            if effort:
                payload["reasoning_effort"] = effort
        # llama.cpp chat templates such as Qwen use this template argument to
        # suppress their reasoning block. It is deliberately provider-specific:
        # OpenAI-compatible APIs do not share this extension.
        if self.name == "llama.cpp" and thinking_level == "off":
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    def with_base_url(self, base_url: str) -> OpenAICompatibleAdapter:
        return OpenAICompatibleAdapter(
            self.name,
            base_url,
            endpoint=self.endpoint,
            supports_reasoning_effort=self.supports_reasoning_effort,
            default_temperature=self.default_temperature,
            reasoning_mode=self.reasoning_mode,
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
        images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
    ) -> ChatResult:
        payload = self._build_payload(model, messages, thinking_level, images=images, storage_dir=storage_dir)
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
            content = msg.get("content") or ""
            # OpenRouter traces are not assistant output.  Generic compatible
            # endpoints intentionally retain their legacy combined result.
            if self.reasoning_mode == "openrouter":
                text = content
            else:
                thinking = msg.get("reasoning_content") or ""
                text = (thinking + "\n\n" + content) if thinking else content
            usage = data.get("usage") or {}
            stop = choice.get("finish_reason")
            return ChatResult(text=text, raw=data, usage=usage, stop_reason=stop)

        text_parts: list[str] = []
        usage: dict[str, Any] = {}
        stop_reason: str | None = None
        raw_last: dict[str, Any] = {}

        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
            async with client.stream("POST", f"{self.base_url}{self.endpoint}", json=payload, headers=req_headers) as resp:
                if resp.is_error:
                    body = (await resp.aread()).decode("utf-8", errors="ignore")[:1000]
                    raise RuntimeError(f"{self.name} API error {resp.status_code}: {body}")

                aiter = resp.aiter_lines()
                while True:
                    try:
                        line = await asyncio.wait_for(
                            aiter.__anext__(), timeout=_IDLE_SSE_TIMEOUT
                        )
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        raise RuntimeError(
                            f"SSE stream idle for {_IDLE_SSE_TIMEOUT:.0f}s — "
                            "the request did not resolve within the idle timeout"
                        )
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
                    if self.reasoning_mode == "openrouter":
                        # Prefer delta fields, then top-level fallbacks used by
                        # different OpenRouter upstream providers.
                        piece_reasoning = (
                            delta.get("reasoning")
                            or delta.get("reasoning_content")
                            or chunk.get("reasoning")
                            or chunk.get("reasoning_content")
                        )
                    else:
                        piece_reasoning = delta.get("reasoning_content")
                    if piece_reasoning:
                        reasoning_str = str(piece_reasoning)
                        if self.reasoning_mode != "openrouter":
                            text_parts.append(reasoning_str)
                        # Forward raw reasoning_content exactly as-is (no synthetic spacing).
                        try:
                            if on_thinking_delta:
                                on_thinking_delta(reasoning_str)
                            elif on_delta:
                                on_delta(reasoning_str)
                        except Exception:
                            pass
                    if choice.get("finish_reason"):
                        stop_reason = choice.get("finish_reason")
                        break

        full_text = "".join(text_parts)
        return ChatResult(text=full_text, raw=raw_last, usage=usage, stop_reason=stop_reason)
