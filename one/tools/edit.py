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
    # (a) Type-check: edits must be a list, not a dict or other type.
    if not isinstance(edits, list):
        raise ValueError(
            "Edit tool input is invalid. edits must be a list of {oldString, newString} objects."
        )

    if not edits:
        raise ValueError("Edit tool input is invalid. edits must contain at least one replacement.")

    # (b) Recovery: if path is empty/missing, try to pull it from edits[0].
    if not path and edits and isinstance(edits[0], dict):
        path = edits[0].get("path") or edits[0].get("file") or ""

    # (c) If path is still empty, raise a clear ValueError instead of a misleading FileNotFoundError.
    if not path:
        raise ValueError(
            "Edit tool input is invalid. 'path' is required as a top-level argument (not inside edits)."
        )

    p = resolve_to_cwd(path, cwd)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    old = p.read_text(encoding="utf-8", errors="ignore")
    new = old

    seen_spans: list[tuple[int, int]] = []
    resolved: list[tuple[int, str, str]] = []  # (start_in_old, old_text, new_text)
    for e in edits:
        # Accept both documented keys (oldString/newString) and legacy keys (oldText/newText).
        old_text: str | None = e.get("oldString") or e.get("oldText")
        new_text: str | None = e.get("newString") or e.get("newText")
        if old_text is None or new_text is None:
            raise ValueError(
                "Edit tool input is invalid. "
                "Each edit needs 'oldString'/'newString' (or legacy 'oldText'/'newText')."
            )
        idx = old.find(old_text)
        if idx < 0:
            raise ValueError("oldString block not found")
        if old.find(old_text, idx + 1) >= 0:
            raise ValueError("oldString block is not unique")
        span = (idx, idx + len(old_text))
        for a, b in seen_spans:
            if not (span[1] <= a or span[0] >= b):
                raise ValueError("Overlapping or nested edits are not allowed")
        seen_spans.append(span)
        resolved.append((idx, old_text, new_text))

    # Apply all edits by splicing the original text at sorted spans.
    # This avoids sequential str.replace corruption where an earlier replacement
    # introduces text that matches a later oldText.
    zipped: list[tuple[int, int, str]] = sorted(
        [(idx, idx + len(ot), nt) for idx, ot, nt in resolved],
        key=lambda t: t[0],
    )
    parts: list[str] = []
    prev_end = 0
    for start, end, new_text in zipped:
        parts.append(old[prev_end:start])
        parts.append(new_text)
        prev_end = end
    parts.append(old[prev_end:])
    new = "".join(parts)

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
