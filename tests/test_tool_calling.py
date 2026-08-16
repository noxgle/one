from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.core.types import ModelInfo


class _Loader:
    def get_system_prompt(self) -> str:
        return "You are a coding agent."


class _FakeProvider:
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


class _FailingProvider:
    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
    ) -> Any:
        raise RuntimeError("provider down")


class _SlowProvider:
    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
    ) -> Any:
        from one.providers.base import ChatResult

        await asyncio.sleep(0.1)
        return ChatResult(text="DONE", raw={}, usage={}, stop_reason="stop")


class _FlakyProvider:
    def __init__(self) -> None:
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

        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary provider issue")
        return ChatResult(text="RECOVERED", raw={}, usage={}, stop_reason="stop")


class _FailThenStableProvider:
    def __init__(self, fail_calls: int = 1, success_delay_sec: float = 0.0) -> None:
        self.fail_calls = fail_calls
        self.success_delay_sec = success_delay_sec
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

        self.calls += 1
        if self.calls <= self.fail_calls:
            raise RuntimeError("transient")
        if self.success_delay_sec > 0:
            await asyncio.sleep(self.success_delay_sec)
        return ChatResult(text="OK", raw={}, usage={}, stop_reason="stop")


@pytest.mark.asyncio
async def test_tool_calling_multistep_cycle(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["read", "ls"])
    agent.providers = {
        "openai": _FakeProvider(
            [
                '{"tool":"read","args":{"path":"a.txt"}}',
                "DONE",
            ]
        )
    }

    seen_turn_end: dict[str, Any] = {}

    def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "turn_end":
            seen_turn_end.update(event)

    agent.subscribe(on_event)
    await agent.prompt("Read file and finish.")

    assert agent.get_last_assistant_text() == "DONE"
    assert len(seen_turn_end.get("toolResults", [])) == 1
    payload = seen_turn_end["toolResults"][0]
    assert payload["ok"] is True
    assert payload["tool"] == "read"
    assert "hello" in payload["result"]


@pytest.mark.asyncio
async def test_tool_calling_step_limit_message(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 2, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["ls"])
    agent.providers = {"openai": _FakeProvider(['{"tool":"ls","args":{"path":"."}}'])}

    await agent.prompt("Loop tools forever")
    assert "limit kroków" in (agent.get_last_assistant_text() or "")


@pytest.mark.asyncio
async def test_tool_timeout_surfaces_error(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 2, "timeoutSec": 1}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["bash"])
    agent.providers = {
        "openai": _FakeProvider(
            [
                '{"tool":"bash","args":{"command":"sleep 1 && echo ok","timeout":0.01}}',
                "done",
            ]
        )
    }

    await agent.prompt("Run command")
    # Assistant finishes second step; tool result should carry timeout error.
    tool_results = [m for m in agent.messages if m.get("role") == "toolResult"]
    assert tool_results
    assert "timeout" in tool_results[0]["content"].lower()


@pytest.mark.asyncio
async def test_turn_end_emitted_on_non_retryable_error(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"retry": {"enabled": False}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium")
    agent.providers = {"openai": _FailingProvider()}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    await agent.prompt("fail now")

    turn_end = [e for e in events if e.get("type") == "turn_end"]
    assert turn_end
    assert turn_end[-1]["ok"] is False
    assert turn_end[-1]["reason"] == "error"
    assert "provider down" in turn_end[-1]["error"]

    assert any(e.get("type") == "agent_end" for e in events)


@pytest.mark.asyncio
async def test_queue_and_active_tools_introspection(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory()
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["ls", "read"])

    assert agent.active_tools == ["ls", "read"]

    await agent.steer("a")
    await agent.follow_up("b")
    queues = agent.get_pending_queues()
    assert queues["steering"] == ["a"]
    assert queues["followUp"] == ["b"]


@pytest.mark.asyncio
async def test_tool_error_payload_contains_contract_fields(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 2, "timeoutSec": 1}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["ls"])
    agent.providers = {"openai": _FakeProvider(['{"tool":"read","args":{"path":"x"}}', "done"])}

    await agent.prompt("trigger disabled tool")
    tool_msg = next(m for m in agent.messages if m.get("role") == "toolResult")
    payload = json.loads(tool_msg["content"])
    assert payload["ok"] is False
    assert payload["tool"] == "read"
    assert payload["errorType"] == "RuntimeError"
    assert "disabled" in payload["error"].lower()


