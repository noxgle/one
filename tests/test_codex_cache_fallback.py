# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from typing import Any

import pytest

from one.providers.codex_responses import CodexResponsesAdapter, PromptCacheUnsupportedError

_CACHE = {"enabled": True, "ttl": "30m", "stableBreakpoint": True, "toolResultBreakpoints": False, "key": "off", "prewarm": False}


def _fake_client_factory(monkeypatch, module, handler):
    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        def stream(self, method: str, url: str, **kwargs: Any):
            return handler(method, url, kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeClient)


def test_prompt_cache_payload_fields_present() -> None:
    adapter = CodexResponsesAdapter()
    payload = adapter._build_payload(
        "gpt-6.2-sol",
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        "medium", stream=False, prompt_cache=_CACHE,
    )
    assert payload["prompt_cache_options"] == {"mode": "implicit", "ttl": "30m"}
    assert payload["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert "prompt_cache_key" not in payload


def test_prompt_cache_disabled_omits_fields() -> None:
    adapter = CodexResponsesAdapter()
    payload = adapter._build_payload(
        "gpt-6.2-sol",
        [{"role": "user", "content": "hi"}],
        "medium", stream=False, prompt_cache={**_CACHE, "enabled": False},
    )
    assert "prompt_cache_options" not in payload
    assert "prompt_cache_breakpoint" not in payload


def test_prompt_cache_12h_ttl() -> None:
    adapter = CodexResponsesAdapter()
    payload = adapter._build_payload(
        "gpt-6.2-sol",
        [{"role": "user", "content": "hi"}],
        "medium", stream=False, prompt_cache={**_CACHE, "ttl": "12h"},
    )
    assert payload["prompt_cache_options"] == {"mode": "implicit", "ttl": "12h"}


def test_prompt_cache_invalid_ttl_raises() -> None:
    adapter = CodexResponsesAdapter()
    with pytest.raises(ValueError, match="unsupported prompt cache ttl"):
        adapter._build_payload(
            "gpt-6.2-sol",
            [{"role": "user", "content": "hi"}],
            "medium", stream=False, prompt_cache={**_CACHE, "ttl": "24h"},
        )


def test_prompt_cache_account_key_is_stable_and_not_per_session() -> None:
    adapter = CodexResponsesAdapter()
    p1 = adapter._build_payload(
        "gpt-6.2-sol", [{"role": "user", "content": "hi"}], "medium", stream=False,
        prompt_cache={**_CACHE, "key": "account"}, api_key="secret-token",
    )
    p2 = adapter._build_payload(
        "gpt-6.2-sol", [{"role": "user", "content": "hi"}], "medium", stream=False,
        prompt_cache={**_CACHE, "key": "account"}, api_key="secret-token",
    )
    assert p1["prompt_cache_key"] == p2["prompt_cache_key"]
    assert p1["prompt_cache_key"].startswith("acct-")
    assert "secret-token" not in p1["prompt_cache_key"]


def test_prompt_cache_prewarm_flag() -> None:
    adapter = CodexResponsesAdapter()
    payload = adapter._build_payload(
        "gpt-6.2-sol", [{"role": "user", "content": "hi"}], "medium", stream=False,
        prompt_cache={**_CACHE, "prewarm": True},
    )
    assert payload["prompt_cache_options"] == {"mode": "implicit", "ttl": "30m", "prewarm": True}


@pytest.mark.asyncio
async def test_chat_400_with_cache_raises_typed_error(monkeypatch) -> None:
    from one.providers import codex_responses as cod_mod

    class _Resp:
        status_code = 400
        is_error = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def aread(self) -> bytes:
            return b'{"error":"unknown field"}'

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Resp:
        return _Resp()

    _fake_client_factory(monkeypatch, cod_mod, handler)
    adapter = CodexResponsesAdapter()
    with pytest.raises(PromptCacheUnsupportedError) as excinfo:
        await adapter.chat(
            "tok", "gpt-6.2-sol", [{"role": "user", "content": "hi"}], "medium",
            prompt_cache=_CACHE,
        )
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_chat_422_with_cache_raises_typed_error(monkeypatch) -> None:
    from one.providers import codex_responses as cod_mod

    class _Resp:
        status_code = 422
        is_error = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def aread(self) -> bytes:
            return b'{"error":"bad"}'

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Resp:
        return _Resp()

    _fake_client_factory(monkeypatch, cod_mod, handler)
    adapter = CodexResponsesAdapter()
    with pytest.raises(PromptCacheUnsupportedError) as excinfo:
        await adapter.chat(
            "tok", "gpt-6.2-sol", [{"role": "user", "content": "hi"}], "medium",
            prompt_cache=_CACHE,
        )
    assert excinfo.value.status_code == 422


@pytest.mark.asyncio
async def test_chat_400_without_cache_raises_generic_error(monkeypatch) -> None:
    from one.providers import codex_responses as cod_mod

    class _Resp:
        status_code = 400
        is_error = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def aread(self) -> bytes:
            return b'{"error":"boom"}'

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Resp:
        return _Resp()

    _fake_client_factory(monkeypatch, cod_mod, handler)
    adapter = CodexResponsesAdapter()
    with pytest.raises(RuntimeError) as excinfo:
        await adapter.chat("tok", "gpt-6.2-sol", [{"role": "user", "content": "hi"}], "medium")
    assert not isinstance(excinfo.value, PromptCacheUnsupportedError)