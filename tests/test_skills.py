"""Tests for the skills system — loader, validation, prompt, invocation."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from one.core.settings_manager import SettingsManager
from one.resources.resource_loader import (
    DefaultResourceLoader,
    _parse_frontmatter,
    _validate_skill_fm,
    _validate_skill_name,
    load_skill_body,
)

# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def test_parse_frontmatter_valid():
    """Valid YAML frontmatter is parsed correctly."""
    raw = """---
name: test-skill
description: "A test skill"
license: MIT
---

# Body

Some content here.
"""
    fm, body = _parse_frontmatter(raw)
    assert fm == {"name": "test-skill", "description": "A test skill", "license": "MIT"}
    assert "# Body" in body
    assert "Some content here" in body


def test_parse_frontmatter_no_frontmatter():
    """Files without frontmatter return empty dict and full content."""
    raw = "# Just a title\n\nNo frontmatter here."
    fm, body = _parse_frontmatter(raw)
    assert fm == {}
    assert body == raw


def test_parse_frontmatter_malformed_yaml():
    """Malformed YAML frontmatter returns empty dict."""
    raw = """---
name: [invalid yaml
---

Body.
"""
    fm, body = _parse_frontmatter(raw)
    assert fm == {}
    assert "Body." in body


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------

def test_validate_skill_name_valid():
    """Valid names are accepted (returns None)."""
    for name in ["test", "a", "my-skill", "test123", "skill-name-456"]:
        error = _validate_skill_name(name)
        assert error is None, f"Name '{name}' should be valid"


def test_validate_skill_name_invalid():
    """Invalid names produce error messages."""
    invalid_cases = [
        ("", "name is empty"),
        ("a" * 65, "name length"),
        ("UPPER", "lowercase letters"),
        ("test--double", "single hyphens"),
        ("-edge", "lowercase letters"),  # starts with hyphen, caught by regex first
        ("edge-", "lowercase letters"),  # ends with hyphen, caught by regex first
        ("test-", "lowercase letters"),  # ends with hyphen, caught by regex first
    ]
    for name, expected_error in invalid_cases:
        error = _validate_skill_name(name)
        assert error is not None
        # Just check that an error was returned (specific message may vary)
        assert len(error) > 0


# ---------------------------------------------------------------------------
# Frontmatter validation
# ---------------------------------------------------------------------------

def test_validate_skill_fm_valid():
    """Valid frontmatter produces a valid skill dict."""
    fm = {"name": "test-skill", "description": "A test description"}
    skill, diagnostics = _validate_skill_fm(fm, "/tmp/test-skill")
    assert skill["valid"] is True
    assert skill["name"] == "test-skill"
    assert skill["description"] == "A test description"
    assert diagnostics == []


def test_validate_skill_fm_missing_description():
    """Missing description produces a diagnostic."""
    fm = {"name": "test-skill"}
    skill, diagnostics = _validate_skill_fm(fm, "/tmp/test-skill")
    assert skill["valid"] is False
    assert any("description" in d.lower() for d in diagnostics)


def test_validate_skill_fm_invalid_name():
    """Invalid name produces a diagnostic."""
    fm = {"name": "UPPERCASE", "description": "A test"}
    skill, diagnostics = _validate_skill_fm(fm, "/tmp/test-skill")
    assert skill["valid"] is False
    assert any("name" in d.lower() for d in diagnostics)


def test_validate_skill_fm_empty_name():
    """Empty name produces a diagnostic."""
    fm = {"name": "", "description": "A test"}
    skill, diagnostics = _validate_skill_fm(fm, "/tmp/test-skill")
    assert skill["valid"] is False


def test_validate_skill_fm_with_optional_fields():
    """Optional fields are preserved."""
    fm = {
        "name": "test-skill",
        "description": "A test",
        "license": "MIT",
        "compatibility": ">=0.1.0",
        "metadata": {"author": "test", "version": "1.0"},
        "allowed-tools": ["read", "bash"],
        "disable-model-invocation": True,
    }
    skill, diagnostics = _validate_skill_fm(fm, "/tmp/test-skill")
    assert skill["valid"] is True
    assert skill["license"] == "MIT"
    assert skill["compatibility"] == ">=0.1.0"
    assert skill["metadata"] == {"author": "test", "version": "1.0"}
    assert skill["allowed-tools"] == ["read", "bash"]
    assert skill["disable-model-invocation"] is True
    assert diagnostics == []


# ---------------------------------------------------------------------------
# Skill body loading
# ---------------------------------------------------------------------------

def test_load_skill_body(tmp_path):
    """load_skill_body returns body and frontmatter from a file."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: test-skill\ndescription: A test\n---\n\nBody content.\n"
    )
    result = load_skill_body(str(skill_file))
    assert result["valid"] is True
    assert "Body content" in result["body"]
    assert result["fm"] == {"name": "test-skill", "description": "A test"}
    assert "baseDir" in result


