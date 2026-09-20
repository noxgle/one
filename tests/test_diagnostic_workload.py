import json
import queue
import time
from pathlib import Path
from typing import Any

import pytest

import scripts.diagnostic_workload as workload
from scripts.diagnostic_workload import (
    SCENARIOS,
    _available_tools,
    classify_coverage,
    create_fixture,
    scenario_prompt,
    workload_plan,
)


def test_fixture_is_deterministic_and_contains_image(tmp_path: Path) -> None:
    first = create_fixture(tmp_path / "fixture")
    second = create_fixture(tmp_path / "fixture")
    assert first == second
    assert (tmp_path / "fixture" / "fixture.png").read_bytes().startswith(b"\x89PNG")
    assert "spawn_subagent" in SCENARIOS


def test_workload_plan_covers_builtin_scenarios() -> None:
    names = {item["id"] for item in workload_plan(1)}
    assert {"read", "read_image", "apply_patch", "ask_user", "evidence_read", "reload"} <= names


def test_special_prompts_explicitly_request_safe_valid_tool_calls() -> None:
    assert "Call read" in scenario_prompt("read")
    plan = scenario_prompt("plan")
    assert "3+ phases" in plan and "execute" in plan and "verify" in plan
    ask_user = scenario_prompt("ask_user")
    assert "ask_user" in ask_user and "timeoutSec 30" in ask_user and "automatic answer" in ask_user
    evidence = scenario_prompt("evidence_read")
    assert "read on notes.txt" in evidence and "evidenceId" in evidence and "maxChars 256" in evidence
    subagent = scenario_prompt("spawn_subagent")
    assert "spawn_subagent" in subagent and "tools [\"read\"]" in subagent and "host state" in subagent
    assert subagent.count("finish") == 1


def test_available_tools_extracts_names_from_dicts_and_strings() -> None:
    response = {"data": {"tools": [{"name": "read", "description": "read files"}, "bash", {"name": 1}, {"description": "missing"}]}}
    assert _available_tools(response) == {"read", "bash"}


class _FakeStdout:
    def __init__(self) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()

    def __iter__(self):
        while (line := self.lines.get()) is not None:
            yield line


class _FakeProcess:
    def __init__(self, *_args, **_kwargs) -> None:
        self.stdout = _FakeStdout()
        self.stderr = self.stdout
        self.returncode = None
        self.pid = 999_999
        self.stdin = self
        self.commands: list[dict[str, object]] = []

    def write(self, line: str) -> None:
        command = json.loads(line)
        self.commands.append(command)
        if command["type"] == "prompt":
            self.stdout.lines.put('{"type":"ask_user","id":"question-1"}\n')
        self.stdout.lines.put(json.dumps({"id": command["id"], "type": "response", "success": True}) + "\n")

    def flush(self) -> None:
        pass

    def poll(self):
        return self.returncode

    def wait(self, timeout=None) -> int:
        self.returncode = 0
        self.stdout.lines.put(None)
        return 0


def test_stdout_reader_marks_stream_closed_before_eof_sentinel() -> None:
    class Process:
        stdout: list[str] = []

    class Lines:
        def put(self, item: str | None) -> None:
            if item is None:
                assert driver._stream_closed is True

    driver: Any = object.__new__(workload.RpcDriver)
    driver.process = Process()
    driver.lines = Lines()
    driver._stream_closed = False

    driver._read_stdout()


def test_persistent_rpc_driver_answers_ask_user_and_collects_events(monkeypatch, tmp_path: Path) -> None:
    fake = _FakeProcess()
    monkeypatch.setattr(workload.subprocess, "Popen", lambda *a, **k: fake)
    emitted: list[str] = []
    driver = workload.RpcDriver(["fake"], {}, emit=emitted.append)
    try:
        response = driver.command({"type": "prompt", "message": "ask"}, time.monotonic() + 1)
    finally:
        driver.close()
    assert response and response["success"] is True
    assert any(command["type"] == "answer_question" for command in fake.commands)
    assert any(event["type"] == "ask_user" for event in driver.events)
    assert emitted


