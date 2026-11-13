# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("args", [
    ["--dry-run"], ["--yes"], ["--check", "--dry-run"], ["--check", "--yes"],
    ["--check", "--fix"], ["--fix", "--setup"],
    ["--setup", "--json"], ["--setup", "--yes"],
    ["--bogus"], ["--j"], ["extra"],
])
def test_doctor_rejects_invalid_or_unavailable_modes(args, capsys, monkeypatch) -> None:
    from one.core.doctor import cli

    monkeypatch.setattr(cli, "DoctorEngine", lambda: pytest.fail("check must not run"))
    assert cli.run_doctor(args) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "error:" in output.err


@pytest.mark.asyncio
async def test_doctor_dispatch_is_early(tmp_path, monkeypatch, capsys) -> None:
    import socket

    from one.cli import main

    def forbidden(*args, **kwargs):
        pytest.fail("runtime initialization or network access")

    monkeypatch.setattr(main, "get_agent_dir", forbidden)
    monkeypatch.setattr(main, "ensure_private_dir", forbidden)
    for name in ("SessionManager", "McpManager", "AuthStorage", "ModelRegistry", "SettingsManager",
                 "create_agent_session_runtime", "create_tui_runtime", "DefaultResourceLoader",
                 "install_default_skills"):
        monkeypatch.setattr(main, name, forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(tmp_path / "agent"))
    monkeypatch.chdir(tmp_path)
    assert await main._run(["doctor", "--help"]) == 0
    assert "--check" in capsys.readouterr().out
    assert await main._run(["doctor", "--check", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "ok"
    assert list(tmp_path.iterdir()) == []


def test_doctor_parser_and_other_commands() -> None:
    from one.cli.args import parse_args

    parsed = parse_args(["doctor", "--json"])
    assert (parsed.command, parsed.command_args) == ("doctor", ["--json"])
    parsed = parse_args(["run", "task", "--json"])
    assert parsed.command == "run" and parsed.run_task == "task" and parsed.json_output
    assert parse_args(["doctoring"]).messages == ["doctoring"]


def test_doctor_action_required_exit(tmp_path, monkeypatch, capsys) -> None:
    from one.core.doctor.cli import run_doctor

    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "settings.json").write_text("{")
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(agent))
    monkeypatch.chdir(tmp_path)
    assert run_doctor(["--json"]) == 1
    assert json.loads(capsys.readouterr().out)["result"] == "action_required"


def test_doctor_readiness_exit_and_isolation(tmp_path: Path) -> None:
    home, project = tmp_path / "home", tmp_path / "project"
    agent = home / "agent"
    agent.mkdir(parents=True)
    project.mkdir()
    settings = agent / "settings.json"
    settings.write_text(json.dumps({"defaultProvider": "openai", "defaultModel": "gpt-4o"}))
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg"),
           "ONE_CODING_AGENT_DIR": str(agent), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    env.pop("OPENAI_API_KEY", None)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    def run():
        return subprocess.run([sys.executable, "-m", "one", "doctor", "--json"], cwd=project,
                              env=env, capture_output=True, text=True, check=False)

    missing = run()
    assert missing.returncode == 1 and not missing.stderr
    assert any(i["id"] == "readiness.fallback" for i in json.loads(missing.stdout)["issues"])
    env["OPENAI_API_KEY"] = "synthetic-cli-secret"
    configured = run()
    assert configured.returncode == 0 and not configured.stderr
    assert json.loads(configured.stdout)["result"] == "ok"
    assert "synthetic-cli-secret" not in configured.stdout
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == [
        Path("home"), Path("home/agent"), Path("home/agent/settings.json"), Path("project")]


