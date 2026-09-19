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
    "read", "read_image", "bash", "write", "edit", "apply_patch", "grep", "find", "ls",
    "evidence_read", "plan", "finish", "ask_user", "spawn_subagent", "retry", "timeout",
    "steering", "follow_up", "compaction", "reload",
)
MAX_EVENTS = 2_000
MAX_MALFORMED = 100
MAX_AUTO_ANSWERS = 100
MAX_SCENARIO_RECORDS = 200


def create_fixture(workspace: Path) -> dict[str, str]:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (workspace / "large.txt").write_text("diagnostic-line\n" * 512, encoding="utf-8")
    (workspace / "patch-target.txt").write_text("before\n", encoding="utf-8")
    (workspace / "fixture.png").write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL6xQAAAABJRU5ErkJggg=="))
    return {"workspace": str(workspace), "image": "fixture.png", "text": "notes.txt", "scenarios": ",".join(SCENARIOS)}


def workload_plan(duration: float, workload: str = "safe") -> list[dict[str, object]]:
    if workload not in {"safe", "stress", "custom"}:
        raise ValueError("workload must be safe, stress, or custom")
    return [{"id": name, "expected": "bounded", "timeoutSec": min(10.0, duration)} for name in SCENARIOS]


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
        "edit": "Call edit to replace `diagnostic` with `edited` in generated.txt, then finish.",
        "apply_patch": "Call apply_patch to replace `before` with `after` in patch-target.txt, then finish.",
        "grep": "Call grep for `alpha` in notes.txt, then finish.",
        "find": "Call find for *.txt, then finish.",
        "ls": "Call ls on ., then finish.",
        "evidence_read": "Call evidence_read with a bounded recent range if evidence is available, then finish.",
        "plan": "Call plan with a one-step diagnostic plan, then finish.",
        "finish": "Call finish with a concise completion summary.",
        "ask_user": "Call ask_user with one yes/no question, wait for the supplied answer, then call finish.",
        "spawn_subagent": "Call spawn_subagent with a tiny read-only task to list notes.txt, then finish.",
    }
    return f"Work only in this fixture workspace. {details.get(name, f'Call {tool}, then finish.')}"


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
        if len(self.events) < MAX_EVENTS:
            self.events.append(item)
        self.emit(json.dumps(item, ensure_ascii=False, sort_keys=True))
        return item

    def command(self, body: dict[str, Any], deadline: float) -> dict[str, Any] | None:
        assert self.process.stdin is not None
        if self._stream_closed:
            return None
        request_id = body.setdefault("id", f"diagnostic-{len(self.events)}")
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
                    self.process.stdin.write(json.dumps({"type": "answer_question", "id": f"answer-{question_id}", "answer": "yes, continue diagnostic"}) + "\n")
                    self.process.stdin.flush()
            if event and event.get("type") == "response":
                if event.get("id") == request_id:
                    return event
                if isinstance(event.get("id"), str):
                    self.responses[event["id"]] = event
        return None

    def close(self) -> None:
        if self.process.poll() is None:
            self.terminated_by_driver = True
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
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
    control_observed: set[str] = set()
    idle = {"requested": 0, "completed": 0, "failed": 0, "timedOut": 0}
    scenarios: list[dict[str, Any]] = []
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
        index = 0
        while monotonic() < deadline:
            name = SCENARIOS[index % len(SCENARIOS)]
            scenario_started = monotonic()
            response = driver.command({"type": "prompt", "message": scenario_prompt(name), "streamingBehavior": "queue"}, deadline)
            record: dict[str, Any] = {"id": name, "startSec": round(scenario_started - started, 3), "waitForIdle": {"requested": 0, "completed": 0, "failed": 0, "timedOut": 0}}
            if response is None:
                record.update(status="prompt_timeout", endSec=round(monotonic() - started, 3), elapsedSec=round(monotonic() - scenario_started, 3))
                if len(scenarios) < MAX_SCENARIO_RECORDS:
                    scenarios.append(record)
                index += 1
                # A closed RPC stream can return immediately. Yield before the
                # next scheduled attempt so it cannot spin until the deadline.
                remaining = deadline - monotonic()
                if remaining > 0:
                    sleep(min(0.2, remaining))
                continue
            idle["requested"] += 1
            record["waitForIdle"]["requested"] = 1
            idle_response = driver.command({"type": "wait_for_idle"}, deadline)
            if idle_response is None:
                idle["timedOut"] += 1
                record["waitForIdle"]["timedOut"] = 1
                record["status"] = "idle_timeout"
            elif idle_response.get("success") is True:
                idle["completed"] += 1
                record["waitForIdle"]["completed"] = 1
                record["status"] = "completed"
                if name not in requested:
                    requested.append(name)
            else:
                idle["failed"] += 1
                record["waitForIdle"]["failed"] = 1
                record["status"] = "idle_failed"
            scenario_ended = monotonic()
            record.update(endSec=round(scenario_ended - started, 3), elapsedSec=round(scenario_ended - scenario_started, 3))
            if len(scenarios) < MAX_SCENARIO_RECORDS:
                scenarios.append(record)
            index += 1
        observed = {str(e.get("toolName") or e.get("tool") or e.get("name")) for e in driver.events if e.get("type") == "tool_call_start"}
        controls = {name for name, _body in controls}
        coverage: dict[str, dict[str, object]] = {
            name: {
                "expected": name in requested,
                "observed": name in (control_observed if name in controls else observed),
            }
            for name in SCENARIOS
        }
        coverage["wait_for_idle"] = {"kind": "rpc", "expected": idle["requested"] > 0, "observed": idle["requested"] > 0 and not idle["failed"] and not idle["timedOut"]}
        result = {"type": "diagnostic_workload", "fixture": fixture, "elapsedSec": round(monotonic() - started, 3), "events": driver.events, "malformed": driver.malformed, "coverage": coverage, "missingCoverage": sorted(name for name, state in coverage.items() if state["expected"] and not state["observed"]), "waitForIdle": idle, "scenarios": scenarios, "scenarioRecordsDropped": max(0, index - len(scenarios))}
    finally:
        driver.close()
    result["processReturnCode"] = driver.process.poll()
    result["processTerminatedByDriver"] = getattr(driver, "terminated_by_driver", False)
    return result


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
    result["runtimeFailure"] = (
        any(event.get("type") in {"error", "provider_error"} or event.get("success") is False for event in result["events"])
        or (result["processReturnCode"] not in (None, 0) and not result["processTerminatedByDriver"])
    )
    print(json.dumps(result, sort_keys=True))
    return 4 if result["runtimeFailure"] or result["missingCoverage"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
