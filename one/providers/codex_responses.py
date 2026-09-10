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
- headers: Bearer access token + ``ChatGPT-Account-Id`` (injected by the
  model registry from the stored OAuth record), ``originator``, ``session_id``
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from .base import ChatResult, ProviderAdapter

BASE_URL = "https://chatgpt.com/backend-api/codex"
# The /models endpoint gates on client_version: a stale/too-low value returns
# an empty {"models": []} with no error. Keep this at a current CLI version.
CLIENT_VERSION = "1.0.0"
ORIGINATOR = "codex_cli_rs"

_EFFORT_BY_LEVEL = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high"}

# Backend rejects requests with empty `instructions`; sessions that carry no
# system message fall back to this Codex-flavored base prompt.
_BASE_INSTRUCTIONS = (
    "You are Codex, based on GPT-5. You are running as a coding agent inside "
    "the `one` terminal agent. Help the user with software engineering tasks: "
    "read and edit code, run commands, and explain your work concisely."
)


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

    def _build_payload(
        self,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        stream: bool,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        instructions = ""
        input_items: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            content = m.get("content", "")
            if role == "system":
                instructions = str(content)
                continue
            input_role = "user" if role == "user" else "assistant"
            input_items.append(
                {
                    "type": "message",
                    "role": input_role,
                    # The Responses API accepts input_text only for user
                    # messages; replayed assistant turns must be output_text.
                    "content": [
                        {"type": "input_text" if input_role == "user" else "output_text", "text": str(content)}
                    ],
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
            payload["reasoning"] = {"effort": effort}
        return payload

    @staticmethod
    def _extract_output(data: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
        text_parts: list[str] = []
        for item in data.get("output", []):
            if item.get("type") != "message":
                continue
            for part in item.get("content", []) or []:
                if part.get("type") == "output_text":
                    text_parts.append(str(part.get("text", "")))
        usage = data.get("usage") or {}
        status = data.get("status")
        return "".join(text_parts), usage if isinstance(usage, dict) else {}, status

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
        url = f"{BASE_URL}/responses"
        use_stream = callable(on_delta)
        payload = self._build_payload(model, messages, thinking_level, stream=use_stream, max_tokens=max_tokens)
        req_headers = self._headers(api_key, extra=headers)

        if not use_stream:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(url, json=payload, headers=req_headers)
                if resp.is_error:
                    raise _error_with_body(resp.status_code, resp.text)
                data = resp.json()
            text, usage, status = self._extract_output(data)
            return ChatResult(text=text, raw=data, usage=usage, stop_reason=status)

        payload["stream"] = True
        text_parts: list[str] = []
        usage: dict[str, Any] = {}
        stop_reason: str | None = None
        raw_last: dict[str, Any] = {}

        async with httpx.AsyncClient(timeout=180) as client:
            async with client.stream("POST", url, json=payload, headers=req_headers) as resp:
                if resp.is_error:
                    raw_body = await resp.aread()
                    raise _error_with_body(resp.status_code, raw_body.decode(errors="replace"))
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
                    elif ctype == "response.completed":
                        response_obj = chunk.get("response") or {}
                        u = response_obj.get("usage")
                        if isinstance(u, dict):
                            usage.update(u)
                        stop_reason = response_obj.get("status")
                        raw_last = response_obj or chunk
                    elif ctype == "response.failed":
                        err = chunk.get("response", {}).get("error") or chunk.get("error")
                        raise RuntimeError(f"chatgpt responses failed: {err}")

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