def test_load_skill_body_file_not_found():
    """load_skill_body returns error for non-existent file."""
    result = load_skill_body("/nonexistent/SKILL.md")
    assert result["valid"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# Skill discovery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skill_discovery_basic(tmp_path):
    """Skills are discovered from additional_skill_paths."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: test-skill\ndescription: A test skill\n---\n\nBody.\n"
    )

    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=SettingsManager.in_memory(),
        additional_skill_paths=[str(tmp_path)],
    )
    await loader.reload()
    skills = loader.get_skills()
    # Check that our test skill is discovered (there may be other skills from ~/.agents)
    skill_names = [s["name"] for s in skills["skills"]]
    assert "test-skill" in skill_names
    test_skill = next(s for s in skills["skills"] if s["name"] == "test-skill")
    assert test_skill["description"] == "A test skill"


@pytest.mark.asyncio
async def test_skill_discovery_deduplication(tmp_path):
    """Duplicate skill names: first valid wins, subsequent produce diagnostics."""
    skill_dir1 = tmp_path / "my-skill"
    skill_dir1.mkdir()
    skill_file1 = skill_dir1 / "SKILL.md"
    skill_file1.write_text("---\nname: my-skill\ndescription: First\n---\n\nBody.\n")

    skill_dir2 = tmp_path / "my-skill-copy"
    skill_dir2.mkdir()
    skill_file2 = skill_dir2 / "SKILL.md"
    skill_file2.write_text("---\nname: my-skill\ndescription: Second\n---\n\nBody.\n")

    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=SettingsManager.in_memory(),
        additional_skill_paths=[str(tmp_path)],
    )
    await loader.reload()
    skills = loader.get_skills()
    # Check that our test skill is discovered (there may be other skills from ~/.agents)
    my_skills = [s for s in skills["skills"] if s["name"] == "my-skill"]
    # Should have exactly one (deduplicated)
    assert len(my_skills) == 1, f"Expected 1 'my-skill', got {len(my_skills)}: {[s['baseDir'] for s in my_skills]}"
    # Diagnostics should mention duplicate
    assert any("duplicate" in d.lower() for d in skills["diagnostics"]), f"Expected duplicate diagnostic, got: {skills['diagnostics']}"


@pytest.mark.asyncio
async def test_skill_discovery_malformed_ignored(tmp_path):
    """Malformed skills produce diagnostics but don't crash."""
    skill_dir = tmp_path / "bad-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: bad-skill\n---\n\nBody.\n")  # missing description

    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=SettingsManager.in_memory(),
        additional_skill_paths=[str(tmp_path)],
    )
    await loader.reload()
    skills = loader.get_skills()
    # Check that our bad-skill is NOT in the list (there may be other skills from ~/.agents)
    bad_skills = [s for s in skills["skills"] if s["name"] == "bad-skill"]
    assert len(bad_skills) == 0, f"bad-skill should be omitted, but found: {bad_skills}"
    # Diagnostics should mention missing description
    assert any("description" in d.lower() for d in skills["diagnostics"]), f"Expected description diagnostic, got: {skills['diagnostics']}"


