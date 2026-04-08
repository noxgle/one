from __future__ import annotations

from pathlib import Path

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager


class _Loader:
    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
        return "You are a coding agent."


def _mk_agent(tmp_path: Path) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory()
    session = SessionManager.in_memory(str(tmp_path))
    return AgentSession(session, settings, registry, _Loader(), model, "medium")


def test_extension_ui_request_response_lifecycle(tmp_path: Path) -> None:
    agent = _mk_agent(tmp_path)
    req = agent.request_extension_ui("demo-ext", "widget", {"foo": 1})
    assert req["extension"] == "demo-ext"
    assert req["uiType"] == "widget"
    state = agent.get_extension_ui_state()
    assert len(state["pending"]) == 1

    resp = agent.respond_extension_ui(req["id"], {"ok": True})
    assert resp["requestId"] == req["id"]
    assert resp["cancelled"] is False
    state2 = agent.get_extension_ui_state()
    assert state2["pending"] == []
    assert len(state2["history"]) == 1


def test_extension_ui_rejects_invalid_type(tmp_path: Path) -> None:
    agent = _mk_agent(tmp_path)
    try:
        agent.request_extension_ui("demo-ext", "panel", {})
    except ValueError as e:
        assert "uiType must be one of: widget, overlay" in str(e)
    else:
        raise AssertionError("expected ValueError")
