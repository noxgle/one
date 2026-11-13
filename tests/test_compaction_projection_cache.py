# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from copy import deepcopy

import httpx
import pytest

from one.core.compaction.checkpoint import CheckpointFailure, SummaryStrategy, generate_checkpoint
from one.core.compaction.projection import project_compaction_summary
from one.core.compaction.summary import extractive_checkpoint


def test_projection_stable_detached():
    messages = [{"customType": "compaction_summary", "content": [1]}, {"role": "user", "content": [2]},
                {"customType": "compaction_summary", "content": [3]}]
    original = deepcopy(messages)
    assert project_compaction_summary(messages) == messages
    projected = project_compaction_summary(messages, "suffix")
    assert projected == [messages[1], messages[0], messages[2]]
    projected[1]["content"].append(4)
    assert messages == original
    with pytest.raises(ValueError):
        project_compaction_summary(messages, "invalid")


@pytest.mark.asyncio
async def test_cached_repair_detached():
    prefix = [{"role": "user", "content": [{"text": "unique transcript"}]}]
    original = deepcopy(prefix)
    requests = []
    valid = extractive_checkpoint([], (), estimate_tokens=len)

    async def invoke(request):
        requests.append(deepcopy(request))
        return {"content": [{"type": "toolCall"}, {"type": "text", "text": valid}] if len(requests) == 1 else valid}

    result = await generate_checkpoint([], (), cache_prefix=prefix, append_instruction_role="developer",
                                       invoke_provider=invoke, estimate_tokens=len, request_fits=lambda _: True)
    assert result.strategy == SummaryStrategy.LOCAL_CACHE_AWARE and result.attempts == 2
    assert prefix == original and requests[0][:1] == original and requests[1][:2] == requests[0]
    assert requests[0][-1]["role"] == requests[1][-1]["role"] == "developer"
    assert "unique transcript" not in requests[0][-1]["content"]


@pytest.mark.asyncio
async def test_cached_size_fallback_async():
    calls = []
    async def fits(request):
        return request[0]["role"] == "system"
    async def invoke(request):
        calls.append(request)
        return {"content": extractive_checkpoint([], (), estimate_tokens=len)}
    result = await generate_checkpoint([], (), cache_prefix=[{"role": "user", "content": "x"}],
                                       invoke_provider=invoke, estimate_tokens=len, request_fits=fits)
    assert result.strategy == "local_separate" and result.fallback and result.attempts == 1 and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 422, 401, 500])
async def test_http_rejection(status):
    calls = []
    async def invoke(request):
        calls.append(request)
        if len(calls) == 1:
            req = httpx.Request("POST", "https://example.invalid")
            raise httpx.HTTPStatusError("error", request=req, response=httpx.Response(status, request=req))
        return {"content": extractive_checkpoint([], (), estimate_tokens=len),
                "_compactionUsage": {"inputTokens": 7}}
    kwargs = dict(cache_prefix=[], invoke_provider=invoke, estimate_tokens=len, request_fits=lambda _: True)
    if status in (400, 422):
        result = await generate_checkpoint([], (), **kwargs)
        assert result.strategy == "local_separate" and result.attempts == 2 and result.fallback
        assert all(value is None for value in result.usage.values())
    else:
        with pytest.raises(CheckpointFailure) as error:
            await generate_checkpoint([], (), **kwargs)
        assert error.value.reason == "provider_error" and len(calls) == 1


@pytest.mark.asyncio
async def test_explicit_predicate_and_fatal_precedence():
    class ContractError(Exception):
        pass

    calls = 0
    async def invoke(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ContractError()
        return {"content": extractive_checkpoint([], (), estimate_tokens=len)}

    kwargs = dict(cache_prefix=[], invoke_provider=invoke, estimate_tokens=len, request_fits=lambda _: True,
                  is_cache_contract_rejection=lambda exc: isinstance(exc, ContractError))
    result = await generate_checkpoint([], (), **kwargs)
    assert result.attempts == 2 and result.strategy == "local_separate"
    calls = 0
    with pytest.raises(ContractError):
        await generate_checkpoint([], (), **kwargs, fatal_exceptions=(ContractError,))
    assert calls == 1