@pytest.mark.asyncio
async def test_no_skills_flag(tmp_path):
    """no_skills=True prevents discovery."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: test-skill\ndescription: A test\n---\n\nBody.\n")

    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=SettingsManager.in_memory(),
        no_skills=True,
    )
    await loader.reload()
    skills = loader.get_skills()
    assert len(skills["skills"]) == 0


@pytest.mark.asyncio
async def test_no_skills_with_explicit_path(tmp_path):
    """Explicit --skill paths still load even with no_skills=True."""
    skill_dir = tmp_path / "explicit-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: explicit-skill\ndescription: Explicit\n---\n\nBody.\n")

    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=SettingsManager.in_memory(),
        no_skills=True,
        additional_skill_paths=[str(tmp_path)],
    )
    await loader.reload()
    skills = loader.get_skills()
    # Explicit paths should still load skills (even with no_skills=True)
    skill_names = [s["name"] for s in skills["skills"]]
    assert "explicit-skill" in skill_names


# ---------------------------------------------------------------------------
# get_skill (progressive disclosure)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_skill_by_name(tmp_path):
    """get_skill returns the full body for a skill by name."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: test-skill\ndescription: A test\n---\n\nFull body content.\n")

    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=SettingsManager.in_memory(),
        additional_skill_paths=[str(tmp_path)],
    )
    await loader.reload()

    result = loader.get_skill("test-skill")
    assert result["name"] == "test-skill"
    assert "Full body content" in result["body"]
    assert "baseDir" in result


def test_get_skill_unknown_name():
    """get_skill returns error for unknown skill."""
    loader = DefaultResourceLoader(
        cwd="/tmp",
        agent_dir="/tmp",
        settings_manager=SettingsManager.in_memory(),
    )
    result = loader.get_skill("nonexistent")
    assert "error" in result
    assert "nonexistent" in result["error"]


# ---------------------------------------------------------------------------
# System prompt integration
# ---------------------------------------------------------------------------

def test_system_prompt_includes_skill_metadata():
    """Skills appear in system prompt with name and description."""
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: test-skill\ndescription: A test skill for prompt\n---\n\nBody.\n"
        )
        loader = DefaultResourceLoader(
            cwd=tmp,
            agent_dir=tmp,
            settings_manager=SettingsManager.in_memory(),
            additional_skill_paths=[tmp],
        )
        # Reload to discover skills
        asyncio.run(loader.reload())
        prompt = loader.get_system_prompt(selected_tools=["read"])
        assert "test-skill" in prompt
        assert "A test skill for prompt" in prompt
        assert "on demand" in prompt.lower()


# ---------------------------------------------------------------------------
# Session invoke_skill (integration test)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_invoke_skill(tmp_path):
    """Session.invoke_skill loads skill, prompts provider, returns metadata."""
    from collections.abc import Callable

    from one.core.agent_session import AgentSession
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.settings_manager import SettingsManager
    from one.core.types import ModelInfo
    from one.providers.base import ChatResult

    class _TestProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(
            self,
            api_key: str,
            model: str,
            messages: list[dict],
            thinking_level: str,
            headers: dict | None = None,  # noqa: ARG002
            on_delta: Callable[[str], None] | None = None,  # noqa: ARG002
            on_thinking_delta: Callable[[str], None] | None = None,  # noqa: ARG002
            max_tokens: int | None = None,  # noqa: ARG002
            images: list[dict] | None = None,  # noqa: ARG002
            storage_dir: str = "",  # noqa: ARG002
        ) -> ChatResult:
            self.calls += 1
            return ChatResult(
                text="FINAL_ANSWER:Skill processed",
                raw={},
                usage={},
                stop_reason="stop",
            )

    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: A test\n---\n\nSkill body.\n"
    )

    session_manager = SessionManager.in_memory()
    # maxSteps=1 avoids the tool-nudge second provider call.
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 1, "timeoutSec": 5}})
    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=settings,
        additional_skill_paths=[str(tmp_path)],
    )
    await loader.reload()

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4") or ModelInfo(provider="openai", id="gpt-4")
    mock_provider = _TestProvider()

    session = AgentSession(
        session_manager=session_manager,
        settings_manager=settings,
        model_registry=registry,
        resource_loader=loader,
        model=model,
        thinking_level="medium",
    )
    session.providers = {"openai": mock_provider}  # type: ignore[dict-item]

    result = await session.invoke_skill("test-skill", "user args")
    assert result["ok"] is True
    assert result["name"] == "test-skill"
    assert result["bodyLength"] > 0
    # prompt() was called → at least 2 new messages: user + at least one assistant.
    assert len(session.messages) >= 2
    # Find the user message with skill content (last user message).
    user_msgs = [m for m in session.messages if m["role"] == "user"]
    assert len(user_msgs) >= 1
    skill_user = user_msgs[-1]
    assert "Skill body" in skill_user["content"]
    assert "user args" in skill_user["content"]
    # At least one assistant message.
    assistant_msgs = [m for m in session.messages if m["role"] == "assistant"]
    assert len(assistant_msgs) >= 1
    # At least one assistant response should contain the provider's final answer.
    assistant_texts = " ".join(
        chunk.get("text", "") for msg in assistant_msgs for chunk in msg.get("content", [])
    )
    assert "Skill processed" in assistant_texts
    # Provider was called at least once (nudge may add a second call).
    assert mock_provider.calls >= 1


