from __future__ import annotations

from one.core.settings_manager import SettingsManager
from one.resources.resource_loader import DefaultResourceLoader


def _make_settings() -> SettingsManager:
    return SettingsManager.in_memory()


def _make_loader(cwd: str, agent_dir: str, settings: SettingsManager | None = None) -> DefaultResourceLoader:
    return DefaultResourceLoader(
        cwd=cwd,
        agent_dir=agent_dir,
        settings_manager=settings or _make_settings(),
    )


def test_prompt_includes_spawn_subagent_schema():
    settings = _make_settings()
    loader = _make_loader(cwd="/tmp/fake", agent_dir="/tmp/fake_agent", settings=settings)
    tools = ["read", "bash", "spawn_subagent", "ask_user", "edit", "write", "grep", "find", "ls", "finish", "plan"]
    prompt = loader.get_system_prompt(selected_tools=tools)
    assert "- spawn_subagent {task, tasks?, model?, tools?}" in prompt
    assert "- ask_user {question, timeoutSec?}" in prompt


def test_prompt_includes_plan_schema():
    """plan schema is in TOOL_ARG_SCHEMAS and appears in the system prompt."""
    from one.resources.resource_loader import TOOL_ARG_SCHEMAS

    assert "plan" in TOOL_ARG_SCHEMAS
    assert "{plan}" in TOOL_ARG_SCHEMAS["plan"]

    settings = _make_settings()
    loader = _make_loader(cwd="/tmp/fake", agent_dir="/tmp/fake_agent", settings=settings)
    tools = ["read", "bash", "plan", "finish"]
    prompt = loader.get_system_prompt(selected_tools=tools)
    assert "- plan {plan}" in prompt
    # PLANNING RULES mention the plan tool
    assert "Use the plan tool to store the plan." in prompt


def test_prompt_includes_plan_completion_and_reporting_integrity_rules():
    loader = _make_loader(cwd="/tmp/fake", agent_dir="/tmp/fake_agent")
    prompt = loader.get_system_prompt(selected_tools=["read", "plan", "finish"])
    assert "After creating a plan, do not call finish immediately. Execute the planned steps and verify the result first. A plan is not task completion." in prompt
    assert "Do not claim changes or verification without a successful, observed tool result." in prompt


def test_prompt_includes_complexity_aware_planning_policy():
    loader = _make_loader(cwd="/tmp/fake", agent_dir="/tmp/fake_agent")
    prompt = loader.get_system_prompt(selected_tools=["read", "plan", "finish"])

    assert "the user explicitly requests planning, or the task is genuinely complex" in prompt
    assert "3+ dependent phases, multiple components or files, security/infrastructure/operational risk, validation or rollback gates, or required approval" in prompt
    assert "Do NOT plan simple questions, simple single-file fixes with clear requirements, straightforward tests or formatting" in prompt
    assert "User-provided phases are draft scope, not an already-created persisted plan." in prompt
    assert "Never claim that a plan exists unless the plan tool succeeded and you observed its result." in prompt


def test_prompt_limits_read_only_plan_mode_and_cooperation_approval():
    loader = _make_loader(cwd="/tmp/fake", agent_dir="/tmp/fake_agent")
    prompt = loader.get_system_prompt(selected_tools=["read", "plan", "finish"])

    assert "Do not assume every request is read-only planning." in prompt
    assert "only when Plan Mode is explicitly activated or required by task policy" in prompt
    assert "When Plan Mode is active and cooperation mode is enabled, the existing cooperation approval callback handles the pause after successful plan creation" in prompt
    assert "Without cooperation mode, continue according to the original execution request" in prompt
    assert "do not add another approval gate." in prompt


def test_planning_policy_precedes_action_strategy():
    loader = _make_loader(cwd="/tmp/fake", agent_dir="/tmp/fake_agent")
    prompt = loader.get_system_prompt(selected_tools=["read", "plan", "finish"])
    action_strategy_index = prompt.index("ACTION STRATEGY")

    policy_rules = (
        "Create a plan ONLY if no active plan exists",
        "simple single-file fixes with clear requirements",
        "User-provided phases are draft scope",
        "Never claim that a plan exists unless the plan tool succeeded",
        "When Plan Mode is active and cooperation mode is enabled",
        "After creating a plan, do not call finish immediately.",
    )
    assert all(prompt.index(rule) < action_strategy_index for rule in policy_rules)


def test_prompt_includes_all_schemas_regression():
    settings = _make_settings()
    loader = _make_loader(cwd="/tmp/fake", agent_dir="/tmp/fake_agent", settings=settings)
    tools = ["read", "bash", "spawn_subagent", "ask_user", "edit", "write", "grep", "find", "ls", "finish", "plan"]
    prompt = loader.get_system_prompt(selected_tools=tools)
    # Regression guards for existing schemas
    assert "- read {path, offset?, limit?}" in prompt
    assert "- bash {command, timeout?}" in prompt
    assert '- edit {path, edits: [{oldString, newString}]}  # path is TOP-LEVEL (never inside edits); oldString must be unique in the file' in prompt
    assert "- write {path, content}" in prompt
    assert "- grep {pattern, path?}" in prompt
    assert "- find {pattern?, path?}" in prompt
    assert "- ls {path?}" in prompt
    assert "- finish {summary, goal_success}" in prompt
    assert "- plan {plan}" in prompt


