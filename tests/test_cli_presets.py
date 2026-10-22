"""CLI preset precedence regression coverage."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one

from pathlib import Path

import pytest

from one.cli import main as cli_main
from one.cli.args import parse_args
from one.core.agent_session_runtime import create_agent_session_runtime
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager


class _Loader:
    """Offline resource facade sufficient for runtime construction."""

    def __init__(self, agent_dir: str) -> None:
        self.agent_dir = agent_dir

    async def reload(self) -> None:
        pass

    def get_extensions(self) -> dict[str, list[object]]:
        return {"extensions": []}


@pytest.mark.asyncio
async def test_preset_values_apply_only_without_explicit_cli_overrides(tmp_path: Path) -> None:
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "review.md").write_text(
        "---\nversion: 1\nid: review\nname: Review\ntemperature: 0.2\nthinking: high\n---\nReview.\n",
        encoding="utf-8",
    )
    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth)
    settings = SettingsManager.in_memory()
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    manager = SessionManager.in_memory(str(tmp_path))
    runtime = await create_agent_session_runtime(
        {"settingsManager": settings, "modelRegistry": registry, "agentDir": str(tmp_path), "model": model, "preset": "review"},
        {"cwd": str(tmp_path), "sessionManager": manager, "resourceLoader": _Loader(str(tmp_path))},
    )
    assert runtime.session.temperature == 0.2
    assert runtime.session.thinking_level == "high"
    await runtime.session.dispose()

    runtime = await create_agent_session_runtime(
        {"settingsManager": settings, "modelRegistry": registry, "agentDir": str(tmp_path), "model": model, "preset": "review", "temperature": 0.8, "thinkingLevel": "low", "explicitTemperature": True, "explicitThinking": True},
        {"cwd": str(tmp_path), "sessionManager": SessionManager.in_memory(str(tmp_path)), "resourceLoader": _Loader(str(tmp_path))},
    )
    assert runtime.session.temperature == 0.8
    assert runtime.session.thinking_level == "low"
    await runtime.session.dispose()


def test_preset_and_system_prompt_are_parser_error() -> None:
    parsed = parse_args(["--preset", "readonly", "--system-prompt", "role"])
    assert parsed.errors == ["--preset cannot be combined with --system-prompt"]


@pytest.mark.asyncio
async def test_help_lists_preset_without_starting_runtime(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fail_if_runtime_starts(*_: object, **__: object) -> object:
        raise AssertionError("runtime startup must not occur for --help")

    monkeypatch.setattr(cli_main, "get_agent_dir", fail_if_runtime_starts)
    assert await cli_main._run(["--help"]) == 0
    assert "--preset <id>" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_invalid_bootstrap_preset_tools_close_mcp_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup validation must use McpManager.close(), including bad tool allowlists."""
    agent_dir = tmp_path / "agent"
    presets = agent_dir / "presets"
    presets.mkdir(parents=True)
    (presets / "broken.md").write_text(
        "---\nversion: 1\nid: broken\nname: Broken\ntools: [read]\n---\nrole\n",
        encoding="utf-8",
    )

    class Loader:
        def __init__(self, **_: object) -> None:
            pass

        async def reload(self) -> None:
            return None

    class Mcp:
        closed = False

        @classmethod
        def create(cls, _: object) -> "Mcp":
            return cls()

        async def start(self) -> None:
            return None

        def errors(self) -> list[str]:
            return []

        async def close(self) -> None:
            self.closed = True

    mcp = Mcp()
    monkeypatch.setattr(cli_main, "get_agent_dir", lambda: str(agent_dir))
    monkeypatch.setattr(cli_main, "get_models_path", lambda: str(tmp_path / "models.json"))
    monkeypatch.setattr(cli_main, "DefaultResourceLoader", Loader)
    monkeypatch.setattr(cli_main, "McpManager", type("Factory", (), {"create": staticmethod(lambda _: mcp)}))
    assert await cli_main._run(["--mode", "text", "--no-session", "--preset", "broken"]) == 2
    assert mcp.closed is True