@pytest.mark.asyncio
async def test_abort_stops_turn_with_abort_message(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"retry": {"enabled": False}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium")
    agent.providers = {"openai": _SlowProvider()}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    task = asyncio.create_task(agent.prompt("run and abort"))
    await asyncio.sleep(0.02)
    await agent.abort()
    await task

    assert agent.get_last_assistant_text() == "Request aborted."
    turn_end = [e for e in events if e.get("type") == "turn_end"]
    assert turn_end
    assert turn_end[-1]["aborted"] is True
    assert turn_end[-1]["reason"] == "abort"


@pytest.mark.asyncio
async def test_tool_result_message_payload_is_capped_for_context(tmp_path: Path):
    big_text = "x" * 20000
    (tmp_path / "big.txt").write_text(big_text, encoding="utf-8")

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 2, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["read"])
    agent.providers = {"openai": _FakeProvider(['{"tool":"read","args":{"path":"big.txt"}}', "done"])}

    await agent.prompt("read big")
    tool_msg = next(m for m in agent.messages if m.get("role") == "toolResult")
    payload = json.loads(tool_msg["content"])
    result = payload["result"]
    assert len(result) <= 13000
    assert "truncated to 12000 chars" in result


@pytest.mark.asyncio
async def test_tool_call_parsed_from_mixed_text_and_bom(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 3, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["read"])
    agent.providers = {
        "openai": _FakeProvider(
            [
                '\ufeffJasne, użyję narzędzia: {"tool":"read","args":{"path":"a.txt"}}',
                "DONE",
            ]
        )
    }

    await agent.prompt("go")
    assert agent.get_last_assistant_text() == "DONE"
    tool_results = [m for m in agent.messages if m.get("role") == "toolResult"]
    assert tool_results
    assert '"tool": "read"' in tool_results[0]["content"]


@pytest.mark.asyncio
async def test_tool_call_parses_string_args_for_bash(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 3, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["bash"])
    agent.providers = {
        "openai": _FakeProvider(
            [
                '{"tool":"bash","args":"pwd"}',
                "DONE",
            ]
        )
    }

    await agent.prompt("pokaż pliki")
    assert agent.get_last_assistant_text() == "DONE"
    tool_results = [m for m in agent.messages if m.get("role") == "toolResult"]
    assert tool_results
    payload = json.loads(tool_results[0]["content"])
    assert payload["tool"] == "bash"
    assert payload["args"]["command"] == "pwd"


@pytest.mark.asyncio
async def test_tool_call_parses_llama_cpp_style_write_payload(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 3, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["write"])
    malformed = (
        '{"tool":"write","args":{"file":"out.py","content":"def x():\\n'
        '    return 1\\n"}}<tool_call|><|tool_response>'
    ).replace("\\n", "\n")
    agent.providers = {"openai": _FakeProvider([malformed, "DONE"])}

    await agent.prompt("zapisz plik")
    assert agent.get_last_assistant_text() == "DONE"
    out_file = tmp_path / "out.py"
    assert out_file.exists()
    assert "def x():" in out_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_tool_call_parses_llama_cpp_write_payload_with_unescaped_quotes(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 3, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["write"])
    malformed = (
        '{"tool":"write","args":{"file":"out2.py","content":"def is_palindrome(s):\n'
        '    processed_s = "".join(filter(str.isalnum), s)).lower()\n'
        '    return processed_s == processed_s[::-1]\n"}}'
    )
    agent.providers = {"openai": _FakeProvider([malformed, "DONE"])}

    await agent.prompt("zapisz plik")
    assert agent.get_last_assistant_text() == "DONE"
    out_file = tmp_path / "out2.py"
    assert out_file.exists()
    written = out_file.read_text(encoding="utf-8")
    assert "def is_palindrome(s):" in written
    assert 'processed_s = "".join' in written


