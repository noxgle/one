from __future__ import annotations

from pathlib import Path
from typing import Any

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
    tools = ["read", "bash", "spawn_subagent", "ask_user", "edit", "write", "grep", "find", "ls", "finish"]
    prompt = loader.get_system_prompt(selected_tools=tools)
    assert "- spawn_subagent {task, tasks?, model?, tools?}" in prompt
    assert "- ask_user {question, timeoutSec?}" in prompt


def test_prompt_includes_all_schemas_regression():
    settings = _make_settings()
    loader = _make_loader(cwd="/tmp/fake", agent_dir="/tmp/fake_agent", settings=settings)
    tools = ["read", "bash", "spawn_subagent", "ask_user", "edit", "write", "grep", "find", "ls", "finish"]
    prompt = loader.get_system_prompt(selected_tools=tools)
    # Regression guards for existing schemas
    assert "- read {path, offset?, limit?}" in prompt
    assert "- bash {command, timeout?}" in prompt
    assert '- edit {path, edits: [{oldString, newString}]}' in prompt
    assert "- write {path, content}" in prompt
    assert "- grep {pattern, path?}" in prompt
    assert "- find {pattern?, path?}" in prompt
    assert "- ls {path?}" in prompt
    assert "- finish {summary, goal_success}" in prompt


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
    assert '- edit {path, edits: [{oldString, newString}]}' in prompt
