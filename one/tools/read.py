from __future__ import annotations

from .common import resolve_to_cwd, truncate_head


def read_tool(cwd: str, path: str, offset: int | None = None, limit: int | None = None) -> dict:
    p = resolve_to_cwd(path, cwd)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    # Images are binary: never dump decoded bytes. Direct the agent to read_image.
    try:
        with open(p, "rb") as f:
            head = f.read(12)
        if head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff") or (
            head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP"
        ):
            raise ValueError(
                f"File is an image ({path}). Use the 'read_image' tool with the same path to load it for vision inspection."
            )
    except ValueError:
        raise
    except OSError:
        pass

    content = p.read_text(encoding="utf-8", errors="ignore")
    lines = content.split("\n")
    start = max((offset or 1) - 1, 0)
    if start >= len(lines):
        raise ValueError(f"Offset {offset} is beyond end of file ({len(lines)} lines total)")

    selected = lines[start : start + limit] if limit is not None else lines[start:]
    selected_text = "\n".join(selected)
    trunc = truncate_head(selected_text)
    output = trunc["content"]

    if trunc["truncated"]:
        end_line = start + trunc["outputLines"]
        output += f"\n\n[Showing lines {start+1}-{end_line} of {len(lines)}. Use offset={end_line+1} to continue.]"
    elif limit is not None and start + len(selected) < len(lines):
        next_offset = start + len(selected) + 1
        output += f"\n\n[{len(lines) - (start + len(selected))} more lines in file. Use offset={next_offset} to continue.]"

    return {
        "content": [{"type": "text", "text": output}],
        "details": {"truncation": trunc if trunc["truncated"] else None},
    }