def test_prompt_with_custom_system_prompt_skips_builtins():
    settings = _make_settings()
    loader = _make_loader(
        cwd="/tmp/fake",
        agent_dir="/tmp/fake_agent",
        settings=settings,
    )
    # When system_prompt is passed to constructor, it overrides the builtin
    loader2 = DefaultResourceLoader(
        cwd="/tmp/fake",
        agent_dir="/tmp/fake_agent",
        settings_manager=settings,
        system_prompt="MY CUSTOM PROMPT",
    )
    prompt = loader2.get_system_prompt(selected_tools=["read"])
    assert "MY CUSTOM PROMPT" in prompt
    assert "__TOOLS__" not in prompt
    assert "TOOL_ARG_SCHEMAS" not in prompt


def test_get_system_prompt_default_tools():
    """get_system_prompt with no selected_tools uses defaults and still shows schemas."""
    settings = _make_settings()
    loader = _make_loader(cwd="/tmp/fake", agent_dir="/tmp/fake_agent", settings=settings)
    prompt = loader.get_system_prompt()  # defaults to ["read", "bash", "edit", "write"]
    assert "- read {path, offset?, limit?}" in prompt
    assert "- bash {command, timeout?}" in prompt
    assert '- edit {path, edits: [{oldString, newString}]}  # path is TOP-LEVEL (never inside edits); oldString must be unique in the file' in prompt


def test_prompt_reflects_actual_default_timeout():
    """Custom timeoutSec should appear in the system prompt."""
    settings = SettingsManager.in_memory({"tools": {"timeoutSec": 45}})
    loader = _make_loader(cwd="/tmp/fake", agent_dir="/tmp/fake_agent", settings=settings)
    prompt = loader.get_system_prompt(selected_tools=["bash"])
    assert "Default timeout 45s if not specified." in prompt


def test_prompt_default_timeout_with_default_settings():
    """With default settings, the prompt should reflect the 30-second default."""
    settings = _make_settings()
    loader = _make_loader(cwd="/tmp/fake", agent_dir="/tmp/fake_agent", settings=settings)
    prompt = loader.get_system_prompt(selected_tools=["bash"])
    assert "Default timeout 30s if not specified." in prompt


class TestUserPrivileges:
    """Tests for _user_privileges() covering all return paths."""

    def test_user_privileges_root(self, monkeypatch):
        """When euid is 0, should return 'root'."""
        from one.resources import resource_loader

        monkeypatch.setattr(resource_loader.os, "geteuid", lambda: 0)
        assert resource_loader._user_privileges() == "root"

    def test_user_privileges_sudo_nopasswd(self, monkeypatch):
        """When sudo -n true succeeds, should return 'user(sudo nopasswd)'."""
        from unittest.mock import patch

        from one.resources import resource_loader

        monkeypatch.setattr(resource_loader.os, "geteuid", lambda: 1000)
        fake_result = type("_FakeResult", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
        with patch.object(resource_loader.subprocess, "run", return_value=fake_result):
            assert resource_loader._user_privileges() == "user(sudo nopasswd)"

    def test_user_privileges_sudo_with_password(self, monkeypatch):
        """sudo -n fails but user is in sudo/wheel group → 'user(sudo)'."""
        from unittest.mock import patch

        from one.resources import resource_loader

        monkeypatch.setattr(resource_loader.os, "geteuid", lambda: 1000)

        def _fake_run(cmd, **kwargs):
            if cmd[0] == "sudo":
                return type("_FakeResult", (), {"returncode": 1, "stdout": b"", "stderr": b""})()
            if cmd[0] == "groups":
                return type("_FakeResult", (), {"returncode": 0, "stdout": "user sudo docker\n", "stderr": b""})()
            return type("_FakeResult", (), {"returncode": 1, "stdout": "", "stderr": b""})()

        with patch.object(resource_loader.subprocess, "run", side_effect=_fake_run):
            assert resource_loader._user_privileges() == "user(sudo)"

    def test_user_privileges_plain_user(self, monkeypatch):
        """sudo -n fails and no sudo/wheel group → 'user'."""
        from unittest.mock import patch

        from one.resources import resource_loader

        monkeypatch.setattr(resource_loader.os, "geteuid", lambda: 1000)

        def _fake_run(cmd, **kwargs):
            if cmd[0] == "sudo":
                return type("_FakeResult", (), {"returncode": 1, "stdout": b"", "stderr": b""})()
            if cmd[0] == "groups":
                return type("_FakeResult", (), {"returncode": 0, "stdout": "user docker\n", "stderr": b""})()
            return type("_FakeResult", (), {"returncode": 1, "stdout": "", "stderr": b""})()

        with patch.object(resource_loader.subprocess, "run", side_effect=_fake_run):
            assert resource_loader._user_privileges() == "user"
