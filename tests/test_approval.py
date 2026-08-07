from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager


class _Loader:
    def get_system_prompt(self) -> str:
        return "You are a coding agent."


class _RecordingProvider:
    """Fake provider that records every request's messages for assertions."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0
        self.requests: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
    ) -> Any:
        from one.providers.base import ChatResult

        self.requests.append(messages)
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ChatResult(text=self.responses[idx], raw={}, usage={}, stop_reason="stop")


def _make_agent(tmp_path: Path, responses: list[str], approval_callback=None) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 6, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(
        session,
        settings,
        registry,
        _Loader(),
        model,
        "medium",
        tools=["read", "bash", "write", "edit", "finish"],
        approval_callback=approval_callback,
    )
    agent.providers = {"openai": _RecordingProvider(responses)}
    return agent


async def _prompt(agent: AgentSession, text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def on_event(event: dict[str, Any]) -> None:
        events.append(event)

    agent.subscribe(on_event)
    await agent.prompt(text)
    return events


@pytest.mark.asyncio
async def test_approval_accept_runs_tool(tmp_path: Path):
    agent = _make_agent(
        tmp_path,
        [
            '{"tool":"bash","args":{"command":"echo accepted > %s"}}' % (tmp_path / "out.txt"),
            "DONE",
        ],
        approval_callback=lambda tool, args: (True, ""),
    )
    await _prompt(agent, "write a file")
    assert (tmp_path / "out.txt").read_text().strip() == "accepted"


@pytest.mark.asyncio
async def test_approval_off_no_callback_executes(tmp_path: Path):
    agent = _make_agent(
        tmp_path,
        [
            '{"tool":"bash","args":{"command":"echo default > %s"}}' % (tmp_path / "out.txt"),
            "DONE",
        ],
    )
    await _prompt(agent, "write a file")
    assert (tmp_path / "out.txt").read_text().strip() == "default"


@pytest.mark.asyncio
async def test_approval_reject_skips_execution_and_feeds_reason(tmp_path: Path):
    agent = _make_agent(
        tmp_path,
        [
            # First the model wants to write a file...
            '{"tool":"write","args":{"path":"secret.txt","content":"x"}}',
            # ...then, after the rejection reason, it finishes instead.
            '{"tool":"finish","args":{"summary":"ok, not touching that","goal_success":true}}',
        ],
        approval_callback=lambda tool, args: (False, "don't touch that file"),
    )

    events = await _prompt(agent, "create secret.txt and report")

    # The tool was NOT executed: the file must not exist.
    assert not (tmp_path / "secret.txt").exists()

    # The rejection reason reached the model (second provider request context).
    provider = agent.providers["openai"]
    assert provider.calls == 2
    ctx = provider.requests[-1]
    ctx_text = "".join(
        str(m.get("content", "")) for m in ctx if isinstance(m.get("content"), str)
    ) + "".join(
        m.get("content", "")
        for m in ctx
        if isinstance(m.get("content"), str) or (isinstance(m.get("content"), list))
    )
    assert "don't touch that file" in ctx_text

    # Event contract: approval rejection is surfaced, turn ends cleanly.
    assert any(e["type"] == "tool_approval_rejected" for e in events)
    assert any(e["type"] == "agent_end" for e in events)
    assert agent.get_last_assistant_text() == "ok, not touching that"


@pytest.mark.asyncio
async def test_approval_gates_only_mutating_tools(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hi\n", encoding="utf-8")
    calls: list[str] = []

    async def callback(tool: str, args: dict[str, Any]) -> tuple[bool, str]:
        calls.append(tool)
        return True, ""

    agent = _make_agent(
        tmp_path,
        [
            '{"tool":"read","args":{"path":"a.txt"}}',
            '{"tool":"bash","args":{"command":"true"}}',
            "DONE",
        ],
        approval_callback=callback,
    )
    await _prompt(agent, "read then run a command")
    # read-only tools bypass approval; only bash/write/edit are gated.
    assert calls == ["bash"]


@pytest.mark.asyncio
async def test_approval_finish_never_gated(tmp_path: Path):
    calls: list[str] = []

    async def callback(tool: str, args: dict[str, Any]) -> tuple[bool, str]:
        calls.append(tool)
        return True, ""

    agent = _make_agent(
        tmp_path,
        ['{"tool":"finish","args":{"summary":"done","goal_success":true}}'],
        approval_callback=callback,
    )
    await _prompt(agent, "just finish")
    assert calls == []


def test_settings_approval_defaults():
    settings = SettingsManager.in_memory()
    assert settings.get_tool_approval() is False
    assert settings.get_tool_approval_tools() == ["bash", "write", "edit"]
    on = SettingsManager.in_memory({"tools": {"approval": True, "approvalTools": ["bash"]}})
    assert on.get_tool_approval() is True
    assert on.get_tool_approval_tools() == ["bash"]
