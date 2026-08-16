from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from .common import resolve_to_cwd

_ENVELOPE_START = "*** Begin Patch"
_ENVELOPE_END = "*** End Patch"

_ADD = "*** Add File:"
_DELETE = "*** Delete File:"
_UPDATE = "*** Update File:"
_MOVE = "*** Move to:"


def _parse_hunks(raw_lines: list[str]) -> list[tuple[str, list[str], list[str], list[str]]]:
    """Parse unified-diff hunks from raw section lines.

    Returns a list of (hunk_header_str, removed_lines, added_lines, context_lines) per hunk.
    """
    hunks: list[tuple[str, list[str], list[str], list[str]]] = []
    current_header: str | None = None
    current_removed: list[str] = []
    current_added: list[str] = []
    current_context: list[str] = []

    def _flush() -> None:
        nonlocal current_header, current_removed, current_added, current_context
        if current_header is not None:
            hunks.append((current_header, current_removed, current_added, current_context))
        current_header = None
        current_removed = []
        current_added = []
        current_context = []

    for line in raw_lines:
        if line.startswith("@@"):
            _flush()
            current_header = line
            continue
        if not line:
            current_context.append("")
            continue
        prefix = line[0] if line else ""
        if prefix == "-":
            current_removed.append(line[1:])
        elif prefix == "+":
            current_added.append(line[1:])
        elif prefix == " ":
            current_context.append(line[1:])
        else:
            current_context.append(line)

    _flush()
    return hunks


def _validate_hunks(
    hunks: list[tuple[str, list[str], list[str], list[str]]],
    file_content: str,
) -> str:
    """Apply hunks to file_content with exact context matching.

    Returns the new content string, or raises ValueError on mismatch.
    """
    lines = file_content.split("\n")
    out: list[str] = []
    pos = 0

    for header, removed, added, context in hunks:
        # If no context lines, use first removal (or addition) as anchor
        if not context and removed:
            # Find the first removal in the remaining file lines
            anchor = removed[0]
            found = False
            for j in range(pos, len(lines)):
                if lines[j] == anchor:
                    # Keep all intermediate lines between current position
                    # and the found anchor position
                    for k in range(pos, j):
                        out.append(lines[k])
                    pos = j  # match at this position
                    found = True
                    break
            if not found:
                raise ValueError(
                    "apply_patch verification failed: hunk anchor not found in file"
                )

        # Match context lines against file content at current position.
        for ctx_line in context:
            if pos >= len(lines):
                raise ValueError("apply_patch verification failed: hunk context exhausted too early")
            if lines[pos] != ctx_line:
                raise ValueError("apply_patch verification failed: hunk context mismatch")
            out.append(lines[pos])
            pos += 1

        # Remove lines: verify each matches, then skip
        for removed_line in removed:
            if pos >= len(lines):
                raise ValueError("apply_patch verification failed: hunk removal ran off end")
            if lines[pos] != removed_line:
                raise ValueError(
                    "apply_patch verification failed: hunk removal mismatch"
                )
            pos += 1

        # Add lines: insert them
        out.extend(added)

    # Append remaining lines after all hunks
    out.extend(lines[pos:])
    return "\n".join(out)


def _rel_path(p: Path, cwd: Path) -> str:
    """Return a POSIX-style relative path from cwd to p."""
    try:
        rel = p.relative_to(cwd)
    except ValueError:
        return str(p).replace("\\", "/")
    return str(rel).replace("\\", "/")