@pytest.mark.asyncio
async def test_tool_call_respects_raw_function_call_parser_order(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = ModelInfo(
        provider="openai",
        id="gpt-4.1",
        tool_parser=[{"type": "raw-function-call"}, {"type": "json"}],
    )

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 3, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["write"])
    raw = (
        'name: write\n'
        'arguments: {"path":"raw_order.py","content":"print(\\"ok\\")\\n"}'
    )
    agent.providers = {"openai": _FakeProvider([raw, "DONE"])}

    await agent.prompt("zapisz")
    assert agent.get_last_assistant_text() == "DONE"
    out_file = tmp_path / "raw_order.py"
    assert out_file.exists()
    assert 'print("ok")' in out_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_tool_call_parses_llama_cpp_write_payload_with_missing_outer_brace(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 3, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["write"])
    malformed = (
        '{"tool":"write","args":{"file":"p1.py","content":"def is_palindrome(s):\\n'
        '    processed_s = \\"\\".join(ch for ch in s if ch.isalnum()).lower()\\n'
        '    return processed_s == processed_s[::-1]\\n"}'
    )
    agent.providers = {"openai": _FakeProvider([malformed, "DONE"])}

    await agent.prompt("zapisz p1")
    assert agent.get_last_assistant_text() == "DONE"
    out_file = tmp_path / "p1.py"
    assert out_file.exists()
    written = out_file.read_text(encoding="utf-8")
    assert "def is_palindrome(s):" in written


@pytest.mark.asyncio
async def test_tool_call_parses_llama_cpp_tokenized_raw_call_format(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = ModelInfo(
        provider="openai",
        id="gpt-4.1",
        tool_parser=[{"type": "raw-function-call"}, {"type": "json"}],
    )

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 3, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["bash"])
    raw = '<|tool_call>call:bash{args:<|"|>echo TOK_OK > /tmp/one_tok.txt<|"|>}<tool_call|><|tool_response>'
    agent.providers = {"openai": _FakeProvider([raw, "DONE"])}

    await agent.prompt("wykonaj")
    assert agent.get_last_assistant_text() == "DONE"
    out_file = Path("/tmp/one_tok.txt")
    assert out_file.exists()
    assert out_file.read_text(encoding="utf-8").strip() == "TOK_OK"


@pytest.mark.asyncio
async def test_deferred_action_response_gets_tool_nudge(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["read"])
    provider = _FakeProvider(
        [
            "Sprawdzę to i zacznę od diagnostyki.",
            '{"tool":"read","args":{"path":"a.txt"}}',
            "DONE",
        ]
    )
    agent.providers = {"openai": provider}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    await agent.prompt("sprawdź")

    assert agent.get_last_assistant_text() == "DONE"
    tool_results = [m for m in agent.messages if m.get("role") == "toolResult"]
    assert tool_results
    assert any(e.get("type") == "tool_call_nudge_start" for e in events)
    assert any(e.get("type") == "tool_call_nudge_end" and e.get("used") is True for e in events)


