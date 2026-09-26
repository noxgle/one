#!/usr/bin/env python3
"""Drive a disposable, real ``one`` RPC session through a fixed workload."""
from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

SCENARIOS = (
    "read", "read_image", "bash", "write", "write_html_preservation", "edit", "apply_patch", "grep", "find", "ls",
    "evidence_read", "plan", "finish", "ask_user", "spawn_subagent", "retry", "timeout",
    "steering", "follow_up", "compaction", "reload",
)
# ``None`` identifies scenarios driven by RPC controls rather than a tool call.
# Keep this explicit: control IDs happen to resemble tool names in some cases,
# but are not part of the advertised get_tools capability set.
SCENARIO_REQUIRED_TOOLS: dict[str, str | None] = {
    "read": "read",
    "read_image": "read_image",
    "bash": "bash",
    "write": "write",
    "write_html_preservation": "write",
    "edit": "edit",
    "apply_patch": "apply_patch",
    "grep": "grep",
    "find": "find",
    "ls": "ls",
    "evidence_read": "evidence_read",
    "plan": "plan",
    "finish": "finish",
    "ask_user": "ask_user",
    "spawn_subagent": "spawn_subagent",
    "retry": None,
    "timeout": None,
    "steering": None,
    "follow_up": None,
    "compaction": None,
    "reload": None,
}
SCENARIO_REQUIRED_CAPABILITIES = {"read_image": "inputImage"}
MAX_EVENTS = 2_000
MAX_MALFORMED = 100
MAX_AUTO_ANSWERS = 100
MAX_SCENARIO_RECORDS = 200
MAX_COVERAGE_WARNINGS = 50
SCENARIO_DRAIN_GRACE_SEC = 60.0
SIMPLE_SCENARIO_TIMEOUT_SEC = 20.0
COMPLEX_SCENARIO_TIMEOUT_SEC = 60.0
COMPLEX_SCENARIOS = frozenset({"evidence_read", "plan", "ask_user", "spawn_subagent"})
SCENARIO_TIMEOUTS = {
    name: COMPLEX_SCENARIO_TIMEOUT_SEC if name in COMPLEX_SCENARIOS else SIMPLE_SCENARIO_TIMEOUT_SEC
    for name in SCENARIOS
}
RECOVERY_TIMEOUT_SEC = 20.0
HTML_PRESERVATION_CONTENT = "\ufeff<html data-marker=\"&lt;marker&gt;\">smart ‘quotes’ &amp; literal &lt;tag&gt; — Ω</html>\n<!-- *** Begin Patch marker-like text -->\n"


def create_fixture(workspace: Path) -> dict[str, str]:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (workspace / "large.txt").write_text("diagnostic-line\n" * 512, encoding="utf-8")
    (workspace / "patch-target.txt").write_text("before\n", encoding="utf-8")
    (workspace / "html-preservation-source.txt").write_text(HTML_PRESERVATION_CONTENT, encoding="utf-8")
    (workspace / "fixture.png").write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL6xQAAAABJRU5ErkJggg=="))
    return {"workspace": str(workspace), "image": "fixture.png", "text": "notes.txt", "scenarios": ",".join(SCENARIOS)}


def workload_plan(duration: float, workload: str = "safe") -> list[dict[str, object]]:
    if workload not in {"safe", "stress", "custom"}:
        raise ValueError("workload must be safe, stress, or custom")
    return [{"id": name, "expected": "bounded", "timeoutSec": min(SCENARIO_TIMEOUTS[name], duration)} for name in SCENARIOS]


