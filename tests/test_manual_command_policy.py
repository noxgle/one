# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from one.core.manual_command_policy import ManualCommandDenied, validate_manual_command
from one.core.settings_manager import SettingsManager
from one.tools.bash import _manual_environment


class _Loader:
    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
        return "system"

    async def reload(self) -> None:
        return


class _RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(self, api_key: str, model: str, messages: list[dict[str, Any]], thinking_level: str, **kwargs: Any) -> Any:  # noqa: ARG002
        from one.providers.base import ChatResult

        self.calls.append(messages)
        return ChatResult(text="ok", raw={}, usage={}, stop_reason="stop")


def _agent(tmp_path: Path, provider: _RecordingProvider, session_manager: Any = None):
    from one.core.agent_session import AgentSession
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    manager = session_manager or SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(manager, SettingsManager.in_memory(), registry, _Loader(), model, "medium")
    agent.providers = {"openai": provider}  # type: ignore[assignment]
    return agent


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("one.core.manual_command_policy._resolve_system_executable", lambda name: f"/usr/bin/{name}")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("pass\n")
    (tmp_path / "file with spaces.txt").write_text("x\n")
    return tmp_path


@pytest.mark.parametrize(
    "command",
    [
        "pwd", "ls -lah .", "tree -L 2 .", "cat 'file with spaces.txt'", "head -n 10 tests/test_a.py",
        "tail -n 10 tests/test_a.py", "wc -lw tests/test_a.py", "stat tests/test_a.py", "file tests/test_a.py",
        "rg -ni pass tests", "grep -rin pass tests", "du -sh .", "df -h", "git status --short",
        "git diff --stat", "git log -n 10 --oneline", "git show --stat", "git branch --list",
        "git rev-parse --show-toplevel",
    ],
)
def test_strict_representatives_make_immutable_absolute_plans(workspace: Path, command: str) -> None:
    plan = validate_manual_command(command, workspace)
    assert Path(plan.executable).is_absolute()
    assert plan.argv[0] == plan.executable
    assert plan.cwd == str(workspace.resolve())
    with pytest.raises(TypeError):
        plan.env_tweaks["PATH"] = "unsafe"  # type: ignore[index]


@pytest.mark.parametrize("command", ["mkdir output", "touch output/x", "git add tests/test_a.py", "python -m pytest -q tests", "npm test", "cargo check", "go vet"])
def test_development_forms_are_not_available_in_strict_mode(workspace: Path, command: str) -> None:
    with pytest.raises(ManualCommandDenied):
        validate_manual_command(command, workspace)
    assert validate_manual_command(command, workspace, "dev").cwd == str(workspace.resolve())


@pytest.mark.parametrize(
    "command",
    [
        "cat tests/test_a.py; pwd", "cat tests/test_a.py | wc", "cat $(pwd)/x", "cat ${HOME}/x",
        "git -C /tmp status", "git config user.name", "git branch new", "git diff --ext-diff",
        "rg --files-from list pass", "grep --include=*.py pass tests", "python -c 'print(1)'",
        "python -m http.server", "/tmp/python -m pytest tests", "npm install", "cp -f tests/test_a.py copy.py",
    ],
)
def test_shell_operators_unsafe_flags_and_interpreter_escapes_are_denied(workspace: Path, command: str) -> None:
    with pytest.raises(ManualCommandDenied):
        validate_manual_command(command, workspace, "dev")


def test_path_traversal_prefix_symlink_and_output_escapes_are_denied(workspace: Path, tmp_path: Path) -> None:
    with pytest.raises(ManualCommandDenied):
        validate_manual_command("cat ../sibling/file", workspace)
    with pytest.raises(ManualCommandDenied):
        validate_manual_command("cat /tmp/file", workspace)
    sibling = tmp_path.parent / f"{tmp_path.name}-sibling"
    sibling.mkdir()
    (workspace / "outside").symlink_to(sibling)
    with pytest.raises(ManualCommandDenied):
        validate_manual_command("cat outside/file", workspace)
    (workspace / "nested").mkdir()
    (workspace / "nested" / "outside").symlink_to(sibling)
    # Exact recursive grammars do not enable symlink following, so validation
    # does not synchronously walk a potentially huge workspace tree.
    assert validate_manual_command("tree -L 2 nested", workspace)
    assert validate_manual_command("grep -r pass nested", workspace)
    with pytest.raises(ManualCommandDenied):
        validate_manual_command("cp tests/test_a.py ../copy.py", workspace, "dev")


