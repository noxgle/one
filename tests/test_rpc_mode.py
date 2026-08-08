from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager


class _FakeLoader:
    def __init__(self, cwd: str) -> None:
        self.cwd = cwd

    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
        return "You are a coding agent."

    def get_extensions(self) -> dict[str, Any]:
        return {"extensions": [{"path": "/tmp/fake.py"}], "errors": [], "runtime": {}}

    def get_skills(self) -> dict[str, Any]:
        return {"skills": [{"name": "analiza", "filePath": "/tmp/SKILL.md"}], "diagnostics": []}

    def get_prompts(self) -> dict[str, Any]:
        return {"prompts": [{"name": "review", "description": "d", "source": "/tmp/p.md"}], "diagnostics": []}

    def get_themes(self) -> dict[str, Any]:
        return {"themes": [{"path": "/tmp/theme.json"}], "diagnostics": []}

    def get_agents_files(self) -> dict[str, Any]:
        return {"agentsFiles": [{"path": "/tmp/AGENTS.md", "content": "x"}]}


class _RuntimeHost:
    def __init__(self, session: AgentSession) -> None:
        self.session = session

    async def new_session(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"cancelled": False}

    async def import_from_jsonl(self, path: str) -> dict[str, Any]:  # noqa: ARG002
        return {"cancelled": False}

    async def switch_session(self, path: str) -> dict[str, Any]:  # noqa: ARG002
        return {"cancelled": False}

    async def fork(self, entry_id: str) -> dict[str, Any]:  # noqa: ARG002
        return {"cancelled": False, "selectedText": None}


def _mk_session(tmp_path: Path) -> AgentSession:
    auth = AuthStorage.in_memory()
    # NOTE: no runtime API key here — a runtime key would shadow stored keys
    # in get_provider_auth_status() and break login/logout assertions.
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session_manager = SessionManager.in_memory(str(tmp_path))
    return AgentSession(session_manager, settings, registry, _FakeLoader(str(tmp_path)), model, "medium")


async def _run_rpc(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    session: AgentSession,
    lines: list[str],
) -> list[dict[str, Any]]:
    """Feed JSON-lines to run_rpc_mode and return all emitted JSON responses."""
    from one.modes.rpc_mode import run_rpc_mode

    feed = iter(lines + [None])

    def fake_input(*args: Any) -> str:  # noqa: ARG001
        try:
            nxt = next(feed)
        except StopIteration:
            raise EOFError from None
        if nxt is None:
            raise EOFError
        return nxt

    monkeypatch.setattr(builtins, "input", fake_input)
    try:
        await run_rpc_mode(_RuntimeHost(session))
    except EOFError:
        # run_rpc_mode only catches EOFError inside command dispatch; EOF on
        # stdin terminates the loop (same as in the real CLI).
        pass
    out = capsys.readouterr().out
    return [json.loads(l) for l in out.splitlines() if l.strip()]


def _resp(responses: list[dict[str, Any]], command: str, rid: str | None = None) -> dict[str, Any]:
    match = [
        r
        for r in responses
        if r.get("command") == command and r.get("type") == "response" and (rid is None or r.get("id") == rid)
    ]
    assert match, f"no response for command {command!r} id={rid!r}: {responses}"
    return match[-1]


@pytest.mark.asyncio
async def test_rpc_get_state_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    session = _mk_session(tmp_path)
    responses = await _run_rpc(monkeypatch, capsys, session, [json.dumps({"type": "get_state", "id": "1"})])

    r = _resp(responses, "get_state", "1")
    assert r["success"] is True
    data = r["data"]
    assert data["model"] == {"provider": "openai", "id": "gpt-4.1"}
    assert data["thinkingLevel"] == "medium"
    assert data["isStreaming"] is False
    assert data["isCompacting"] is False
    assert data["sessionId"] == session.session_id
    assert data["sessionName"] == session.session_name
    assert data["autoCompactionEnabled"] is True
    assert data["messageCount"] == 0
    assert data["pendingMessageCount"] == 0
    assert data["activeTools"] == ["read", "bash", "edit", "write", "grep", "find", "ls", "finish"]
    assert data["autoRetryEnabled"] == session.auto_retry_enabled


@pytest.mark.asyncio
async def test_rpc_get_session_stats_and_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    session = _mk_session(tmp_path)
    session.messages.append({"role": "user", "content": "hi", "timestamp": 0})
    responses = await _run_rpc(
        monkeypatch,
        capsys,
        session,
        [
            json.dumps({"type": "get_session_stats", "id": "s"}),
            json.dumps({"type": "get_queue", "id": "q"}),
        ],
    )

    stats = _resp(responses, "get_session_stats")
    assert stats["success"] is True
    assert stats["data"]["userMessages"] == 1
    assert stats["data"]["totalMessages"] == 1
    assert "sessionId" in stats["data"] and "tokens" in stats["data"]

    queue = _resp(responses, "get_queue")
    assert queue["success"] is True
    assert queue["data"] == {"steering": [], "followUp": []}


