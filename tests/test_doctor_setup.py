# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
import socket
import sys

import pytest

from one.core.auth_storage import AuthStorage
from one.core.doctor.cli import run_doctor
from one.core.model_registry import ModelRegistry
from one.core.settings_manager import SettingsManager


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(tmp_path / "agent"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network access"))
    return tmp_path


def tty(monkeypatch, responses):
    import sys
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    answers = iter(responses)
    def prompt(_):
        value = next(answers)
        if isinstance(value, BaseException):
            raise value
        return value
    monkeypatch.setattr("builtins.input", prompt)


@pytest.mark.parametrize("scope", ["global", "project"])
def test_setup_and_rerun(environment, monkeypatch, scope, capsys):
    path = environment / ("agent/settings.json" if scope == "global" else ".one/settings.json")
    path.parent.mkdir()
    path.write_text(json.dumps({"unknown": {"secret": "synthetic-canary"}}))
    for _ in range(2):
        tty(monkeypatch, [scope, "ollama", "local", "y"])
        assert run_doctor(["--setup"]) == 1  # Unknown settings require review.
        assert json.loads(path.read_text())["unknown"] == {"secret": "synthetic-canary"}
    assert "synthetic-canary" not in capsys.readouterr().out
    assert not (environment / "agent/auth.json").exists()


@pytest.mark.parametrize("stage", range(4))
def test_interrupt_before_confirmation(environment, monkeypatch, stage):
    tty(monkeypatch, ["project", "ollama", "local"][:stage] + [KeyboardInterrupt()])
    assert run_doctor(["--setup"]) == 1
    assert list(environment.iterdir()) == []


def test_dry_run_and_non_tty(environment, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("prompt"))
    assert run_doctor(["--setup", "--dry-run"]) == 0
    assert "preview" in capsys.readouterr().out
    assert run_doctor(["--setup"]) == 2
    assert list(environment.iterdir()) == []


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_cloud_missing_auth(environment, monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    tty(monkeypatch, ["global", "openai", "gpt-4o", "y"])
    assert run_doctor(["--setup"]) == 1
    output = capsys.readouterr().out
    assert "selected authentication: not_required" in output and "different model" in output and "unverified" in output


@pytest.mark.parametrize("scope", ["global", "project"])
@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_fresh_local_setup_missing_directories(environment, monkeypatch, scope, capsys):
    tty(monkeypatch, [scope, "ollama", "local-model", "yes"])
    assert run_doctor(["--setup"]) == 0
    path = environment / ("agent/settings.json" if scope == "global" else ".one/settings.json")
    assert json.loads(path.read_text()) == {"defaultProvider": "ollama", "defaultModel": "local-model"}
    assert not list(path.parent.glob(".settings.json.backup-*"))
    assert not (environment / "agent/auth.json").exists()
    assert "unverified" in capsys.readouterr().out


@pytest.mark.parametrize("stage", range(4))
@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
def test_each_prompt_interruption_preserves_existing_files(environment, monkeypatch, stage, interruption):
    agent = environment / "agent"
    agent.mkdir()
    settings = agent / "settings.json"
    original = b'{"theme": "dark"}\n'
    settings.write_bytes(original)
    before = {p.relative_to(environment): p.read_bytes() for p in environment.rglob("*") if p.is_file()}
    tty(monkeypatch, ["global", "ollama", "local"][:stage] + [interruption()])
    assert run_doctor(["--setup"]) == 1
    assert {p.relative_to(environment): p.read_bytes() for p in environment.rglob("*") if p.is_file()} == before
    assert not (environment / ".one").exists()


@pytest.mark.parametrize("answers", [[""], ["global", ""], ["global", "ollama", ""],
                                      ["global", "ollama", "local", "no"]])
def test_decline_or_invalid_at_each_prompt_writes_nothing(environment, monkeypatch, answers):
    tty(monkeypatch, answers)
    assert run_doctor(["--setup"]) == 1
    assert list(environment.iterdir()) == []


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_existing_settings_preserved_with_backup_and_idempotent_rerun(environment, monkeypatch):
    path = environment / "agent/settings.json"
    path.parent.mkdir()
    original = b'{"theme": "dark", "custom": {"nested": true}}\n'
    path.write_bytes(original)
    tty(monkeypatch, ["global", "ollama", "local", "y"])
    run_doctor(["--setup"])
    assert json.loads(path.read_text()) == {
        "theme": "dark", "custom": {"nested": True}, "defaultProvider": "ollama", "defaultModel": "local"
    }
    backups = {p.name: p.read_bytes() for p in path.parent.glob(".settings.json.backup-*")}
    assert list(backups.values()) == [original]
    tty(monkeypatch, ["global", "ollama", "local", "y"])
    run_doctor(["--setup"])
    assert {p.name: p.read_bytes() for p in path.parent.glob(".settings.json.backup-*")} == backups


def test_dry_run_existing_configuration_is_read_only(environment, monkeypatch, capsys):
    path = environment / "agent/settings.json"
    path.parent.mkdir()
    path.write_text('{"defaultProvider":"ollama","defaultModel":"existing"}\n')
    before = {p.relative_to(environment): p.read_bytes() for p in environment.rglob("*") if p.is_file()}
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("prompt"))
    assert run_doctor(["--setup", "--dry-run"]) == 0
    assert "preview" in capsys.readouterr().out
    assert {p.relative_to(environment): p.read_bytes() for p in environment.rglob("*") if p.is_file()} == before


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_fresh_setup_nested_missing_global_parent(environment, monkeypatch):
    agent = environment / "missing" / "nested" / "agent"
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(agent))
    tty(monkeypatch, ["global", "ollama", "local", "y"])
    assert run_doctor(["--setup"]) == 0
    assert json.loads((agent / "settings.json").read_text())["defaultModel"] == "local"


