# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from one.core.run_pricing import TokenUsage


@dataclass(frozen=True, slots=True)
class NormalizedUsage:
    """Raw adapter usage and a complete accounting value, if unambiguous."""

    source: str
    raw: dict[str, Any]
    usage: TokenUsage | None


@dataclass(frozen=True, slots=True)
class UsageCounts:
    """Provider-reported counts for cache diagnostics; missing values stay None.

    These counts describe the wire payload and are deliberately separate from
    :class:`TokenUsage`: a missing counter is unknown, not zero, and cache
    tokens are a subset of the reported input total for Responses/OpenAI.
    """

    input_tokens: int | None = None
    cached_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None

    @property
    def cache_ratio(self) -> float | None:
        if (
            self.input_tokens
            and self.cached_tokens is not None
            and 0 <= self.cached_tokens <= self.input_tokens
        ):
            return self.cached_tokens / self.input_tokens
        return None


def _reported_count(container: Any, key: str) -> int | None:
    if not isinstance(container, dict):
        return None
    value = container.get(key)
    return value if type(value) is int and value >= 0 else None


def extract_usage_counts(raw: dict[str, Any] | None) -> UsageCounts:
    """Extract cache-relevant counters from Responses/OpenAI-compatible usage.

    The function never interprets omission as zero. Anthropic's disjoint
    cache counters are intentionally not mapped here; callers that need them
    already normalize them through :func:`normalize_usage`.
    """
    if not isinstance(raw, dict):
        return UsageCounts()
    input_tokens = _reported_count(raw, "input_tokens")
    if input_tokens is None:
        input_tokens = _reported_count(raw, "prompt_tokens")
    output_tokens = _reported_count(raw, "output_tokens")
    if output_tokens is None:
        output_tokens = _reported_count(raw, "completion_tokens")
    input_details = raw.get("input_tokens_details")
    if not isinstance(input_details, dict):
        input_details = raw.get("prompt_tokens_details")
    output_details = raw.get("output_tokens_details")
    if not isinstance(output_details, dict):
        output_details = raw.get("completion_tokens_details")
    return UsageCounts(
        input_tokens=input_tokens,
        cached_tokens=_reported_count(input_details, "cached_tokens"),
        output_tokens=output_tokens,
        reasoning_tokens=_reported_count(output_details, "reasoning_tokens"),
    )


def normalize_usage(source: str, raw: dict[str, Any]) -> NormalizedUsage:
    """Normalize known wire formats without interpreting missing counts as zero.

    Cache counters are mandatory: omission does not prove that no cache was
    billed. Gemini's total is used to include reasoning and tool-use tokens;
    unsupported or contradictory breakdowns remain unknown.
    """
    if not isinstance(source, str) or not isinstance(raw, dict):
        raise ValueError("source and raw usage must be a string and object")
    data = deepcopy(raw)

    def count(obj: dict[str, Any], key: str) -> int:
        value = obj.get(key)
        if type(value) is not int or value < 0:
            raise ValueError(key)
        return value

    try:
        if source == "anthropic":
            read = count(data, "cache_read_input_tokens")
            write = count(data, "cache_creation_input_tokens")
            inp = count(data, "input_tokens") + read + write
            out = count(data, "output_tokens")
        elif source in ("openai_compatible", "codex_responses"):
            responses = source == "codex_responses"
            inp = count(data, "input_tokens" if responses else "prompt_tokens")
            out = count(data, "output_tokens" if responses else "completion_tokens")
            details = data.get("input_tokens_details" if responses else "prompt_tokens_details")
            if not isinstance(details, dict):
                raise ValueError("input details")
            read = count(details, "cached_tokens")
            write = 0  # These formats expose cached input, not a separate write category.
            if "total_tokens" in data and count(data, "total_tokens") != inp + out:
                raise ValueError("inconsistent total")
            output_details = data.get("output_tokens_details" if responses else "completion_tokens_details")
            if output_details is not None:
                if not isinstance(output_details, dict) or count(output_details, "reasoning_tokens") > out:
                    raise ValueError("invalid reasoning tokens")
        elif source == "gemini":
            prompt = count(data, "promptTokenCount")
            tool_input = count(data, "toolUsePromptTokenCount")
            inp = prompt + tool_input
            total = count(data, "totalTokenCount")
            read = count(data, "cachedContentTokenCount")
            out = count(data, "candidatesTokenCount") + count(data, "thoughtsTokenCount")
            write = 0
            if inp + out != total:
                raise ValueError("inconsistent total")
        else:
            raise ValueError("unsupported source")
        usage = TokenUsage(inp, out, read, write)
    except ValueError:
        usage = None
    return NormalizedUsage(source, data, usage)
