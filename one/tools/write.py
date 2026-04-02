from __future__ import annotations

from .common import resolve_to_cwd


def write_tool(cwd: str, path: str, content: str) -> dict:
    p = resolve_to_cwd(path, cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {
        "content": [{"type": "text", "text": f"Successfully wrote {len(content)} bytes to {path}"}],
        "details": None,
    }