def scenario_prompt(name: str) -> str:
    """Prompts deliberately name the tool: text-tool models need an unambiguous contract."""
    tool = name
    if name in {"retry", "timeout", "steering", "follow_up", "compaction", "reload"}:
        return "A runtime control diagnostic is being run. Acknowledge the requested control and finish."
    details = {
        "read": "Call read on notes.txt, then finish.",
        "read_image": "Call read_image on fixture.png, then finish.",
        "bash": "Call bash with `pwd` only, then finish.",
        "write": "Call write to create generated.txt with exactly `diagnostic`, then finish.",
        "write_html_preservation": "Call read on html-preservation-source.txt, then call write to create html-preservation-output.txt with exactly the returned content, preserving its BOM, literal HTML, &lt;, &amp;, Unicode smart quotes, and marker-like text. Call read on html-preservation-output.txt to verify it exactly matches the source, then finish.",
        "edit": "Call edit to replace `diagnostic` with `edited` in generated.txt, then finish.",
        "apply_patch": "Call apply_patch with patchText exactly in this valid OpenCode format (NOT ---/+++ unified diff): `*** Begin Patch\n*** Update File: patch-target.txt\n@@\n-before\n+after\n*** End Patch`. Then finish.",
        "grep": "Call grep for `alpha` in notes.txt, then finish.",
        "find": "Call find for *.txt, then finish.",
        "ls": "Call ls on ., then finish.",
        "evidence_read": "First call read on notes.txt. Use the diagnostic evidenceId returned by that read to call evidence_read with offset 0 and maxChars 256. Do not read host state. Then finish.",
        "plan": "This is a genuinely complex diagnostic with three dependent phases: inspect notes.txt, verify the fixture image, and patch patch-target.txt, followed by validation. Call plan with those 3+ phases, execute the fixture-only steps, verify them, then finish.",
        "finish": "Call finish with a concise completion summary.",
        "ask_user": "Call ask_user with the harmless diagnostic question `Should the fixture-only diagnostic continue?` and timeoutSec 30. Wait for the automatic answer, then call finish.",
        "spawn_subagent": "Call spawn_subagent with task `In the fixture workspace only, read notes.txt and return its two lines; do not modify files or inspect host state. Finish with the two-line result.`. Report the child result, then call finish exactly once.",
    }
    return f"Work only in this fixture workspace. {details.get(name, f'Call {tool}, then finish.')}"


def _available_tools(response: dict[str, Any] | None) -> set[str] | None:
    """Extract advertised RPC tools without treating an unavailable query as evidence."""
    data = response.get("data") if isinstance(response, dict) else None
    tools = data.get("tools") if isinstance(data, dict) else None
    if not isinstance(tools, list):
        return None
    names: set[str] = set()
    for tool in tools:
        if isinstance(tool, str):
            names.add(tool)
        elif isinstance(tool, dict) and isinstance(name := tool.get("name"), str):
            names.add(name)
    return names


def _capabilities(response: dict[str, Any] | None) -> dict[str, bool] | None:
    """Extract explicitly reported boolean RPC capabilities."""
    data = response.get("data") if isinstance(response, dict) else None
    capabilities = data.get("capabilities") if isinstance(data, dict) else None
    if not isinstance(capabilities, dict):
        return None
    return {name: value for name, value in capabilities.items() if isinstance(name, str) and isinstance(value, bool)}


def _scenario_unavailable(name: str, available_tools: set[str] | None, capabilities: dict[str, bool] | None) -> str | None:
    required_tool = SCENARIO_REQUIRED_TOOLS[name]
    if required_tool is not None and available_tools is not None and required_tool not in available_tools:
        return f"{required_tool} was not advertised by the RPC session configuration."
    required_capability = SCENARIO_REQUIRED_CAPABILITIES.get(name)
    if required_capability and capabilities is not None and capabilities.get(required_capability) is False:
        return f"{required_capability} is unavailable for the selected model."
    return None


def _tool_preflight_status(name: str, available_tools: set[str] | None, capabilities: dict[str, bool] | None) -> str:
    if detail := _scenario_unavailable(name, available_tools, capabilities):
        return detail
    if available_tools is None:
        return "unknown (get_tools did not return an advertised tool list)"
    return "advertised"


