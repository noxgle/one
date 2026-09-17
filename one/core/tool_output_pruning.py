"""Pure provider-context pruning for stale tool-result messages."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


def prune_stale_tool_outputs(
    messages: list[dict[str, Any]],
    *,
    enabled: bool,
    recent_tokens: int,
    min_result_tokens: int,
    marker: str,
    estimate_tokens: Callable[[dict[str, Any]], int],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Return a detached provider view and deterministic pruning statistics.

    The session message list is deliberately never modified. Untouched messages
    are safely shared; only messages whose top-level content is replaced are
    cloned. Tool results are
    represented as JSON text in a ``toolResult`` message; the replacement keeps
    its envelope (including any provider-specific call IDs) but contains only a
    bounded, safe JSON marker.
    """
    stats = {"count": 0, "tokensReclaimed": 0}
    if not enabled or min_result_tokens <= 0:
        return list(messages), stats

    protected = 0
    out = list(messages)
    for index in range(len(messages) - 1, -1, -1):
        original = messages[index]
        tokens = estimate_tokens(original)
        if protected < recent_tokens:
            protected += tokens
            continue
        if original.get("role") != "toolResult" or tokens < min_result_tokens:
            continue

        content = original.get("content", "")
        original_size = len(content) if isinstance(content, str) else len(str(content))
        payload: dict[str, Any] = {}
        if isinstance(content, str):
            try:
                candidate = json.loads(content)
                if isinstance(candidate, dict):
                    payload = candidate
            except (TypeError, ValueError):
                pass
        # A provider view passed back through this pure function remains stable.
        if payload.get("pruned") is True:
            continue

        tool = payload.get("tool")
        tool_name = tool[:128] if isinstance(tool, str) else "unknown"
        replacement: dict[str, Any] = {
            "tool": tool_name,
            "pruned": True,
            "originalSizeChars": original_size,
            "notice": marker[:512],
        }
        if isinstance(payload.get("ok"), bool):
            replacement["ok"] = payload["ok"]
        # Native/provider adapters may attach a call id in the JSON payload.
        for key in ("toolCallId", "tool_call_id"):
            value = payload.get(key)
            if isinstance(value, (str, int)):
                replacement[key] = str(value)[:128]

        pruned = dict(original)
        pruned["content"] = json.dumps(replacement, ensure_ascii=False, separators=(",", ":"))
        out[index] = pruned
        stats["count"] += 1
        stats["tokensReclaimed"] += max(0, tokens - estimate_tokens(pruned))
    return out, stats
