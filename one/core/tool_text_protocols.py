# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
import re
from typing import Any

# DeepSeek-V4 was trained on the DSML tool protocol and can emit it as plain
# text when a provider does not expose native tools.  The canonical token uses
# the fullwidth vertical bar (U+FF5C); some tokenizers/proxies normalize it to
# ASCII ``|``.  Both spellings identify the same wire format.
_DSML = r"(?:｜DSML｜|\|DSML\|)"
_BLOCK_RE = re.compile(
    rf"<{_DSML}tool_calls>(?P<body>.*?)</{_DSML}tool_calls>",
    re.DOTALL,
)
_INVOKE_RE = re.compile(
    rf"<{_DSML}invoke name=\"(?P<name>[^\"]+)\">(?P<body>.*?)</{_DSML}invoke>",
    re.DOTALL,
)
_PARAMETER_RE = re.compile(
    rf"<{_DSML}parameter name=\"(?P<name>[^\"]+)\" string=\"(?P<kind>true|false)\">"
    rf"(?P<value>.*?)</{_DSML}parameter>",
    re.DOTALL,
)
_THINK_RE = re.compile(r"^<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)
_BLOCK_START_RE = re.compile(rf"^<{_DSML}tool_calls>")


def _dsml_payload(text: str) -> str | None:
    """Return text positioned at a leading DSML block, or ``None``.

    Only a block at the start of the response is authoritative.  An example
    quoted in prose therefore never becomes an executable tool call.
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip().lstrip("\ufeff").strip()
    if not stripped:
        return None
    think = _THINK_RE.match(stripped)
    if think:
        stripped = stripped[think.end():].lstrip()
    return stripped if _BLOCK_START_RE.match(stripped) else None


def is_dsml_tool_call_candidate(text: str) -> bool:
    """Whether *text* starts with DSML tool framing, complete or not."""
    return _dsml_payload(text) is not None


def parse_dsml_tool_calls(text: str) -> list[dict[str, Any]] | None:
    """Parse a leading DeepSeek DSML ``tool_calls`` block.

    Returns provider-neutral calls (``id`` is intentionally empty so the
    session assigns a stable local correlation id) or ``None`` when the block
    is incomplete, ambiguous, or contains a non-string parameter whose value
    is not valid JSON.  Values are never guessed or coerced.
    """
    payload = _dsml_payload(text)
    if payload is None:
        return None
    block = _BLOCK_RE.match(payload)
    if block is None:
        return None
    body = block.group("body")
    calls: list[dict[str, Any]] = []
    position = 0
    for invoke in _INVOKE_RE.finditer(body):
        if body[position:invoke.start()].strip():
            return None
        position = invoke.end()
        name = invoke.group("name").strip()
        if not name:
            return None
        arguments: dict[str, Any] = {}
        arguments_body = invoke.group("body")
        cursor = 0
        for parameter in _PARAMETER_RE.finditer(arguments_body):
            if arguments_body[cursor:parameter.start()].strip():
                return None
            cursor = parameter.end()
            key = parameter.group("name").strip()
            if not key or key in arguments:
                return None
            value = parameter.group("value")
            if parameter.group("kind") == "true":
                arguments[key] = value
            else:
                try:
                    arguments[key] = json.loads(value)
                except (TypeError, ValueError):
                    return None
        if arguments_body[cursor:].strip():
            return None
        calls.append({"id": "", "name": name, "arguments": arguments})
    if not calls or body[position:].strip():
        return None
    return calls