@pytest.mark.parametrize("stdin_tty,stdout_tty", [(False, True), (True, False), (False, False)])
def test_setup_requires_both_terminal_streams(environment, monkeypatch, stdin_tty, stdout_tty):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: stdin_tty)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: stdout_tty)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("prompt"))
    assert run_doctor(["--setup"]) == 2
    assert list(environment.iterdir()) == []


@pytest.mark.parametrize("scope", ["global", "project"])
@pytest.mark.parametrize("provider", ["ollama", "llama.cpp"])
@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_custom_local_model_selected_by_runtime(environment, monkeypatch, scope, provider):
    tty(monkeypatch, [scope, provider, "custom:latest", "y"])
    assert run_doctor(["--setup"]) == 0
    agent = environment / "agent"
    settings = SettingsManager.read_only(str(environment), str(agent))
    registry = ModelRegistry(AuthStorage(str(agent / "auth.json"), read_only=True),
                             str(agent / "models.json"), read_only=True)
    selected = registry.select_default(settings.get_default_provider(), settings.get_default_model())
    assert selected is not None and (selected.provider, selected.id) == (provider, "custom:latest")
    assert not (agent / "models.json").exists()
    assert not (agent / "auth.json").exists()


@pytest.mark.parametrize("override", [
    {"defaultProvider": "llama.cpp"}, {"defaultModel": "other"},
    {"defaultProvider": "llama.cpp", "defaultModel": "other"},
])
@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_global_setup_rejects_project_override_without_writing(environment, monkeypatch, capsys, override):
    project = environment / ".one/settings.json"
    project.parent.mkdir()
    project.write_text(json.dumps(override))
    before = project.read_bytes()
    tty(monkeypatch, ["global", "ollama", "custom"])
    assert run_doctor(["--setup"]) == 1
    assert "override" in capsys.readouterr().err
    assert project.read_bytes() == before
    assert not (environment / "agent").exists()
