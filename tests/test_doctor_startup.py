# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from one.core.doctor.startup import startup_hints


def test_read_only_hints_and_migration(tmp_path, monkeypatch):
    import one.core.doctor as doctor

    monkeypatch.setattr(doctor, "DoctorEngine", lambda: pytest.fail("full engine used"))
    monkeypatch.setattr(socket.socket, "connect", lambda *_a, **_k: pytest.fail("network used"))
    agent = tmp_path / "agent"
    agent.mkdir()
    settings = agent / "settings.json"
    settings.write_text(json.dumps({"defaultProvider": "ollama", "defaultModel": "local"}))
    settings.chmod(0o644)
    before = (settings.read_bytes(), settings.stat().st_mode, sorted(tmp_path.rglob("*")))
    hints = startup_hints(str(tmp_path), str(agent))
    assert any("--fix --dry-run" in hint for hint in hints)
    assert not any("--setup" in hint for hint in hints)
    assert (settings.read_bytes(), settings.stat().st_mode, sorted(tmp_path.rglob("*"))) == before
    settings.write_text('{"configVersion": 999}')
    assert "--check" in startup_hints(str(tmp_path), str(agent))[0]
    settings.write_text('{')
    assert "--check" in startup_hints(str(tmp_path), str(agent))[0]


def test_missing_and_explicit_selection(tmp_path):
    assert not any("--setup" in hint for hint in startup_hints(str(tmp_path), str(tmp_path / "agent")))
    assert startup_hints(str(tmp_path), str(tmp_path / "agent"), explicit_selection=True) == ()


def test_dynamic_default_warns_about_runtime_fallback(tmp_path, monkeypatch):
    """ollama with a dynamic (unregistered) model is valid – no --setup hint."""
    monkeypatch.setattr(socket.socket, "connect", lambda *_a, **_k: pytest.fail("network used"))
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "settings.json").write_text(json.dumps({"defaultProvider": "ollama", "defaultModel": "custom"}))
    assert not any("--setup" in hint for hint in startup_hints(str(tmp_path), str(agent)))


def test_cloud_unregistered_model_warns_about_setup(tmp_path, monkeypatch):
    """Cloud provider with an unregistered model falls back to first available – setup hint."""
    monkeypatch.setattr(socket.socket, "connect", lambda *_a, **_k: pytest.fail("network used"))
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "settings.json").write_text(json.dumps({"defaultProvider": "openai", "defaultModel": "nonexistent"}))
    assert any("--setup" in hint for hint in startup_hints(str(tmp_path), str(agent)))


