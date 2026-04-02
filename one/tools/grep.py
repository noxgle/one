from __future__ import annotations

import re
from pathlib import Path

from .common import resolve_to_cwd


def grep_tool(cwd: str, pattern: str, path: str = ".") -> dict:
    root = resolve_to_cwd(path, cwd)
    if not root.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    rgx = re.compile(pattern)
    matches: list[str] = []

    files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
    for file in files:
        try:
            for i, line in enumerate(file.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
                if rgx.search(line):
                    matches.append(f"{file}:{i}:{line}")
        except Exception:
            continue

    return {"content": [{"type": "text", "text": "\n".join(matches) if matches else "No matches"}], "details": None}