@pytest.mark.asyncio
async def test_rpc_theme_get_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    session = _mk_session(tmp_path)
    responses = await _run_rpc(
        monkeypatch,
        capsys,
        session,
        [
            json.dumps({"type": "get_theme", "id": "1"}),
            json.dumps({"type": "set_theme", "id": "2", "theme": "test-theme"}),
            json.dumps({"type": "get_theme", "id": "3"}),
        ],
    )

    assert _resp(responses, "get_theme", "1")["data"]["theme"] == "default"
    assert _resp(responses, "get_theme", "3")["data"]["theme"] == "test-theme"
    assert session.settings_manager.get_theme() == "test-theme"


@pytest.mark.asyncio
async def test_rpc_settings_get_and_set_config_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    session = _mk_session(tmp_path)
    responses = await _run_rpc(
        monkeypatch,
        capsys,
        session,
        [
            json.dumps({"type": "get_settings", "id": "1"}),
            json.dumps({"type": "set_config_value", "id": "2", "key": "theme", "value": "dark"}),
            json.dumps({"type": "get_theme", "id": "3"}),
            json.dumps({"type": "set_config_value", "id": "4", "key": "bad key", "value": 1}),
        ],
    )

    settings = _resp(responses, "get_settings", "1")
    assert settings["success"] is True
    assert "tools" in settings["data"] and "theme" in settings["data"]

    assert _resp(responses, "set_config_value", "2")["success"] is True
    assert _resp(responses, "get_theme", "3")["data"]["theme"] == "dark"

    bad = _resp(responses, "set_config_value", "4")
    assert bad["success"] is False
    assert "Invalid config key" in bad["error"]


@pytest.mark.asyncio
async def test_rpc_retry_settings_and_tool_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    session = _mk_session(tmp_path)
    responses = await _run_rpc(
        monkeypatch,
        capsys,
        session,
        [
            json.dumps({"type": "get_retry_settings", "id": "1"}),
            json.dumps({"type": "get_tool_approval", "id": "2"}),
        ],
    )

    retry = _resp(responses, "get_retry_settings")
    assert retry["success"] is True
    assert "enabled" in retry["data"] and "maxRetries" in retry["data"]

    approval = _resp(responses, "get_tool_approval")
    assert approval["success"] is True
    assert approval["data"] == {"enabled": False, "tools": ["bash", "write", "edit"]}


@pytest.mark.asyncio
async def test_rpc_login_logout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    session = _mk_session(tmp_path)
    responses = await _run_rpc(
        monkeypatch,
        capsys,
        session,
        [
            json.dumps({"type": "login", "id": "1", "provider": "openai", "apiKey": "k123"}),
            json.dumps({"type": "logout", "id": "2", "provider": "openai"}),
            json.dumps({"type": "login", "id": "3", "provider": "openai"}),
        ],
    )

    login = _resp(responses, "login", "1")
    assert login["success"] is True
    assert login["data"]["status"]["configured"] is True

    logout = _resp(responses, "logout", "2")
    assert logout["success"] is True
    assert logout["data"]["status"]["configured"] is False

    missing = _resp(responses, "login", "3")
    assert missing["success"] is False
    assert "apiKey" in missing["error"]


@pytest.mark.asyncio
async def test_rpc_resource_getters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    session = _mk_session(tmp_path)
    responses = await _run_rpc(
        monkeypatch,
        capsys,
        session,
        [
            json.dumps({"type": "get_extensions", "id": "1"}),
            json.dumps({"type": "get_skills", "id": "2"}),
            json.dumps({"type": "get_prompts", "id": "3"}),
            json.dumps({"type": "get_themes", "id": "4"}),
            json.dumps({"type": "get_agents_files", "id": "5"}),
        ],
    )

    assert _resp(responses, "get_extensions")["data"]["extensions"] == [{"path": "/tmp/fake.py"}]
    assert _resp(responses, "get_skills")["data"]["skills"][0]["name"] == "analiza"
    assert _resp(responses, "get_prompts")["data"]["prompts"][0]["name"] == "review"
    assert _resp(responses, "get_themes")["data"]["themes"] == [{"path": "/tmp/theme.json"}]
    assert _resp(responses, "get_agents_files")["data"]["agentsFiles"][0]["path"] == "/tmp/AGENTS.md"


@pytest.mark.asyncio
async def test_rpc_unknown_command_and_parse_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    session = _mk_session(tmp_path)
    responses = await _run_rpc(monkeypatch, capsys, session, ["{not json", json.dumps({"type": "nope", "id": "9"})])

    parse = _resp(responses, "parse")
    assert parse["success"] is False
    assert "Failed to parse" in parse["error"]

    unknown = _resp(responses, "nope")
    assert unknown["id"] == "9"
    assert unknown["success"] is False
    assert "Unknown command" in unknown["error"]
