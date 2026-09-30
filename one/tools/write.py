from __future__ import annotations

from .common import resolve_to_cwd


def write_tool(cwd: str, path: str, content: str) -> dict:
    # AgentSession validates tool-call arguments before filesystem operations;
    # retain this narrow guard for direct callers.
    if not isinstance(path, str) or not path:
        raise ValueError("write requires args.path (or args.file) to be a non-empty string")
    if not isinstance(content, str):
        raise ValueError("write requires explicit string args.content (or legacy args.text)")
    p = resolve_to_cwd(path, cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {
        "content": [{"type": "text", "text": f"Successfully wrote {len(content)} bytes to {path}"}],
        "details": None,
    }
