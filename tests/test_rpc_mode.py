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

    def get_skill(self, name: str) -> dict[str, Any]:
        return {"error": f"Unknown skill: {name}"}

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


def _mk_session(tmp_path: Path, *, models_path: Path | None = None) -> AgentSession:
    auth = AuthStorage.in_memory()
    # NOTE: no runtime API key here — a runtime key would shadow stored keys
    # in get_provider_auth_status() and break login/logout assertions.
    # Always use an isolated tmp_path models file — never fall back to global config.
    mp = models_path or (tmp_path / "models.json")
    registry = ModelRegistry.create(auth, str(mp))
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
    assert data["activeTools"] == ["read", "read_image", "bash", "edit", "write", "grep", "find", "ls", "finish", "plan", "ask_user", "spawn_subagent", "apply_patch"]
    assert data["autoRetryEnabled"] == session.auto_retry_enabled
    # Task 4 — lastSubagentTimeout diagnostic field is present.
    assert "lastSubagentTimeout" in data
    assert data["lastSubagentTimeout"] == {}  # no timeout has occurred yet


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
    assert "mode" in retry["data"] and "maxRetries" in retry["data"]

    approval = _resp(responses, "get_tool_approval")
    assert approval["success"] is True
    assert approval["data"] == {"enabled": False, "tools": ["bash", "write", "edit", "plan", "apply_patch"]}


@pytest.mark.asyncio
async def test_rpc_login_logout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """RPC login/logout using a fake adapter (no network, no unsafe direct storage)."""
    adapter = _LoginStubAdapter(models=["m1"])
    session = _mk_session_with_provider(tmp_path, "openai", adapter)
    models_path = tmp_path / "models.json"
    session.model_registry._models_path = models_path

    responses = await _run_rpc(
        monkeypatch,
        capsys,
        session,
        [
            json.dumps({"type": "login", "id": "1", "provider": "openai", "apiKey": "sk-ok"}),
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
async def test_rpc_inspect_subagent_timeout_no_diag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """inspect_subagent_timeout (standalone RPC ctype): no diagnostic → available:false, empty diagnostic."""
    session = _mk_session(tmp_path)
    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "inspect_subagent_timeout", "id": "t1"})],
    )
    r = _resp(responses, "inspect_subagent_timeout", "t1")
    assert r["success"] is True
    data = r["data"]
    assert data["available"] is False
    assert data["diagnostic"] == {}


@pytest.mark.asyncio
async def test_rpc_inspect_subagent_timeout_with_diag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """inspect_subagent_timeout with a populated diagnostic: available:true, structured fields present."""
    session = _mk_session(tmp_path)
    # Manually set _last_subagent_timeout to simulate a timeout event.
    session._last_subagent_timeout = {
        "operation": "timed out",
        "errorType": "SubagentTimeout",
        "externalState": "unknown",
        "sessionId": "sub-abc",
        "elapsedSec": 60.5,
        "lastTool": "bash",
        "lastEvent": "message",
        "error": "timed out",
        "summary": "subagent timed out",
        "lastAssistantText": "running...",
        "actionableHint": "increase timeout",
    }
    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "inspect_subagent_timeout", "id": "t2"})],
    )
    r = _resp(responses, "inspect_subagent_timeout", "t2")
    assert r["success"] is True
    data = r["data"]
    assert data["available"] is True
    d = data["diagnostic"]
    assert d["operation"] == "timed out"
    assert d["errorType"] == "SubagentTimeout"
    assert d["externalState"] == "unknown"
    assert d["sessionId"] == "sub-abc"
    assert d["elapsedSec"] == 60.5
    assert d["lastTool"] == "bash"
    assert d["lastEvent"] == "message"
    assert d["error"] == "timed out"
    assert d["summary"] == "subagent timed out"
    assert d["lastAssistantText"] == "running..."
    assert d["actionableHint"] == "increase timeout"


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


@pytest.mark.asyncio
async def test_rpc_answer_question_unknown_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    session = _mk_session(tmp_path)
    responses = await _run_rpc(
        monkeypatch,
        capsys,
        session,
        [json.dumps({"type": "answer_question", "id": "1", "questionId": "nope", "answer": "x"})],
    )
    r = _resp(responses, "answer_question", "1")
    assert r["success"] is False
    assert "No pending question" in r["error"]


@pytest.mark.asyncio
async def test_rpc_get_pending_questions_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    session = _mk_session(tmp_path)
    responses = await _run_rpc(monkeypatch, capsys, session, [json.dumps({"type": "get_pending_questions", "id": "1"})])
    r = _resp(responses, "get_pending_questions", "1")
    assert r["success"] is True
    assert r["data"]["questions"] == []


