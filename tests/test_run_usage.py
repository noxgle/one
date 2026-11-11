# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import pytest

from one.core.run_pricing import TokenUsage
from one.core.run_usage import UsageCounts, extract_usage_counts, normalize_usage


@pytest.mark.parametrize(("source", "raw", "expected"), [
    ("anthropic", {"input_tokens": 70, "output_tokens": 20, "cache_read_input_tokens": 20,
                   "cache_creation_input_tokens": 10}, TokenUsage(100, 20, 20, 10)),
    ("openai_compatible", {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                           "prompt_tokens_details": {"cached_tokens": 30},
                           "completion_tokens_details": {"reasoning_tokens": 10}}, TokenUsage(100, 20, 30, 0)),
    ("codex_responses", {"input_tokens": 100, "output_tokens": 20,
                         "input_tokens_details": {"cached_tokens": 30}}, TokenUsage(100, 20, 30, 0)),
    ("gemini", {"promptTokenCount": 90, "toolUsePromptTokenCount": 10, "totalTokenCount": 120,
                "cachedContentTokenCount": 30, "candidatesTokenCount": 15, "thoughtsTokenCount": 5},
     TokenUsage(100, 20, 30, 0)),
    ("ollama", {}, None),
    ("anthropic", {"input_tokens": 0, "output_tokens": 0}, None),
    ("openai_compatible", {"prompt_tokens": 1, "completion_tokens": 0,
                           "prompt_tokens_details": {"cached_tokens": 2}}, None),
    ("codex_responses", {"input_tokens": 0, "output_tokens": 0, "total_tokens": 1,
                         "input_tokens_details": {"cached_tokens": 0}}, None),
    ("gemini", {"promptTokenCount": 1, "totalTokenCount": 1}, None),
])
def test_normalize(source: str, raw: dict, expected: TokenUsage | None) -> None:
    result = normalize_usage(source, raw)
    assert result.source == source
    assert result.raw == raw and result.raw is not raw
    assert result.usage == expected


def test_zero_is_not_missing_and_raw_is_detached() -> None:
    raw = {"prompt_tokens": 0, "completion_tokens": 0, "prompt_tokens_details": {"cached_tokens": 0}}
    result = normalize_usage("openai_compatible", raw)
    raw["prompt_tokens_details"]["cached_tokens"] = 1
    assert result.usage == TokenUsage(0, 0, 0, 0)
    assert result.raw["prompt_tokens_details"]["cached_tokens"] == 0


def test_extract_usage_counts_responses_format() -> None:
    counts = extract_usage_counts({
        "input_tokens": 1000,
        "output_tokens": 50,
        "input_tokens_details": {"cached_tokens": 800},
        "output_tokens_details": {"reasoning_tokens": 20},
    })
    assert counts == UsageCounts(1000, 800, 50, 20)
    assert counts.cache_ratio == 0.8


def test_extract_usage_counts_chat_completions_format() -> None:
    counts = extract_usage_counts({
        "prompt_tokens": 200,
        "completion_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 50},
        "completion_tokens_details": {"reasoning_tokens": 4},
    })
    assert counts == UsageCounts(200, 50, 10, 4)
    assert counts.cache_ratio == 0.25


@pytest.mark.parametrize("raw", [None, {}, {"input_tokens": 5}, {"output_tokens": 2}])
def test_extract_usage_counts_missing_values_are_unknown(raw: dict | None) -> None:
    counts = extract_usage_counts(raw)
    assert counts.cached_tokens is None
    assert counts.cache_ratio is None
    if raw == {"output_tokens": 2}:
        assert counts.input_tokens is None and counts.output_tokens == 2


def test_extract_usage_counts_explicit_zero_is_reported() -> None:
    counts = extract_usage_counts({
        "input_tokens": 100,
        "output_tokens": 0,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
    })
    assert counts == UsageCounts(100, 0, 0, 0)
    assert counts.cache_ratio == 0.0


@pytest.mark.parametrize(
    "raw",
    [
        {"input_tokens": -1, "input_tokens_details": {"cached_tokens": -1}},
        {"input_tokens": True, "input_tokens_details": {"cached_tokens": False}},
        {"input_tokens": "100", "input_tokens_details": {"cached_tokens": "50"}},
        {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 101}},
    ],
)
def test_extract_usage_counts_rejects_malformed_or_contradictory_values(raw: dict) -> None:
    counts = extract_usage_counts(raw)
    assert counts.cache_ratio is None


def test_extract_usage_counts_zero_input_has_no_ratio() -> None:
    counts = extract_usage_counts({"input_tokens": 0, "input_tokens_details": {"cached_tokens": 0}})
    assert counts == UsageCounts(0, 0, None, None)
    assert counts.cache_ratio is None
