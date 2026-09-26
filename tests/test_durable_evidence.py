from __future__ import annotations

import json
from pathlib import Path

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager


class _Loader:
    def get_system_prompt(self, selected_tools=None):  # type: ignore[no-untyped-def]
        return "test"


class _Mcp:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def tools(self):  # type: ignore[no-untyped-def]
        return []

    def has_tool(self, name: str) -> bool:
        return name == "mcp_lookup"

    async def call_tool(self, name: str, args: dict[str, object], timeout=None):  # type: ignore[no-untyped-def]
        if self.fail:
            raise RuntimeError("MCP raw failure")
        return {"ok": True, "output": "MCP complete result " + "x" * 100, "content": [{"type": "text", "text": "MCP complete result " + "x" * 100}]}


def _agent(manager: SessionManager, tmp_path: Path) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "test")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    return AgentSession(manager, SettingsManager.in_memory(), registry, _Loader(), model, "off", tools=["read", "write", "evidence_read"])


@pytest.mark.asyncio
async def test_large_unicode_evidence_survives_reload_and_is_chunked(tmp_path: Path) -> None:
    source = tmp_path / "large.txt"
    source.write_text("ą" * 13_000, encoding="utf-8")
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    agent = _agent(manager, tmp_path)
    result = await agent._run_tool_call("read", {"path": str(source)})
    message = json.loads(agent.messages[-1]["content"])
    evidence_id = message["evidenceId"]
    assert "truncated" in message["result"]
    assert len(message["result"]) > 12_000  # existing head/tail contract remains

    reloaded = _agent(SessionManager.open(manager.session_file or ""), tmp_path)
    first = await reloaded._run_tool_call("evidence_read", {"evidenceId": evidence_id, "maxChars": 100})
    assert first["ok"] is True
    first_payload = json.loads(reloaded.messages[-1]["content"])
    next_offset = first_payload["details"]["nextOffset"]
    assert next_offset == 100
    rest = await reloaded._run_tool_call("evidence_read", {"evidenceId": evidence_id, "offset": next_offset, "maxChars": 8000})
    assert rest["ok"] is True
    # Retrieval reads durable data only; it did not invoke/read the source again.
    assert result["evidenceId"] == evidence_id


@pytest.mark.asyncio
async def test_write_content_is_omitted_from_context_but_retained_in_evidence(tmp_path: Path) -> None:
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    agent = _agent(manager, tmp_path)
    expected = '\ufeff<p>Żółć &amp; <tool_call|> TOOL_CALL: “quoted”</p>'

    result = await agent._run_tool_call("write", {"path": "page.html", "content": expected})

    message = json.loads(agent.messages[-1]["content"])
    assert message["args"]["content"] == "[omitted from model context]"
    assert expected not in agent._flatten_messages_for_provider()[-1]["content"]
    evidence = manager.read_evidence(result["evidenceId"])
    assert evidence is not None
    assert evidence["args"]["content"] == expected


def test_parsed_write_call_source_is_omitted_before_next_provider_request(tmp_path: Path) -> None:
    agent = _agent(SessionManager.create(str(tmp_path), str(tmp_path / "sessions")), tmp_path)
    source = "<tool_call|> private &amp; 😀"
    assistant = {"role": "assistant", "content": [{"type": "text", "text": source}]}
    agent.messages.append(assistant)
    agent._omit_write_call_from_assistant_context(assistant, {"tool": "write", "args": {"path": "x", "content": source}})
    visible = agent._flatten_messages_for_provider()[-1]["content"]
    assert source not in visible
    assert "writeContentOmitted" in visible


@pytest.mark.asyncio
async def test_evidence_rejects_other_session_and_bad_ranges(tmp_path: Path) -> None:
    one = SessionManager.create(str(tmp_path), str(tmp_path / "one"))
    evidence_id, error = one.append_evidence({"id": "owned", "tool": "test", "ok": True, "args": {}, "rawResult": {"x": "y"}})
    assert error is None and evidence_id == "owned"
    other = _agent(SessionManager.create(str(tmp_path), str(tmp_path / "two")), tmp_path)
    assert (await other._run_tool_call("evidence_read", {"evidenceId": "owned"}))["ok"] is False
    owner = _agent(one, tmp_path)
    assert (await owner._run_tool_call("evidence_read", {"evidenceId": "owned", "offset": -1}))["ok"] is False


def test_evidence_malformed_and_size_failures_are_nonfatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    path = Path(manager.session_file or "").with_suffix(".jsonl.evidence")
    path.write_text("not json\n", encoding="utf-8")
    assert manager.read_evidence("anything") is None
    monkeypatch.setattr("one.core.session_manager.EVIDENCE_MAX_RECORD_BYTES", 10)
    evidence_id, error = manager.append_evidence({"id": "too-big", "rawResult": {"text": "x" * 100}})
    assert evidence_id is None
    assert error and "record limit" in error


@pytest.mark.asyncio
async def test_mcp_success_and_error_are_durable_and_write_failure_is_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    agent = _agent(manager, tmp_path)
    agent._mcp_manager = _Mcp()
    agent._active_tools.append("mcp_lookup")
    success = await agent._run_tool_call("mcp_lookup", {"token": "private"})
    assert success["evidenceId"]
    stored = manager.read_evidence(success["evidenceId"])
    assert stored and stored["args"]["token"] == "[REDACTED]"
    assert "MCP complete result" in json.dumps(stored["rawResult"])

    agent._mcp_manager = _Mcp(fail=True)
    failure = await agent._run_tool_call("mcp_lookup", {})
    assert failure["ok"] is False and failure["evidenceId"]
    assert "MCP raw failure" in json.dumps(manager.read_evidence(failure["evidenceId"]))

    monkeypatch.setattr(manager, "append_evidence", lambda evidence: (None, "disk full"))
    source = tmp_path / "small.txt"
    source.write_text("ok", encoding="utf-8")
    await agent._run_tool_call("read", {"path": str(source)})
    assert json.loads(agent.messages[-1]["content"])["evidenceUnavailable"] == "disk full"


def test_evidence_redacts_sensitive_status_and_hides_sidecar_metadata(tmp_path: Path) -> None:
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    agent = _agent(manager, tmp_path)

    # Tool errors may carry structured diagnostic data; status must receive the
    # same recursive credential redaction as arguments and raw results.
    evidence_id, error = agent._store_tool_evidence({
        "tool": "test",
        "ok": False,
        "args": {},
        "error": {"authorization": "Bearer secret-value"},
    })
    assert error is None and evidence_id is not None
    stored = manager.read_evidence(evidence_id)
    assert stored is not None
    assert stored["status"]["error"] == {"authorization": "[REDACTED]"}

    retrieved = agent._read_evidence({"evidenceId": evidence_id})
    evidence = json.loads(retrieved["output"])
    assert "sessionId" not in evidence
    assert "type" not in evidence
    assert "version" not in evidence


def test_first_evidence_write_is_atomic_then_later_records_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    calls: list[str] = []
    from one.core.persistence import append_private_text as real_append
    from one.core.persistence import atomic_write_text as real_atomic

    def atomic(path: Path, text: str) -> None:
        calls.append("atomic")
        real_atomic(path, text)

    def append(path: Path, text: str) -> None:
        calls.append("append")
        real_append(path, text)

    monkeypatch.setattr("one.core.session_manager.atomic_write_text", atomic)
    monkeypatch.setattr("one.core.session_manager.append_private_text", append)
    assert manager.append_evidence({"id": "first"}) == ("first", None)
    assert manager.append_evidence({"id": "second"}) == ("second", None)
    assert calls == ["atomic", "append"]
