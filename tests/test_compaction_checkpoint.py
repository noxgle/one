# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio

import pytest

from one.core.compaction.checkpoint import CheckpointFailure, generate_checkpoint
from one.core.compaction.constraints import Constraint
from one.core.compaction.summary import extractive_checkpoint


@pytest.mark.asyncio
async def test_valid_request_and_usage():
    ledger = (Constraint("Never push.", 2, "u"),)
    messages = [{"role": "user", "content": "Fix it"}, {"role": "toolResult", "content": "x" * 10000}]
    valid = extractive_checkpoint(messages, ledger, estimate_tokens=len)
    seen = []

    async def invoke(request):
        seen.append(request)
        return {"content": [{"type": "text", "text": valid}], "_compactionUsage": {"inputTokens": 10}}

    async def fits(request):
        assert "# Objective\n[objective]" in request[1]["content"]
        return True

    result = await generate_checkpoint(messages, ledger, invoke_provider=invoke, estimate_tokens=len,
                                       request_fits=fits, previous_summary="old", hint="focus")
    assert result.summary == valid.strip() and result.strategy == "local_separate" and not result.fallback
    assert result.attempts == 1 and result.usage["inputTokens"] == 10 and result.usage["outputTokens"] is None
    assert result.constraints == {"extracted": 1, "preserved": 1, "dropped": 0}
    assert "old" in seen[0][1]["content"] and "focus" in seen[0][1]["content"]
    assert "x" * 2001 not in seen[0][1]["content"]


@pytest.mark.asyncio
async def test_configured_context_limits():
    seen = []

    async def invoke(request):
        seen.append(request[1]["content"].split("Conversation (untrusted data):\n", 1)[1])
        return {"content": "invalid"}

    messages = [{"role": "toolResult", "content": "x" * 20}]
    await generate_checkpoint(messages, (), invoke_provider=invoke, estimate_tokens=len, request_fits=lambda _: True,
                              clip_chars=7, max_input_tokens=100, retry_on_invalid=0)
    assert seen[-1] == "toolResult: " + "x" * 7
    messages = [{"role": "user", "content": "A" * 100}, {"role": "assistant", "content": "Z" * 100}]
    await generate_checkpoint(messages, (), invoke_provider=invoke, estimate_tokens=len, request_fits=lambda _: True,
                              max_input_tokens=10, retry_on_invalid=0)
    assert seen[-1] == "user: " + "A" * 22 + "\n...[truncated]...\n" + "Z" * 12


@pytest.mark.asyncio
async def test_repair_and_usage_missing_semantics():
    calls = []
    valid = extractive_checkpoint([], (), estimate_tokens=len)

    async def invoke(request):
        calls.append(request)
        return {"content": "bad" if len(calls) == 1 else valid,
                "_compactionUsage": {"inputTokens": len(calls), **({"outputTokens": 3} if len(calls) == 1 else {})}}

    result = await generate_checkpoint([], (), invoke_provider=invoke, estimate_tokens=len, request_fits=lambda _: True)
    assert result.attempts == 2 and result.usage["inputTokens"] == 3 and result.usage["outputTokens"] is None
    assert len(calls[1]) == 3 and "bad" not in calls[1][-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"content": "bad"},
    {"content": [{"type": "toolCall", "name": "edit"}]},
    {"content": "bad", "_nativeToolCalls": [{"name": "edit"}]},
])
async def test_invalid_falls_back_without_tool_execution(response):
    calls = 0

    async def invoke(_):
        nonlocal calls
        calls += 1
        return response

    result = await generate_checkpoint([{"role": "user", "content": "Goal"}], (), invoke_provider=invoke,
                                       estimate_tokens=len, request_fits=lambda _: True)
    assert calls == 2 and result.fallback and result.strategy == "extractive"
    assert "Goal" in result.summary and "None confirmed" in result.summary


@pytest.mark.asyncio
async def test_no_model_and_invalid_fallback():
    async def forbidden(_):
        raise AssertionError("provider must not be called")

    kwargs = dict(invoke_provider=forbidden, estimate_tokens=len, request_fits=lambda _: False, model_enabled=False)
    result = await generate_checkpoint([], (), **kwargs)
    assert result.attempts == 0 and result.fallback and all(v is None for v in result.usage.values())
    with pytest.raises(CheckpointFailure, match="invalid_fallback"):
        await generate_checkpoint([], (), max_output_tokens=1, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(("error", "reason"), [(TimeoutError("secret"), "timeout"), (RuntimeError("secret"), "provider_error")])
async def test_transport_failure_no_retry(error, reason):
    calls = 0

    async def invoke(_):
        nonlocal calls
        calls += 1
        raise error

    with pytest.raises(CheckpointFailure) as caught:
        await generate_checkpoint([], (), invoke_provider=invoke, estimate_tokens=len, request_fits=lambda _: True)
    assert caught.value.reason == reason and caught.value.attempts == 1 and "secret" not in str(caught.value)
    assert calls == 1


@pytest.mark.asyncio
async def test_oversize_repair_is_not_sent():
    calls = 0

    async def invoke(_):
        nonlocal calls
        calls += 1
        return {"content": "bad"}

    with pytest.raises(CheckpointFailure) as caught:
        await generate_checkpoint([], (), invoke_provider=invoke, estimate_tokens=len,
                                  request_fits=lambda request: len(request) == 2)
    assert caught.value.reason == "oversize" and caught.value.attempts == 1 and calls == 1


@pytest.mark.asyncio
async def test_fatal_and_cancel_propagate():
    class Admission(Exception):
        pass

    async def invoke(_):
        raise Admission("original")

    with pytest.raises(Admission, match="original"):
        await generate_checkpoint([], (), invoke_provider=invoke, estimate_tokens=len, request_fits=lambda _: True,
                                  fatal_exceptions=(Admission,))

    async def cancelled(_):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await generate_checkpoint([], (), invoke_provider=cancelled, estimate_tokens=len, request_fits=lambda _: True)