@pytest.mark.asyncio
async def test_rpc_bash_structured_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """RPC bash with structured timeout result: data.ok:false, timedOut:true, errorType:TimeoutError, nonzero exitCode."""
    session = _mk_session(tmp_path)

    # Monkeypatch execute_bash to return a structured timeout result
    original_execute = session.execute_bash

    async def fake_execute_bash(command):
        return {
            "ok": False,
            "output": "some output\n\nCommand timed out",
            "exitCode": -9,
            "timedOut": True,
            "cancelled": False,
            "truncated": False,
            "fullscreen": False,
            "fullOutputPath": None,
            "content": [{"type": "text", "text": "some output\n\nCommand timed out"}],
            "details": {"truncation": None, "fullscreen": False, "fullOutputPath": None},
            "error": "Command timed out",
            "errorType": "TimeoutError",
        }

    monkeypatch.setattr(session, "execute_bash", fake_execute_bash)

    responses = await _run_rpc(
        monkeypatch,
        capsys,
        session,
        [json.dumps({"type": "bash", "id": "b1", "command": "sleep 100"})],
    )
    r = _resp(responses, "bash", "b1")
    # Transport envelope: command processed successfully (RPC itself succeeded)
    assert r["success"] is True
    data = r["data"]
    # But the bash result itself carries structured timeout fields
    assert data["ok"] is False
    assert data["timedOut"] is True
    assert data["errorType"] == "TimeoutError"
    assert data["exitCode"] is not None and data["exitCode"] != 0
    assert data["cancelled"] is False


# ---------------------------------------------------------------------------
# Phase 30.5: RPC login via validate_and_fetch
# ---------------------------------------------------------------------------


class _LoginStubAdapter:
    """Fake adapter for RPC login tests: configurable list_models / chat behaviour."""

    def __init__(
        self,
        models: list[str] | None = None,
        detailed: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
        chat_error: Exception | None = None,
    ) -> None:
        self.models = models
        self.detailed = detailed
        self.error = error
        self.chat_error = chat_error
        self.list_calls = 0
        self.chat_calls: list[tuple[str, str, list[dict[str, Any]], int | None]] = []

    async def list_models(self, api_key: str, headers: dict[str, str] | None = None) -> list[str] | None:
        self.list_calls += 1
        if self.error is not None:
            raise self.error
        return self.models

    async def list_models_detailed(
        self, api_key: str, headers: dict[str, str] | None = None
    ) -> list[dict[str, Any]] | None:
        self.list_calls += 1
        if self.error is not None:
            raise self.error
        if self.detailed is not None:
            return self.detailed
        return [{"id": m, "contextWindow": None} for m in (self.models or [])]

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
        on_delta=None,
        max_tokens: int | None = None,
    ):
        self.chat_calls.append((api_key, model, messages, max_tokens))
        if self.chat_error is not None:
            raise self.chat_error
        return None


def _mk_session_with_provider(tmp_path: Path, provider: str, adapter: _LoginStubAdapter, *, models_path: Path | None = None) -> AgentSession:
    """Create a session with the given provider pre-injected into session.providers."""
    auth = AuthStorage.in_memory()
    # Always use an isolated tmp_path models file — never fall back to None
    # which would resolve real user config and potentially write it.
    mp = models_path or (tmp_path / "models.json")
    registry = ModelRegistry.create(auth, str(mp))
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session_manager = SessionManager.in_memory(str(tmp_path))
    session = AgentSession(session_manager, settings, registry, _FakeLoader(str(tmp_path)), model, "medium")
    session.providers[provider] = adapter
    return session


@pytest.mark.asyncio
async def test_rpc_login_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """Known provider success: detailed entries with contextWindow registered/persisted; models.json verified; key stored; safe response schema."""
    models_path = tmp_path / "models.json"
    adapter = _LoginStubAdapter(
        detailed=[
            {"id": "nemotron-3-ultra", "contextWindow": 262_144},
            {"id": "glm-5.2", "contextWindow": None},
        ],
    )
    session = _mk_session_with_provider(tmp_path, "openai", adapter, models_path=models_path)

    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "login", "id": "1", "provider": "openai", "apiKey": "sk-test-key"})],
    )
    r = _resp(responses, "login", "1")
    assert r["success"] is True
    assert r["data"]["provider"] == "openai"
    assert r["data"]["status"]["configured"] is True
    # Safe response schema: no key/token leakage in parsed JSON
    all_json = json.dumps(responses)
    assert "apiKey" not in all_json
    assert "sk-test-key" not in all_json
    # Key stored
    assert session.model_registry._auth.get_api_key("openai") == "sk-test-key"
    # Models registered in-memory with context windows
    m1 = session.model_registry.find("openai", "nemotron-3-ultra")
    assert m1 is not None
    assert m1.context_window == 262_144
    m2 = session.model_registry.find("openai", "glm-5.2")
    assert m2 is not None
    assert m2.context_window is None
    # Chat probe used first detailed model id
    assert adapter.chat_calls[0][1] == "nemotron-3-ultra"
    # models.json persisted correctly
    persisted = json.loads(models_path.read_text(encoding="utf-8"))
    entries = {e["id"]: e for e in persisted["providers"]["openai"]}
    assert entries["nemotron-3-ultra"]["contextWindow"] == 262_144
    # contextWindow=None means key absent (persist_models skips falsy values)
    assert entries["glm-5.2"].get("contextWindow") is None


