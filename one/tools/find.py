from __future__ import annotations

from .common import resolve_to_cwd


def find_tool(cwd: str, pattern: str = "*", path: str = ".") -> dict:
    root = resolve_to_cwd(path, cwd)
    if not root.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    files = [str(p) for p in root.rglob(pattern)]
    return {"content": [{"type": "text", "text": "\n".join(files) if files else "No files found"}], "details": None}
