"""Native llama.cpp (``llama-server``) chat adapter.

``llama-server`` exposes an OpenAI-compatible Chat Completions endpoint, so this
adapter specializes :class:`OpenAICompatibleAdapter` and reuses its shared
HTTP/SSE transport, tool serialization and response normalization.  It only
adds the llama-server-specific extensions:

- ``chat_template_kwargs`` for ``enable_thinking`` (Qwen-style templates);
- the ``/apply-template`` + ``/tokenize`` token-count path;
- the ``/props`` context-window read.

Native tool calling is enabled by default: llama-server returns ``tool_calls``
in the standard Chat Completions shape, which the shared adapter already
normalizes into ``ChatResult.native_tool_calls``.
"""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from typing import Any

import httpx

from .openai_compatible import OpenAICompatibleAdapter


class LlamaCppAdapter(OpenAICompatibleAdapter):
    name = "llama.cpp"

    def __init__(
        self,
        base_url: str,
        *,
        supports_reasoning_effort: bool = False,
        default_temperature: float | None = None,
    ) -> None:
        super().__init__(
            "llama.cpp",
            base_url,
            supports_reasoning_effort=supports_reasoning_effort,
            default_temperature=default_temperature,
            supports_native_tools=True,
        )

    def with_base_url(self, base_url: str) -> LlamaCppAdapter:
        return LlamaCppAdapter(
            base_url,
            supports_reasoning_effort=self.supports_reasoning_effort,
            default_temperature=self.default_temperature,
        )

    def _build_payload(
        self,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        *,
        disable_tool_calls: bool = False,
    ) -> dict[str, Any]:
        payload = super()._build_payload(
            model, messages, thinking_level, images=images, storage_dir=storage_dir,
            temperature=temperature, tools=tools, disable_tool_calls=disable_tool_calls,
        )
        # llama.cpp chat templates such as Qwen use this template argument to
        # suppress their reasoning block. It is deliberately provider-specific:
        # OpenAI-compatible APIs do not share this extension.
        if thinking_level in {"off", "on"}:
            payload["chat_template_kwargs"] = {"enable_thinking": thinking_level == "on"}
        return payload

    async def count_request_tokens(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
        images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> int | None:
        """Count a llama.cpp chat request with its native template/tokenizer.

        These endpoints are optional in llama.cpp builds and are not part of the
        OpenAI-compatible API.  A failure is intentionally indistinguishable
        from an unavailable endpoint so callers retain their local estimate.
        """
        try:
            payload = self._build_payload(
                model, messages, thinking_level, images=images,
                storage_dir=storage_dir, temperature=temperature, tools=tools,
            )
            template_payload: dict[str, Any] = {
                "messages": payload["messages"],
                "add_generation_prompt": True,
            }
            for key in ("tools", "chat_template_kwargs"):
                if key in payload:
                    template_payload[key] = payload[key]
            # llama.cpp normally serves these endpoints at the server root even
            # when a compatible base URL was configured with a /v1 suffix.
            base = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
            async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
                rendered = await client.post(
                    f"{base}/apply-template", json=template_payload,
                    headers=self._build_headers(api_key, headers),
                )
                if rendered.is_error:
                    return None
                prompt = rendered.json().get("prompt")
                if not isinstance(prompt, str):
                    return None
                tokenized = await client.post(
                    f"{base}/tokenize", json={"content": prompt},
                    headers=self._build_headers(api_key, headers),
                )
                if tokenized.is_error:
                    return None
                tokens = tokenized.json().get("tokens")
                if not isinstance(tokens, list):
                    return None
                return len(tokens)
        except Exception:
            return None

    async def get_context_window(
        self, api_key: str, model: str, headers: dict[str, str] | None = None,
    ) -> int | None:
        """Return llama.cpp's configured slot context, when its /props route exists.

        OpenAI-compatible ``/v1/models`` metadata describes a model's training
        limit at best.  llama.cpp's ``/props`` reports the actual server slot
        configuration, so only that value is suitable for compaction/display.
        """
        if not isinstance(model, str) or not model:
            return None
        root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                response = await client.get(
                    f"{root}/props",
                    params={"model": model},
                    headers=self._build_headers(api_key, headers),
                )
                if response.is_error:
                    return None
                payload = response.json()
        except Exception:
            return None
        settings = payload.get("default_generation_settings") if isinstance(payload, dict) else None
        window = settings.get("n_ctx") if isinstance(settings, dict) else None
        return window if isinstance(window, int) and not isinstance(window, bool) and window > 0 else None
