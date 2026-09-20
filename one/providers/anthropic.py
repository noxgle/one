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
# of no data. Must stay < the outer 300 s provider deadline.
_IDLE_SSE_TIMEOUT = 120

_THINKING_BUDGET_BY_LEVEL = {
    "minimal": 1024,
    "low": 2048,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
}


def _is_oauth_token(api_key: str | None) -> bool:
    """Subscription access token (Claude Pro/Max login), not an API key."""
    return bool(api_key) and api_key.startswith("sk-ant-oat")


class MissingBlobError(AttachmentStorageError):
    """Raised when a required image blob is missing or corrupt before HTTP."""


class AnthropicAdapter(ProviderAdapter):
    name = "anthropic"

    def _build_image_part(self, image_ref: dict[str, Any], storage_dir: str) -> dict[str, Any]:
        """Build an Anthropic ``image`` source block.

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
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": b64,
            },
        }

    def _read_blob(self, storage_dir: str, blob_hash: str) -> bytes | None:
        try:
            from one.core.attachments import read_blob_bytes
            return read_blob_bytes(storage_dir, blob_hash)
        except Exception:
            return None

    def _resolve_message_content(
        self, role: str, content: Any, images: list[dict[str, Any]] | None,
        storage_dir: str,
    ) -> Any:
        """Expand content with image blocks for Anthropic format.

        Note: _build_image_part now raises ``MissingBlobError`` on failure,
        so this method propagates that exception when blobs are corrupt.
        """
        if role != "user":
            return content
        if not images:
            return content

        # Anthropic requires content to be a list when mixing text and images
        parts: list[dict[str, Any]] = []
        if isinstance(content, str) and content.strip():
            parts.append({"type": "text", "text": content.strip()})
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        parts.append(part)
                    else:
                        parts.append(part)
                elif isinstance(part, str):
                    parts.append({"type": "text", "text": part})

        for img in images:
            part = self._build_image_part(img, storage_dir)
            parts.append(part)

        return parts if parts else content

    async def list_models(self, api_key: str, headers: dict[str, str] | None = None) -> list[str] | None:
        # Only the subscription-OAuth credential may call GET /v1/models;
        # plain API keys have no public models-list endpoint and callers fall
        # back to minimal-chat validation.
        if not _is_oauth_token(api_key):
            return None
        detailed = await self.list_models_detailed(api_key, headers)
        return [d["id"] for d in (detailed or [])]

    async def list_models_detailed(self, api_key: str, headers: dict[str, str] | None = None) -> list[dict[str, Any]] | None:
        if not _is_oauth_token(api_key):
            return None
        req_headers = self._auth_headers(api_key)
        if headers:
            req_headers.update(headers)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get("https://api.anthropic.com/v1/models", headers=req_headers)
            if resp.is_error:
                body = resp.text[:1000]
                raise RuntimeError(f"anthropic API error {resp.status_code}: {body}")
            data = resp.json()
        out: list[dict[str, Any]] = []
        for m in data.get("data", []):
            mid = m.get("id")
            if not mid:
                continue
            out.append({"id": mid, "contextWindow": None})
        return out

    def _auth_headers(self, api_key: str) -> dict[str, str]:
        if _is_oauth_token(api_key):
            return {
                "Authorization": f"Bearer {api_key}",
                "anthropic-beta": "oauth-2025-04-20",
                "user-agent": "claude-cli/1.0.0 (external, cli)",
            }
        return {"x-api-key": api_key}

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
        # Attach images to the last user message (current turn).
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

        content_messages = []
        system = None
        for m in messages:
            if m.get("role") == "system":
                system = m.get("content", "")
                continue
            content_messages.append({"role": m.get("role"), "content": m.get("content", "")})

        # ── cache_control breakpoints (Anthropic prompt caching) ──
        payload_system: list[dict[str, Any]] | None = None
        if system is not None:
            payload_system = [{"text": system, "cache_control": {"type": "ephemeral"}}]

        # Add cache_control to the LAST user message in the conversation tail.
        # Anthropic allows up to 4 breakpoints total (1 system + 3 on messages).
        last_user = -1
        for i, m in enumerate(content_messages):
            if m.get("role") == "user":
                last_user = i
        if last_user >= 0:
            msg = content_messages[last_user]
            content = msg["content"]
            if isinstance(content, str):
                content_messages[last_user]["content"] = [
                    {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
                ]
            elif isinstance(content, list):
                parts: list[dict[str, Any]] = []
                for part in content:
                    if isinstance(part, str):
                        parts.append({"type": "text", "text": part})
                    elif isinstance(part, dict):
                        parts.append(part)
                    else:
                        parts.append({"type": "text", "text": str(part)})
                # Attach cache_control to the last *text* block.
                for i in range(len(parts) - 1, -1, -1):
                    if parts[i].get("type") == "text":
                        parts[i]["cache_control"] = {"type": "ephemeral"}
                        break
                content_messages[last_user]["content"] = parts

        thinking_budget = _THINKING_BUDGET_BY_LEVEL.get(thinking_level)
        output_tokens = max_tokens or 4096
        # Anthropic requires max_tokens to exceed the enabled thinking budget.
        if thinking_budget:
            output_tokens = max(output_tokens, thinking_budget + 1024)
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": output_tokens,
            "messages": content_messages,
        }
        if thinking_budget:
            payload["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
        if payload_system is not None:
            payload["system"] = payload_system
        req_headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        req_headers.update(self._auth_headers(api_key))
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

        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("POST", "https://api.anthropic.com/v1/messages", json=payload, headers=req_headers) as resp:
                resp.raise_for_status()
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
                        delta_obj = chunk.get("delta") or {}
                        delta = delta_obj.get("thinking") if delta_obj.get("type") == "thinking_delta" else delta_obj.get("text")
                        if delta:
                            delta_s = str(delta)
                            try:
                                if delta_obj.get("type") == "thinking_delta":
                                    if on_thinking_delta:
                                        on_thinking_delta(delta_s)
                                else:
                                    text_parts.append(delta_s)
                                    on_delta(delta_s)
                            except Exception:
                                pass
                    if ctype == "message_start":
                        msg_usage = (chunk.get("message") or {}).get("usage")
                        if isinstance(msg_usage, dict):
                            usage.update(msg_usage)
                        if stop_reason:
                            break
                    if ctype == "message_delta":
                        delta_obj = chunk.get("delta") or {}
                        if isinstance(delta_obj, dict) and delta_obj.get("stop_reason"):
                            stop_reason = delta_obj.get("stop_reason")
                        msg_usage = chunk.get("usage")
                        if isinstance(msg_usage, dict):
                            usage.update(msg_usage)

        return ChatResult(text="".join(text_parts), raw=raw_last, usage=usage, stop_reason=stop_reason)
