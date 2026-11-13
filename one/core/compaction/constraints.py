"""Verbatim, provenance-bearing user constraints; extraction is heuristic."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_PATTERN = re.compile(
    r"\b(?:must|never|always|do\s+not|don't|require(?:d|s)?|constraints?|shall\s+not|"
    r"nie\s+wolno|zawsze|nigdy|wymagane|wymaga(?:m|ne)?|należy|nie\s+można|musisz)\b",
    re.IGNORECASE,
)
_CORRECTION = re.compile(r"^(?:Replace constraint: |Zastąp ograniczenie: )(.+?) => (.+)$")


@dataclass(frozen=True)
class Constraint:
    text: str
    entry_index: int
    entry_id: str | None = None


def extract_constraints(
    messages: Sequence[Mapping[str, Any]],
    previous: Sequence[Constraint] = (),
    *,
    estimate_tokens: Callable[[str], int],
    max_tokens: int = 2000,
    index_offset: int = 0,
    supersede: Mapping[Constraint, Constraint] | None = None,
) -> tuple[Constraint, ...]:
    """Merge exact directives; explicit caller-verified replacements only.

    User lines may explicitly say ``Replace constraint: <exact old> => <new>``
    or ``Zastąp ograniczenie: <exact old> => <new>``. The old text must
    match an existing ledger entry exactly; the new segment is stored verbatim.
    A caller-supplied replacement must itself be extracted from this batch, and the old entry
    must exist. No natural-language contradiction or semantic matching is inferred.
    Overflow raises rather than silently losing a constraint. Index offset refers
    to the source history, not the position within this batch.
    """
    if not 0 <= max_tokens <= 2000:
        raise ValueError("max_tokens must be between 0 and 2000")
    found: list[Constraint] = []
    corrections: list[tuple[str, Constraint]] = []
    for index, message in enumerate(messages, index_offset):
        if message.get("role") != "user" or message.get("customType"):
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(block["text"] for block in content if isinstance(block, Mapping)
                                and block.get("type") == "text" and isinstance(block.get("text"), str))
        if not isinstance(content, str):
            continue
        fenced = False
        quoted_block = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fenced = not fenced
                continue
            if re.match(r"(?i)^\s*<(?:tool|toolresult|untrusted-tool-output)\b", line):
                quoted_block = True
            if not fenced and not quoted_block and not stripped.startswith((">", "|")):
                entry_id = str(message["id"]) if message.get("id") is not None else None
                correction = _CORRECTION.fullmatch(line)
                if correction:
                    corrections.append((correction.group(1), Constraint(correction.group(2), index, entry_id)))
                elif _PATTERN.search(line) and not stripped.startswith(('"', "'")):
                    found.append(Constraint(line, index, entry_id))
            if re.search(r"(?i)</(?:tool|toolresult|untrusted-tool-output)>\s*$", line):
                quoted_block = False
    result = list(previous)
    for old_text, new in corrections:
        matches = [entry for entry in result if entry.text == old_text]
        if len(matches) != 1 or matches[0] not in previous:
            raise ValueError("correction requires an exact previously user-sourced constraint")
        result.remove(matches[0])
        if all(entry.text != new.text for entry in result):
            result.append(new)
    for old, new in (supersede or {}).items():
        if old not in result or new not in found:
            raise ValueError("supersession requires a previous entry and a directive from this batch")
        result.remove(old)
    seen = {entry.text for entry in result}
    for entry in found:
        if entry.text not in seen:
            result.append(entry)
            seen.add(entry.text)
    cost = estimate_tokens(render_ledger(result))
    if cost < 0 or cost > max_tokens:
        raise ValueError("constraint ledger exceeds token limit or estimator returned a negative value")
    return tuple(result)


def render_ledger(entries: Sequence[Constraint]) -> str:
    """Render provenance alongside each unchanged directive."""
    return "\n".join(f"[{e.entry_index}{' id=' + e.entry_id if e.entry_id else ''}] {e.text}" for e in entries)
