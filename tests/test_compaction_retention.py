# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from copy import deepcopy

import pytest

from one.core.compaction.policy import should_compact, trigger_threshold
from one.core.compaction.retention import native_replay_keep_start, plan_retention


def msg(role, tokens, **extra):
    return {"role": role, "tokens": tokens, **extra}


def plan(messages, recent=100, floor=1, available=1000):
    return plan_retention(messages, lambda m: m["tokens"], recent, floor, available)


@pytest.mark.parametrize(
    ("budget", "window", "expected"),
    [(32000, 32000, 16000), (200000, 200000, 160000),
     (1000000, 1000000, 800000), (100000, None, 80000),
     (10000, 32000, 8000)],
)
def test_trigger(budget, window, expected):
    assert trigger_threshold(budget, window) == expected
    assert not should_compact(expected - 1, budget, window)
    assert should_compact(expected, budget, window)


def test_trigger_overrides_and_rounding():
    assert trigger_threshold(1000, 1001, 95, 10, 0) == 899
    assert trigger_threshold(1000, None, 50, 0, 0) == 500
    with pytest.raises(ValueError):
        trigger_threshold(0, None)


@pytest.mark.parametrize(
    "args",
    [(0, None), (-1, None), (1, 0), (1, -1), (1, None, -1), (1, None, 101),
     (1, None, 80, -1), (1, None, 80, 101), (1, None, 80, 15, -1)],
)
def test_trigger_rejects_invalid_parameters(args):
    with pytest.raises(ValueError):
        trigger_threshold(*args)


def test_trigger_boundaries_and_request_validation():
    assert trigger_threshold(100, None, 100, 0, 0) == 100
    assert trigger_threshold(100, None, 0, 100, 0) == 0
    assert trigger_threshold(100, 100, 80, 100, 0) == 80
    with pytest.raises(ValueError):
        should_compact(-1, 100, None)
    with pytest.raises(ValueError):
        should_compact(0, 0, None)


def test_boundary_tolerance_and_budget():
    messages = [msg("user", 10), msg("assistant", 50), msg("user", 15), msg("assistant", 100)]
    assert plan(messages).keep_start == 2  # 115% is inclusive
    # The latest exchange is mandatory, even when the soft target is exceeded.
    assert plan(messages, available=110).reason == "oversized_exchange"
    messages[2]["tokens"] = 16
    assert plan(messages).keep_start == 2  # latest exchange outranks tolerance
    messages = [msg("assistant", 20), msg("user", 15), msg("assistant", 49),
                msg("assistant", 50), msg("user", 1)]
    assert plan(messages).keep_start == 1  # boundary expansion to exactly 115
    messages[1]["tokens"] = 16
    assert plan(messages).keep_start == 2
    messages[1]["tokens"] = 15
    assert plan(messages, available=110).keep_start == 2
    assert plan(messages[1:]).keep_start == 1  # prefer a reduction to retaining all


def test_floor_adaptation_short_and_empty():
    messages = [msg("user", 20), msg("assistant", 20)] * 5
    assert plan(messages, recent=20, floor=4).keep_start == 6
    assert plan(messages, recent=200, floor=2, available=60).keep_start == 7
    assert plan(messages, recent=20, floor=4, available=60).reason == "no_safe_reduction"
    assert plan(messages[:2]).reason == "no_safe_reduction"
    assert plan([]).reason == "no_safe_reduction"


def test_v2_default_15k_tail_and_eight_message_floor():
    messages = [msg("user" if i % 2 == 0 else "assistant", 1000) for i in range(30)]
    result = plan(messages, recent=15000, floor=8, available=30000)
    assert (result.keep_start, result.tokens_kept, result.reason) == (14, 16000, "ok")


def test_latest_exchange_and_large_tool_result():
    messages = [msg("user", 1), msg("assistant", 1), msg("user", 5), msg("assistant", 300)]
    result = plan(messages, recent=10, available=304)
    assert (result.reason, result.tokens_kept) == ("oversized_exchange", 305)
    assert plan(messages, recent=10, available=305).keep_start == 2


def test_native_pairs_and_legacy_helper():
    call = {"id": "x", "name": "read", "arguments": {}}
    messages = [msg("user", 1), msg("assistant", 10, _nativeToolCalls=[call]),
                msg("user", 1), msg("toolResult", 10, _nativeToolCallId="x")]
    assert native_replay_keep_start(messages, 3) == 1
    assert plan(messages, recent=1, available=100).keep_start == 1
    assert plan(messages, recent=1, available=20).reason == "oversized_exchange"


def test_plan_marker_is_not_a_user_boundary_and_input_is_unchanged():
    messages = [msg("user", 2), msg("assistant", 2), msg("user", 4),
                msg("user", 1, customType="plan", content=[{"step": "fix"}]), msg("assistant", 4)]
    original = deepcopy(messages)
    result = plan(messages, recent=1, available=20)
    assert result.keep_start == 2
    assert messages == original


def test_invalid_limits_and_estimates():
    with pytest.raises(ValueError):
        plan([], available=-1)
    with pytest.raises(ValueError):
        plan([msg("user", -1)])
