"""Small, dependency-free schemas and deterministic checks for diagnostic runs."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

SCHEMA_VERSION = "one.diagnostic/v1"
MAX_TEXT = 4096
CONTEXT_EXHAUSTION = re.compile(
    r"(?:context|token).{0,48}(?:limit|exceed(?:ed|s|ing)?|length|window)"
    r"|(?:limit|exceed(?:ed|s|ing)?|length).{0,48}(?:context|token)"
    r"|context[_ -]?(?:length|window)[_ -]?(?:limit|exceed(?:ed|s|ing)?)",
    re.I,
)


def redact(value: Any, limit: int = MAX_TEXT) -> Any:
    """Recursively remove common credentials and bound untrusted diagnostic text."""
    if isinstance(value, dict):
        return {str(key): redact("[REDACTED]" if re.search(r"(?:api[_-]?key|token|password|secret|authorization)", str(key), re.I) else item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, limit) for item in value[:200]]
    if isinstance(value, str):
        value = re.sub(r"(?i)\b(?:bearer\s+)?(?:sk-[\w-]+|api[_-]?key\s*[=:]\s*[^\s,]+|token\s*[=:]\s*[^\s,]+|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})", "[REDACTED]", value)
        return value[:limit] + ("…[truncated]" if len(value) > limit else "")
    return value


def finding(
    identifier: str, severity: str, category: str, summary: str, evidence: list[str], cause: str, confidence: str = "medium"
) -> dict[str, Any]:
    return {"id": identifier, "severity": severity, "category": category, "summary": summary, "evidence": evidence, "probableCause": cause, "confidence": confidence}


def detect(events: list[dict[str, Any]], diagnostics: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return evidence-based suspicions; these deliberately do not assert causes as facts."""
    diagnostics = diagnostics or {}
    output: list[dict[str, Any]] = []
    types = [str(event.get("type", "unknown")) for event in events]
    counts = Counter(types)
    context_evidence: list[str] = []
    for index, event in enumerate(events):
        if event.get("type") in {"malformed", "stdout_text", "non_object_output"}:
            evidence = f"events[{index}]"
            raw = event.get("raw")
            if raw is not None:
                evidence += f":raw={redact(raw, 256)!r}"
            source = event.get("source")
            if source:
                evidence += f":source={redact(source, 128)!r}"
            output.append(finding("malformed-rpc", "medium", "protocol", "Non-JSON or non-object RPC output was collected.", [evidence], "The child process may have written diagnostics to stdout.", "high"))
        if event.get("type") == "tool_call_end" and not event.get("toolCallId"):
            output.append(finding("malformed-tool-lifecycle", "medium", "lifecycle", "A tool completion has no tool-call identifier.", [f"events[{index}]"], "The event producer may not have supplied correlation metadata."))
        if event.get("type") in {"error", "provider_error", "tool_error"} or event.get("success") is False:
            output.append(finding("runtime-failure", "medium", "provider" if "provider" in str(event.get("type")) else "tool", "A provider, tool, or RPC request reported failure.", [f"events[{index}]"], "The recorded failure needs inspection; no root cause is inferred.", "high"))
        # Event type, message, and error fields are the evidence-bearing locations
        # used by the RPC adapters for context-window failures.
        context_text = " ".join(
            str(event.get(key, ""))
            for key in ("type", "message", "error", "errors", "detail", "reason", "code")
        )
        if CONTEXT_EXHAUSTION.search(context_text):
            context_evidence.append(f"events[{index}]")
    if counts["agent_start"] and not counts["agent_end"]:
        output.append(finding("missing-agent-end", "high", "lifecycle", "Agent start was observed without a matching agent end.", ["events:type=agent_start"], "The process may have stalled, been terminated, or omitted a lifecycle event."))
    terminal_groups: Counter[str] = Counter()
    for index, event in enumerate(events):
        if event.get("type") != "agent_end":
            continue
        # Legacy streams lack a boundary, so retain the previous conservative
        # global check. New RPC streams provide turnId/requestId.
        key = event.get("turnId") or event.get("requestId")
        terminal_groups[str(key) if key else "legacy"] += 1
    for key, count in terminal_groups.items():
        if count > 1:
            output.append(finding("duplicate-agent-end", "medium", "lifecycle", "More than one agent-end event was observed for one turn.", [f"agentEnd:{key}"], "Duplicate emission or merged runs are possible."))
    starts = Counter(str(e.get("toolCallId")) for e in events if e.get("type") == "tool_call_start" and e.get("toolCallId"))
    ends = Counter(str(e.get("toolCallId")) for e in events if e.get("type") == "tool_call_end" and e.get("toolCallId"))
    for call_id, count in starts.items():
        if count > 1 or ends[call_id] != 1:
            output.append(finding("tool-lifecycle-mismatch", "medium", "lifecycle", f"Tool call {call_id!r} has {count} starts and {ends[call_id]} ends.", [f"toolCallId:{call_id}"], "A tool lifecycle event may be missing or duplicated."))
    if diagnostics.get("timedOut"):
        output.append(finding("workload-timeout", "medium", "timeout", "The workload reached its deadline.", ["manifest:timedOut"], "The workload or child process did not complete within its configured budget.", "high"))
    if diagnostics.get("retries", 0):
        output.append(finding("retries-observed", "low", "retry", "Retries were observed during the run.", ["manifest:retries"], "Retries can be expected; inspect paired failures before treating this as a defect.", "high"))
    if diagnostics.get("cleanup", {}).get("ok") is False:
        output.append(finding("cleanup-failure", "high", "cleanup", "One or more temporary diagnostic resources could not be removed.", ["manifest:cleanup"], "The operating system or Docker runtime rejected cleanup."))
    coverage = diagnostics.get("coverage")
    missing = diagnostics.get("missingCoverage")
    requested: set[str] = set()
    unobserved: set[str] = set()
    if isinstance(coverage, dict):
        for name, state in coverage.items():
            if isinstance(state, dict) and state.get("expected") is True and state.get("kind") != "rpc":
                requested.add(str(name))
                if state.get("observed") is not True and state.get("category") != "capability_unavailable":
                    unobserved.add(str(name))
    if isinstance(missing, list):
        listed = {str(name) for name in missing if isinstance(name, str) and name}
        unobserved.update(listed & requested if isinstance(coverage, dict) else listed)
    if unobserved:
        evidence = [f"coverage:{name}" for name in sorted(unobserved)]
        severity = "high" if requested and requested <= unobserved else "medium"
        output.append(finding(
            "tool-coverage", severity, "coverage",
            f"Requested built-in scenarios were not observed: {', '.join(sorted(unobserved))}.",
            evidence,
            "The model, RPC process, or workload deadline did not produce the expected built-in scenario activity.",
            "high",
        ))
    warnings = diagnostics.get("coverageWarnings")
    if isinstance(warnings, list):
        unavailable = [item for item in warnings if isinstance(item, dict) and item.get("category") == "capability_unavailable"]
        if unavailable:
            output.append(finding(
                "coverage-capability-unavailable", "low", "coverage",
                "Expected scenario coverage was unavailable because the capability is disabled or unadvertised.",
                [f"coverage:{item.get('scenario', 'unknown')}:capability_unavailable" for item in unavailable[:20]],
                "The diagnostic retained the unavailable scenario as configuration evidence rather than treating it as model/tool coverage failure.",
                "high",
            ))
    if context_evidence:
        output.append(finding(
            "context-window-exhaustion", "high", "context",
            "A recorded event indicates that the model context window was exhausted or exceeded.",
            context_evidence[:20],
            "The provider or runtime rejected input because its context-window limit was reached.",
            "high",
        ))
    return output