def _online_tool_counts(driver: Any, event_start: int = 0) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Return unbounded tool counters, falling back to retained test events."""
    counter_names = ("tool_observed_counts", "successful_tool_counts", "failed_tool_counts")
    if all(hasattr(driver, name) for name in counter_names):
        return tuple(dict(getattr(driver, name)) for name in counter_names)  # type: ignore[return-value]
    counts: list[dict[str, int]] = [{}, {}, {}]
    for event in driver.events[event_start:]:
        tool_name = event.get("toolName") or event.get("tool") or event.get("name")
        if not isinstance(tool_name, str):
            continue
        index = 0 if event.get("type") == "tool_call_start" else 1 if event.get("type") == "tool_call_end" and event.get("ok") is True else 2 if event.get("type") == "tool_call_end" and event.get("ok") is False else None
        if index is not None:
            counts[index][tool_name] = counts[index].get(tool_name, 0) + 1
    return counts[0], counts[1], counts[2]


def _tool_count_delta(before: dict[str, int], after: dict[str, int]) -> set[str]:
    return {name for name, count in after.items() if count > before.get(name, 0)}


def classify_coverage(
    attempted: set[str], observed: set[str], available_tools: set[str] | None,
    idle: dict[str, int], final_idle_deadline_truncated: bool,
    capabilities: dict[str, bool] | None = None,
    successful_tools: set[str] | None = None,
    failed_tools: set[str] | None = None,
) -> tuple[dict[str, dict[str, object]], list[str], list[dict[str, str]]]:
    """Classify coverage without conflating disabled tools or deadline cleanup with misses."""
    coverage: dict[str, dict[str, object]] = {}
    warnings: list[dict[str, str]] = []
    missing: list[str] = []
    for name in SCENARIOS:
        expected = name in attempted
        state: dict[str, object] = {"expected": expected, "observed": name in observed}
        required_tool = SCENARIO_REQUIRED_TOOLS[name]
        if required_tool is not None:
            state["success"] = name in (successful_tools or set())
        if expected and state["observed"] and name in (failed_tools or set()) and name not in (successful_tools or set()):
            state["category"] = "tool_execution_failed"
        elif expected and not state["observed"]:
            if detail := _scenario_unavailable(name, available_tools, capabilities):
                state["category"] = "capability_unavailable"
                warnings.append({"scenario": name, "category": "capability_unavailable", "detail": detail})
            else:
                state["category"] = "missing_model_or_tool_coverage"
                missing.append(name)
        coverage[name] = state
    idle_expected = idle["requested"] > 0
    idle_observed = idle["completed"] > 0
    idle_state: dict[str, object] = {"kind": "rpc", "expected": idle_expected, "observed": idle_observed, "completed": idle["completed"], "failed": idle["failed"], "timedOut": idle["timedOut"], "partial": idle_observed and (idle["failed"] > 0 or idle["timedOut"] > 0)}
    if idle_expected and not idle_observed and final_idle_deadline_truncated:
        idle_state["category"] = "deadline_truncated"
        warnings.append({"scenario": "wait_for_idle", "category": "deadline_truncated", "detail": "The final wait_for_idle reached the outer workload deadline after all planned scenarios were attempted."})
    elif idle_expected and not idle_observed:
        idle_state["category"] = "missing_model_or_tool_coverage"
        missing.append("wait_for_idle")
    coverage["wait_for_idle"] = idle_state
    return coverage, missing, warnings[:MAX_COVERAGE_WARNINGS]


class RpcDriver:
    """Persistent JSON-lines RPC process with bounded collection and hard cleanup."""

    def __init__(self, command: list[str], env: dict[str, str], cwd: Path | None = None, emit: Any = print) -> None:
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, env=env, cwd=cwd, start_new_session=True)
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.events: list[dict[str, Any]] = []
        self.malformed: list[dict[str, str]] = []
        self.responses: dict[str, dict[str, Any]] = {}
        self.emit = emit
        self.terminated_by_driver = False
        self._stream_closed = False
        self._answered_questions: set[str] = set()
        self._request_counter = 0
        self._event_sequence = 0
        self.tool_observed: set[str] = set()
        self.successful_tools: set[str] = set()
        self.failed_tools: set[str] = set()
        self.tool_observed_counts: dict[str, int] = {}
        self.successful_tool_counts: dict[str, int] = {}
        self.failed_tool_counts: dict[str, int] = {}
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.put(line.rstrip("\n"))
        self._stream_closed = True
        self.lines.put(None)

    def _record(self, line: str) -> dict[str, Any] | None:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            if len(self.malformed) < MAX_MALFORMED:
                self.malformed.append({"type": "malformed", "raw": line[:512]})
            self.emit(json.dumps({"type": "malformed", "raw": line[:512]}, sort_keys=True))
            return None
        if not isinstance(item, dict):
            if len(self.malformed) < MAX_MALFORMED:
                self.malformed.append({"type": "non_object_output"})
            self.emit(json.dumps({"type": "non_object_output"}))
            return None
        # Assign IDs as events arrive, independently from bounded retention.
        # The emitted stream and retained evidence therefore retain one stable,
        # contiguous diagnostic sequence even after the event buffer fills.
        self._event_sequence += 1
        item = dict(item)
        item["diagnosticEventId"] = self._event_sequence
        # Track coverage before applying the retained-event bound.
        tool_name = item.get("toolName") or item.get("tool") or item.get("name")
        if item.get("type") == "tool_call_start" and isinstance(tool_name, str):
            self.tool_observed.add(tool_name)
            self.tool_observed_counts[tool_name] = self.tool_observed_counts.get(tool_name, 0) + 1
        elif item.get("type") == "tool_call_end" and isinstance(tool_name, str):
            if item.get("ok") is True:
                self.successful_tools.add(tool_name)
                self.successful_tool_counts[tool_name] = self.successful_tool_counts.get(tool_name, 0) + 1
            elif item.get("ok") is False:
                self.failed_tools.add(tool_name)
                self.failed_tool_counts[tool_name] = self.failed_tool_counts.get(tool_name, 0) + 1
        if len(self.events) < MAX_EVENTS:
            self.events.append(item)
        self.emit(json.dumps(item, ensure_ascii=False, sort_keys=True))
        return item

    def command(self, body: dict[str, Any], deadline: float) -> dict[str, Any] | None:
        assert self.process.stdin is not None
        if self._stream_closed:
            return None
        self._request_counter += 1
        request_id = body.setdefault("id", f"diagnostic-{self._request_counter}")
        try:
            self.process.stdin.write(json.dumps(body) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            self._stream_closed = True
            return None
        while time.monotonic() < deadline:
            if (saved := self.responses.pop(str(request_id), None)) is not None:
                return saved
            try:
                line = self.lines.get(timeout=min(0.2, deadline - time.monotonic()))
            except queue.Empty:
                continue
            if line is None:
                self._stream_closed = True
                return None
            event = self._record(line)
            if event and event.get("type") == "ask_user" and isinstance(event.get("id"), str):
                question_id = event["id"]
                if question_id not in self._answered_questions and len(self._answered_questions) < MAX_AUTO_ANSWERS:
                    self._answered_questions.add(question_id)
                    assert self.process.stdin is not None
                    self.process.stdin.write(json.dumps({"type": "answer_question", "id": f"answer-{question_id}", "questionId": question_id, "answer": "yes, continue diagnostic"}) + "\n")
                    self.process.stdin.flush()
            if event and event.get("type") == "response":
                if event.get("id") == request_id:
                    return event
                if isinstance(event.get("id"), str):
                    self.responses[event["id"]] = event
        return None

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                if self.process.stdin is not None and not self.process.stdin.closed:
                    self.process.stdin.close()
                self.process.wait(timeout=3)
            except (AttributeError, OSError, ValueError, subprocess.TimeoutExpired):
                self.terminated_by_driver = True
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                    self.process.wait(timeout=3)
                except OSError:
                    return
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    self.process.wait(timeout=3)
        self._reader.join(timeout=1)


def run_workload(workspace: Path, duration: float, provider: str, model: str, endpoint: str, emit: Any = print, monotonic: Any = time.monotonic, sleep: Any = time.sleep) -> dict[str, Any]:
    fixture = create_fixture(workspace)
    env = os.environ.copy()
    env["ONE_CODING_AGENT_DIR"] = str(workspace / "state")
    command = [sys.executable, "-m", "one.cli.main", "--mode", "rpc", "--provider", provider, "--model", model, "--llama-cpp-url", endpoint, "--no-extensions", "--no-mcp"]
    started = monotonic()
    deadline = started + duration
    # The fixture is both the tool workspace and the child cwd: scenario prompts
    # intentionally use relative paths such as ``notes.txt``.
    driver = RpcDriver(command, env, cwd=workspace, emit=emit)
    requested: list[str] = []
    attempted: set[str] = set()
    control_observed: set[str] = set()
    idle = {"requested": 0, "completed": 0, "failed": 0, "timedOut": 0}
    scenarios: list[dict[str, Any]] = []
    # Keep per-scenario attribution separately from the unbounded global tool
    # counters.  A tool name can appear in more than one scenario (notably
    # ``write``), so session-wide tool presence is not coverage evidence.
    scenario_tool_coverage: dict[str, dict[str, bool]] = {
        name: {"observed": False, "successful": False, "failed": False}
        for name in SCENARIOS
    }
    available_tools: set[str] | None = None
    capabilities: dict[str, bool] | None = None
    final_idle_deadline_truncated = False
    final_scenario_truncated = False
    clean_shutdown = False
    drain: dict[str, object] = {"graceSec": SCENARIO_DRAIN_GRACE_SEC, "attempted": False, "aborted": False, "cleanShutdown": False}
    execution_failed = False
    result: dict[str, Any]
    try:
        # Controls are real RPC commands; unavailable model failures remain visible.
        controls = [("retry", {"type": "set_auto_retry", "mode": "on"}), ("timeout", {"type": "inspect_subagent_timeout"}), ("steering", {"type": "steer", "message": "diagnostic steering"}), ("follow_up", {"type": "follow_up", "message": "diagnostic follow up"}), ("compaction", {"type": "compact"}), ("reload", {"type": "reload_resources"})]
        for name, body in controls:
            if monotonic() >= deadline:
                break
            requested.append(name)
            response = driver.command(body, deadline)
            if response and response.get("success") is True:
                control_observed.add(name)
            else:
                execution_failed = True
        if monotonic() < deadline:
            tools_response = driver.command({"type": "get_tools"}, deadline)
            available_tools = _available_tools(tools_response)
            capabilities = _capabilities(tools_response)
        index = 0
        while monotonic() < deadline:
            name = SCENARIOS[index % len(SCENARIOS)]
            scenario_started = monotonic()
            # This is intentionally per-iteration. An earlier RPC timeout must
            # not make a later, successful scenario look deadline-truncated.
            scenario_truncated = False
            attempted.add(name)
            scenario_timeout = min(SCENARIO_TIMEOUTS[name], duration)
            record: dict[str, Any] = {"id": name, "startSec": round(scenario_started - started, 3), "scenarioTimeoutSec": scenario_timeout, "waitForIdle": {"requested": 0, "completed": 0, "failed": 0, "timedOut": 0}}
            event_start = len(driver.events)
            tool_counts_before = _online_tool_counts(driver, event_start)

            def capture_scenario_tools(scenario_name: str, scenario_record: dict[str, Any]) -> None:
                observed_counts, successful_counts, failed_counts = _online_tool_counts(driver, event_start)
                observed_tools = _tool_count_delta(tool_counts_before[0], observed_counts)
                successful_tools = _tool_count_delta(tool_counts_before[1], successful_counts)
                failed_tools = _tool_count_delta(tool_counts_before[2], failed_counts)
                required_tool = SCENARIO_REQUIRED_TOOLS[scenario_name]
                if required_tool is None:
                    return
                state = scenario_tool_coverage[scenario_name]
                # HTML preservation is a scenario-level assertion, rather than
                # generic write coverage. It requires this turn's successful
                # write and controller-side byte-for-character verification.
                if scenario_name == "write_html_preservation":
                    verified = scenario_record.get("status") == "completed" and scenario_record.get("contentVerified") is True
                    if required_tool in successful_tools and verified:
                        state["observed"] = True
                        state["successful"] = True
                    elif required_tool in failed_tools:
                        state["failed"] = True
                    return
                state["observed"] |= required_tool in observed_tools
                state["successful"] |= required_tool in successful_tools
                state["failed"] |= required_tool in failed_tools
            if detail := _scenario_unavailable(name, available_tools, capabilities):
                record.update(status="capability_unavailable", detail=detail, endSec=round(monotonic() - started, 3), elapsedSec=round(monotonic() - scenario_started, 3))
                if len(scenarios) < MAX_SCENARIO_RECORDS:
                    scenarios.append(record)
                index += 1
                remaining = deadline - monotonic()
                if remaining > 0:
                    sleep(min(0.2, remaining))
                continue
            # The patch scenario mutates its fixture.  Restore its exact input
            # before each cycle so a long run remains idempotent.
            if name == "apply_patch":
                (workspace / "patch-target.txt").write_text("before\n", encoding="utf-8")
            scenario_deadline = min(scenario_started + scenario_timeout, deadline)
            response = driver.command({"type": "prompt", "message": scenario_prompt(name), "streamingBehavior": "queue"}, scenario_deadline)
            if response is None:
                scenario_truncated = monotonic() >= deadline
                if not scenario_truncated:
                    execution_failed = True
                record.update(status="incomplete_at_deadline" if scenario_truncated else "prompt_timeout", endSec=round(monotonic() - started, 3), elapsedSec=round(monotonic() - scenario_started, 3))
                capture_scenario_tools(name, record)
                if len(scenarios) < MAX_SCENARIO_RECORDS:
                    scenarios.append(record)
                index += 1
                if scenario_truncated:
                    final_scenario_truncated = True
                    break
                recovery_deadline = min(monotonic() + RECOVERY_TIMEOUT_SEC, deadline)
                abort_response = driver.command({"type": "abort"}, recovery_deadline)
                record["recovery"] = {"attempted": True, "aborted": bool(abort_response and abort_response.get("success") is True)}
                if record["recovery"]["aborted"]:
                    recovered = driver.command({"type": "wait_for_idle"}, recovery_deadline)
                    record["recovery"]["idle"] = bool(recovered and recovered.get("success") is True)
                # A closed RPC stream can return immediately. Yield before the
                # next scheduled attempt so it cannot spin until the deadline.
                remaining = deadline - monotonic()
                if remaining > 0:
                    sleep(min(0.2, remaining))
                continue
            if response.get("success") is not True:
                execution_failed = True
            idle["requested"] += 1
            record["waitForIdle"]["requested"] = 1
            # A prompt may consume most of its own bounded window while the
            # queued turn is still making progress. Give idle detection a fresh
            # bounded window, but never extend beyond the workload deadline.
            idle_deadline = min(monotonic() + scenario_timeout, deadline)
            idle_response = driver.command({"type": "wait_for_idle"}, idle_deadline)
            if idle_response is None:
                idle["timedOut"] += 1
                record["waitForIdle"]["timedOut"] = 1
                all_planned_attempted = set(SCENARIOS) <= attempted
                final_idle_deadline_truncated = monotonic() >= deadline and all_planned_attempted and idle["failed"] == 0 and idle["timedOut"] == 1
                scenario_truncated = monotonic() >= deadline
                if not final_idle_deadline_truncated:
                    # Deadline truncation is reported separately from an RPC
                    # failure; a bounded drain below will abort and clean up.
                    if not scenario_truncated:
                        execution_failed = True
                record["status"] = "incomplete_at_deadline" if scenario_truncated else "idle_timeout"
            elif idle_response.get("success") is True:
                idle["completed"] += 1
                record["waitForIdle"]["completed"] = 1
                record["status"] = "completed"
                if name == "write_html_preservation":
                    output = workspace / "html-preservation-output.txt"
                    if not output.is_file() or output.read_text(encoding="utf-8") != HTML_PRESERVATION_CONTENT:
                        execution_failed = True
                        record.update(status="content_verification_failed", contentVerified=False)
                    else:
                        record["contentVerified"] = True
                if name not in requested:
                    requested.append(name)
            else:
                execution_failed = True
                idle["failed"] += 1
                record["waitForIdle"]["failed"] = 1
                record["status"] = "idle_failed"
            if record.get("status") in {"idle_timeout", "idle_failed"} and not scenario_truncated:
                recovery_deadline = min(monotonic() + RECOVERY_TIMEOUT_SEC, deadline)
                abort_response = driver.command({"type": "abort"}, recovery_deadline)
                record["recovery"] = {"attempted": True, "aborted": bool(abort_response and abort_response.get("success") is True)}
                if record["recovery"]["aborted"]:
                    recovered = driver.command({"type": "wait_for_idle"}, recovery_deadline)
                    record["recovery"]["idle"] = bool(recovered and recovered.get("success") is True)
            scenario_ended = monotonic()
            record.update(endSec=round(scenario_ended - started, 3), elapsedSec=round(scenario_ended - scenario_started, 3))
            capture_scenario_tools(name, record)
            if len(scenarios) < MAX_SCENARIO_RECORDS:
                scenarios.append(record)
            index += 1
            if scenario_truncated:
                final_scenario_truncated = True
                break
        if final_scenario_truncated:
            # No scenario is admitted after the duration window.  Give the
            # one already admitted a bounded cleanup path instead of treating
            # the outer deadline as an ordinary failed RPC command.
            drain["attempted"] = True
            drain_deadline = deadline + SCENARIO_DRAIN_GRACE_SEC
            abort_response = driver.command({"type": "abort"}, drain_deadline)
            drain["aborted"] = bool(abort_response and abort_response.get("success") is True)
            if drain["aborted"]:
                shutdown_response = driver.command({"type": "wait_for_idle"}, drain_deadline)
                clean_shutdown = bool(shutdown_response and shutdown_response.get("success") is True)
            drain["cleanShutdown"] = clean_shutdown
        # Test doubles from older focused tests expose only events; production
        # drivers use the online counters, which remain correct after truncation.
        online_failed = getattr(driver, "failed_tools", {str(e.get("toolName") or e.get("tool") or e.get("name")) for e in driver.events if e.get("type") == "tool_call_end" and e.get("ok") is False})
        observed = {name for name, state in scenario_tool_coverage.items() if state["observed"]}
        successful_tools = {name for name, state in scenario_tool_coverage.items() if state["successful"]}
        failed_tools = {name for name, state in scenario_tool_coverage.items() if state["failed"]}
        # A tool advertised by get_tools but ending unsuccessfully is a real
        # dispatch/runtime failure, not a disabled capability.  Optional
        # scenarios are skipped above, so they cannot be mistaken for this.
        if online_failed:
            execution_failed = True
        attempted.update(requested)
        observed.update(control_observed)
        coverage, missing_coverage, coverage_warnings = classify_coverage(
            attempted, observed, available_tools, idle, final_idle_deadline_truncated, capabilities,
            successful_tools, failed_tools,
        )
        incomplete_at_deadline = {
            "finalWaitForIdle": final_idle_deadline_truncated,
            "finalScenarioTruncated": final_scenario_truncated,
            "allPlannedScenariosAttempted": set(SCENARIOS) <= attempted,
        }
        tool_preflight = {name: _tool_preflight_status(name, available_tools, capabilities) for name in SCENARIOS if SCENARIO_REQUIRED_TOOLS[name] is not None}
        result = {"type": "diagnostic_workload", "fixture": fixture, "elapsedSec": round(monotonic() - started, 3), "events": driver.events, "malformed": driver.malformed, "coverage": coverage, "missingCoverage": missing_coverage, "coverageWarnings": coverage_warnings, "toolPreflight": tool_preflight, "incompleteAtDeadline": incomplete_at_deadline, "completed": incomplete_at_deadline["allPlannedScenariosAttempted"] and not execution_failed, "waitForIdle": idle, "drain": drain, "cleanShutdown": clean_shutdown, "runtimeFailure": execution_failed, "scenarios": scenarios, "scenarioRecordsDropped": max(0, index - len(scenarios))}
    finally:
        driver.close()
    result["processReturnCode"] = driver.process.poll()
    result["processTerminatedByDriver"] = getattr(driver, "terminated_by_driver", False)
    return result


def _runtime_failure(events: list[dict[str, Any]], process_return_code: int | None, terminated_by_driver: bool) -> bool:
    return (
        any(
            event.get("type") in {"error", "provider_error"}
            or event.get("success") is False
            or (event.get("type") == "tool_call_end" and event.get("ok") is False)
            for event in events
        )
        or (process_return_code not in (None, 0) and not terminated_by_driver)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--workload", default="safe")
    parser.add_argument("--provider", default="llama.cpp")
    parser.add_argument("--model", default="local")
    parser.add_argument("--llama-cpp-url", default="http://192.168.200.19:8089")
    args = parser.parse_args()
    if args.duration <= 0:
        parser.error("--duration must be greater than zero")
    workload_plan(args.duration, args.workload)
    result = run_workload(Path(args.workspace), args.duration, args.provider, args.model, args.llama_cpp_url)
    result["runtimeFailure"] = bool(result.get("runtimeFailure")) or _runtime_failure(
        result["events"], result["processReturnCode"], result["processTerminatedByDriver"],
    )
    print(json.dumps(result, sort_keys=True))
    return 4 if result["runtimeFailure"] or result["missingCoverage"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