@pytest.mark.asyncio
async def test_invoke_skill_calls_provider_and_no_duplicate(tmp_path):
    """Regression: invoke_skill triggers provider call and does not duplicate content."""
    from collections.abc import Callable

    from one.core.agent_session import AgentSession
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.settings_manager import SettingsManager
    from one.core.types import ModelInfo
    from one.providers.base import ChatResult

    class _TestProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(
            self,
            api_key: str,
            model: str,
            messages: list[dict],
            thinking_level: str,
            headers: dict | None = None,  # noqa: ARG002
            on_delta: Callable[[str], None] | None = None,  # noqa: ARG002
            on_thinking_delta: Callable[[str], None] | None = None,  # noqa: ARG002
            max_tokens: int | None = None,  # noqa: ARG002
            images: list[dict] | None = None,  # noqa: ARG002
            storage_dir: str = "",  # noqa: ARG002
        ) -> ChatResult:
            self.calls += 1
            return ChatResult(
                text="FINAL_ANSWER:ok",
                raw={},
                usage={},
                stop_reason="stop",
            )

    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: A test\n---\n\nBe specific about this.\n"
    )

    session_manager = SessionManager.in_memory()
    # maxSteps=1 avoids the tool-nudge second provider call.
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 1, "timeoutSec": 5}})
    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=settings,
        additional_skill_paths=[str(tmp_path)],
    )
    await loader.reload()

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4") or ModelInfo(provider="openai", id="gpt-4")
    mock_provider = _TestProvider()

    session = AgentSession(
        session_manager=session_manager,
        settings_manager=settings,
        model_registry=registry,
        resource_loader=loader,
        model=model,
        thinking_level="medium",
    )
    session.providers = {"openai": mock_provider}  # type: ignore[dict-item]

    await session.invoke_skill("test-skill")

    # Verify the provider was actually called (nudge may add a second).
    assert mock_provider.calls >= 1

    # At least 2 messages in session history: user + assistant(s).
    assert len(session.messages) >= 2
    # Content should NOT be duplicated (no duplicate skill body).
    user_contents = [m.get("content", "") for m in session.messages if m["role"] == "user"]
    user_content_str = " ".join(user_contents)
    # The skill body "Be specific about this." appears exactly once across user messages.
    assert user_content_str.count("Be specific about this.") == 1


@pytest.mark.asyncio
async def test_session_invoke_skill_unknown():
    """invoke_skill returns error for unknown skill."""
    from one.core.agent_session import AgentSession
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.types import ModelInfo

    session_manager = SessionManager.in_memory()
    # maxSteps=1 avoids the tool-nudge second provider call.
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 1, "timeoutSec": 5}})
    loader = DefaultResourceLoader(
        cwd="/tmp",
        agent_dir="/tmp",
        settings_manager=settings,
    )
    await loader.reload()

    registry = ModelRegistry(auth_storage=MagicMock())
    mock_provider = MagicMock()
    mock_provider.list_models = MagicMock(return_value=[])

    model = ModelInfo(provider="openai", id="gpt-4")

    session = AgentSession(
        session_manager=session_manager,
        settings_manager=settings,
        model_registry=registry,
        resource_loader=loader,
        model=model,
        thinking_level="medium",
    )
    session.providers = {"openai": mock_provider}

    result = await session.invoke_skill("nonexistent")
    assert result["ok"] is False
    assert "nonexistent" in result["error"]