@pytest.mark.asyncio
async def test_rpc_login_401_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """Invalid 401: failure, no key/model persistence, models absent from registry and file."""
    models_path = tmp_path / "models.json"
    adapter = _LoginStubAdapter(
        models=["custom-fail-1"],
        chat_error=RuntimeError("openrouter API error 401: bad key"),
    )
    session = _mk_session_with_provider(tmp_path, "openrouter", adapter, models_path=models_path)

    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "login", "id": "1", "provider": "openrouter", "apiKey": "sk-bad"})],
    )
    r = _resp(responses, "login", "1")
    assert r["success"] is False
    assert "Authorization failed" in r["error"]
    # Nothing persisted
    assert session.model_registry._auth.get_api_key("openrouter") is None
    # Attempted models absent from in-memory registry
    assert session.model_registry.find("openrouter", "custom-fail-1") is None
    # models.json file absent (never written on failure)
    assert not models_path.exists()


@pytest.mark.asyncio
async def test_rpc_login_403_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """Invalid 403: failure, no key/model persistence, models absent from registry and file."""
    models_path = tmp_path / "models.json"
    adapter = _LoginStubAdapter(
        error=RuntimeError("openrouter API error 403: forbidden"),
    )
    session = _mk_session_with_provider(tmp_path, "openrouter", adapter, models_path=models_path)

    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "login", "id": "1", "provider": "openrouter", "apiKey": "sk-bad"})],
    )
    r = _resp(responses, "login", "1")
    assert r["success"] is False
    assert "Authorization failed" in r["error"]
    # Nothing persisted
    assert session.model_registry._auth.get_api_key("openrouter") is None
    # models.json file absent (never written on failure)
    assert not models_path.exists()


@pytest.mark.asyncio
async def test_rpc_login_validation_failure_after_model_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """Generic chat validation failure after model list: failure, no key/model persistence, models absent from registry and file."""
    models_path = tmp_path / "models.json"
    adapter = _LoginStubAdapter(
        models=["custom-fail-1"],
        chat_error=RuntimeError("openrouter API error 500: boom"),
    )
    session = _mk_session_with_provider(tmp_path, "openrouter", adapter, models_path=models_path)

    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "login", "id": "1", "provider": "openrouter", "apiKey": "sk-test"})],
    )
    r = _resp(responses, "login", "1")
    assert r["success"] is False
    assert "Validation failed" in r["error"]
    assert session.model_registry._auth.get_api_key("openrouter") is None
    # Attempted models absent from in-memory registry
    assert session.model_registry.find("openrouter", "custom-fail-1") is None
    # models.json file absent (never written on failure)
    assert not models_path.exists()


@pytest.mark.asyncio
async def test_rpc_login_unknown_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """Unknown provider: failure/no mutation."""
    session = _mk_session(tmp_path)
    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "login", "id": "1", "provider": "nonexistent", "apiKey": "sk-x"})],
    )
    r = _resp(responses, "login", "1")
    assert r["success"] is False
    assert "unknown provider" in r["error"].lower()


@pytest.mark.asyncio
async def test_rpc_login_missing_key_for_auth_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """Missing key for auth provider: failure."""
    session = _mk_session(tmp_path)
    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "login", "id": "1", "provider": "openai"})],
    )
    r = _resp(responses, "login", "1")
    assert r["success"] is False
    assert "apiKey is required" in r["error"]


