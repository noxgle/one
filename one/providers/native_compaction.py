"""Validation of private Responses replay items without exposing their contents."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import copy
from typing import Any


class NativeCompactionProtocolError(RuntimeError):
    """The private native replay protocol was not satisfied."""


class NativeCompactionHTTPError(RuntimeError):
    """Content-free HTTP failure for a native attempt or safe retry."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"chatgpt API error {status_code}")


def validate_native_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list) or len(items) > 10000:
        raise NativeCompactionProtocolError("Invalid native compaction items")
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str) or item.get("type") not in {
            "message", "reasoning", "function_call", "function_call_output", "compaction"
        }:
            raise NativeCompactionProtocolError("Invalid native compaction items")
        encrypted = item.get("encrypted_content")
        if item["type"] == "compaction" or encrypted is not None:
            if not isinstance(encrypted, str) or not 0 < len(encrypted) <= 1_000_000:
                raise NativeCompactionProtocolError("Invalid native compaction items")
    return copy.deepcopy(items)


def sanitize_native_raw(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: sanitize_native_raw(v) for k, v in value.items() if k != "encrypted_content"}
    if isinstance(value, list):
        return [sanitize_native_raw(v) for v in value]
    return value
