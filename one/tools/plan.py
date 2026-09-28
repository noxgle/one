from __future__ import annotations

from typing import Any

PLAN_STATUSES = frozenset({"pending", "in_progress", "completed", "blocked"})


def normalize_plan(plan: Any) -> list[dict[str, str]]:
    """Validate and normalize the canonical plan payload."""
    if not isinstance(plan, list):
        raise ValueError("plan must be a list of step objects; each item requires step and status")
    if not plan:
        raise ValueError("plan must contain at least one step")

    normalized: list[dict[str, str]] = []
    in_progress = 0
    for index, item in enumerate(plan):
        label = f"plan[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{label} must be an object with step and status")
        step = item.get("step")
        if not isinstance(step, str) or not step.strip():
            raise ValueError(f"{label}.step must be a non-empty string")
        status = item.get("status")
        if not isinstance(status, str) or status not in PLAN_STATUSES:
            allowed = ", ".join(sorted(PLAN_STATUSES))
            raise ValueError(f"{label}.status must be one of: {allowed}")
        if status == "in_progress":
            in_progress += 1
        normalized.append({"step": step.strip(), "status": status})
    if in_progress > 1:
        raise ValueError("plan may contain at most one item with status 'in_progress'")
    return normalized


def render_plan(plan: list[dict[str, str]] | str | None, *, max_chars: int | None = None) -> str:
    """Render canonical plans and legacy persisted strings for display/context."""
    if plan is None:
        rendered = ""
    elif isinstance(plan, str):
        rendered = plan
    else:
        markers = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]", "blocked": "[!]"}
        rendered = "\n".join(f"{markers[item['status']]} {item['step']}" for item in plan)
    if max_chars is not None:
        if max_chars <= 0:
            return ""
        if len(rendered) > max_chars:
            return rendered[: max_chars - 1] + "…"
    return rendered


def plan_tool(plan: Any) -> dict:
    """Store an execution plan for the current task.

    The plan is injected into the system prompt on every step so the model
    always sees it. Adapt it via the plan tool when the situation changes
    materially.
    """
    normalized = normalize_plan(plan)
    return {
        "ok": True,
        "result": "Plan stored. Follow it; adapt it via the plan tool when the situation changes materially.",
        "plan": normalized,
    }