@pytest.mark.asyncio
@pytest.mark.parametrize("args,is_tui", [
    ([], True), (["--mode", "tui"], True), (["run", "task"], False),
    (["--mode", "text", "hello"], False), (["--mode", "json", "hello"], False),
    (["--mode", "rpc"], False), (["--print", "hello"], False),
])
async def test_only_interactive_tui_generates_real_hints_without_engine(
    tmp_path, monkeypatch, capsys, args, is_tui
):
    from one.cli import main
    from one.core import doctor
    from one.core.doctor import startup

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(tmp_path / "agent"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(doctor, "DoctorEngine", lambda *a, **k: pytest.fail("full engine used"))
    calls = []
    real_hints = startup.startup_hints

    def record_hints(*args, **kwargs):
        calls.append(1)
        return real_hints(*args, **kwargs)

    monkeypatch.setattr(startup, "startup_hints", record_hints)

    class Tui:
        def __init__(self, *_args):
            pass

        async def run(self):
            pass

    class LegacyDispatch(Exception):
        pass

    def stop(*_args):
        raise LegacyDispatch

    monkeypatch.setattr(main, "TuiMode", Tui)
    monkeypatch.setattr(main.SettingsManager, "create", stop)
    if is_tui:
        assert await main._run(args) == 0
    else:
        with pytest.raises(LegacyDispatch):
            await main._run(args)
    output = capsys.readouterr()
    assert calls == ([1] if is_tui else [])
    assert "[hint]" not in output.out
    assert "[hint] Provider/model setup may be needed" not in output.err


@pytest.mark.asyncio
async def test_interactive_tui_hints_once_before_facade(tmp_path, monkeypatch, capsys):
    import sys

    from one.cli import main
    from one.core.agent_runtime_facade import AgentRuntimeFacade
    from one.core.doctor import startup

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(tmp_path / "agent"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    calls = []
    ui_thread = threading.get_ident()
    monkeypatch.setattr(
        startup, "startup_hints",
        lambda *a, **k: calls.append((a, k, threading.get_ident())) or ("test hint",),
    )

    class Tui:
        def __init__(self, facade, options):
            assert isinstance(facade, AgentRuntimeFacade)
            assert len(calls) == 1
            assert calls[0][2] != ui_thread

        async def run(self):
            pass

    monkeypatch.setattr(main, "TuiMode", Tui)
    monkeypatch.setattr(main.SettingsManager, "create", lambda *_: pytest.fail("legacy runtime on UI loop"))
    assert await main._run(["--mode", "tui"]) == 0
    output = capsys.readouterr()
    assert output.out == "" and output.err.count("test hint") == 1


@pytest.mark.asyncio
async def test_failed_check_does_not_block_or_disclose_error(tmp_path, monkeypatch, capsys):
    from one.cli import main
    from one.core.doctor import startup

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(tmp_path / "agent"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    def fail(*_args, **_kwargs):
        raise ValueError("sensitive diagnostic detail")

    class Tui:
        def __init__(self, *_args):
            pass

        async def run(self):
            pass

    monkeypatch.setattr(startup, "startup_hints", fail)
    monkeypatch.setattr(main, "TuiMode", Tui)
    assert await main._run(["--mode", "tui"]) == 0
    output = capsys.readouterr()
    assert not output.out
    assert "one doctor --check" in output.err
    assert "sensitive diagnostic detail" not in output.err


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    ["run", "task"], ["--mode", "text", "hello"], ["--mode", "json", "hello"], ["--mode", "rpc"],
    ["--print", "hello"],
])
async def test_headless_dispatch_never_checks_startup(tmp_path, monkeypatch, capsys, args):
    import sys

    from one.cli import main
    from one.core.doctor import startup

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(tmp_path / "agent"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(startup, "startup_hints", lambda *a, **k: pytest.fail("headless startup check"))

    class LegacyDispatch(Exception):
        pass

    def stop(*_args):
        raise LegacyDispatch

    monkeypatch.setattr(main.SettingsManager, "create", stop)
    with pytest.raises(LegacyDispatch):
        await main._run(args)
    assert "[hint]" not in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("stdin_tty,stdout_tty", [(False, True), (True, False), (False, False)])
async def test_noninteractive_tui_skips_check(tmp_path, monkeypatch, stdin_tty, stdout_tty):
    import sys

    from one.cli import main
    from one.core.doctor import startup

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(tmp_path / "agent"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: stdin_tty)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: stdout_tty)
    monkeypatch.setattr(startup, "startup_hints", lambda *a, **k: pytest.fail("noninteractive check"))

    class Tui:
        def __init__(self, *_args):
            pass

        async def run(self):
            pass

    monkeypatch.setattr(main, "TuiMode", Tui)
    assert await main._run(["--mode", "tui"]) == 0


@pytest.mark.parametrize("mode", ["run", "text", "json", "rpc"])
def test_headless_subprocess_with_fake_provider(tmp_path, mode):
    from tests.test_headless_e2e import _E2EServer

    server = _E2EServer()
    port = server.start()
    try:
        agent = tmp_path / "agent"
        agent.mkdir()
        settings = agent / "settings.json"
        settings.write_text(json.dumps({"defaultProvider": "openai", "defaultModel": "gpt-4.1",
                                        "quietStartup": True}))
        models = agent / "models.json"
        models.write_text(json.dumps({"providers": {"openai": [{"id": "gpt-4.1",
            "contextWindow": 1000000, "url": f"http://127.0.0.1:{port}"}]}}))
        before = {p.name: p.read_bytes() for p in (settings, models)}
        home, xdg = tmp_path / "home", tmp_path / "xdg"
        home.mkdir()
        xdg.mkdir()
        env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(xdg),
               "ONE_CODING_AGENT_DIR": str(agent), "OPENAI_API_KEY": "test-key",
               "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        common = ["--no-session", "--no-mcp", "--no-extensions", "--no-skills"]
        args = (["run", *common, "test task"] if mode == "run" else
                [*common, "--mode", mode] + ([] if mode == "rpc" else ["test task"]))
        result = subprocess.run([sys.executable, "-m", "one.cli.main", *args],
                                cwd=tmp_path, env=env, input='{"id":"1","type":"get_state"}\n' if mode == "rpc" else "",
                                capture_output=True, text=True, timeout=30)
        # RPC currently propagates EOF after processing the final request.
        assert result.returncode == (1 if mode == "rpc" else 0), (result.stdout, result.stderr)
        if mode == "rpc":
            assert "EOFError" in result.stderr
        assert "[hint]" not in result.stdout
        assert "one doctor" not in result.stdout
        assert {p.name: p.read_bytes() for p in (settings, models)} == before
        if mode in {"json", "rpc"}:
            assert all(isinstance(json.loads(line), dict) for line in result.stdout.splitlines() if line.strip())
            assert result.stdout.strip()
        else:
            assert "e2e done" in result.stdout
        if mode != "rpc":
            assert server.state["requests"]
    finally:
        server.stop()