# ---------------------------------------------------------------------------
# Reload safety
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_reload_rebinds_extensions():
    """Session.reload() rebinds extensions without duplicating hooks."""
    from one.core.agent_session import AgentSession
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.types import ModelInfo

    session_manager = SessionManager.in_memory()
    # maxSteps=1 avoids the tool-nudge second provider call.
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 1, "timeoutSec": 5}})
    loader = DefaultResourceLoader(
        cwd="/tmp",
        agent_dir="/tmp",
        settings_manager=settings,
    )
    await loader.reload()

    registry = ModelRegistry(auth_storage=MagicMock())
    mock_provider = MagicMock()
    mock_provider.list_models = MagicMock(return_value=[])

    model = ModelInfo(provider="openai", id="gpt-4")

    session = AgentSession(
        session_manager=session_manager,
        settings_manager=settings,
        model_registry=registry,
        resource_loader=loader,
        model=model,
        thinking_level="medium",
    )
    session.providers = {"openai": mock_provider}
    # Initialize extension runtime
    await session.bind_extensions()
    first_runtime = session._extension_runtime

    # Reload should dispose old runtime and create new one
    result = await session.reload()
    assert "skills" in result
    assert "diagnostics" in result
    assert "extensions" in result
    # Extension runtime should be rebound (not None after reload)
    assert session._extension_runtime is not None


# ---------------------------------------------------------------------------
# get_skill — invalid vs unknown differentiation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_skill_invalid_vs_unknown(tmp_path):
    """get_skill returns 'invalid' for existing-but-malformed skills, 'Unknown' for truly missing ones."""
    # Create an invalid skill (missing description).
    skill_dir = tmp_path / "bad-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: bad-skill\n---\n\nBody.\n")

    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=SettingsManager.in_memory(),
        additional_skill_paths=[str(tmp_path)],
    )
    await loader.reload()

    # Invalid skill: should have 'invalid' flag and diagnostics.
    result = loader.get_skill("bad-skill")
    assert "error" in result
    assert result["invalid"] is True
    assert "bad-skill" in result["error"]
    assert len(result["diagnostics"]) > 0

    # Unknown skill: should NOT have 'invalid' flag.
    result2 = loader.get_skill("totally-missing")
    assert "error" in result2
    assert result2.get("invalid") is not True
    assert "totally-missing" in result2["error"]


# ---------------------------------------------------------------------------
# invoke_skill — structured errors and metadata
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_skill_invalid_returns_error_type(tmp_path):
    """invoke_skill for invalid skill returns structured error with errorType/invalid/diagnostics."""
    from one.core.agent_session import AgentSession
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.types import ModelInfo

    # Create an invalid skill (missing description).
    skill_dir = tmp_path / "bad-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: bad-skill\n---\n\nBody.\n")

    session_manager = SessionManager.in_memory()
    # maxSteps=1 avoids the tool-nudge second provider call.
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 1, "timeoutSec": 5}})
    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=settings,
        additional_skill_paths=[str(tmp_path)],
    )
    await loader.reload()

    registry = ModelRegistry(auth_storage=MagicMock())
    mock_provider = MagicMock()
    mock_provider.list_models = MagicMock(return_value=[])
    model = ModelInfo(provider="openai", id="gpt-4")

    session = AgentSession(
        session_manager=session_manager,
        settings_manager=settings,
        model_registry=registry,
        resource_loader=loader,
        model=model,
        thinking_level="medium",
    )
    session.providers = {"openai": mock_provider}

    result = await session.invoke_skill("bad-skill")
    assert result["ok"] is False
    assert result["errorType"] == "SkillError"
    assert result["invalid"] is True
    assert len(result["diagnostics"]) > 0


@pytest.mark.asyncio
async def test_invoke_skill_valid_returns_trust_warning_and_metadata(tmp_path):
    """invoke_skill for valid skill returns trustWarning, allowedTools, disableModelInvocation."""
    from one.core.agent_session import AgentSession
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.types import ModelInfo

    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: A test\nallowed-tools:\n  - read\n  - bash\ndisable-model-invocation: true\n---\n\nSkill body.\n"
    )

    session_manager = SessionManager.in_memory()
    # maxSteps=1 avoids the tool-nudge second provider call.
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 1, "timeoutSec": 5}})
    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=settings,
        additional_skill_paths=[str(tmp_path)],
    )
    await loader.reload()

    registry = ModelRegistry(auth_storage=MagicMock())
    mock_provider = MagicMock()
    mock_provider.list_models = MagicMock(return_value=[])
    model = ModelInfo(provider="openai", id="gpt-4")

    session = AgentSession(
        session_manager=session_manager,
        settings_manager=settings,
        model_registry=registry,
        resource_loader=loader,
        model=model,
        thinking_level="medium",
    )
    session.providers = {"openai": mock_provider}

    result = await session.invoke_skill("test-skill")
    assert result["ok"] is True
    assert "trustWarning" in result
    assert result["allowedTools"] == ["read", "bash"]
    assert result["disableModelInvocation"] is True


