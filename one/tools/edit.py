from __future__ import annotations

import difflib
from typing import Any

from .common import resolve_to_cwd


def _line_of_first_diff(old: str, new: str) -> int | None:
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    for idx, (a, b) in enumerate(zip(old_lines, new_lines), start=1):
        if a != b:
            return idx
    if len(old_lines) != len(new_lines):
        return min(len(old_lines), len(new_lines)) + 1
    return None


def edit_tool(cwd: str, path: str, edits: list[dict[str, str]]) -> dict:
    if not edits:
        raise ValueError("Edit tool input is invalid. edits must contain at least one replacement.")

    p = resolve_to_cwd(path, cwd)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    old = p.read_text(encoding="utf-8", errors="ignore")
    new = old

    seen_spans: list[tuple[int, int]] = []
    for e in edits:
        old_text = e["oldText"]
        new_text = e["newText"]
        idx = old.find(old_text)
        if idx < 0:
            raise ValueError("oldText block not found")
        if old.find(old_text, idx + 1) >= 0:
            raise ValueError("oldText block is not unique")
        span = (idx, idx + len(old_text))
        for a, b in seen_spans:
            if not (span[1] <= a or span[0] >= b):
                raise ValueError("Overlapping or nested edits are not allowed")
        seen_spans.append(span)
        new = new.replace(old_text, new_text)

    p.write_text(new, encoding="utf-8")
    diff = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
        )
    )
    first_changed = _line_of_first_diff(old, new)

    return {
        "content": [{"type": "text", "text": f"Successfully replaced {len(edits)} block(s) in {path}."}],
        "details": {"diff": diff, "firstChangedLine": first_changed},
    }
