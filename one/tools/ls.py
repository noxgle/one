from __future__ import annotations

from .common import resolve_to_cwd


def ls_tool(cwd: str, path: str = ".") -> dict:
    p = resolve_to_cwd(path, cwd)
    if not p.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    if p.is_file():
        return {"content": [{"type": "text", "text": str(p)}], "details": None}
    lines = []
    for child in sorted(p.iterdir(), key=lambda x: x.name):
        suffix = "/" if child.is_dir() else ""
        lines.append(child.name + suffix)
    return {"content": [{"type": "text", "text": "\n".join(lines)}], "details": None}