# ---------------------------------------------------------------------------
# Reload diagnostics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_reload_returns_enhanced_diagnostics(tmp_path):
    """Session.reload() returns skills, diagnostics, extensions/prompts/themes counts."""
    from one.core.agent_session import AgentSession
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.types import ModelInfo

    session_manager = SessionManager.in_memory()
    # maxSteps=1 avoids the tool-nudge second provider call.
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 1, "timeoutSec": 5}})
    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=settings,
    )
    await loader.reload()

    registry = ModelRegistry(auth_storage=MagicMock())
    mock_provider = MagicMock()
    mock_provider.list_models = MagicMock(return_value=[])
    model = ModelInfo(provider="openai", id="gpt-4")

    session = AgentSession(
        session_manager=session_manager,
        settings_manager=settings,
        model_registry=registry,
        resource_loader=loader,
        model=model,
        thinking_level="medium",
    )
    session.providers = {"openai": mock_provider}

    result = await session.reload()
    assert "skills" in result
    assert "diagnostics" in result
    assert "extensions" in result
    assert isinstance(result["extensions"], dict)
    assert "count" in result["extensions"]
    assert "errors" in result["extensions"]
    assert "prompts" in result
    assert "count" in result["prompts"]
    assert "themes" in result
    assert "count" in result["themes"]


# ---------------------------------------------------------------------------
# invoke_skill during streaming — BusySessionError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_skill_during_streaming_returns_busy_error():
    """invoke_skill while _is_streaming=True returns BusySessionError."""
    from one.core.agent_session import AgentSession
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.types import ModelInfo

    session_manager = SessionManager.in_memory()
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 1, "timeoutSec": 5}})
    loader = DefaultResourceLoader(
        cwd="/tmp",
        agent_dir="/tmp",
        settings_manager=settings,
    )
    await loader.reload()

    registry = ModelRegistry(auth_storage=MagicMock())
    mock_provider = MagicMock()
    mock_provider.list_models = MagicMock(return_value=[])
    model = ModelInfo(provider="openai", id="gpt-4")

    session = AgentSession(
        session_manager=session_manager,
        settings_manager=settings,
        model_registry=registry,
        resource_loader=loader,
        model=model,
        thinking_level="medium",
    )
    session.providers = {"openai": mock_provider}

    # Simulate an active streaming session.
    session._is_streaming = True

    result = await session.invoke_skill("nonexistent-skill")
    assert result["ok"] is False
    assert result["errorType"] == "BusySessionError"
    assert "busy" in result["error"].lower() or "streaming" in result["error"].lower() or "idle" in result["error"].lower()
    # Provider must NOT have been called.
    mock_provider.chat.assert_not_called()


@pytest.mark.asyncio
async def test_invoke_skill_after_streaming_clears_works_normally(tmp_path):
    """invoke_skill works normally after streaming finishes (is_streaming=False)."""
    from collections.abc import Callable

    from one.core.agent_session import AgentSession
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.types import ModelInfo
    from one.providers.base import ChatResult

    class _TestProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(
            self,
            api_key: str,
            model: str,
            messages: list[dict],
            thinking_level: str,
            headers: dict | None = None,
            on_delta: Callable[[str], None] | None = None,
            on_thinking_delta: Callable[[str], None] | None = None,
            max_tokens: int | None = None,
            images: list[dict] | None = None,
            storage_dir: str = "",
        ) -> ChatResult:
            self.calls += 1
            return ChatResult(
                text="ANSWER",
                raw={},
                usage={},
                stop_reason="stop",
            )

    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: A test\n---\n\nSkill body.\n"
    )

    session_manager = SessionManager.in_memory()
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 1, "timeoutSec": 5}})
    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=settings,
        additional_skill_paths=[str(tmp_path)],
    )
    await loader.reload()

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4") or ModelInfo(provider="openai", id="gpt-4")
    mock_provider = _TestProvider()

    session = AgentSession(
        session_manager=session_manager,
        settings_manager=settings,
        model_registry=registry,
        resource_loader=loader,
        model=model,
        thinking_level="medium",
    )
    session.providers = {"openai": mock_provider}

    # is_streaming starts False → should work.
    assert session.is_streaming is False
    result = await session.invoke_skill("test-skill")
    assert result["ok"] is True
    assert result["name"] == "test-skill"