def validate_report(report: dict[str, Any]) -> None:
    if not isinstance(report, dict):
        raise ValueError("invalid diagnostic report; report must be an object")
    required = {"schemaVersion", "run", "findings", "modelAnalysis", "cleanup", "reportPaths"}
    missing = required - set(report)
    if (missing or report.get("schemaVersion") != SCHEMA_VERSION or not isinstance(report.get("run"), dict)
            or not isinstance(report.get("findings"), list) or not isinstance(report.get("modelAnalysis"), dict)
            or not isinstance(report.get("cleanup"), dict) or not isinstance(report.get("reportPaths"), dict)):
        raise ValueError(f"invalid diagnostic report; missing or invalid fields: {sorted(missing)}")
    for item in report["findings"]:
        if not isinstance(item, dict) or not {"severity", "category", "summary", "evidence", "probableCause", "confidence"}.issubset(item):
            raise ValueError("invalid finding")
        if item["severity"] not in {"low", "medium", "high", "critical"}:
            raise ValueError("invalid finding severity")
        if not all(isinstance(item[key], str) and item[key] for key in ("category", "summary", "probableCause", "confidence")):
            raise ValueError("invalid finding text")
        if not isinstance(item["evidence"], list) or not item["evidence"] or not all(isinstance(ref, str) and ref for ref in item["evidence"]):
            raise ValueError("invalid finding evidence")
    telemetry = report["run"].get("telemetry")
    if telemetry is not None:
        if not isinstance(telemetry, dict) or not isinstance(telemetry.get("samples", []), list) or not isinstance(telemetry.get("workspaceSamples", []), list) or not isinstance(telemetry.get("summary", {}), dict):
            raise ValueError("invalid telemetry")