@pytest.mark.asyncio
async def test_rpc_login_no_auth_provider_without_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """No-auth provider without apiKey: success, models fetched/registered/persisted, no stored apiKeys entry, models.json verified."""
    models_path = tmp_path / "models.json"
    adapter = _LoginStubAdapter(models=["local1", "local2"])
    session = _mk_session_with_provider(tmp_path, "llama.cpp", adapter, models_path=models_path)

    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "login", "id": "1", "provider": "llama.cpp"})],
    )
    r = _resp(responses, "login", "1")
    assert r["success"] is True
    assert r["data"]["provider"] == "llama.cpp"
    # No apiKeys entry stored
    assert session.model_registry._auth._data.get("apiKeys", {}).get("llama.cpp") is None
    # Models registered
    assert session.model_registry.find("llama.cpp", "local1") is not None
    assert session.model_registry.find("llama.cpp", "local2") is not None
    assert adapter.list_calls == 1
    # No chat calls (no validation for no-auth providers)
    assert adapter.chat_calls == []
    # models.json persisted correctly
    persisted = json.loads(models_path.read_text(encoding="utf-8"))
    entries = {e["id"]: e for e in persisted["providers"]["llama.cpp"]}
    assert "local1" in entries and "local2" in entries


@pytest.mark.asyncio
async def test_rpc_login_reflected_secret_in_adapter_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """Adapter exception containing the secret: parsed response JSON does not contain exact key/token; no double prefix."""
    secret = "sk-secret-token-xyz-123"
    adapter = _LoginStubAdapter(
        models=["m1"],
        chat_error=RuntimeError(f"openrouter API error 401: invalid key: {secret}"),
    )
    session = _mk_session_with_provider(tmp_path, "openrouter", adapter)
    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "login", "id": "1", "provider": "openrouter", "apiKey": secret})],
    )
    r = _resp(responses, "login", "1")
    assert r["success"] is False
    # Assert against parsed response JSON (meaningful — responses list contains raw JSON strings captured by _run_rpc)
    r_json = json.dumps(responses)
    assert secret not in r_json, f"secret leaked in RPC response: {r_json}"
    # Stable error message preserved (no double prefix)
    assert "Authorization failed" in r["error"]
    assert r["error"].count("Authorization failed") == 1


@pytest.mark.asyncio
async def test_rpc_login_no_auth_adapter_error_no_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """No-auth provider whose adapter raises during list_models: success but no models."""
    adapter = _LoginStubAdapter(error=RuntimeError("connection refused"))
    session = _mk_session_with_provider(tmp_path, "llama.cpp", adapter)
    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "login", "id": "1", "provider": "llama.cpp"})],
    )
    r = _resp(responses, "login", "1")
    assert r["success"] is True  # no-auth: fetch error is a warning, not failure
    # Status still shows configured (NO_AUTH is always "configured")
    assert r["data"]["status"]["configured"] is True


@pytest.mark.asyncio
async def test_rpc_login_400_probe_model_soft_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """400 on probe (e.g. reasoning_effort not supported): success with warning."""
    adapter = _LoginStubAdapter(
        models=["m1"],
        chat_error=RuntimeError("openrouter API error 400: reasoning_effort not supported"),
    )
    session = _mk_session_with_provider(tmp_path, "openrouter", adapter)
    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "login", "id": "1", "provider": "openrouter", "apiKey": "sk-ok"})],
    )
    r = _resp(responses, "login", "1")
    assert r["success"] is True
    assert r["data"]["provider"] == "openrouter"
    # Key is stored even on soft-pass
    assert session.model_registry._auth.get_api_key("openrouter") == "sk-ok"
    assert "warning" in r["data"]


@pytest.mark.asyncio
async def test_rpc_login_model_id_passed_to_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """Explicit modelId passed to validation probe."""
    adapter = _LoginStubAdapter(models=["m1"])
    session = _mk_session_with_provider(tmp_path, "openai", adapter)
    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "login", "id": "1", "provider": "openai", "apiKey": "sk-x", "modelId": "custom-model"})],
    )
    r = _resp(responses, "login", "1")
    assert r["success"] is True
    # Chat probe used the explicit model_id
    assert adapter.chat_calls[0][1] == "custom-model"


@pytest.mark.asyncio
async def test_rpc_login_preserves_pre_existing_auth_and_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """Unrelated pre-existing auth/models remain intact after login."""
    models_path = tmp_path / "models.json"
    models_path.write_text(
        json.dumps({"providers": {"anthropic": [{"id": "claude-3", "reasoning": True}]}}),
        encoding="utf-8",
    )
    auth = AuthStorage.in_memory()
    auth.set_stored_api_key("anthropic", "sk-anthropic-key")
    registry = ModelRegistry.create(auth, str(models_path))
    adapter = _LoginStubAdapter(models=["new1", "new2"])
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session_manager = SessionManager.in_memory(str(tmp_path))
    session = AgentSession(session_manager, settings, registry, _FakeLoader(str(tmp_path)), model, "medium")
    session.providers["openai"] = adapter
    session.providers["anthropic"] = _LoginStubAdapter()

    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "login", "id": "1", "provider": "openai", "apiKey": "sk-open"})],
    )
    r = _resp(responses, "login", "1")
    assert r["success"] is True
    # Anthropic auth preserved
    assert session.model_registry._auth.get_api_key("anthropic") == "sk-anthropic-key"
    # Anthropic model still there
    assert session.model_registry.find("anthropic", "claude-3") is not None


