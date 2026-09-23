"""ChatGPT/Codex subscription backend.

Talks to the Codex Responses API used by ChatGPT Plus/Pro subscriptions.
This endpoint is not part of the public OpenAI API — it is externally controlled
by OpenAI and may change without notice.  Third-party use of subscription
credentials against this backend is at the user's own risk and may violate the
ChatGPT Terms of Service.

The API speaks the **Responses API** (not chat/completions) with several
mandatory quirks:

- replayed user items use ``input_text`` and assistant items use
  ``output_text`` (plain ``text`` is rejected)
- ``store: false`` is mandatory
- ``instructions`` (system prompt) is required; stateless — full history
  every request
- images are sent as ``input_image`` content parts on the user message
- headers: Bearer access token + ``ChatGPT-Account-Id`` (injected by the
  model registry from the stored OAuth record), ``originator``, ``session_id``
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from one.core.attachments import AttachmentStorageError

from .base import ChatResult, ProviderAdapter


class MissingBlobError(AttachmentStorageError):
    """Raised when a required image blob is missing or corrupt before HTTP."""


BASE_URL = "https://chatgpt.com/backend-api/codex"
# The /models endpoint gates on client_version: a stale/too-low value returns
# an empty {"models": []} with no error. Keep this at a current CLI version.
CLIENT_VERSION = "1.0.0"
ORIGINATOR = "codex_cli_rs"

# Responses does not expose minimal/xhigh; use its nearest supported bounds.
_EFFORT_BY_LEVEL = {"minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "high"}

# Backend rejects requests with empty `instructions`; sessions that carry no
# system message fall back to this Codex-flavored base prompt.
_BASE_INSTRUCTIONS = (
    "You are Codex, based on GPT-5. You are running as a coding agent inside "
    "the `one` terminal agent. Help the user with software engineering tasks: "
    "read and edit code, run commands, and explain your work concisely."
)

# 120 s is the direct-call/default finite transport watchdog. AgentSession
# overrides it with a longer value for managed streams so its meaningful-token
# idle timeout remains authoritative.
_IDLE_SSE_TIMEOUT = 120


def _error_with_body(status_code: int, body: str) -> RuntimeError:
    """Build an error carrying the server's response body (diagnosability)."""
    snippet = (body or "").strip()
    if len(snippet) > 800:
        snippet = snippet[:800] + "…"
    return RuntimeError(f"chatgpt API error {status_code}: {snippet}")


