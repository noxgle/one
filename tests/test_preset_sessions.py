"""Historical preset snapshots survive session forks without definition files."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from pathlib import Path

import pytest

from one.core.agent_session_runtime import AgentSessionRuntimeHost, create_agent_session_runtime
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.presets import PresetError
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.core.tui_runtime_host import TuiRuntimeHost


class _Loader:
    def __init__(self, agent_dir: str) -> None:
        self.agent_dir = agent_dir

    async def reload(self) -> None:
        return None

    def get_extensions(self) -> dict[str, list[object]]:
        return {"extensions": []}

    def get_system_prompt(self, **_: object) -> str:
        return "base"


@pytest.mark.asyncio
async def test_runtime_tools_follow_preset_switch_and_failed_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent_dir = tmp_path / "agent"
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(agent_dir))
    presets = agent_dir / "presets"
    presets.mkdir(parents=True)
    for preset_id, tools in (("alpha", "read, finish"), ("beta", "grep, finish")):
        (presets / f"{preset_id}.md").write_text(
            f"---\nversion: 1\nid: {preset_id}\nname: {preset_id.title()}\ntools: [{tools}]\n---\n",
            encoding="utf-8",
        )
    registry = ModelRegistry.create(AuthStorage.in_memory(), str(tmp_path / "models.json"))
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    bootstrap = {"agentDir": str(agent_dir), "settingsManager": SettingsManager.in_memory(),
                 "modelRegistry": registry, "model": model, "preset": "alpha"}
    runtime = await create_agent_session_runtime(bootstrap, {
        "cwd": str(tmp_path), "sessionManager": SessionManager.in_memory(str(tmp_path)),
        "resourceLoader": _Loader(str(agent_dir)),
    })
    host = TuiRuntimeHost(AgentSessionRuntimeHost(bootstrap, runtime), None)
    assert await host.ui_operation("tools", {}) == {"tools": ["read", "finish"]}
    await host.ui_operation("set_preset", {"id": "beta"})
    assert await host.ui_operation("tools", {}) == {"tools": ["grep", "finish"]}
    with pytest.raises(PresetError, match="Unknown preset"):
        await host.ui_operation("set_preset", {"id": "missing"})
    assert await host.ui_operation("tools", {}) == {"tools": ["grep", "finish"]}


@pytest.mark.asyncio
async def test_fork_restores_historical_preset_snapshot_after_definitions_are_deleted(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    presets = agent_dir / "presets"
    presets.mkdir(parents=True)
    (presets / "alpha.md").write_text(
        "---\nversion: 1\nid: alpha\nname: Alpha\ntemperature: 0.2\nthinking: high\n"
        "tools: [read, finish]\n---\nALPHA BODY\n", encoding="utf-8"
    )
    (presets / "beta.md").write_text(
        "---\nversion: 1\nid: beta\nname: Beta\ntemperature: 0.7\n"
        "tools: [read, finish]\n---\nBETA BODY\n", encoding="utf-8"
    )
    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth, str(tmp_path / "models.json"))
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory()
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    bootstrap = {"agentDir": str(agent_dir), "settingsManager": settings, "modelRegistry": registry, "model": model, "preset": "alpha"}
    runtime = await create_agent_session_runtime(bootstrap, {"cwd": str(tmp_path), "sessionManager": manager, "resourceLoader": _Loader(str(agent_dir))})
    host = AgentSessionRuntimeHost(bootstrap, runtime)
    manager.append_message({"role": "user", "content": "alpha marker"})
    alpha_marker = manager.get_leaf_id()
    assert alpha_marker is not None
    host.session.activate_preset("beta", str(agent_dir))
    manager.append_message({"role": "user", "content": "beta marker"})
    beta_marker = manager.get_leaf_id()
    assert beta_marker is not None
    parent_path = manager.session_file
    assert parent_path is not None
    for path in presets.iterdir():
        path.unlink()

    await host.fork(beta_marker)
    assert host.session.preset_id == "beta"
    assert host.session.temperature == 0.7
    assert host.session._preset.body == "BETA BODY\n"
    assert host.session.active_tools == ["read", "finish"]

    await host.switch_session(parent_path)
    await host.fork(alpha_marker)
    assert host.session.preset_id == "alpha"
    assert host.session.temperature == 0.2
    assert host.session.thinking_level == "high"
    assert host.session._preset.body == "ALPHA BODY\n"
    assert host.session.active_tools == ["read", "finish"]
