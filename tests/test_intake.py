from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from one.cli.main import _expand_file_tokens, _parse_params


def test_parse_params_ok():
    params, errors = _parse_params(["a=1", "b=hello world"])
    assert params == {"a": "1", "b": "hello world"}
    assert errors == []


def test_parse_params_invalid():
    params, errors = _parse_params(["nope", "=x", "a=1"])
    assert len(errors) == 2
    assert "Invalid --param" in errors[0]
    assert "Invalid --param" in errors[1]
    assert params == {"a": "1"}


def test_expand_file_tokens_substitution(tmp_path: Path):
    task_md = tmp_path / "task.md"
    task_md.write_text("Do {{thing}} in {{dir}}", encoding="utf-8")
    # Use absolute path token so expanduser/Path works without cwd context.
    expanded, errors = _expand_file_tokens([f"@{task_md}", "plain"], {"thing": "X", "dir": "/tmp"})
    assert errors == []
    assert expanded[0] == "Do X in /tmp"
    assert expanded[1] == "plain"


def test_expand_file_tokens_missing(tmp_path: Path):
    expanded, errors = _expand_file_tokens(["@nope.md"], {})
    assert len(errors) == 1
    assert "File not found" in errors[0]


def test_expand_file_tokens_plain_passthrough():
    expanded, errors = _expand_file_tokens(["a", "b"], {})
    assert expanded == ["a", "b"]
    assert errors == []


def test_cli_run_task_file_missing(tmp_path: Path):
    env = os.environ.copy()
    env["ONE_CODING_AGENT_DIR"] = str(tmp_path / ".one" / "agent")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    res = subprocess.run(
        [sys.executable, "-m", "one.cli.main", "run", "@missing.md"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert res.returncode == 2
    assert "File not found" in res.stdout


def test_cli_run_param_invalid(tmp_path: Path):
    env = os.environ.copy()
    env["ONE_CODING_AGENT_DIR"] = str(tmp_path / ".one" / "agent")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    res = subprocess.run(
        [sys.executable, "-m", "one.cli.main", "run", "--param", "bad", "task"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert res.returncode == 2
    assert "Invalid --param" in res.stdout


def test_cli_run_steer_file_flag_parses():
    from one.cli.args import parse_args

    parsed = parse_args(["run", "--steer-file", "/tmp/steer.txt", "task here"])
    assert parsed.errors == []
    assert parsed.run_task == "task here"
    assert parsed.steer_file == "/tmp/steer.txt"


def test_cli_run_task_file_reaches_dispatch(tmp_path: Path):
    env = os.environ.copy()
    env["ONE_CODING_AGENT_DIR"] = str(tmp_path / ".one" / "agent")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    task_md = tmp_path / "task.md"
    task_md.write_text("do the thing", encoding="utf-8")

    res = subprocess.run(
        [sys.executable, "-m", "one.cli.main", "run", "--json", "@task.md"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert res.returncode == 1
    data = json.loads(res.stdout)
    assert "goalSuccess" in data
    assert "finished" in data
    assert "summary" in data


def test_cli_run_param_flag_parses(tmp_path: Path):
    env = os.environ.copy()
    env["ONE_CODING_AGENT_DIR"] = str(tmp_path / ".one" / "agent")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    res = subprocess.run(
        [sys.executable, "-m", "one.cli.main", "run", "--param", "a=1", "--json", "task"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert res.returncode == 1
    data = json.loads(res.stdout)
    assert "goalSuccess" in data