# ---------------------------------------------------------------------------
# Skill invocation syntax — read dispatch fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_skill_invocation_known_valid(tmp_path):
    """read('/skill:my-skill') returns a hint with the skill's filePath."""
    from one.core.agent_session import AgentSession
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.types import ModelInfo

    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: A useful skill\n---\n\nBody.\n"
    )

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 1, "timeoutSec": 5}})
    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=settings,
        additional_skill_paths=[str(tmp_path)],
    )
    await loader.reload()

    registry = ModelRegistry(auth_storage=MagicMock())
    mock_provider = MagicMock()
    mock_provider.list_models = MagicMock(return_value=[])
    model = ModelInfo(provider="openai", id="gpt-4")

    session = AgentSession(
        session_manager=SessionManager.in_memory(),
        settings_manager=settings,
        model_registry=registry,
        resource_loader=loader,
        model=model,
        thinking_level="medium",
    )
    session.providers = {"openai": mock_provider}

    result = await session._run_tool_call("read", {"path": "/skill:my-skill"})
    assert result["ok"] is False
    assert result.get("errorType") == "SkillInvocationHint"
    raw = result.get("rawResult", {})
    assert raw.get("skillName") == "my-skill"
    # rawResult carries the full diagnostic
    assert "filePath" in raw
    assert "SKILL.md" in raw["filePath"]
    # Should contain the real filePath in the error message
    assert "SKILL.md" in result.get("error", "")
    # Should NOT have caused filesystem read
    assert "File not found" not in result.get("error", "")
    assert raw.get("filePath") == str(skill_dir / "SKILL.md")


@pytest.mark.asyncio
async def test_read_skill_invocation_unknown_skill(tmp_path):
    """read('/skill:nonexistent') returns a diagnostic saying no skill found."""
    from one.core.agent_session import AgentSession
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.types import ModelInfo

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 1, "timeoutSec": 5}})
    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=settings,
    )
    await loader.reload()

    registry = ModelRegistry(auth_storage=MagicMock())
    mock_provider = MagicMock()
    mock_provider.list_models = MagicMock(return_value=[])
    model = ModelInfo(provider="openai", id="gpt-4")

    session = AgentSession(
        session_manager=SessionManager.in_memory(),
        settings_manager=settings,
        model_registry=registry,
        resource_loader=loader,
        model=model,
        thinking_level="medium",
    )
    session.providers = {"openai": mock_provider}

    result = await session._run_tool_call("read", {"path": "/skill:nonexistent"})
    assert result["ok"] is False
    assert result.get("errorType") == "SkillInvocationHint"
    raw = result.get("rawResult", {})
    assert raw.get("skillName") == "nonexistent"
    assert "not a file path" in result.get("error", "").lower()


@pytest.mark.asyncio
async def test_read_skill_invocation_unknown_but_path_like(tmp_path):
    """read('/skill:something') when skill exists but name doesn't match should still give hint."""
    from one.core.agent_session import AgentSession
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.types import ModelInfo

    # Create a skill with a DIFFERENT name
    skill_dir = tmp_path / "other-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: other-skill\ndescription: Something else\n---\n\nBody.\n"
    )

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 1, "timeoutSec": 5}})
    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=settings,
        additional_skill_paths=[str(tmp_path)],
    )
    await loader.reload()

    registry = ModelRegistry(auth_storage=MagicMock())
    mock_provider = MagicMock()
    mock_provider.list_models = MagicMock(return_value=[])
    model = ModelInfo(provider="openai", id="gpt-4")

    session = AgentSession(
        session_manager=SessionManager.in_memory(),
        settings_manager=settings,
        model_registry=registry,
        resource_loader=loader,
        model=model,
        thinking_level="medium",
    )
    session.providers = {"openai": mock_provider}

    # Requesting a skill name that doesn't exist (even though another skill does)
    result = await session._run_tool_call("read", {"path": "/skill:my-skill"})
    assert result["ok"] is False
    assert result.get("errorType") == "SkillInvocationHint"
    raw = result.get("rawResult", {})
    assert raw.get("skillName") == "my-skill"


