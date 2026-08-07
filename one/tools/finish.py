from __future__ import annotations


async def finish_tool(summary: str, goal_success: bool = True) -> dict:
    """Signal task completion and end the current turn.

    The harness treats a `finish` tool call as terminal: the summary becomes
    the final assistant message and no further provider calls are made for
    this turn.
    """
    text = (summary or "").strip() or "Task complete."
    return {
        "summary": text,
        "goal_success": bool(goal_success),
        "isFinish": True,
        "output": text,
        "content": [{"type": "text", "text": text}],
    }