def test_workload_uses_fixture_as_rpc_cwd(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class Driver:
        def __init__(self, _command, env, cwd=None, emit=print) -> None:
            captured.update(env=env, cwd=cwd)
            self.events = []
            self.malformed = []
            self.process = type("Process", (), {"poll": lambda self: 0})()

        def command(self, body, _deadline):
            return {"success": True, "id": body.get("id")}

        def close(self) -> None:
            pass

    monkeypatch.setattr(workload, "RpcDriver", Driver)
    workspace = tmp_path / "fixture"
    workload.run_workload(workspace, 0.001, "llama.cpp", "local", "http://example.test", emit=lambda _line: None)
    assert captured["cwd"] == workspace
    assert captured["env"]["ONE_CODING_AGENT_DIR"] == str(workspace / "state")


def test_workload_records_wait_for_idle_coverage(monkeypatch, tmp_path: Path) -> None:
    class Driver:
        def __init__(self, *_args, **_kwargs) -> None:
            self.events = []
            self.malformed = []
            self.process = type("Process", (), {"poll": lambda self: 0})()

        def command(self, body, _deadline):
            if body["type"] == "wait_for_idle":
                return {"success": False}
            return {"success": True, "id": body.get("id")}

        def close(self) -> None:
            pass

    monkeypatch.setattr(workload, "RpcDriver", Driver)
    result = workload.run_workload(tmp_path / "fixture", 0.01, "llama.cpp", "local", "http://example.test", emit=lambda _line: None)
    assert result["waitForIdle"]["failed"] > 0
    assert result["coverage"]["wait_for_idle"] == {"kind": "rpc", "expected": True, "observed": False, "category": "missing_model_or_tool_coverage"}
    assert "wait_for_idle" in result["missingCoverage"]
    assert result["completed"] is False


@pytest.mark.parametrize("idle_response", [{"success": False}, None])
def test_completed_is_false_after_failed_or_timed_out_scenario(monkeypatch, tmp_path: Path, idle_response: dict[str, bool] | None) -> None:
    clock = {"value": 0.0}

    class Driver:
        def __init__(self, *_args, **_kwargs) -> None:
            self.events = []
            self.malformed = []
            self.process = type("Process", (), {"poll": lambda self: 0})()

        def command(self, body, _deadline):
            if body["type"] == "wait_for_idle":
                clock["value"] = 1.0
                return idle_response
            return {"success": True, "id": body.get("id")}

        def close(self) -> None:
            pass

    monkeypatch.setattr(workload, "SCENARIOS", ("read",))
    monkeypatch.setattr(workload, "RpcDriver", Driver)
    result = workload.run_workload(tmp_path / "fixture", 1, "llama.cpp", "local", "http://example.test", emit=lambda _line: None, monotonic=lambda: clock["value"])
    assert result["incompleteAtDeadline"]["allPlannedScenariosAttempted"] is True
    assert result["completed"] is False


def test_coverage_classifies_unavailable_arbitrary_tool_as_warning_not_missing() -> None:
    coverage, missing, warnings = classify_coverage(
        {"read_image"}, set(), {"read", "finish"},
        {"requested": 1, "completed": 1, "failed": 0, "timedOut": 0}, False,
    )
    assert coverage["read_image"]["category"] == "capability_unavailable"
    assert missing == []
    assert warnings == [{"scenario": "read_image", "category": "capability_unavailable", "detail": "read_image was not advertised by the RPC session configuration."}]


def test_coverage_does_not_misclassify_available_tools_or_controls() -> None:
    coverage, missing, warnings = classify_coverage(
        {"read", "retry"}, set(), {"read"},
        {"requested": 1, "completed": 1, "failed": 0, "timedOut": 0}, False,
    )
    assert coverage["read"]["category"] == "missing_model_or_tool_coverage"
    assert coverage["retry"]["category"] == "missing_model_or_tool_coverage"
    assert missing == ["read", "retry"]
    assert warnings == []


def test_coverage_excludes_final_idle_truncated_by_outer_deadline() -> None:
    coverage, missing, warnings = classify_coverage(
        set(SCENARIOS), set(SCENARIOS), set(SCENARIOS),
        {"requested": 1, "completed": 0, "failed": 0, "timedOut": 1}, True,
    )
    assert coverage["wait_for_idle"]["category"] == "deadline_truncated"
    assert "wait_for_idle" not in missing
    assert warnings[-1]["scenario"] == "wait_for_idle"


def test_coverage_keeps_true_missing_tool_coverage_as_failure() -> None:
    coverage, missing, _warnings = classify_coverage(
        {"read"}, set(), {"read"},
        {"requested": 1, "completed": 1, "failed": 0, "timedOut": 0}, False,
    )
    assert coverage["read"]["category"] == "missing_model_or_tool_coverage"
    assert missing == ["read"]


def test_workload_schedules_scenarios_until_full_duration(monkeypatch, tmp_path: Path) -> None:
    clock = {"value": 0.0}

    class Driver:
        def __init__(self, *_args, **_kwargs) -> None:
            self.events = []
            self.malformed = []
            self.process = type("Process", (), {"poll": lambda self: 0})()

        def command(self, body, _deadline):
            clock["value"] += 1.0
            return {"success": True, "id": body.get("id")}

        def close(self) -> None:
            pass

    monkeypatch.setattr(workload, "RpcDriver", Driver)
    result = workload.run_workload(tmp_path / "fixture", 20, "llama.cpp", "local", "http://example.test", emit=lambda _line: None, monotonic=lambda: clock["value"])
    assert result["scenarios"][-1]["startSec"] >= 18
    assert result["elapsedSec"] >= 20
    assert len(result["scenarios"]) <= workload.MAX_SCENARIO_RECORDS


def test_workload_continues_after_a_prompt_timeout(monkeypatch, tmp_path: Path) -> None:
    clock = {"value": 0.0}
    prompts: list[str] = []

    class Driver:
        def __init__(self, *_args, **_kwargs) -> None:
            self.events = []
            self.malformed = []
            self.process = type("Process", (), {"poll": lambda self: 0})()

        def command(self, body, _deadline):
            clock["value"] += 1.0
            if body["type"] == "prompt":
                prompts.append(body["message"])
                if len(prompts) == 1:
                    return None
            return {"success": True, "id": body.get("id")}

        def close(self) -> None:
            pass

    monkeypatch.setattr(workload, "RpcDriver", Driver)
    result = workload.run_workload(tmp_path / "fixture", 14, "llama.cpp", "local", "http://example.test", emit=lambda _line: None, monotonic=lambda: clock["value"], sleep=lambda seconds: clock.__setitem__("value", clock["value"] + seconds))

    assert result["scenarios"][0]["status"] == "prompt_timeout"
    assert result["completed"] is False
    assert len(prompts) > 1
    assert any(record["id"] == "read_image" for record in result["scenarios"])
