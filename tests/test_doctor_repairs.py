# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from one.core.doctor.cli import run_doctor


def snapshot(root: Path) -> list[tuple[str, int, bytes | str | None]]:
    return sorted((str(p.relative_to(root)), stat.S_IMODE(p.lstat().st_mode),
                   str(p.readlink()) if p.is_symlink() else p.read_bytes() if p.is_file() else None)
                  for p in root.rglob("*"))


@pytest.fixture
def environment(tmp_path, monkeypatch):
    agent = tmp_path / "agent"
    project = tmp_path / "project"
    agent.mkdir()
    (project / ".one").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(agent))
    monkeypatch.chdir(project)
    return agent / "settings.json", project / ".one" / "settings.json"


def test_preview_is_deterministic_redacted_and_read_only(environment, tmp_path, capsys):
    global_file, project_file = environment
    global_file.write_text('{"subagents":{"timeoutSec":12},"secret-canary":"oauth-canary"}')
    project_file.write_text('{"configVersion":1}')
    before = snapshot(tmp_path)
    outputs = []
    for _ in range(2):
        assert run_doctor(["--fix", "--dry-run", "--yes", "--json"]) == 1
        outputs.append(capsys.readouterr().out)
    assert outputs[0] == outputs[1]
    assert "canary" not in outputs[0]
    actions = json.loads(outputs[0])["actions"]
    assert any(a["id"] == "settings.migrate" and "[other changes redacted]" in a["diff"] for a in actions)
    assert snapshot(tmp_path) == before


def test_text_preview_redacts_secrets_in_paths_and_contents(environment, tmp_path, capsys):
    global_file, project_file = environment
    global_file.write_text('{"subagents":{"timeoutSec":12},"secret-canary":"oauth-canary"}')
    project_file.write_text('{"configVersion":1}')
    before = snapshot(tmp_path)
    assert run_doctor(["--fix", "--dry-run"]) == 1
    output = capsys.readouterr().out
    assert "canary" not in output and str(tmp_path) not in output
    assert "[other changes redacted]" in output
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("bad", ['{', '{"configVersion":999}', '{"configVersion":true}'])
def test_bad_layer_blocks_both_files(environment, tmp_path, capsys, bad):
    global_file, project_file = environment
    global_file.write_text('{}')
    project_file.write_text(bad)
    before = snapshot(tmp_path)
    assert run_doctor(["--fix", "--dry-run", "--json"]) == 1
    actions = json.loads(capsys.readouterr().out)["actions"]
    assert all(a["id"] != "settings.migrate" for a in actions)
    assert snapshot(tmp_path) == before


def test_conflict_not_approved_and_no_execution(environment, capsys):
    global_file, project_file = environment
    global_file.write_text('{"subagents":{"timeoutSec":10}}')
    project_file.write_text('{"subagents":{"timeoutSec":20}}')
    before = (global_file.read_bytes(), project_file.read_bytes())
    assert run_doctor(["--fix", "--yes", "--json"]) == 1
    actions = json.loads(capsys.readouterr().out)["actions"]
    assert any(a["classification"] == "decision" and a["status"] != "applied" for a in actions)
    assert (global_file.read_bytes(), project_file.read_bytes()) == before


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("tty", [False, True])
def test_preview_formats_no_prompts_or_filesystem_changes(environment, tmp_path, monkeypatch, capsys, json_output, tty):
    import sys

    global_file, project_file = environment
    global_file.write_text('{"subagents":{"timeoutSec":12}}')
    project_file.write_text('{"configVersion":1}')
    global_file.chmod(0o640)
    project_file.chmod(0o600)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("unexpected prompt"))
    before = snapshot(tmp_path)
    args = ["--fix", "--dry-run"] + (["--json"] if json_output else [])
    assert run_doctor(args) == 0
    output = capsys.readouterr()
    assert not output.err
    if json_output:
        action = json.loads(output.out)["actions"][0]
        assert (action["id"], action["file"], action["classification"], action["status"]) == (
            "settings.migrate", "global settings.json", "safe", "planned")
        assert action["preconditions"] and action["diff"] == "configVersion: 0 -> 1; [other changes redacted]"
    else:
        assert all(value in output.out for value in (
            "settings.migrate", "global settings.json", "safe", "planned", "[other changes redacted]", "Recheck"))
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("args", [["--fix"], ["--fix", "--yes"], ["--fix", "--dry-run", "--yes"]])
def test_conflict_is_never_approved(environment, tmp_path, capsys, args):
    global_file, project_file = environment
    global_file.write_text('{"subagents":{"timeoutSec":10}}')
    project_file.write_text('{"subagents":{"timeoutSec":20}}')
    before = snapshot(tmp_path)
    assert run_doctor([*args, "--json"]) == 1
    actions = json.loads(capsys.readouterr().out)["actions"]
    assert any(a["classification"] == "decision" and a["status"] == "not_available" for a in actions)
    assert all(a["status"] != "applied" for a in actions)
    assert snapshot(tmp_path) == before


