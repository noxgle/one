# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from typing import Any

import httpx

from one.core.attachments import AttachmentStorageError

from .base import ChatResult, ProviderAdapter, native_tool_replay_pairs

# 120 s is the direct-call/default finite transport watchdog. AgentSession
# overrides it with a longer value for managed streams so its meaningful-token
# idle timeout remains authoritative.
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
        supports_native_tools: bool = False,
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
        # OpenAI-compatible does not imply tool-compatible.  Local/custom
        # endpoints retain JSON-in-text unless explicitly opted in.
        self.supports_native_tools = supports_native_tools

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
                       images: list[dict[str, Any]] | None = None, storage_dir: str = "", temperature: float | None = None, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
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
        if self.supports_native_tools:
            messages = self._serialize_native_replay_messages(messages)
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
        if temperature is not None:
            payload["temperature"] = temperature
        elif self.default_temperature is not None:
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
        # Native tools are opt-in: local/legacy endpoints retain the proven
        # JSON-in-text contract even though they share this adapter.
        if tools and self.supports_native_tools:
            payload["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["parameters"]}} for t in tools]
        return payload

    @staticmethod
    def _serialize_native_replay_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Translate only complete durable pairs to Chat Completions items."""
        paired_calls, paired_outputs = native_tool_replay_pairs(messages)
        serialized: list[dict[str, Any]] = []
        for message_index, message in enumerate(messages):
            content = message.get("content", "")
            if message_index in paired_outputs:
                call_index = paired_outputs[message_index][1]
                call = messages[paired_outputs[message_index][0]]["_nativeToolCalls"][call_index]
                serialized.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": str(content),
                })
                continue
            role = message.get("role", "user")
            if role == "assistant" and isinstance(message.get("_nativeToolCalls"), list):
                tool_calls = []
                for call_index, call in enumerate(message["_nativeToolCalls"]):
                    if (message_index, call_index) not in paired_calls:
                        continue
                    tool_calls.append({
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                        },
                    })
                if tool_calls:
                    item: dict[str, Any] = {"role": "assistant", "tool_calls": tool_calls}
                    # Chat Completions accepts text alongside tool calls.
                    item["content"] = content if content else None
                    serialized.append(item)
                    continue
                if not content:
                    # Do not send a dangling empty assistant tool-call turn as
                    # an ordinary message; it is invalid for some providers.
                    continue
            serialized.append({"role": role, "content": content})
        return serialized

    def with_base_url(self, base_url: str) -> OpenAICompatibleAdapter:
        return OpenAICompatibleAdapter(
            self.name,
            base_url,
            endpoint=self.endpoint,
            supports_reasoning_effort=self.supports_reasoning_effort,
            default_temperature=self.default_temperature,
            reasoning_mode=self.reasoning_mode,
            supports_native_tools=self.supports_native_tools,
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
        temperature: float | None = None,
        stream_transport_timeout: float | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        payload = self._build_payload(model, messages, thinking_level, images=images, storage_dir=storage_dir, temperature=temperature, tools=tools)
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        use_stream = callable(on_delta)
        if use_stream:
            payload["stream"] = True
        transport_timeout = stream_transport_timeout or _IDLE_SSE_TIMEOUT
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
            # Reasoning is never parseable assistant content.  Non-streaming
            # callers can still surface it through the thinking callback.
            thinking = msg.get("reasoning_content") or ""
            if thinking:
                try:
                    if on_thinking_delta:
                        on_thinking_delta(str(thinking))
                except Exception:
                    pass
            text = content
            usage = data.get("usage") or {}
            stop = choice.get("finish_reason")
            native_calls = self._parse_native_calls(msg)
            return ChatResult(
                text=text,
                raw=data,
                usage=usage,
                stop_reason=stop,
                had_thinking=bool(str(thinking).strip()),
                had_native_tool_call=bool(native_calls), native_tool_calls=native_calls or None,
            )

        text_parts: list[str] = []
        usage: dict[str, Any] = {}
        stop_reason: str | None = None
        raw_last: dict[str, Any] = {}
        had_thinking = False
        had_native_tool_call = False
        calls_by_index: dict[int, dict[str, Any]] = {}

        async with httpx.AsyncClient(timeout=httpx.Timeout(transport_timeout, connect=30, write=30), follow_redirects=True) as client:
            async with client.stream("POST", f"{self.base_url}{self.endpoint}", json=payload, headers=req_headers) as resp:
                if resp.is_error:
                    body = (await resp.aread()).decode("utf-8", errors="ignore")[:1000]
                    raise RuntimeError(f"{self.name} API error {resp.status_code}: {body}")

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
                    had_native_tool_call = had_native_tool_call or bool(
                        delta.get("tool_calls") or delta.get("toolCalls") or delta.get("function_call")
                    )
                    for call_delta in delta.get("tool_calls") or delta.get("toolCalls") or []:
                        if not isinstance(call_delta, dict):
                            continue
                        index = int(call_delta.get("index", 0))
                        call = calls_by_index.setdefault(index, {"id": str(call_delta.get("id") or ""), "name": "", "arguments": ""})
                        if call_delta.get("id"):
                            call["id"] = str(call_delta["id"])
                        fn = call_delta.get("function") or {}
                        if isinstance(fn, dict):
                            call["name"] = str(fn.get("name") or call["name"])
                            call["arguments"] += str(fn.get("arguments") or "")
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
                        had_thinking = had_thinking or bool(reasoning_str.strip())
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
        native_calls = []
        for call in calls_by_index.values():
            try:
                call["arguments"] = json.loads(call["arguments"])
            except json.JSONDecodeError:
                pass
            native_calls.append(call)
        return ChatResult(
            text=full_text,
            raw=raw_last,
            usage=usage,
            stop_reason=stop_reason,
            had_thinking=had_thinking,
            had_native_tool_call=had_native_tool_call or bool(native_calls), native_tool_calls=native_calls or None,
        )

    @staticmethod
    def _parse_native_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
        raw_calls = message.get("tool_calls") or message.get("toolCalls") or []
        if isinstance(message.get("function_call"), dict):
            raw_calls = [message["function_call"]]
        calls = []
        for raw in raw_calls:
            if not isinstance(raw, dict):
                continue
            fn = raw.get("function") if isinstance(raw.get("function"), dict) else raw
            arguments = fn.get("arguments", "")
            try:
                arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            except json.JSONDecodeError:
                pass
            calls.append({"id": str(raw.get("id") or ""), "name": str(fn.get("name") or ""), "arguments": arguments})
        return calls
