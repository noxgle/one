from __future__ import annotations

import re
from pathlib import Path

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 256000

# Matches CSI sequences (ESC [ ... final byte), OSC sequences (ESC ] ... BEL or ST),
# and lone ESC bytes.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b"
)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from *text* (CSI, OSC, lone ESC)."""
    return _ANSI_ESCAPE_RE.sub("", text)


# Control characters that are safe to render in a terminal UI.
_SAFE_CONTROL_CHARS = frozenset({"\t", "\n"})


def sanitize_display_text(text: str) -> str:
    """Make untrusted text safe to render in a terminal UI.

    Strips ANSI escape sequences and replaces every remaining control
    character (C0 except tab/newline, C1 U+0080-U+009F, DEL, stray ESC)
    with U+FFFD so rendering can never corrupt the terminal state.
    Printable characters (including box-drawing) pass through unchanged.
    """
    text = strip_ansi(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for ch in text:
        cp = ord(ch)
        if ch in _SAFE_CONTROL_CHARS:
            out.append(ch)
        elif cp < 0x20 or 0x7F <= cp <= 0x9F:
            out.append("\ufffd")
        else:
            out.append(ch)
    return "".join(out)


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