def test_no_clobber_and_fixed_environment(workspace: Path) -> None:
    plan = validate_manual_command("cp tests/test_a.py copy.py", workspace, "dev")
    assert plan.argv[1] == "-n"
    assert plan.argv[-1] == str(workspace / "copy.py")
    git = validate_manual_command("git status", workspace)
    assert git.executable == "/usr/bin/git"
    assert git.env_tweaks["PATH"] == "/usr/bin:/bin"
    assert git.env_tweaks["GIT_DIR"] is None
    assert git.env_tweaks["RIPGREP_CONFIG_PATH"] is None


def test_ripgrep_config_is_disabled_and_background_is_denied(workspace: Path) -> None:
    plan = validate_manual_command("rg -g '*.py' pass tests", workspace)
    assert "--no-config" in plan.argv
    with pytest.raises(ManualCommandDenied):
        validate_manual_command("pwd &", workspace)


def test_python_functional_forms_and_git_restore_paths(workspace: Path) -> None:
    assert validate_manual_command("python -m pytest -q", workspace, "dev").argv[-1] == str(workspace)
    assert validate_manual_command("python -m compileall -q", workspace, "dev").argv[-1] == str(workspace)
    assert validate_manual_command("python -m ruff check .", workspace, "dev").argv[-1] == str(workspace)
    assert validate_manual_command("python -m pip show textual", workspace, "dev")
    assert validate_manual_command("python -m pip show foo-bar", workspace, "dev")
    for option in ("--help", "--files", "--log"):
        with pytest.raises(ManualCommandDenied):
            validate_manual_command(f"python -m pip show {option}", workspace, "dev")
    plan = validate_manual_command("git restore --staged -- tests/test_a.py", workspace, "dev")
    assert "--" in plan.argv
    assert "--" in validate_manual_command("git add -- tests/test_a.py", workspace, "dev").argv
    nodeid = validate_manual_command("python -m pytest tests/test_a.py::test_a", workspace, "dev")
    assert nodeid.argv[-1].endswith("tests/test_a.py::test_a")


