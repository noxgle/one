from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import json

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.modes.run_mode import run_run_mode


class _Loader:
    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
        return "You are a coding agent."


class _Provider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
    ) -> Any:
        from one.providers.base import ChatResult

        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ChatResult(text=self.responses[idx], raw={}, usage={}, stop_reason="stop")


class _MessageCapturingProvider:
    """Provider that captures the messages it receives (for resume testing)."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.captured_messages: list[list[dict[str, Any]]] = []
        self.calls = 0

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
    ) -> Any:
        from one.providers.base import ChatResult

        self.captured_messages.append(list(messages))
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ChatResult(text=self.responses[idx], raw={}, usage={}, stop_reason="stop")


class _MockHost:
    """Minimal host-like object with a .session property for run_mode tests."""

    def __init__(self, session: AgentSession) -> None:
        self.session = session


class _SteerableSession:
    def __init__(self) -> None:
        self.steered: list[str] = []
        self._steer_seen = asyncio.Event()

    def subscribe(self, fn) -> None:  # noqa: ARG002
        pass

    def answer_question(self, qid: str, answer: str) -> None:  # noqa: ARG002
        pass

    async def steer(self, text: str) -> None:
        self.steered.append(text)
        self._steer_seen.set()

    async def prompt(self, task: str) -> None:  # noqa: ARG002
        await asyncio.wait_for(self._steer_seen.wait(), timeout=10)

    def get_last_finish_result(self) -> dict:
        return {"summary": "done", "goalSuccess": True, "finished": True}

    def get_last_assistant_text(self) -> str:
        return "done"


def _mk_host(
    tmp_path: Path,
    responses: list[str],
    tools: list[str] | None = None,
) -> _MockHost:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=tools)
    agent.providers = {"openai": _Provider(responses)}
    return _MockHost(agent)


@pytest.mark.asyncio
async def test_run_mode_finish_success_returns_zero(tmp_path: Path, capsys):
    host = _mk_host(tmp_path, ['{"tool":"finish","args":{"summary":"done","goal_success":true}}', "DONE"])
    code = await run_run_mode(host, {"task": "go"})
    assert code == 0
    captured = capsys.readouterr()
    assert "done" in captured.out


@pytest.mark.asyncio
async def test_run_mode_finish_failure_returns_one(tmp_path: Path, capsys):
    host = _mk_host(tmp_path, ['{"tool":"finish","args":{"summary":"failed task","goal_success":false}}', "DONE"])
    code = await run_run_mode(host, {"task": "go"})
    assert code == 1
    captured = capsys.readouterr()
    assert "failed task" in captured.out


@pytest.mark.asyncio
async def test_run_mode_no_finish_returns_one(tmp_path: Path, capsys):
    host = _mk_host(tmp_path, ["DONE"])
    code = await run_run_mode(host, {"task": "go"})
    assert code == 1
    captured = capsys.readouterr()
    assert "DONE" in captured.out


@pytest.mark.asyncio
async def test_run_mode_json_output(tmp_path: Path, capsys):
    host = _mk_host(tmp_path, ['{"tool":"finish","args":{"summary":"done","goal_success":true}}', "DONE"])
    code = await run_run_mode(host, {"task": "go", "json": True})
    assert code == 0
    captured = capsys.readouterr()
    import json

    data = json.loads(captured.out)
    assert data["goalSuccess"] is True
    assert data["finished"] is True
    assert data["summary"] == "done"


@pytest.mark.asyncio
async def test_run_mode_resume_reruns_last_user_message(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium")
    agent.providers = {"openai": _MessageCapturingProvider(
        ['{"tool":"finish","args":{"summary":"replayed","goal_success":true}}', "DONE"])}
    host = _MockHost(agent)

    # Pre-populate a user message into the session context.
    agent.messages.append({"role": "user", "content": "original task"})

    code = await run_run_mode(host, {"task": "", "resume": True})
    assert code == 0
    # Verify the provider received the original task in the messages.
    captured = host.session.providers["openai"].captured_messages
    assert len(captured) >= 1
    last_msgs = captured[-1]
    last_content = " ".join(m.get("content", "") for m in last_msgs)
    assert "original task" in last_content


@pytest.mark.asyncio
async def test_run_mode_empty_task_usage_error(tmp_path: Path, capsys):
    host = _mk_host(tmp_path, ["DONE"])
    code = await run_run_mode(host, {"task": ""})
    assert code == 2
    captured = capsys.readouterr()
    assert "Usage: one run" in captured.out


def test_get_last_finish_result_helpers(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium")

    # No finish message yet.
    result = agent.get_last_finish_result()
    assert result == {"finished": False, "goalSuccess": False, "summary": ""}

    # Manually append a finish assistant message.
    agent.messages.append({
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "goalSuccess": True,
    })
    result = agent.get_last_finish_result()
    assert result["finished"] is True
    assert result["goalSuccess"] is True
    assert result["summary"] == "ok"

    # Test get_last_user_text.
    agent.messages.append({"role": "user", "content": "hello world"})
    assert agent.get_last_user_text() == "hello world"
    agent.messages.append({"role": "user", "content": "  "})  # empty-ish
    assert agent.get_last_user_text() == "hello world"


@pytest.mark.asyncio
async def test_run_mode_answer_file(tmp_path: Path):
    """ask_user in headless mode: question written to the answer file, answer read back."""
    host = _mk_host(
        tmp_path,
        [
            '{"tool":"ask_user","args":{"question":"which dir?"}}',
            '{"tool":"finish","args":{"summary":"answered","goal_success":true}}',
            "DONE",
        ],
    )
    answer_file = tmp_path / "answers.txt"
    task = asyncio.create_task(run_run_mode(host, {"task": "go", "answer_file": str(answer_file)}))
    # Wait until the question appears in the file, then write the answer.
    for _ in range(200):
        if answer_file.exists() and "which dir?" in answer_file.read_text(encoding="utf-8"):
            break
        await asyncio.sleep(0.05)
    answer_file.write_text("/tmp", encoding="utf-8")
    code = await asyncio.wait_for(task, timeout=10)
    assert code == 0
    # The answer reached the model context.
    tool_results = [m for m in host.session.messages if m.get("role") == "toolResult"]
    assert any("which dir?" in m.get("content", "") and "/tmp" in m.get("content", "") for m in tool_results)


@pytest.mark.asyncio
async def test_run_mode_no_answer_file_canned(tmp_path: Path, capsys):
    """Without --answer-file, ask_user gets a deterministic canned answer (no hang)."""
    host = _mk_host(
        tmp_path,
        [
            '{"tool":"ask_user","args":{"question":"q?"}}',
            '{"tool":"finish","args":{"summary":"done","goal_success":true}}',
            "DONE",
        ],
    )
    code = await run_run_mode(host, {"task": "go"})
    assert code == 0
    tool_results = [m for m in host.session.messages if m.get("role") == "toolResult"]
    assert any("No answer channel" in m.get("content", "") for m in tool_results)


@pytest.mark.asyncio
async def test_run_mode_steer_file_injects_message(tmp_path: Path):
    steer_file = tmp_path / "steer.txt"
    steer_file.write_text("", encoding="utf-8")
    session = _SteerableSession()
    host = _MockHost(session)
    run_task = asyncio.create_task(run_run_mode(host, {"task": "go", "steer_file": str(steer_file)}))
    await asyncio.sleep(0.2)
    steer_file.write_text("please use python", encoding="utf-8")
    code = await asyncio.wait_for(run_task, timeout=15)
    assert code == 0
    assert session.steered == ["please use python"]
    assert steer_file.read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_run_mode_report_written(tmp_path: Path, capsys):
    host = _mk_host(tmp_path, ['{"tool":"finish","args":{"summary":"done","goal_success":true}}', "DONE"])
    agent_dir = tmp_path / "agentdir"
    code = await run_run_mode(host, {"task": "go", "agentDir": str(agent_dir)})
    assert code == 0
    report = agent_dir / "reports.jsonl"
    assert report.exists()
    lines = report.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["goalSuccess"] is True
    assert entry["finished"] is True
    assert entry["summary"] == "done"
    assert entry["task"] == "go"
    assert entry["exitCode"] == 0
    out = capsys.readouterr().out
    assert f"[report] {report}" in out


@pytest.mark.asyncio
async def test_run_mode_json_report_no_extra_output(tmp_path: Path, capsys):
    host = _mk_host(tmp_path, ['{"tool":"finish","args":{"summary":"done","goal_success":true}}', "DONE"])
    agent_dir = tmp_path / "agentdir"
    code = await run_run_mode(host, {"task": "go", "json": True, "agentDir": str(agent_dir)})
    assert code == 0
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["goalSuccess"] is True