def test_symlink_target_blocks_all_migrations(environment, tmp_path, capsys):
    global_file, project_file = environment
    global_file.write_text('{}')
    project_file.symlink_to(global_file)
    before = snapshot(tmp_path)
    assert run_doctor(["--fix", "--dry-run", "--json"]) == 1
    actions = json.loads(capsys.readouterr().out)["actions"]
    assert actions and all(a["id"] != "settings.migrate" for a in actions)
    assert snapshot(tmp_path) == before


def test_fix_yes_both_layers_backup_and_idempotence(environment, capsys):
    global_file, project_file = environment
    auth = global_file.parent / "auth.json"
    auth.write_text('{"synthetic":"canary-token"}')
    global_file.write_text('{"theme":"hacker"}')
    project_file.write_text('{"tools":{"maxSteps":42}}')
    original = [p.read_bytes() for p in (global_file, project_file)]
    assert run_doctor(["--fix", "--yes", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert "canary-token" not in json.dumps(result)
    assert auth.read_text() == '{"synthetic":"canary-token"}'
    assert [a["status"] for a in result["actions"]] == ["applied", "applied"]
    for path, before in zip((global_file, project_file), original, strict=True):
        assert json.loads(path.read_bytes())["configVersion"] == 1
        backups = list(path.parent.glob(".settings.json.backup-*"))
        assert len(backups) == 1 and backups[0].read_bytes() == before
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert run_doctor(["--fix", "--yes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["actions"] == []


def test_fix_non_tty_declines_and_tty_approves(environment, monkeypatch, capsys):
    import io
    import sys

    global_file, _ = environment
    global_file.write_text('{}')
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert run_doctor(["--fix", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["actions"][0]["status"] == "declined"
    assert global_file.read_text() == '{}'
    stream = io.StringIO("yes\n")
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdin", stream)
    assert run_doctor(["--fix", "--json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["actions"][0]["status"] == "applied"
    assert "Apply safe repairs?" in output.err


def test_plan_rechecks_other_layer(environment, monkeypatch):
    from one.core.doctor import DoctorEngine
    from one.core.doctor.repairs import execute_plan, make_plan

    global_file, project_file = environment
    global_file.write_text('{}')
    project_file.write_text('{"configVersion":1}')
    plan = make_plan(Path.cwd(), DoctorEngine().check(Path.cwd()))
    project_file.write_text('{"configVersion":1,"theme":"other"}')
    result = execute_plan(Path.cwd(), plan, True)
    assert result.actions[0].status == "conflict"
    assert global_file.read_text() == '{}'


def test_partial_failure_redacted_and_rechecked(environment, monkeypatch, capsys):
    from one.core.doctor import settings_write

    global_file, project_file = environment
    global_file.write_text('{}')
    project_file.write_text('{}')
    apply = settings_write.apply
    def fail_second(path, expected, data, validate):
        if path == project_file:
            raise settings_write.WriteError("unsafe_rollback")
        return apply(path, expected, data, validate)
    monkeypatch.setattr(settings_write, "apply", fail_second)
    assert run_doctor(["--fix", "--yes", "--json"]) == 1
    output = capsys.readouterr().out
    assert str(global_file.parent) not in output
    actions = json.loads(output)["actions"]
    assert [a["status"] for a in actions] == ["applied", "unsafe_rollback"]
    assert json.loads(global_file.read_text())["configVersion"] == 1
    assert project_file.read_text() == '{}'


def test_failure_stops_remaining_actions_without_claiming_conflict(environment, monkeypatch, capsys):
    from one.core.doctor import settings_write

    global_file, project_file = environment
    global_file.write_text('{}')
    project_file.write_text('{}')

    def fail_first(path, expected, data, validate):
        assert path == global_file
        raise settings_write.WriteError("failed")

    monkeypatch.setattr(settings_write, "apply", fail_first)
    assert run_doctor(["--fix", "--yes", "--json"]) == 1
    actions = json.loads(capsys.readouterr().out)["actions"]
    assert [a["status"] for a in actions] == ["failed", "not_available"]
    assert global_file.read_text() == project_file.read_text() == '{}'


def test_interrupt_after_first_action_reports_partial_result(environment, monkeypatch, capsys):
    from one.core.doctor import settings_write

    global_file, project_file = environment
    global_file.write_text('{}')
    project_file.write_text('{}')
    apply = settings_write.apply

    def interrupt_second(path, expected, data, validate):
        if path == project_file:
            raise KeyboardInterrupt("secret-canary")
        return apply(path, expected, data, validate)

    monkeypatch.setattr(settings_write, "apply", interrupt_second)
    assert run_doctor(["--fix", "--yes", "--json"]) == 1
    output = capsys.readouterr().out
    assert "secret-canary" not in output
    assert str(global_file.parent) not in output
    assert [a["status"] for a in json.loads(output)["actions"]] == ["applied", "unsafe_rollback"]
    assert json.loads(global_file.read_text())["configVersion"] == 1
    assert project_file.read_text() == '{}'