@pytest.mark.asyncio
async def test_retry_then_success_emits_reason_completed(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"retry": {"enabled": True, "maxRetries": 2, "baseDelayMs": 1, "maxDelayMs": 1}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium")
    agent.providers = {"openai": _FlakyProvider()}

    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    await agent.prompt("retry once")

    assert agent.get_last_assistant_text() == "RECOVERED"
    turn_ends = [e for e in events if e.get("type") == "turn_end"]
    assert len(turn_ends) >= 2
    assert turn_ends[0]["ok"] is False
    assert turn_ends[-1]["ok"] is True
    assert turn_ends[-1]["reason"] in {"stop", "completed"}


@pytest.mark.asyncio
async def test_abort_does_not_drop_queued_messages(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"retry": {"enabled": False}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium")
    agent.providers = {"openai": _SlowProvider()}

    task = asyncio.create_task(agent.prompt("long task"))
    await asyncio.sleep(0.02)
    await agent.steer("next-a")
    await agent.follow_up("next-b")
    await agent.abort()
    await task

    queues = agent.get_pending_queues()
    assert queues["steering"] == ["next-a"]
    assert queues["followUp"] == ["next-b"]


@pytest.mark.asyncio
async def test_retry_success_then_queue_drains_once_without_duplication(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"retry": {"enabled": True, "maxRetries": 2, "baseDelayMs": 1, "maxDelayMs": 1}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium")
    provider = _FailThenStableProvider(fail_calls=1, success_delay_sec=0.03)
    agent.providers = {"openai": provider}

    task = asyncio.create_task(agent.prompt("start"))
    await asyncio.sleep(0.005)
    await agent.steer("s1")
    await agent.follow_up("f1")
    await task

    user_messages = [m for m in agent.messages if m.get("role") == "user"]
    contents = [m.get("content") for m in user_messages]
    assert contents.count("start") == 1
    assert contents.count("s1") == 1
    assert contents.count("f1") == 1
    assert agent.get_pending_queues() == {"steering": [], "followUp": []}


def test_finish_tool_registered_in_defaults() -> None:
    from one.tools.index import all_tools

    assert "finish" in all_tools
    # AgentSession defaults its active tools to all_tools keys when none are passed.
    assert "finish" in list(all_tools.keys())


def test_plan_in_default_tool_names() -> None:
    from one.tools.index import DEFAULT_TOOL_NAMES, all_tools

    assert "plan" in DEFAULT_TOOL_NAMES
    # Every name in DEFAULT_TOOL_NAMES must exist in all_tools.
    for name in DEFAULT_TOOL_NAMES:
        assert name in all_tools, f"{name!r} in DEFAULT_TOOL_NAMES but missing from all_tools"


@pytest.mark.asyncio
async def test_finish_tool_ends_turn_with_summary(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["read", "finish"])
    provider = _FakeProvider(['{"tool":"finish","args":{"summary":"All done.","goal_success":true}}'])
    agent.providers = {"openai": provider}

    seen_turn_end: dict[str, Any] = {}

    def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "turn_end":
            seen_turn_end.update(event)

    agent.subscribe(on_event)
    await agent.prompt("Finish the task.")

    # finish is terminal: no further provider calls, final message is the summary.
    assert provider.calls == 1
    assert agent.get_last_assistant_text() == "All done."
    last = agent.messages[-1]
    assert last.get("goalSuccess") is True
    assert last.get("stopReason") == "completed"
    assert seen_turn_end.get("ok") is True
    assert seen_turn_end.get("reason") == "completed"
    tool_results = seen_turn_end.get("toolResults", [])
    assert len(tool_results) == 1
    assert tool_results[0]["tool"] == "finish"
    assert tool_results[0]["ok"] is True


@pytest.mark.asyncio
async def test_finish_tool_flat_args_format(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["finish"])
    provider = _FakeProvider(['{"tool":"finish","summary":"Flat done.","goal_success":false}'])
    agent.providers = {"openai": provider}

    await agent.prompt("Finish.")
    assert provider.calls == 1
    assert agent.get_last_assistant_text() == "Flat done."
    assert agent.messages[-1].get("goalSuccess") is False
    assert agent.messages[-1].get("stopReason") == "completed"


@pytest.mark.asyncio
async def test_finish_tool_disabled_raises_and_continues(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    # finish is not in the active set here.
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["ls"])
    provider = _FakeProvider(['{"tool":"finish","args":{"summary":"nope","goal_success":true}}', "DONE"])
    agent.providers = {"openai": provider}

    await agent.prompt("Try finish.")

    assert agent.get_last_assistant_text() == "DONE"
    assert provider.calls == 2
    tool_results = [m for m in agent.messages if m.get("role") == "toolResult"]
    assert tool_results
    assert "disabled" in tool_results[0]["content"]


@pytest.mark.asyncio
async def test_bash_model_timeout_honored_above_settings(tmp_path: Path):
    """Model's explicit 'timeout' arg overrides the settings default — command must NOT be cancelled."""
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 2, "timeoutSec": 1}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["bash"])
    agent.providers = {
        "openai": _FakeProvider(
            [
                '{"tool":"bash","args":{"command":"sleep 2 && echo ok","timeout":3}}',
                "done",
            ]
        )
    }

    await agent.prompt("Run command")
    tool_results = [m for m in agent.messages if m.get("role") == "toolResult"]
    assert len(tool_results) == 1
    payload = json.loads(tool_results[0]["content"])
    assert payload["ok"] is True
    assert "ok" in payload.get("result", "")


@pytest.mark.asyncio
async def test_bash_no_timeout_uses_settings_default(tmp_path: Path):
    """When model omits 'timeout', the settings default applies — command is timed out."""
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None

    settings = SettingsManager.in_memory({"tools": {"maxSteps": 2, "timeoutSec": 1}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium", tools=["bash"])
    agent.providers = {
        "openai": _FakeProvider(
            [
                '{"tool":"bash","args":{"command":"sleep 3"}}',
                "done",
            ]
        )
    }

    await agent.prompt("Run command")
    tool_results = [m for m in agent.messages if m.get("role") == "toolResult"]
    assert tool_results
    content = tool_results[0]["content"].lower()
    assert "timed out" in content
    assert "(cancelled)" not in content
