"""Pure V2 compaction trigger policy."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from math import ceil


def trigger_threshold(
    input_budget: int,
    window: int | None,
    threshold_percent: float = 80,
    buffer_percent: float = 15,
    buffer_min_tokens: int = 16000,
) -> float:
    """Return the request token threshold for triggering compaction."""
    if input_budget <= 0 or (window is not None and window <= 0):
        raise ValueError("input budget and window must be positive")
    if not 0 <= threshold_percent <= 100 or not 0 <= buffer_percent <= 100:
        raise ValueError("percentages must be between 0 and 100")
    if buffer_min_tokens < 0:
        raise ValueError("minimum buffer must be nonnegative")
    percentage = input_budget * threshold_percent / 100
    buffered = input_budget - max(buffer_min_tokens, ceil((window or input_budget) * buffer_percent / 100))
    return min(percentage, buffered) if buffered > 0 else percentage


def should_compact(
    request_tokens: int,
    input_budget: int,
    window: int | None,
    threshold_percent: float = 80,
    buffer_percent: float = 15,
    buffer_min_tokens: int = 16000,
) -> bool:
    """Return whether the request has reached the compaction threshold."""
    if request_tokens < 0:
        raise ValueError("request tokens must be nonnegative")
    return request_tokens >= trigger_threshold(input_budget, window, threshold_percent, buffer_percent, buffer_min_tokens)
