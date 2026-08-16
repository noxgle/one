from __future__ import annotations


def plan_tool(plan: str) -> dict:
    """Store an execution plan for the current task.

    The plan is injected into the system prompt on every step so the model
    always sees it. Adapt it via the plan tool when the situation changes
    materially.
    """
    if not plan or not plan.strip():
        raise ValueError("plan must be a non-empty string")
    return {
        "ok": True,
        "result": "Plan stored. Follow it; adapt it via the plan tool when the situation changes materially.",
    }
