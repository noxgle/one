from __future__ import annotations

from pathlib import Path

import pytest

from one.resources.resource_loader import DefaultResourceLoader


@pytest.mark.asyncio
async def test_system_prompt_includes_tools_and_project_context(tmp_path: Path):
    cwd = tmp_path / "proj"
    cwd.mkdir(parents=True, exist_ok=True)
    (cwd / "AGENTS.md").write_text("Project rules here.", encoding="utf-8")

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)

    loader = DefaultResourceLoader(
        cwd=str(cwd),
        agent_dir=str(agent_dir),
        settings_manager=None,
        no_skills=True,
    )
    await loader.reload()
    prompt = loader.get_system_prompt(["read", "bash", "grep"])

    assert "You are an expert coding assistant operating inside one" in prompt
    assert "- read" in prompt
    assert "- bash" in prompt
    assert "- grep" in prompt
    assert "Prefer grep/find/ls tools over bash for file exploration." in prompt
    assert "Project rules here." in prompt
    assert "Current date:" in prompt
    assert f"Current working directory: {cwd}" in prompt


@pytest.mark.asyncio
async def test_system_prompt_honors_custom_and_append(tmp_path: Path):
    cwd = tmp_path / "proj"
    cwd.mkdir(parents=True, exist_ok=True)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)

    loader = DefaultResourceLoader(
        cwd=str(cwd),
        agent_dir=str(agent_dir),
        settings_manager=None,
        system_prompt="CUSTOM SYSTEM",
        append_system_prompt="APPENDIX",
        no_skills=True,
    )
    await loader.reload()
    prompt = loader.get_system_prompt(["read"])

    assert "CUSTOM SYSTEM" in prompt
    assert "APPENDIX" in prompt
    assert "Current date:" in prompt
    assert "Current working directory:" in prompt
