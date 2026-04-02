from __future__ import annotations

import os
from pathlib import Path

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 256000


def resolve_to_cwd(path: str, cwd: str) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p.resolve()
    return (Path(cwd) / p).resolve()


def truncate_head(text: str, max_lines: int = DEFAULT_MAX_LINES, max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    lines = text.split("\n")
    if len(lines) > max_lines:
        content = "\n".join(lines[:max_lines])
        return {
            "content": content,
            "truncated": True,
            "truncatedBy": "lines",
            "totalLines": len(lines),
            "outputLines": max_lines,
            "maxLines": max_lines,
            "maxBytes": max_bytes,
        }

    raw = text.encode("utf-8")
    if len(raw) > max_bytes:
        clipped = raw[:max_bytes].decode("utf-8", errors="ignore")
        out_lines = clipped.split("\n")
        return {
            "content": clipped,
            "truncated": True,
            "truncatedBy": "bytes",
            "totalLines": len(lines),
            "outputLines": len(out_lines),
            "outputBytes": len(clipped.encode("utf-8")),
            "maxLines": max_lines,
            "maxBytes": max_bytes,
        }

    return {
        "content": text,
        "truncated": False,
        "truncatedBy": None,
        "totalLines": len(lines),
        "outputLines": len(lines),
        "maxLines": max_lines,
        "maxBytes": max_bytes,
    }


def truncate_tail(text: str, max_lines: int = DEFAULT_MAX_LINES, max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    lines = text.split("\n")
    if len(lines) > max_lines:
        content = "\n".join(lines[-max_lines:])
        return {
            "content": content,
            "truncated": True,
            "truncatedBy": "lines",
            "totalLines": len(lines),
            "outputLines": max_lines,
            "maxLines": max_lines,
            "maxBytes": max_bytes,
            "lastLinePartial": False,
        }

    raw = text.encode("utf-8")
    if len(raw) > max_bytes:
        clipped = raw[-max_bytes:].decode("utf-8", errors="ignore")
        out_lines = clipped.split("\n")
        return {
            "content": clipped,
            "truncated": True,
            "truncatedBy": "bytes",
            "totalLines": len(lines),
            "outputLines": len(out_lines),
            "outputBytes": len(clipped.encode("utf-8")),
            "maxLines": max_lines,
            "maxBytes": max_bytes,
            "lastLinePartial": True,
        }

    return {
        "content": text,
        "truncated": False,
        "truncatedBy": None,
        "totalLines": len(lines),
        "outputLines": len(lines),
        "maxLines": max_lines,
        "maxBytes": max_bytes,
        "lastLinePartial": False,
    }
