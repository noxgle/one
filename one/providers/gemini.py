from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from typing import Any

import httpx

from one.core.attachments import AttachmentStorageError

from .base import ChatResult, ProviderAdapter

# 120 s is the direct-call/default finite transport watchdog. AgentSession
# overrides it with a longer value for managed streams so its meaningful-token
# idle timeout remains authoritative.
_IDLE_SSE_TIMEOUT = 120

_THINKING_BUDGET_BY_LEVEL = {
    "minimal": 1024,
    "low": 2048,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
}


class MissingBlobError(AttachmentStorageError):
    """Raised when a required image blob is missing or corrupt before HTTP."""


class GeminiAdapter(ProviderAdapter):
    name = "gemini"

    def _build_image_part(self, image_ref: dict[str, Any], storage_dir: str) -> dict[str, Any]:
        """Build a Gemini ``inline_data`` part for an image.

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
        return {"inline_data": {"data": b64, "mime_type": mime}}

    def _read_blob(self, storage_dir: str, blob_hash: str) -> bytes | None:
        try:
            from one.core.attachments import read_blob_bytes
            return read_blob_bytes(storage_dir, blob_hash)
        except Exception:
            return None

    def _resolve_message_parts(
        self, role: str, content: Any, images: list[dict[str, Any]] | None,
        storage_dir: str,
    ) -> list[dict[str, Any]]:
        """Build parts list for Gemini: text + inline_data images.

        Note: _build_image_part now raises ``MissingBlobError`` on failure,
        so this method propagates that exception when blobs are corrupt.
        """
        parts: list[dict[str, Any]] = []
        if isinstance(content, str) and content.strip():
            parts.append({"text": content.strip()})
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        parts.append({"text": part.get("text", "")})
                    # skip other types (images handled separately)
                elif isinstance(part, str):
                    parts.append({"text": part})
        if images:
            for img in images:
                part = self._build_image_part(img, storage_dir)
                parts.append(part)
        return parts if parts else [{"text": ""}]

    async def list_models(self, api_key: str, headers: dict[str, str] | None = None) -> list[str] | None:
        detailed = await self.list_models_detailed(api_key, headers)
        if detailed is None:
            return None
        return [d["id"] for d in detailed]

    async def list_models_detailed(self, api_key: str, headers: dict[str, str] | None = None) -> list[dict[str, Any]] | None:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            if resp.is_error:
                body = resp.text[:1000]
                raise RuntimeError(f"gemini API error {resp.status_code}: {body}")
            data = resp.json()
        out: list[dict[str, Any]] = []
        for m in data.get("models", []):
            name = m.get("name", "")
            methods = m.get("supportedGenerationMethods") or []
            if name and "generateContent" in methods:
                limit = m.get("inputTokenLimit")
                out.append({
                    "id": name.removeprefix("models/"),
                    "contextWindow": limit if isinstance(limit, int) and limit > 0 else None,
                })
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
        stream_transport_timeout: float | None = None,
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
        last_user_idx = -1
        for i, m in enumerate(messages):
            if m.get("role") == "user":
                last_user_idx = i
        contents = []
        for i, m in enumerate(messages):
            if m.get("role") == "system":
                continue
            role = "user" if m.get("role") == "user" else "model"
            content = m.get("content", "")
            if role == "user" and images and i == last_user_idx:
                parts = self._resolve_message_parts(role, content, images, storage_dir)
            else:
                parts = [{"text": str(content)}]
            contents.append({"role": role, "parts": parts})

        generation_config: dict[str, Any] = {"temperature": 0.1}
        # Gemini 2.5 Pro rejects a zero thinking budget, so omit the field to
        # disable thinking; recognized non-off levels use deterministic budgets.
        budget = _THINKING_BUDGET_BY_LEVEL.get(thinking_level)
        if budget is not None:
            generation_config["thinkingConfig"] = {
                "thinkingBudget": budget
            }
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
        transport_timeout = stream_transport_timeout or _IDLE_SSE_TIMEOUT
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
                    if not part.get("thought"):
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(transport_timeout, connect=30, write=30)) as client:
            async with client.stream("POST", url, json=payload, headers=h) as resp:
                resp.raise_for_status()
                aiter = resp.aiter_lines()
                while True:
                    try:
                        line = await asyncio.wait_for(
                            aiter.__anext__(), timeout=transport_timeout
                        )
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        raise RuntimeError(
                            f"SSE stream idle for {transport_timeout:.0f}s — "
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
                    if isinstance(chunk.get("usageMetadata"), dict):
                        usage = chunk["usageMetadata"]
                    candidates = chunk.get("candidates") or []
                    if not candidates:
                        continue
                    c0 = candidates[0]
                    parts = (c0.get("content") or {}).get("parts") or []
                    for part in parts:
                        if not isinstance(part, dict) or not part.get("text"):
                            continue
                        piece = str(part["text"])
                        try:
                            if part.get("thought"):
                                if on_thinking_delta:
                                    on_thinking_delta(piece)
                            else:
                                text_parts.append(piece)
                                on_delta(piece)
                        except Exception:
                            pass
                    if c0.get("finishReason"):
                        stop_reason = c0.get("finishReason")
                        break

        return ChatResult(text="".join(text_parts), raw=raw_last, usage=usage, stop_reason=stop_reason)