def test_doctor_offline_check_and_invalid_argument(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg"),
           "ONE_CODING_AGENT_DIR": str(home / "agent"), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    result = subprocess.run([sys.executable, "-m", "one", "doctor", "--json"], cwd=project,
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["result"] == "ok"
    assert before == sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    invalid = subprocess.run([sys.executable, "-m", "one", "doctor", "--fix"], cwd=project,
                             env=env, capture_output=True, text=True, check=False)
    if sys.platform == "linux":
        assert invalid.returncode == 0
        assert "Result: ok" in invalid.stdout
    else:
        assert invalid.returncode == 1
        assert "Result: action_required" in invalid.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_doctor_fix_dry_run_subprocess(tmp_path: Path) -> None:
    home = tmp_path / "home"
    agent = home / "agent"
    project = tmp_path / "project"
    agent.mkdir(parents=True)
    project.mkdir()
    settings = agent / "settings.json"
    settings.write_text('{"subagents":{"timeoutSec":12}}')
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg"),
           "ONE_CODING_AGENT_DIR": str(agent), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    before = settings.read_bytes()
    result = subprocess.run([sys.executable, "-m", "one", "doctor", "--fix", "--dry-run", "--json"],
                            cwd=project, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0 and not result.stderr
    assert any(a["id"] == "settings.migrate" for a in json.loads(result.stdout)["actions"])
    assert settings.read_bytes() == before


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_doctor_fix_subprocess_consent_backup_and_idempotence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    agent = home / "agent"
    project = tmp_path / "project"
    agent.mkdir(parents=True)
    (project / ".one").mkdir(parents=True)
    paths = [agent / "settings.json", project / ".one" / "settings.json"]
    originals = [b'{"theme":"hacker"}', b'{"tools":{"maxSteps":42}}']
    for path, original in zip(paths, originals, strict=True):
        path.write_bytes(original)
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg"),
           "ONE_CODING_AGENT_DIR": str(agent), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}

    def run(*args):
        return subprocess.run([sys.executable, "-m", "one", "doctor", "--fix", "--json", *args],
                              cwd=project, env=env, input="", capture_output=True, text=True, check=False)

    declined = run()
    assert declined.returncode == 1 and not declined.stderr
    assert all(a["status"] == "declined" and "--yes" in a["description"]
               for a in json.loads(declined.stdout)["actions"])
    assert [p.read_bytes() for p in paths] == originals
    applied = run("--yes")
    assert applied.returncode == 0 and not applied.stderr
    assert [a["status"] for a in json.loads(applied.stdout)["actions"]] == ["applied", "applied"]
    for path, original in zip(paths, originals, strict=True):
        assert json.loads(path.read_bytes())["configVersion"] == 1
        backups = list(path.parent.glob(".settings.json.backup-*"))
        assert len(backups) == 1 and backups[0].read_bytes() == original
    again = run("--yes")
    assert again.returncode == 0 and json.loads(again.stdout)["actions"] == []
    assert all(len(list(path.parent.glob(".settings.json.backup-*"))) == 1 for path in paths)


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_doctor_fix_subprocess_conflicting_layers(tmp_path: Path) -> None:
    home = tmp_path / "home"
    agent = home / "agent"
    project = tmp_path / "project"
    agent.mkdir(parents=True)
    (project / ".one").mkdir(parents=True)
    paths = [agent / "settings.json", project / ".one" / "settings.json"]
    originals = [b'{"subagents":{"timeoutSec":10}}', b'{"subagents":{"timeoutSec":20}}']
    for path, original in zip(paths, originals, strict=True):
        path.write_bytes(original)
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg"),
           "ONE_CODING_AGENT_DIR": str(agent), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    result = subprocess.run([sys.executable, "-m", "one", "doctor", "--fix", "--yes", "--json"],
                            cwd=project, env=env, input="", capture_output=True, text=True, check=False)
    assert result.returncode == 1 and not result.stderr
    assert any(a["classification"] == "decision" for a in json.loads(result.stdout)["actions"])
    assert [p.read_bytes() for p in paths] == originals
    assert all(not list(path.parent.glob(".settings.json.backup-*")) for path in paths)


def test_doctor_redacts_unknown_and_bad_json(tmp_path: Path, monkeypatch) -> None:
    from one.core.doctor import DoctorEngine

    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(tmp_path / "agent"))
    agent = tmp_path / "agent"
    agent.mkdir()
    project = tmp_path / "project"
    (project / ".one").mkdir(parents=True)
    (agent / "settings.json").write_text('{"secret-canary": {"token": "canary-key"}}')
    (project / ".one" / "settings.json").write_text('{"canary-oauth":')
    report = DoctorEngine().check(project)
    assert report.result == "action_required"
    assert "canary" not in report.to_json() + report.to_text()
    assert report.to_json() == DoctorEngine().check(project).to_json()
