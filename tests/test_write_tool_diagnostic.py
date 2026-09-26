from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import diagnose_write_tool as runner
from scripts import write_tool_workload as workload


def test_report_parser_requires_case_ids(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"cases": [{"id": "html", "exact": True}]}), encoding="utf-8")
    assert runner.load_report(path)["cases"][0]["id"] == "html"
    path.write_text('{"cases":[{}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="valid cases"):
        runner.load_report(path)


def test_workload_case_set_covers_sensitive_forms() -> None:
    ids = {case["id"] for case in workload.CASES}
    assert {"markers", "html", "entities", "json-escapes", "unicode", "framed", "empty", "one-char", "moderate"} <= ids
    malformed = {case["id"] for case in workload.malformed_cases()}
    assert {"raw-controls", "missing-outer-brace", "unescaped-quote"} <= malformed
    assert len(workload.sha256("😀")) == 64


def test_docker_runner_command_has_required_hardening(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object):
        commands.append(command)
        if command[:2] == ["docker", "version"]:
            return type("Result", (), {"returncode": 0})()
        if command[:2] == ["docker", "build"]:
            return type("Result", (), {"returncode": 0})()
        report = next(part for part in command if part.startswith("type=bind,"))
        source = Path(dict(piece.split("=", 1) for piece in report.split(",") if "=" in piece)["src"])
        (source / "write-tool-report.json").write_text(json.dumps({"ok": True, "cases": [{"id": "x"}]}), encoding="utf-8")
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner.main(["--report-dir", str(tmp_path), "--json"]) == runner.EXIT_OK
    command = next(command for command in commands if command[:2] == ["docker", "run"])
    for flag in ("--network", "--read-only", "--cap-drop", "--security-opt", "--pids-limit", "--memory", "--cpus"):
        assert flag in command
    assert command[command.index("--network") + 1] == "none"


@pytest.mark.skipif(__import__("os").environ.get("WRITE_TOOL_DOCKER_SMOKE") != "1", reason="set WRITE_TOOL_DOCKER_SMOKE=1")
def test_docker_smoke(tmp_path: Path) -> None:
    assert runner.main(["--report-dir", str(tmp_path), "--json"]) == runner.EXIT_OK