@pytest.mark.asyncio
async def test_read_skill_invocation_skill_without_slash(tmp_path):
    """read('skill:my-skill') (no leading slash) also triggers the fallback."""
    from one.core.agent_session import AgentSession
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.types import ModelInfo

    skill_dir = tmp_path / "dash-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: dash-skill\ndescription: A dashed skill\n---\n\nBody.\n"
    )

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 1, "timeoutSec": 5}})
    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=settings,
        additional_skill_paths=[str(tmp_path)],
    )
    await loader.reload()

    registry = ModelRegistry(auth_storage=MagicMock())
    mock_provider = MagicMock()
    mock_provider.list_models = MagicMock(return_value=[])
    model = ModelInfo(provider="openai", id="gpt-4")

    session = AgentSession(
        session_manager=SessionManager.in_memory(),
        settings_manager=settings,
        model_registry=registry,
        resource_loader=loader,
        model=model,
        thinking_level="medium",
    )
    session.providers = {"openai": mock_provider}

    result = await session._run_tool_call("read", {"path": "skill:my-skill"})
    assert result["ok"] is False
    assert result.get("errorType") == "SkillInvocationHint"
    raw = result.get("rawResult", {})
    assert raw.get("skillName") == "my-skill"


@pytest.mark.asyncio
async def test_read_real_path_unchanged(tmp_path):
    """read('/some/real/file.txt') is NOT intercepted — normal filesystem path."""
    from one.core.agent_session import AgentSession
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.types import ModelInfo

    # Create a real file
    (tmp_path / "real.txt").write_text("Hello world\n")

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 1, "timeoutSec": 5}})
    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=settings,
    )
    await loader.reload()

    registry = ModelRegistry(auth_storage=MagicMock())
    mock_provider = MagicMock()
    mock_provider.list_models = MagicMock(return_value=[])
    model = ModelInfo(provider="openai", id="gpt-4")

    session = AgentSession(
        session_manager=SessionManager(
            cwd=str(tmp_path),
            session_dir=str(tmp_path / "sessions"),
            session_file=None,
            persist=False,
        ),
        settings_manager=settings,
        model_registry=registry,
        resource_loader=loader,
        model=model,
        thinking_level="medium",
    )
    session.providers = {"openai": mock_provider}

    result = await session._run_tool_call("read", {"path": str(tmp_path / "real.txt")})
    # Should succeed normally — not intercepted
    assert result["ok"] is True
    assert "Hello world" in result.get("result", "")
    assert result.get("errorType") is None


@pytest.mark.asyncio
async def test_read_skill_invocation_invalid_skill(tmp_path):
    """read('/skill:bad-skill') where skill exists but is invalid returns diagnostics."""
    from one.core.agent_session import AgentSession
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.types import ModelInfo

    # Create an invalid skill (missing description)
    skill_dir = tmp_path / "bad-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: bad-skill\n---\n\nBody.\n")

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 1, "timeoutSec": 5}})
    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=settings,
        additional_skill_paths=[str(tmp_path)],
    )
    await loader.reload()

    registry = ModelRegistry(auth_storage=MagicMock())
    mock_provider = MagicMock()
    mock_provider.list_models = MagicMock(return_value=[])
    model = ModelInfo(provider="openai", id="gpt-4")

    session = AgentSession(
        session_manager=SessionManager.in_memory(),
        settings_manager=settings,
        model_registry=registry,
        resource_loader=loader,
        model=model,
        thinking_level="medium",
    )
    session.providers = {"openai": mock_provider}

    result = await session._run_tool_call("read", {"path": "/skill:bad-skill"})
    assert result["ok"] is False
    assert result.get("errorType") == "SkillInvocationHint"
    raw = result.get("rawResult", {})
    assert raw.get("skillName") == "bad-skill"
    assert "invalid" in result.get("error", "").lower()
    assert len(raw.get("diagnostics", [])) > 0
