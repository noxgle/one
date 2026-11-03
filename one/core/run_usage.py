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