def apply_patch_tool(cwd: str, patchText: str) -> dict[str, Any]:
    # Normalize line endings
    text = patchText.replace("\r\n", "\n").replace("\r", "\n")
    text = text.strip()

    if text == f"{_ENVELOPE_START}\n{_ENVELOPE_END}":
        raise ValueError("patch rejected: empty patch")
    if text.strip() == "":
        raise ValueError("patch rejected: empty patch")

    if not text.startswith(_ENVELOPE_START + "\n"):
        raise ValueError("apply_patch verification failed: missing *** Begin Patch header")
    if not text.endswith("\n" + _ENVELOPE_END):
        raise ValueError("apply_patch verification failed: missing *** End Patch trailer")

    inner = text[len(_ENVELOPE_START) + 1:-len(_ENVELOPE_END) - 1]
    all_lines = inner.split("\n")

    # sections: list of (kind, path, dest_or_None, payload_or_None)
    sections: list[tuple[str, str, str | None, Any]] = []
    i = 0
    while i < len(all_lines):
        line = all_lines[i]
        if line.startswith(_ADD + " "):
            path = line[len(_ADD):].strip()
            if not path:
                raise ValueError("apply_patch verification failed: Add File requires a path")
            content_lines: list[str] = []
            i += 1
            while i < len(all_lines):
                cline = all_lines[i]
                if cline.startswith(_DELETE + " ") or cline.startswith(_UPDATE + " ") or cline.startswith(_ADD + " ") or cline.startswith(_ENVELOPE_END):
                    break
                if cline.startswith("+"):
                    content_lines.append(cline[1:])
                elif cline == "":
                    content_lines.append("")
                else:
                    raise ValueError(f"apply_patch verification failed: unexpected line in Add File section: {cline!r}")
                i += 1
            sections.append(("add", path, None, content_lines))
        elif line.startswith(_DELETE + " "):
            path = line[len(_DELETE):].strip()
            if not path:
                raise ValueError("apply_patch verification failed: Delete File requires a path")
            sections.append(("delete", path, None, None))
            i += 1
        elif line.startswith(_UPDATE + " "):
            path = line[len(_UPDATE):].strip()
            if not path:
                raise ValueError("apply_patch verification failed: Update File requires a path")
            dest: str | None = None
            i += 1
            if i < len(all_lines) and all_lines[i].startswith(_MOVE + " "):
                dest = all_lines[i][len(_MOVE):].strip()
                if not dest:
                    raise ValueError("apply_patch verification failed: Move to requires a path")
                i += 1
            raw_hunk: list[str] = []
            while i < len(all_lines):
                cline = all_lines[i]
                if cline.startswith(_DELETE + " ") or cline.startswith(_UPDATE + " ") or cline.startswith(_ADD + " ") or cline.startswith(_ENVELOPE_END):
                    break
                raw_hunk.append(cline)
                i += 1
            if not raw_hunk:
                raise ValueError("apply_patch verification failed: Update File must have hunks")
            hunks = _parse_hunks(raw_hunk)
            sections.append(("update", path, dest, hunks))
        elif line.startswith(_ENVELOPE_END):
            i += 1
        else:
            raise ValueError(f"apply_patch verification failed: unknown header: {line!r}")

    if not sections:
        raise ValueError("apply_patch verification failed: no hunks found")

    # --- Validation phase (all-or-nothing) ---
    cwd_path = Path(cwd).resolve()

    for kind, path, _dest, _payload in sections:
        if kind in ("update", "delete"):
            resolved = resolve_to_cwd(path, cwd)
            if not resolved.exists() or not resolved.is_file():
                raise ValueError(f"apply_patch verification failed: Failed to read file to update: {path}")

    for kind, path, _dest, payload in sections:
        if kind == "update":
            resolved = resolve_to_cwd(path, cwd)
            file_content = resolved.read_text(encoding="utf-8", errors="ignore").replace("\r\n", "\n").replace("\r", "\n")
            _validate_hunks(payload, file_content)

    # --- Apply phase ---
    results: list[tuple[str, str]] = []
    diff_parts: list[str] = []

    for kind, path, dest, payload in sections:
        resolved = resolve_to_cwd(path, cwd)

        if kind == "add":
            content_lines = payload  # type: ignore[assignment]
            content = "\n".join(content_lines)
            if not content.endswith("\n"):
                content += "\n"
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
            rel = _rel_path(resolved, cwd_path)
            results.append(("A", rel))
            diff_parts.append("".join(difflib.unified_diff(
                [], content.splitlines(keepends=True),
                fromfile=rel, tofile=rel,
            )))

        elif kind == "delete":
            resolved.unlink()
            rel = _rel_path(resolved, cwd_path)
            results.append(("D", rel))

        elif kind == "update":
            resolved = resolve_to_cwd(path, cwd)
            old_content = resolved.read_text(encoding="utf-8", errors="ignore").replace("\r\n", "\n").replace("\r", "\n")
            new_content = _validate_hunks(payload, old_content)

            if dest is not None:
                new_resolved = resolve_to_cwd(dest, cwd)
                new_resolved.parent.mkdir(parents=True, exist_ok=True)
                new_resolved.write_text(new_content, encoding="utf-8")
                resolved.unlink()
                rel = _rel_path(new_resolved, cwd_path)
            else:
                resolved.write_text(new_content, encoding="utf-8")
                rel = _rel_path(resolved, cwd_path)

            results.append(("M", rel))
            diff_parts.append("".join(difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=rel, tofile=rel,
            )))

    summary_lines = "\n".join(f"{act} {rp}" for act, rp in results)
    combined_diff = "".join(diff_parts)

    return {
        "content": [{"type": "text", "text": f"Success. Updated the following files:\n{summary_lines}"}],
        "details": {"diff": combined_diff},
    }