class CodexResponsesAdapter(ProviderAdapter):
    name = "chatgpt"

    def _headers(self, api_key: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
            "originator": ORIGINATOR,
            "User-Agent": f"codex_cli_rs/{CLIENT_VERSION}",
            # Stateless backend: a fresh session id per request enables
            # server-side prompt caching alongside prompt_cache_key.
            "session_id": str(uuid.uuid4()),
        }
        if extra:
            for k, v in extra.items():
                if v:
                    headers[k] = v
        return headers

    # ------------------------------------------------------------------
    # Image helpers (Responses API: input_image content parts)
    # ------------------------------------------------------------------

    def _read_blob(self, storage_dir: str, blob_hash: str) -> bytes | None:
        """Read a blob from the local store (inline import to avoid circular deps)."""
        try:
            from one.core.attachments import read_blob_bytes
            return read_blob_bytes(storage_dir, blob_hash)
        except Exception:
            return None

    def _build_image_part(self, image_ref: dict[str, Any], storage_dir: str) -> dict[str, Any]:
        """Build an ``input_image`` part from an image reference.

        The Responses API uses ``input_image`` content parts with an
        ``image_url`` field holding a data-URI (``data:<mime>;base64,<b64>``).

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
        return {"type": "input_image", "image_url": f"data:{mime};base64,{b64}"}

    # ------------------------------------------------------------------

    def _build_payload(
        self,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        stream: bool,
        max_tokens: int | None = None,
        images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
    ) -> dict[str, Any]:
        instructions = ""
        input_items: list[dict[str, Any]] = []

        # Validate all image blobs BEFORE building the payload (no HTTP).
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

        # Track which user turn gets the images (last user turn = current turn).
        last_user_idx = -1
        for i, m in enumerate(messages):
            if m.get("role") == "user":
                last_user_idx = i

        for idx, m in enumerate(messages):
            role = m.get("role")
            content = m.get("content", "")
            if role == "system":
                instructions = str(content)
                continue
            input_role = "user" if role == "user" else "assistant"
            if input_role == "user" and idx == last_user_idx:
                # Build content parts for the last user turn:
                # text parts + image parts (input_image).
                parts: list[dict[str, Any]] = []
                if isinstance(content, str) and content.strip():
                    parts.append({"type": "input_text", "text": content.strip()})
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") in ("input_text", "text"):
                            parts.append(part)
                if images:
                    for img in images:
                        parts.append(self._build_image_part(img, storage_dir))
                # Use parts list (may be just text if no images)
                content_for_item = parts if parts else [{"type": "input_text", "text": str(content)}]
            else:
                content_for_item = [
                    {"type": "input_text" if input_role == "user" else "output_text", "text": str(content)}
                ]
            input_items.append(
                {
                    "type": "message",
                    "role": input_role,
                    "content": content_for_item,
                }
            )
        payload: dict[str, Any] = {
            "model": model,
            "instructions": instructions or _BASE_INSTRUCTIONS,
            "input": input_items,
            "store": False,
            "stream": stream,
        }
        if max_tokens is not None:
            payload["max_output_tokens"] = max_tokens
        effort = _EFFORT_BY_LEVEL.get(thinking_level)
        if effort:
            # Request readable summaries rather than only invisible reasoning.
            payload["reasoning"] = {"effort": effort, "summary": "auto"}
        return payload

    @staticmethod
    def _extract_output(data: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
        text_parts: list[str] = []
        output = data.get("output", [])
        if not isinstance(output, list):
            output = []
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "message":
                continue
            content = item.get("content", [])
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text = part.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
        usage = data.get("usage") or {}
        status = data.get("status")
        return "".join(text_parts), usage if isinstance(usage, dict) else {}, status if isinstance(status, str) else None

    @staticmethod
    def _extract_reasoning_summaries(data: dict[str, Any]) -> list[str]:
        """Extract only human-readable reasoning summaries from Responses output."""
        summaries: list[str] = []
        output = data.get("output", [])
        if not isinstance(output, list):
            return summaries
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "reasoning":
                continue
            summary = item.get("summary")
            if summary is None:
                continue
            entries = summary if isinstance(summary, list) else [summary]
            for entry in entries:
                if isinstance(entry, str):
                    summaries.append(entry)
                elif isinstance(entry, dict):
                    text = entry.get("text")
                    if isinstance(text, str) and (
                        entry.get("type") in (None, "summary_text")
                    ):
                        summaries.append(text)
        return summaries

    @staticmethod
    def _summary_key(chunk: dict[str, Any]) -> tuple[Any, ...]:
        """Return the stable identity supplied by Responses for a summary part."""
        return tuple(
            chunk.get(name)
            for name in ("item_id", "output_index", "summary_index", "content_index")
        )

    @staticmethod
    def _summary_text(value: Any) -> str | None:
        """Accept a textual summary value, never event metadata."""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            text = value.get("text")
            return text if isinstance(text, str) else None
        return None

    @staticmethod
    def _incomplete_error(response: Any) -> RuntimeError:
        details = response.get("incomplete_details") if isinstance(response, dict) else None
        if not details and isinstance(response, dict):
            details = response.get("error")
        return RuntimeError(f"chatgpt responses incomplete: {details or 'response ended before completion'}")

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
        url = f"{BASE_URL}/responses"
        use_stream = callable(on_delta)
        payload = self._build_payload(
            model, messages, thinking_level,
            stream=use_stream, max_tokens=max_tokens,
            images=images, storage_dir=storage_dir,
        )
        req_headers = self._headers(api_key, extra=headers)
        transport_timeout = stream_transport_timeout or _IDLE_SSE_TIMEOUT

        if not use_stream:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(url, json=payload, headers=req_headers)
                if resp.is_error:
                    raise _error_with_body(resp.status_code, resp.text)
                data = resp.json()
            text, usage, status = self._extract_output(data)
            if status == "failed":
                raise RuntimeError(f"chatgpt responses failed: {data.get('error')}")
            if status == "incomplete":
                raise self._incomplete_error(data)
            if on_thinking_delta:
                for summary in self._extract_reasoning_summaries(data):
                    try:
                        on_thinking_delta(summary)
                    except Exception:
                        pass
            return ChatResult(text=text, raw=data, usage=usage, stop_reason=status)

        payload["stream"] = True
        text_parts: list[str] = []
        usage: dict[str, Any] = {}
        stop_reason: str | None = None
        raw_last: dict[str, Any] = {}
        summary_delta_keys: set[tuple[Any, ...]] = set()

        async with httpx.AsyncClient(timeout=httpx.Timeout(transport_timeout, connect=30, write=30)) as client:
            async with client.stream("POST", url, json=payload, headers=req_headers) as resp:
                if resp.is_error:
                    raw_body = await resp.aread()
                    raise _error_with_body(resp.status_code, raw_body.decode(errors="replace"))
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
                    if not isinstance(chunk, dict):
                        continue
                    ctype = chunk.get("type")
                    raw_last = chunk
                    if ctype == "response.output_text.delta":
                        delta = chunk.get("delta")
                        if delta:
                            delta_s = str(delta)
                            text_parts.append(delta_s)
                            try:
                                on_delta(delta_s)
                            except Exception:
                                pass
                    elif ctype in {
                        "response.reasoning_summary_text.delta",
                        "response.reasoning_text.delta",
                    }:
                        reasoning_delta = self._summary_text(chunk.get("delta"))
                        if reasoning_delta:
                            summary_delta_keys.add(self._summary_key(chunk))
                        if reasoning_delta and on_thinking_delta:
                            try:
                                on_thinking_delta(reasoning_delta)
                            except Exception:
                                pass
                    elif ctype == "response.reasoning_summary_text.done":
                        # A done event repeats the complete text after delta
                        # events. Emit it only when it was not streamed.
                        key = self._summary_key(chunk)
                        summary_text = self._summary_text(chunk.get("text"))
                        if summary_text and key not in summary_delta_keys and on_thinking_delta:
                            try:
                                on_thinking_delta(summary_text)
                            except Exception:
                                pass
                    elif ctype == "response.reasoning_summary_part.done":
                        # Older part events carry the completed text nested in
                        # `part`, with the same no-duplicate fallback.
                        key = self._summary_key(chunk)
                        summary_text = self._summary_text(chunk.get("part"))
                        if summary_text and key not in summary_delta_keys and on_thinking_delta:
                            try:
                                on_thinking_delta(summary_text)
                            except Exception:
                                pass
                    elif ctype == "response.completed":
                        response_obj = chunk.get("response") or {}
                        if not isinstance(response_obj, dict):
                            response_obj = {}
                        u = response_obj.get("usage")
                        if isinstance(u, dict):
                            usage.update(u)
                        status = response_obj.get("status")
                        stop_reason = status if isinstance(status, str) else None
                        raw_last = response_obj or chunk
                        if stop_reason == "incomplete":
                            raise self._incomplete_error(response_obj)
                        break
                    elif ctype == "response.failed":
                        response_obj = chunk.get("response")
                        err = (
                            response_obj.get("error") if isinstance(response_obj, dict) else None
                        ) or chunk.get("error")
                        raise RuntimeError(f"chatgpt responses failed: {err}")
                    elif ctype == "response.incomplete":
                        raise self._incomplete_error(chunk.get("response") or chunk)

        return ChatResult(text="".join(text_parts), raw=raw_last, usage=usage, stop_reason=stop_reason)

    async def list_models(self, api_key: str, headers: dict[str, str] | None = None) -> list[str] | None:
        detailed = await self.list_models_detailed(api_key, headers)
        return [d["id"] for d in (detailed or [])]

    async def list_models_detailed(
        self, api_key: str, headers: dict[str, str] | None = None
    ) -> list[dict[str, Any]] | None:
        """Non-standard schema: {"models": [{"slug": ...}, ...]}."""
        url = f"{BASE_URL}/models?client_version={CLIENT_VERSION}"
        req_headers = self._headers(api_key, extra=headers)
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url, headers=req_headers)
            if resp.is_error:
                body = ""
                try:
                    body = str(resp.json())
                except Exception:
                    body = resp.text[:1000]
                raise RuntimeError(f"chatgpt API error {resp.status_code}: {body}")
            data = resp.json()
        # HOTFIX-6: loud error on shape mismatch (silently returning [] hid
        # missing ChatGPT-Account-Id headers — the response lacked "models").
        if not isinstance(data, dict) or "models" not in data:
            raise RuntimeError(f"chatgpt models endpoint returned unexpected shape: {str(data)[:400]}")
        out: list[dict[str, Any]] = []
        for m in data.get("models", []):
            slug = m.get("slug")
            if slug:
                out.append({"id": slug, "contextWindow": None})
        if not out:
            raise RuntimeError(
                "chatgpt models endpoint returned 0 models — server gates the "
                "list on client_version; try updating CLIENT_VERSION"
            )
        return out