def test_pytest_nodeid_symlink_base_cannot_escape(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("pass")
    (workspace / "outside.py").symlink_to(outside)
    with pytest.raises(ManualCommandDenied, match="escapes"):
        validate_manual_command("python -m pytest outside.py::test_x", workspace, "dev")


def test_workspace_venv_symlink_to_trusted_python_keeps_lexical_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trusted = tmp_path / "trusted-python"
    trusted.write_text("binary")
    trusted.chmod(0o755)
    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(trusted)
    monkeypatch.setattr("one.core.manual_command_policy._resolve_system_executable", lambda name: str(trusted))
    plan = validate_manual_command(".venv/bin/python -m pip check", tmp_path, "dev")
    assert plan.executable == str(venv / "python")


def test_venv_parent_symlink_escape_is_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir()
    (outside / "bin").mkdir(parents=True)
    trusted = outside / "python"
    trusted.write_text("binary")
    trusted.chmod(0o755)
    (outside / "bin" / "python").symlink_to(trusted)
    (workspace / ".venv").symlink_to(outside)
    monkeypatch.setattr("one.core.manual_command_policy._resolve_system_executable", lambda name: str(trusted))
    with pytest.raises(ManualCommandDenied, match="virtualenv"):
        validate_manual_command(".venv/bin/python -m pip check", workspace, "dev")


def test_manual_runner_environment_removes_loader_and_git_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LD_PRELOAD", "bad")
    monkeypatch.setenv("LD_AUDIT", "bad")
    monkeypatch.setenv("LD_LIBRARY_PATH", "bad")
    monkeypatch.setenv("PYTHONPATH", "bad")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "bad")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "bad")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "bad")
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "bad")
    monkeypatch.setenv("GIT_SSH_COMMAND", "bad")
    monkeypatch.setenv("GIT_ASKPASS", "bad")
    env = _manual_environment({"PATH": "/usr/bin:/bin", "RIPGREP_CONFIG_PATH": None})
    for key in ("LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH", "PYTHONPATH", "DYLD_INSERT_LIBRARIES", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_KEY_0", "GIT_EXTERNAL_DIFF", "GIT_SSH_COMMAND", "GIT_ASKPASS"):
        assert key not in env
    assert env["PATH"] == "/usr/bin:/bin"


@pytest.mark.parametrize("mode", ["", "production", "DEV", None, [], {}])
def test_unknown_modes_fail_closed(workspace: Path, mode: object) -> None:
    with pytest.raises(ManualCommandDenied):
        validate_manual_command("mkdir x", workspace, mode)  # type: ignore[arg-type]


def test_manual_bash_mode_is_strict_by_default_and_project_cannot_weaken_it(tmp_path: Path) -> None:
    assert SettingsManager.in_memory().get_tui_manual_bash_mode() == "strict"
    assert SettingsManager.in_memory({"tui": {"manualBashMode": "dev"}}).get_tui_manual_bash_mode() == "dev"
    assert SettingsManager.in_memory({"tui": {"manualBashMode": "no"}}).get_tui_manual_bash_mode() == "strict"
    agent_dir, project = tmp_path / "agent", tmp_path / "project"
    agent_dir.mkdir()
    project.mkdir()
    (agent_dir / "settings.json").write_text('{"tui":{"manualBashMode":"strict"}}')
    (project / ".one").mkdir()
    (project / ".one" / "settings.json").write_text('{"tui":{"manualBashMode":"dev"}}')
    assert SettingsManager(str(project), str(agent_dir)).get_tui_manual_bash_mode() == "strict"


def test_git_log_show_disable_signature_helpers_and_prompt(workspace: Path) -> None:
    for command in ("git log -n 1", "git show --stat"):
        plan = validate_manual_command(command, workspace)
        assert "log.showSignature=false" in plan.argv
        assert "--no-show-signature" in plan.argv
        assert plan.env_tweaks["GIT_TERMINAL_PROMPT"] == "0"
        assert plan.env_tweaks["GIT_OPTIONAL_LOCKS"] == "0"
        assert plan.env_tweaks["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert "--no-show-signature" not in validate_manual_command("git status", workspace).argv


@pytest.mark.asyncio
async def test_manual_history_persists_through_reload_branch_and_export_but_never_reaches_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from one.core.session_manager import SessionManager

    provider = _RecordingProvider()
    sessions = tmp_path / "sessions"
    manager = SessionManager.create(str(tmp_path), str(sessions))
    agent = _agent(tmp_path, provider, manager)
    (tmp_path / "private.txt").write_text("local only\n")
    secret_command = "cat private.txt"
    secret_output = "MANUAL_OUTPUT_SECRET"

    async def fake_manual(plan: object, timeout: int | None = None) -> dict[str, object]:  # noqa: ARG001
        return {"ok": True, "output": secret_output, "exitCode": 0, "timedOut": False, "cancelled": False,
                "truncated": False, "fullOutputPath": None}

    monkeypatch.setattr("one.tools.bash.manual_command_tool", fake_manual)
    await agent.execute_tui_command(secret_command)
    session_path = manager.session_file
    assert session_path is not None
    stored = manager.build_session_context()["messages"][-1]
    assert stored["command"] == secret_command and stored["output"] == secret_output
    assert stored["contextVisibility"] == "userOnly"

    # Reload from the real JSONL file, then exercise normal prompt and
    # compaction provider paths. Neither marker nor an empty user entry leaks.
    reloaded = _agent(tmp_path, provider, SessionManager.open(session_path, str(sessions)))
    reloaded.messages = reloaded.session_manager.build_session_context()["messages"]
    await reloaded.prompt("next")
    payload = provider.calls[-1]
    rendered = repr(payload)
    assert secret_command not in rendered and secret_output not in rendered
    assert not any(message["role"] == "user" and message["content"] == "" for message in payload)

    await reloaded._summarize_context(reloaded.messages)
    summary_payload = provider.calls[-1]
    assert secret_command not in repr(summary_payload) and secret_output not in repr(summary_payload)

    # Branching and exporting retain the local record without making it model
    # context after another physical reload.
    branch_path = reloaded.session_manager.create_branched_session(reloaded.session_manager.get_leaf_id() or "")
    assert branch_path is not None
    exported = reloaded.session_manager.export_to_jsonl(str(tmp_path / "export.jsonl"))
    for path in (branch_path, exported):
        reopened = SessionManager.open(path, str(sessions))
        entries = reopened.build_session_context()["messages"]
        assert any(item.get("command") == secret_command and item.get("output") == secret_output for item in entries)
        assert secret_command not in repr(reloaded._flatten_conversation(entries))
        assert secret_output not in repr(reloaded._flatten_conversation(entries))


def test_legacy_manual_metadata_is_filtered_but_model_tool_results_remain_visible(tmp_path: Path) -> None:
    provider = _RecordingProvider()
    agent = _agent(tmp_path, provider)
    flattened = agent._flatten_conversation([
        {
            "role": "bashExecution",
            "content": "LEGACY_COMMAND_SECRET",
            "_nativeToolCalls": [{"id": "manual-secret", "name": "bash"}],
            "_nativeToolCallId": "manual-secret",
        },
        {"role": "toolResult", "content": "model tool result"},
        {"role": "bashExecution", "command": "no content"},
    ])
    assert len(flattened) == 1
    assert "model tool result" in flattened[0]["content"]