@pytest.mark.asyncio
async def test_rpc_invoke_skill_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """RPC invoke_skill returns trustWarning and skill metadata."""
    from one.core.agent_session import AgentSession
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.settings_manager import SettingsManager
    from one.resources.resource_loader import DefaultResourceLoader

    # Create a valid skill
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: A test\nallowed-tools:\n  - read\n---\n\nSkill body.\n"
    )

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=settings,
        additional_skill_paths=[str(tmp_path)],
    )
    await loader.reload()

    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth, str(tmp_path / "models.json"))
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    session_manager = SessionManager.in_memory(str(tmp_path))
    session = AgentSession(session_manager, settings, registry, loader, model, "medium")

    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "invoke_skill", "id": "s1", "name": "test-skill"})],
    )
    r = _resp(responses, "invoke_skill", "s1")
    assert r["success"] is True
    data = r["data"]
    assert data["name"] == "test-skill"
    assert "trustWarning" in data
    assert data["allowedTools"] == ["read"]


@pytest.mark.asyncio
async def test_rpc_invoke_skill_unknown_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """RPC invoke_skill for unknown skill returns structured error."""
    session = _mk_session(tmp_path)
    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "invoke_skill", "id": "s2", "name": "nonexistent"})],
    )
    r = _resp(responses, "invoke_skill", "s2")
    assert r["success"] is False
    assert "SkillError" in r["error"]
    assert "nonexistent" in r["error"]


@pytest.mark.asyncio
async def test_rpc_invoke_skill_missing_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """RPC invoke_skill without name returns error."""
    session = _mk_session(tmp_path)
    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "invoke_skill", "id": "s3"})],
    )
    r = _resp(responses, "invoke_skill", "s3")
    assert r["success"] is False
    assert "name is required" in r["error"]


@pytest.mark.asyncio
async def test_rpc_get_commands_has_descriptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """RPC get_commands returns commands with descriptions from prompts and skills."""
    from one.core.agent_session import AgentSession
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.settings_manager import SettingsManager
    from one.resources.resource_loader import DefaultResourceLoader

    # Create a prompt file with content that can be used as description
    prompt_file = tmp_path / "review.md"
    prompt_file.write_text("# Code Review\n\nReview code for best practices.")

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=settings,
        additional_prompt_template_paths=[str(tmp_path)],
    )
    await loader.reload()

    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth, str(tmp_path / "models.json"))
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    session_manager = SessionManager.in_memory(str(tmp_path))
    session = AgentSession(session_manager, settings, registry, loader, model, "medium")

    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "get_commands", "id": "c1"})],
    )
    r = _resp(responses, "get_commands", "c1")
    assert r["success"] is True
    commands = r["data"]["commands"]
    # Should have a command for the prompt with a non-empty description
    prompt_cmds = [c for c in commands if c.get("source") == "prompt"]
    assert len(prompt_cmds) > 0
    prompt_desc = prompt_cmds[0].get("description", "")
    assert prompt_desc != ""  # Description should be populated from prompt content


@pytest.mark.asyncio
async def test_rpc_invoke_skill_during_streaming(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """RPC invoke_skill while session is streaming returns BusySessionError."""
    from one.core.agent_session import AgentSession
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.settings_manager import SettingsManager
    from one.resources.resource_loader import DefaultResourceLoader

    # Create a valid skill
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: A test\n---\n\nSkill body.\n"
    )

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    loader = DefaultResourceLoader(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path),
        settings_manager=settings,
        additional_skill_paths=[str(tmp_path)],
    )
    await loader.reload()

    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth, str(tmp_path / "models.json"))
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    session_manager = SessionManager.in_memory(str(tmp_path))
    session = AgentSession(session_manager, settings, registry, loader, model, "medium")

    # Simulate an active streaming session.
    session._is_streaming = True

    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "invoke_skill", "id": "s1", "name": "test-skill"})],
    )
    r = _resp(responses, "invoke_skill", "s1")
    assert r["success"] is False
    assert "BusySessionError" in r["error"]
    # Ensure the body does NOT get silently queued — the caller must know.
    assert "busy" in r["error"].lower() or "streaming" in r["error"].lower() or "idle" in r["error"].lower()
