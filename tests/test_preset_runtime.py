"""Offline preset policy, persistence, and RPC regression tests."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.agent_session_runtime import create_agent_session_runtime
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.presets import PresetError, parse_preset
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.core.tool_policy import ToolPolicy
from one.modes.rpc_mode import run_rpc_mode
from one.resources.resource_loader import DefaultResourceLoader


class _Loader:
    def __init__(self, agent_dir: str) -> None:
        self.agent_dir = agent_dir
        self.extension_reads = 0

    def get_system_prompt(self, **_: Any) -> str:
        return "base"

    async def reload(self) -> None:
        return None

    def get_extensions(self) -> dict[str, Any]:
        self.extension_reads += 1
        return {"extensions": [{"path": "/does/not/run.py"}]}


def _session(tmp_path: Path, *, policy: ToolPolicy | None = None) -> AgentSession:
    registry = ModelRegistry.create(AuthStorage.in_memory(), str(tmp_path / "models.json"))
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    return AgentSession(
        SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory(), registry,
        _Loader(str(tmp_path)), model, "medium", tool_policy=policy,
    )


def _preset(tmp_path: Path, ident: str, extra: str = "", body: str = "role") -> None:
    directory = tmp_path / "presets"
    directory.mkdir(exist_ok=True)
    (directory / f"{ident}.md").write_text(
        f"---\nversion: 1\nid: {ident}\nname: {ident.title()}\n{extra}---\n{body}", encoding="utf-8"
    )


def test_parser_strictly_rejects_yaml_boolean_version_and_anchors() -> None:
    for header in ("version: true", "thinking: true", "tools: &x [read]", "tools: [*x]"):
        with pytest.raises(PresetError):
            parse_preset(f"---\n{header}\nid: safe\nname: Safe\n---\n")
    valid = parse_preset("---\r\nversion: 1\r\nid: safe\r\nname: Safe\r\n---\r\nrole")
    assert valid.body == "role"


def test_discovery_rejects_oversized_invalid_utf8_and_symlink_files(tmp_path: Path) -> None:
    presets = tmp_path / "presets"
    presets.mkdir()
    target = tmp_path / "outside.md"
    target.write_text("---\nversion: 1\nid: outside\nname: Outside\n---\n", encoding="utf-8")
    (presets / "outside.md").symlink_to(target)
    with pytest.raises(PresetError, match="escapes"):
        # Symlinks are rejected even when their resolved target is benign.
        from one.core.presets import discover_presets
        discover_presets(str(tmp_path))
    (presets / "outside.md").unlink()
    (presets / "bad.md").write_bytes(b"\xff")
    with pytest.raises(PresetError, match="cannot read"):
        discover_presets(str(tmp_path))
    (presets / "bad.md").write_bytes(b"x" * (256 * 1024 + 1))
    with pytest.raises(PresetError, match="exceeds"):
        discover_presets(str(tmp_path))


@pytest.mark.asyncio
async def test_inherited_readonly_ceiling_blocks_shell_and_extensions_after_default(tmp_path: Path) -> None:
    session = _session(tmp_path, policy=ToolPolicy.readonly())
    session.activate_preset("default", str(tmp_path))
    assert session._is_readonly
    with pytest.raises(RuntimeError, match="disabled"):
        await session.execute_bash("echo must-not-run")
    await session.bind_extensions()
    assert session.resource_loader.extension_reads == 0


@pytest.mark.asyncio
async def test_forbidden_dispatch_stops_before_approval_hooks_or_tool_body(tmp_path: Path) -> None:
    approvals: list[str] = []

    async def approval(name: str, _: dict[str, Any]) -> tuple[bool, str]:
        approvals.append(name)
        return True, ""

    session = _session(tmp_path, policy=ToolPolicy.readonly())
    session._approval_callback = approval
    session._cooperation_state["enabled"] = True

    class Hooks:
        calls = 0

        def has_hooks(self, _: str) -> bool:
            self.calls += 1
            return True

    hooks = Hooks()
    session._extension_runtime = hooks
    for name in ("bash", "write", "apply_patch"):
        result = await session._run_tool_call(name, {"path": "never", "patchText": "never"})
        assert result["ok"] is False and "disabled" in result["error"]
        with pytest.raises(RuntimeError, match="disabled"):
            await session._execute_tool_by_name(name, {"path": "never", "patchText": "never"})
    assert approvals == []
    assert hooks.calls == 0


@pytest.mark.asyncio
async def test_custom_policy_forbidden_dispatch_also_skips_extension_hooks(tmp_path: Path) -> None:
    session = _session(tmp_path, policy=ToolPolicy(frozenset({"read", "bash", "finish"})))
    session._active_tools = ["read", "bash", "finish", "write"]

    class Hooks:
        calls = 0

        def has_hooks(self, _: str) -> bool:
            self.calls += 1
            return True

    hooks = Hooks()
    session._extension_runtime = hooks
    result = await session._run_tool_call("write", {"path": "never", "content": "never"})
    assert result["ok"] is False and "disabled" in result["error"]
    assert hooks.calls == 0


@pytest.mark.asyncio
async def test_runtime_resume_default_keeps_dynamic_mcp_but_restricted_policy_does_not(tmp_path: Path) -> None:
    class Tool:
        name = "mcp_safe"

    class Mcp:
        def tools(self) -> list[Tool]:
            return [Tool()]

    registry = ModelRegistry.create(AuthStorage.in_memory(), str(tmp_path / "models.json"))
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory()
    manager = SessionManager.in_memory(str(tmp_path))
    first = AgentSession(manager, settings, registry, _Loader(str(tmp_path)), model, "medium", mcp_manager=Mcp())
    first.activate_preset("default", str(tmp_path))
    runtime = await create_agent_session_runtime(
        {"settingsManager": settings, "modelRegistry": registry, "agentDir": str(tmp_path), "model": model, "mcpManager": Mcp()},
        {"cwd": str(tmp_path), "sessionManager": manager, "resourceLoader": _Loader(str(tmp_path))},
    )
    assert "mcp_safe" in runtime.session.active_tools
    await runtime.session.dispose()

    restricted = _session(tmp_path, policy=ToolPolicy.readonly())
    restricted.activate_preset("default", str(tmp_path))
    assert restricted._is_readonly
    assert "mcp_safe" not in restricted.active_tools


@pytest.mark.asyncio
async def test_custom_role_keeps_invariant_protocol_and_project_context(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("project-guidance", encoding="utf-8")
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: Demo skill\n---\nbody", encoding="utf-8")
    loader = DefaultResourceLoader(str(tmp_path), str(tmp_path), SettingsManager.in_memory())
    await loader.reload()
    prompt = loader.get_system_prompt(selected_tools=["read", "finish"], base_role="CUSTOM ROLE")
    assert "CUSTOM ROLE" in prompt and "You are ONE an autonomous terminal agent" not in prompt
    assert "# Tool Protocol" in prompt and '"tool":"finish"' in prompt
    assert "- read {path" in prompt and "project-guidance" in prompt and "Demo skill" in prompt
    assert "Inspect tool results before claiming success" in prompt


def test_activation_failures_leave_state_and_branch_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session(tmp_path)
    before = (session.preset_id, session.temperature, list(session.active_tools), list(session.session_manager.get_branch()))
    _preset(tmp_path, "badmodel", "model: nowhere/missing\n")
    with pytest.raises(PresetError, match="unavailable"):
        session.activate_preset("badmodel", str(tmp_path))
    assert (session.preset_id, session.temperature, session.active_tools, session.session_manager.get_branch()) == before
    _preset(tmp_path, "custom", "tools: [read, finish]\n")
    monkeypatch.setattr(session.session_manager, "append_preset_state", lambda _: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError, match="disk"):
        session.activate_preset("custom", str(tmp_path))
    assert (session.preset_id, session.temperature, session.active_tools, session.session_manager.get_branch()) == before


def test_mcp_failure_and_default_restore_are_atomic_and_keep_permitted_mcp(tmp_path: Path) -> None:
    class Mcp:
        def tools(self):
            raise RuntimeError("mcp unavailable")

    session = _session(tmp_path)
    session._mcp_manager = Mcp()  # setup a failure after ordinary construction
    before = list(session.session_manager.get_branch())
    with pytest.raises(RuntimeError, match="mcp unavailable"):
        session.activate_preset("default", str(tmp_path))
    assert session.session_manager.get_branch() == before

    class Tool:
        name = "mcp_read"

    class GoodMcp:
        def tools(self):
            return [Tool()]

    session._mcp_manager = GoodMcp()
    session.activate_preset("default", str(tmp_path))
    assert "mcp_read" in session.active_tools


def test_activation_normalizes_nonreasoning_model_and_rejects_finishless_custom_tools(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _preset(tmp_path, "local", "model: llama.cpp/local\n")
    session.activate_preset("local", str(tmp_path))
    assert session.thinking_level == "off"
    _preset(tmp_path, "broken", "tools: [read]\n")
    with pytest.raises(PresetError, match="include finish"):
        session.activate_preset("broken", str(tmp_path))


def test_constructor_rejects_invalid_preset_tools_before_persisting_state(tmp_path: Path) -> None:
    registry = ModelRegistry.create(AuthStorage.in_memory(), str(tmp_path / "models.json"))
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    manager = SessionManager.in_memory(str(tmp_path))
    broken = parse_preset("---\nversion: 1\nid: broken\nname: Broken\ntools: [typo, finish]\n---\nrole")
    with pytest.raises(PresetError, match="unknown tool"):
        AgentSession(manager, SettingsManager.in_memory(), registry, _Loader(str(tmp_path)), model, "medium", preset=broken)
    assert not any(entry.get("type") == "preset_state" for entry in manager.get_branch())


@pytest.mark.asyncio
@pytest.mark.parametrize("preset_id,tools,error", [("missing", None, "Invalid preset"), ("finishless", "[read]", "include finish")])
async def test_public_runtime_rejects_invalid_bootstrap_preset_without_state(
    tmp_path: Path, preset_id: str, tools: str | None, error: str
) -> None:
    if tools is not None:
        _preset(tmp_path, preset_id, f"tools: {tools}\n")
    registry = ModelRegistry.create(AuthStorage.in_memory(), str(tmp_path / "models.json"))
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    manager = SessionManager.in_memory(str(tmp_path))
    with pytest.raises((ValueError, PresetError), match=error):
        await create_agent_session_runtime(
            {"settingsManager": SettingsManager.in_memory(), "modelRegistry": registry, "agentDir": str(tmp_path),
             "model": model, "preset": preset_id},
            {"cwd": str(tmp_path), "sessionManager": manager, "resourceLoader": _Loader(str(tmp_path))},
        )
    assert not any(entry.get("type") == "preset_state" for entry in manager.get_branch())


@pytest.mark.asyncio
async def test_bootstrap_custom_mcp_preset_matches_activation_and_keeps_no_tools_builtin_empty(tmp_path: Path) -> None:
    _preset(tmp_path, "mcp-only", "tools: [mcp_safe, finish]\n")

    class Tool:
        name = "mcp_safe"

    class Mcp:
        def tools(self) -> list[Tool]:
            return [Tool()]

    registry = ModelRegistry.create(AuthStorage.in_memory(), str(tmp_path / "models.json"))
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory()
    runtime = await create_agent_session_runtime(
        {"settingsManager": settings, "modelRegistry": registry, "agentDir": str(tmp_path), "model": model,
         "mcpManager": Mcp(), "preset": "mcp-only"},
        {"cwd": str(tmp_path), "sessionManager": SessionManager.in_memory(str(tmp_path)), "resourceLoader": _Loader(str(tmp_path))},
    )
    assert runtime.session.active_tools == ["finish", "mcp_safe"]
    await runtime.session.dispose()

    readonly = await create_agent_session_runtime(
        {"settingsManager": settings, "modelRegistry": registry, "agentDir": str(tmp_path), "model": model,
         "mcpManager": Mcp(), "preset": "readonly"},
        {"cwd": str(tmp_path), "sessionManager": SessionManager.in_memory(str(tmp_path)), "resourceLoader": _Loader(str(tmp_path))},
    )
    assert "mcp_safe" not in readonly.session.active_tools
    await readonly.session.dispose()

    explicit_empty = await create_agent_session_runtime(
        {"settingsManager": settings, "modelRegistry": registry, "agentDir": str(tmp_path), "model": model,
         "tools": [], "mcpManager": Mcp()},
        {"cwd": str(tmp_path), "sessionManager": SessionManager.in_memory(str(tmp_path)), "resourceLoader": _Loader(str(tmp_path))},
    )
    # ``--no-tools`` selects no builtins; independently configured MCP tools
    # remain available unless ``--no-mcp`` or a policy/preset denies them.
    assert explicit_empty.session.active_tools == ["mcp_safe"]
    await explicit_empty.session.dispose()

    no_mcp = await create_agent_session_runtime(
        {"settingsManager": settings, "modelRegistry": registry, "agentDir": str(tmp_path), "model": model, "tools": []},
        {"cwd": str(tmp_path), "sessionManager": SessionManager.in_memory(str(tmp_path)), "resourceLoader": _Loader(str(tmp_path))},
    )
    assert no_mcp.session.active_tools == []
    await no_mcp.session.dispose()

    readonly_empty = await create_agent_session_runtime(
        {"settingsManager": settings, "modelRegistry": registry, "agentDir": str(tmp_path), "model": model,
         "tools": [], "mcpManager": Mcp(), "preset": "readonly"},
        {"cwd": str(tmp_path), "sessionManager": SessionManager.in_memory(str(tmp_path)), "resourceLoader": _Loader(str(tmp_path))},
    )
    assert readonly_empty.session.active_tools == []
    await readonly_empty.session.dispose()


@pytest.mark.parametrize("attribute,value", [("_is_streaming", True), ("_is_compacting", True), ("_pending_questions", {"wait": {}})])
def test_busy_activation_rejected(tmp_path: Path, attribute: str, value: Any) -> None:
    session = _session(tmp_path)
    setattr(session, attribute, value)
    with pytest.raises(Exception, match="Cannot change preset"):
        session.activate_preset("readonly", str(tmp_path))


def test_restore_preserves_ceiling_and_modified_baseline_without_definition(tmp_path: Path) -> None:
    session = _session(tmp_path, policy=ToolPolicy.readonly())
    session.activate_preset("default", str(tmp_path))
    state = session.session_manager.get_branch()[-1]["state"]
    # Simulate a resumed snapshot after the definition was deleted/changed.
    resumed = _session(tmp_path, policy=ToolPolicy())
    resumed.restore_preset_state(state)
    assert resumed._is_readonly
    assert not resumed.preset_modified
    resumed.temperature = 0.1
    assert resumed.preset_modified
    resumed.activate_preset("default", str(tmp_path))
    assert resumed._is_readonly


class _Host:
    def __init__(self, session: AgentSession) -> None:
        self.session = session


@pytest.mark.asyncio
async def test_rpc_set_preset_keeps_correlation_id_separate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    session = _session(tmp_path)
    lines = iter([
        json.dumps({"type": "set_preset", "id": 42, "presetId": "readonly"}),
        json.dumps({"type": "set_preset", "id": "corr", "preset": "default"}),
        json.dumps({"type": "set_preset", "id": "missing"}),
    ])

    def fake_input() -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(builtins, "input", fake_input)
    with pytest.raises(EOFError):
        await run_rpc_mode(_Host(session))
    responses = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(item["id"], item["success"]) for item in responses] == [(42, True), ("corr", True), ("missing", False)]
    assert session.preset_id == "default"


@pytest.mark.asyncio
async def test_explore_enforces_local_readonly_and_restore_ceiling(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.activate_preset("explore", str(tmp_path))
    assert session._is_readonly
    await session.bind_extensions()
    assert session.resource_loader.extension_reads == 0
    with pytest.raises(RuntimeError, match="disabled"):
        await session.execute_bash("echo never")
    for name in ("bash", "write", "web_connect"):
        session._active_tools.append(name)
        result = await session._run_tool_call(name, {})
        assert result["ok"] is False
        assert not session._tool_policy.permits(name)
    state = session._preset_state(session._preset, session.model, session.temperature,
                                  session.thinking_level, session._base_tools, session._tool_policy)
    child = _session(tmp_path, policy=ToolPolicy(frozenset({"read", "finish"})))
    child.restore_preset_state(state)
    assert child._tool_policy.allowed == frozenset({"read", "finish"})
    child.activate_preset("default", str(tmp_path))
    assert child._tool_policy.allowed == frozenset({"read", "finish"})
    for invalid in (None, ["read", "finish", "web_connect"], ["read", "finish", "bash"]):
        with pytest.raises(PresetError, match="Explore snapshot policy"):
            child.restore_preset_state({**state, "policy": invalid})
    session._extensions_executed = True
    with pytest.raises(PresetError, match="clean session"):
        session.restore_preset_state(state)
    with pytest.raises(PresetError, match="clean session"):
        session.activate_preset("explore", str(tmp_path))


def test_explore_reserved_ceiling_applies_to_untrusted_definition(tmp_path: Path) -> None:
    session = _session(tmp_path)
    definition = parse_preset("---\nversion: 1\nid: explore\nname: Explore\ntools: [read, finish, web_connect, bash]\n---\n")
    _, policy = session._resolve_preset_tools(definition, ["read", "finish", "web_connect", "bash"], ToolPolicy())
    assert policy.allowed == frozenset({"read", "finish"})
