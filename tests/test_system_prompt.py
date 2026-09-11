from __future__ import annotations

from pathlib import Path

import pytest

from one.resources.resource_loader import DefaultResourceLoader, _build_header, _system_env, _user_privileges


def _mk_loader(tmp_path: Path, no_skills: bool = True, **kwargs):
    cwd = tmp_path / "proj"
    cwd.mkdir(parents=True, exist_ok=True)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    return DefaultResourceLoader(cwd=str(cwd), agent_dir=str(agent_dir), settings_manager=None, no_skills=no_skills, **kwargs)


@pytest.mark.asyncio
async def test_system_prompt_includes_header_tools_and_project_context(tmp_path: Path):
    (tmp_path / "proj").mkdir(parents=True, exist_ok=True)
    (tmp_path / "proj" / "AGENTS.md").write_text("Project rules here.", encoding="utf-8")

    loader = _mk_loader(tmp_path)
    await loader.reload()
    prompt = loader.get_system_prompt(["read", "bash", "grep", "finish"])

    # Header with environment context.
    assert "Current time:" in prompt
    assert f"workspace={tmp_path / 'proj'}" in prompt
    assert "env=" in prompt
    assert "user_privileges=" in prompt

    # Adapted base prompt.
    assert "You are an autonomous terminal agent." in prompt
    assert 'Return exactly ONE dict per response:' in prompt

    # Tools listed with arg schemas.
    assert "- read {path, offset?, limit?}" in prompt
    assert "- bash {command, timeout?}" in prompt
    assert "- grep {pattern, path?}" in prompt
    assert "- finish {summary, goal_success}" in prompt

    # Project context still appended.
    assert "Project rules here." in prompt


@pytest.mark.asyncio
async def test_system_prompt_honors_custom_and_append(tmp_path: Path):
    loader = _mk_loader(tmp_path, system_prompt="CUSTOM SYSTEM", append_system_prompt="APPENDIX")
    await loader.reload()
    prompt = loader.get_system_prompt(["read"])

    assert "CUSTOM SYSTEM" in prompt
    assert "APPENDIX" in prompt
    assert "Current time:" in prompt  # header always present
    assert "workspace=" in prompt
    assert "env=" in prompt


@pytest.mark.asyncio
async def test_system_prompt_without_read_skips_skills(tmp_path: Path):
    skills_dir = tmp_path / "agent" / "skills" / "demo"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "SKILL.md").write_text("## Demo skill", encoding="utf-8")

    loader = _mk_loader(tmp_path, no_skills=False)
    await loader.reload()

    with_read = loader.get_system_prompt(["read", "bash"])
    assert "# Skills" in with_read
    assert "demo" in with_read

    without_read = loader.get_system_prompt(["bash", "finish"])
    assert "# Skills" not in without_read


def test_env_helpers_do_not_crash() -> None:
    header = _build_header("/tmp/some/workspace")
    assert "Current time:" in header
    assert "workspace=/tmp/some/workspace" in header
    assert isinstance(_system_env(), str) and _system_env()
    assert _user_privileges() in {"root", "user", "user(sudo)", "user(sudo nopasswd)"}


@pytest.mark.asyncio
async def test_system_prompt_includes_read_image_schema(tmp_path: Path):
    """The read_image tool schema should appear when included in the tools list."""
    loader = _mk_loader(tmp_path)
    await loader.reload()
    prompt = loader.get_system_prompt(["read", "read_image", "bash", "finish"])
    assert "read_image" in prompt
    assert "{path}" in prompt
    # read_image schema should appear in the tools list.
    assert "read_image {path}" in prompt
