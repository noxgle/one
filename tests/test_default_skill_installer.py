# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re

import pytest

from one.core.agent_session_runtime import create_agent_session_runtime
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.resources.default_skill_installer import default_skill_bytes, install_default_skills
from one.resources.resource_loader import DefaultResourceLoader


def test_installs_packaged_default_skills_exactly_once(tmp_path) -> None:
    agent_dir = tmp_path / "agent"

    assert install_default_skills(agent_dir, tmp_path, external_skill_roots=[]) == []

    for name in ("explore", "one"):
        assert (agent_dir / "skills" / name / "SKILL.md").read_bytes() == default_skill_bytes(name)

    # A subsequent startup leaves user edits untouched.
    custom = agent_dir / "skills" / "explore" / "SKILL.md"
    custom.write_text("user customization", encoding="utf-8")
    assert install_default_skills(agent_dir, tmp_path, external_skill_roots=[]) == []
    assert custom.read_text(encoding="utf-8") == "user customization"


def test_packaged_default_skill_resources_have_expected_contents() -> None:
    assert hashlib.sha256(default_skill_bytes("explore")).hexdigest() == (
        "8ec62877338d4d2a35a244e774df2aeb4597a3b846fd84ea07c044dc12102171"
    )
    assert hashlib.sha256(default_skill_bytes("one")).hexdigest() == (
        "c6d4c99505048944270a8aa5408b7aa691bb7ea755bb321423c54799189286af"
    )


def test_explore_skill_delegates_to_preset_without_overrides() -> None:
    text = default_skill_bytes("explore").decode("utf-8")
    examples = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert len(examples) == 1
    args = json.loads(examples[0])
    assert set(args) == {"preset", "task"}
    assert args["preset"] == "explore"
    assert args["task"].strip()
    assert len(text.splitlines()) <= 80
    assert "presets.overrides.explore.model" in text
    assert "Loading this skill does not activate" in text
    assert "do not delegate further or reload this skill" in text
    assert "## Allowed Tools" not in text


def test_existing_file_or_unsafe_destination_is_never_overwritten(tmp_path, caplog) -> None:
    agent_dir = tmp_path / "agent"
    existing = agent_dir / "skills" / "one" / "SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("custom", encoding="utf-8")
    unsafe = agent_dir / "skills" / "explore"
    unsafe.write_text("not a directory", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="one.resources.default_skill_installer"):
        diagnostics = install_default_skills(agent_dir, tmp_path, external_skill_roots=[])

    assert existing.read_text(encoding="utf-8") == "custom"
    assert unsafe.read_text(encoding="utf-8") == "not a directory"
    assert any("explore" in diagnostic for diagnostic in diagnostics)
    assert any("explore" in record.message for record in caplog.records)


def test_skips_default_when_lower_precedence_skill_already_available_silently(tmp_path, caplog) -> None:
    agent_dir = tmp_path / "agent"
    platform_skills = tmp_path / "platform"
    skill = platform_skills / "custom-location" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: explore\ndescription: Existing\n---\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="one.resources.default_skill_installer"):
        diagnostics = install_default_skills(agent_dir, tmp_path, external_skill_roots=[platform_skills])
        repeated_diagnostics = install_default_skills(agent_dir, tmp_path, external_skill_roots=[platform_skills])

    assert not (agent_dir / "skills" / "explore" / "SKILL.md").exists()
    assert diagnostics == []
    assert repeated_diagnostics == []
    assert caplog.records == []


def test_installed_skills_are_discovered_as_user_files(tmp_path) -> None:
    agent_dir = tmp_path / "agent"
    install_default_skills(agent_dir, tmp_path, external_skill_roots=[])
    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(agent_dir),
        settings_manager=SettingsManager.in_memory(),
    )

    asyncio.run(loader.reload())
    skills = {skill["name"]: skill for skill in loader.get_skills()["skills"]}

    assert skills["explore"]["filePath"] == str(agent_dir / "skills" / "explore" / "SKILL.md")
    assert skills["one"]["filePath"] == str(agent_dir / "skills" / "one" / "SKILL.md")


@pytest.mark.asyncio
async def test_runtime_bootstrap_installs_defaults_with_injected_agent_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("one.resources.default_skill_installer._root_has_skill_named", lambda *_args: False)
    agent_dir = tmp_path / "agent"
    settings = SettingsManager.in_memory()
    runtime = await create_agent_session_runtime(
        {"agentDir": str(agent_dir), "settingsManager": settings},
        {"cwd": str(tmp_path), "sessionManager": SessionManager.in_memory(str(tmp_path))},
    )
    try:
        assert (agent_dir / "skills" / "explore" / "SKILL.md").exists()
        assert (agent_dir / "skills" / "one" / "SKILL.md").exists()
    finally:
        await runtime.session.dispose()
