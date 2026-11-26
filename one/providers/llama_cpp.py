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

import functools
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

    @staticmethod
    def _usage_from_timings(raw: Any) -> dict[str, int]:
        """Derive token usage from llama-server's ``timings`` block.

        Some builds omit the OpenAI ``usage`` object even when
        ``stream_options.include_usage`` is requested, but they always report
        ``timings.prompt_n`` / ``timings.predicted_n`` on the final chunk. Without
        this fallback the session never calibrates its token estimator and
        output-generation telemetry stays permanently empty.
        """
        timings = raw.get("timings") if isinstance(raw, dict) else None
        if not isinstance(timings, dict):
            return {}
        prompt_n = timings.get("prompt_n")
        predicted_n = timings.get("predicted_n")
        if not isinstance(prompt_n, int) or isinstance(prompt_n, bool) or prompt_n <= 0:
            return {}
        usage = {"prompt_tokens": prompt_n}
        if isinstance(predicted_n, int) and not isinstance(predicted_n, bool) and predicted_n >= 0:
            usage["completion_tokens"] = predicted_n
        return usage

    # ``functools.wraps`` keeps the inherited signature introspectable.
    # AgentSession gates fourteen request features on
    # ``inspect.signature(provider.chat).parameters`` (streaming, tools,
    # temperature, images, max_tokens, storage_dir...). A bare ``*args/**kwargs``
    # override erased all of them and silently downgraded every request.
    @functools.wraps(OpenAICompatibleAdapter.chat)
    async def chat(self, *args: Any, **kwargs: Any) -> Any:
        result = await super().chat(*args, **kwargs)
        if not result.usage:
            derived = self._usage_from_timings(result.raw)
            if derived:
                result.usage = derived
        return result

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
            # A router-mode llama-server (``--models``/``--models-dir``) rejects
            # ``/apply-template`` and ``/tokenize`` with "model name is missing
            # from the request" unless the model is named in the JSON body.  A
            # single-model server ignores the extra field, so always send it.
            if isinstance(model, str) and model:
                template_payload["model"] = model
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
                tokenize_payload: dict[str, Any] = {"content": prompt}
                if isinstance(model, str) and model:
                    tokenize_payload["model"] = model
                tokenized = await client.post(
                    f"{base}/tokenize", json=tokenize_payload,
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
        """Return llama.cpp's configured slot context, when a route exposes it.

        OpenAI-compatible ``/v1/models`` metadata describes a model's training
        limit at best.  llama.cpp's ``/props`` reports the actual server slot
        configuration, so only that value is suitable for compaction/display.

        A router-mode server answers ``/props`` with ``role: "router"`` and
        ``n_ctx: 0`` because no single slot is configured globally; the real
        per-model slot size is only available from ``/slots?model=`` (or the
        ``memory.effective.n_ctx`` / ``meta.n_ctx`` fields of ``/v1/models``).
        Falling back to those keeps the context gauge honest instead of treating
        the router's zero as "unknown" and letting compaction run against a
        stale training limit.
        """
        if not isinstance(model, str) or not model:
            return None
        root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        req_headers = self._build_headers(api_key, headers)
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                response = await client.get(
                    f"{root}/props",
                    params={"model": model},
                    headers=req_headers,
                )
                if not response.is_error:
                    payload = response.json()
                    settings = payload.get("default_generation_settings") if isinstance(payload, dict) else None
                    window = settings.get("n_ctx") if isinstance(settings, dict) else None
                    if isinstance(window, int) and not isinstance(window, bool) and window > 0:
                        return window
                # Router mode (or a build without a usable /props): ask the
                # per-model slot list, then the model catalogue.
                slots = await client.get(
                    f"{root}/slots", params={"model": model}, headers=req_headers,
                )
                if not slots.is_error:
                    entries = slots.json()
                    if isinstance(entries, list):
                        for entry in entries:
                            value = entry.get("n_ctx") if isinstance(entry, dict) else None
                            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                                return value
                models = await client.get(f"{root}/v1/models", headers=req_headers)
                if not models.is_error:
                    for entry in models.json().get("data", []) or []:
                        if not isinstance(entry, dict) or entry.get("id") != model:
                            continue
                        memory = entry.get("memory") if isinstance(entry.get("memory"), dict) else {}
                        effective = memory.get("effective") if isinstance(memory.get("effective"), dict) else {}
                        meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
                        for value in (effective.get("n_ctx"), meta.get("n_ctx")):
                            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                                return value
        except Exception:
            return None
        return None
