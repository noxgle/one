import json
import queue
import time
from pathlib import Path
from typing import Any

import scripts.diagnostic_workload as workload
from scripts.diagnostic_workload import SCENARIOS, create_fixture, scenario_prompt, workload_plan


def test_fixture_is_deterministic_and_contains_image(tmp_path: Path) -> None:
    first = create_fixture(tmp_path / "fixture")
    second = create_fixture(tmp_path / "fixture")
    assert first == second
    assert (tmp_path / "fixture" / "fixture.png").read_bytes().startswith(b"\x89PNG")
    assert "spawn_subagent" in SCENARIOS


def test_workload_plan_covers_builtin_scenarios() -> None:
    names = {item["id"] for item in workload_plan(1)}
    assert {"read", "read_image", "apply_patch", "ask_user", "evidence_read", "reload"} <= names


def test_prompts_force_tools_and_ask_user_is_explicit() -> None:
    assert "Call read" in scenario_prompt("read")
    assert "ask_user" in scenario_prompt("ask_user")


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
    assert result["coverage"]["wait_for_idle"] == {"kind": "rpc", "expected": True, "observed": False}
    assert "wait_for_idle" in result["missingCoverage"]
