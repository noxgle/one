from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.core.types import ModelInfo
from one.providers.base import ChatResult
from one.providers.codex_responses import CodexResponsesAdapter
from one.providers.gemini import GeminiAdapter
from tests.support.agents import _FakeProvider, _Loader


class _RecordingCodex(CodexResponsesAdapter):
    """Fake transport retaining the exact native payload passed between turns."""

    def __init__(self, responses: list[ChatResult]) -> None:
        self.responses = responses
        self.payloads: list[dict[str, Any]] = []

    async def chat(
        self, api_key: str, model: str, messages: list[dict[str, Any]], thinking_level: str,
        headers: dict[str, str] | None = None, on_delta: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None, max_tokens: int | None = None,
        images: list[dict[str, Any]] | None = None, storage_dir: str = "",
        tools: list[dict[str, Any]] | None = None,
        stream_transport_timeout: float | None = None,
    ) -> ChatResult:
        self.payloads.append(self._build_payload(model, messages, thinking_level, True, tools=tools))
        return self.responses.pop(0)


class _RecordingNative:
    supports_native_tools = True

    def __init__(self, responses: list[ChatResult]) -> None:
        self.responses = responses
        self.requests: list[list[dict[str, Any]]] = []

    async def chat(self, api_key: str, model: str, messages: list[dict[str, Any]], thinking_level: str, **kwargs: Any) -> ChatResult:
        self.requests.append(messages)
        return self.responses.pop(0)


def _chatgpt_agent(
    session: SessionManager, tmp_path: Path, tools: list[str] | None = None, **kwargs: Any,
) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("chatgpt", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("chatgpt", "gpt-5.6-sol")
    assert model is not None
    return AgentSession(
        session, SettingsManager.in_memory({"tools": {"maxSteps": 3}}), registry,
        _Loader(), model, "medium", tools=tools or ["read", "bash"], **kwargs,
    )


def _native_read() -> ChatResult:
    return ChatResult(
        text="", raw={}, usage={}, had_native_tool_call=True,
        native_tool_calls=[{"id": "call_native_read", "name": "read", "arguments": {"path": "a.txt"}}],
    )


def test_compaction_keeps_both_sides_of_retained_native_replay_pair() -> None:
    messages = [
        {"role": "user", "content": "read"},
        {"role": "assistant", "content": "", "_nativeToolCalls": [
            {"id": "call_compact", "name": "read", "arguments": {"path": "a.txt"}},
        ]},
        {"role": "toolResult", "content": "result", "_nativeToolCallId": "call_compact"},
    ]

    assert AgentSession._native_replay_keep_start(messages, 2) == 1


@pytest.mark.asyncio
async def test_codex_native_tool_replay_survives_flattening_and_session_reload(tmp_path: Path):
    (tmp_path / "a.txt").write_text("safe contents\n", encoding="utf-8")
    session = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    agent = _chatgpt_agent(session, tmp_path)
    provider = _RecordingCodex([_native_read(), ChatResult(text="done", raw={}, usage={})])
    agent.providers = {"chatgpt": provider}

    await agent.prompt("read a.txt")

    replay = provider.payloads[1]["input"]
    assert [item["type"] for item in replay if item["type"] != "message"] == [
        "function_call", "function_call_output"
    ]
    assert replay[-2]["call_id"] == replay[-1]["call_id"] == "call_native_read"
    assert replay[-1]["output"].startswith("<untrusted-tool-output>")
    assert not any(
        item["type"] == "message" and "safe contents" in str(item["content"])
        for item in replay
    )

    path = session.session_file
    assert path is not None
    reloaded = _chatgpt_agent(SessionManager.open(path), tmp_path)
    replay_provider = _RecordingCodex([ChatResult(text="after reload", raw={}, usage={})])
    reloaded.providers = {"chatgpt": replay_provider}
    await reloaded.prompt("continue")
    restored_replay = replay_provider.payloads[0]["input"]
    assert [item["type"] for item in restored_replay if item["type"] != "message"] == [
        "function_call", "function_call_output"
    ]


@pytest.mark.asyncio
async def test_codex_native_rejected_tool_replays_matching_output(tmp_path: Path):
    async def reject(_: str, __: dict[str, Any]) -> tuple[bool, str]:
        return False, "not now"

    agent = _chatgpt_agent(SessionManager.in_memory(str(tmp_path)), tmp_path, approval_callback=reject)
    provider = _RecordingCodex([
        ChatResult(
            text="", raw={}, usage={}, had_native_tool_call=True,
            native_tool_calls=[{"id": "call_rejected", "name": "bash", "arguments": {"command": "true"}}],
        ),
        ChatResult(text="done", raw={}, usage={}),
    ])
    agent.providers = {"chatgpt": provider}

    await agent.prompt("run safely")

    replay = provider.payloads[1]["input"]
    assert [(item["type"], item.get("call_id")) for item in replay if item["type"] != "message"] == [
        ("function_call", "call_rejected"),
        ("function_call_output", "call_rejected"),
    ]
    assert "User rejected the command: not now" in replay[-1]["output"]


@pytest.mark.asyncio
async def test_native_write_failure_then_correction_replays_original_arguments(tmp_path: Path) -> None:
    agent = _chatgpt_agent(SessionManager.in_memory(str(tmp_path)), tmp_path, tools=["write"])
    provider = _RecordingCodex([
        ChatResult(text="", raw={}, usage={}, had_native_tool_call=True, native_tool_calls=[
            {"id": "call_bad_write", "name": "write", "arguments": {"path": "page.txt"}},
        ]),
        ChatResult(text="", raw={}, usage={}, had_native_tool_call=True, native_tool_calls=[
            {"id": "call_good_write", "name": "write", "arguments": {"path": "page.txt", "content": "fixed"}},
        ]),
        ChatResult(text="done", raw={}, usage={}),
    ])
    agent.providers = {"chatgpt": provider}

    await agent.prompt("write safely")

    assert (tmp_path / "page.txt").read_text(encoding="utf-8") == "fixed"
    first_replay = provider.payloads[1]["input"]
    assert [(item["type"], item.get("call_id")) for item in first_replay if item["type"] != "message"] == [
        ("function_call", "call_bad_write"), ("function_call_output", "call_bad_write"),
    ]
    assert "requires explicit string" in first_replay[-1]["output"]
    second_replay = provider.payloads[2]["input"]
    calls = [item for item in second_replay if item["type"] == "function_call"]
    assert json.loads(calls[0]["arguments"]) == {"path": "page.txt"}
    assert json.loads(calls[1]["arguments"]) == {"path": "page.txt", "content": "fixed"}
    assert not any("Historical write call" in str(item) for item in second_replay)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name,model_id", [
    ("openai", "gpt-4.1"), ("anthropic", "claude-3-7-sonnet-latest"), ("gemini", "gemini-2.5-pro"),
])
async def test_native_adapters_preserve_and_persist_call_result_pairs(
    tmp_path: Path, provider_name: str, model_id: str,
) -> None:
    (tmp_path / "a.txt").write_text("safe\n", encoding="utf-8")
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key(provider_name, "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find(provider_name, model_id)
    assert model is not None
    session = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    agent = AgentSession(session, SettingsManager.in_memory({"tools": {"maxSteps": 2}}), registry, _Loader(), model, "medium", tools=["read"])
    provider = _RecordingNative([
        ChatResult(text="", raw={}, usage={}, native_tool_calls=[{"id": "call_pair", "name": "read", "arguments": {"path": "a.txt"}}]),
        ChatResult(text="done", raw={}, usage={}),
    ])
    agent.providers = {provider_name: provider}
    await agent.prompt("read")
    replay = provider.requests[1]
    assistant = next(message for message in replay if message.get("_nativeToolCalls"))
    result = next(message for message in replay if message.get("_nativeToolCallId") == "call_pair")
    assert assistant["_nativeToolCalls"][0]["id"] == result["_nativeToolCallId"] == "call_pair"
    path = session.session_file
    assert path is not None
    restored = AgentSession(SessionManager.open(path), SettingsManager.in_memory({"tools": {"maxSteps": 2}}), registry, _Loader(), model, "medium", tools=["read"])
    restored_provider = _RecordingNative([ChatResult(text="after reload", raw={}, usage={})])
    restored.providers = {provider_name: restored_provider}
    await restored.prompt("continue")
    assert any(message.get("_nativeToolCallId") == "call_pair" for message in restored_provider.requests[0])


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

    events: list[dict[str, object]] = []
    agent.subscribe(events.append)
    await agent.prompt("go")
    assert agent.get_last_assistant_text() == "DONE"
    tool_results = [m for m in agent.messages if m.get("role") == "toolResult"]
    assert tool_results
    assert '"tool": "read"' in tool_results[0]["content"]
    start = next(e for e in events if e["type"] == "tool_call_start")
    end = next(e for e in events if e["type"] == "tool_call_end")
    assert start["toolCallId"] == end["toolCallId"]
    assert str(start["toolCallId"]).startswith("runtime-")


def test_tool_parser_preserves_native_and_function_metadata_ids(tmp_path: Path):
    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    agent = AgentSession(SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory(), registry, _Loader(), model, "medium")
    direct = agent._try_parse_tool_call('{"id":"call-direct","tool":"read","args":{"path":"x"}}')
    nested = agent._try_parse_tool_call('{"id":"call-function","function":{"name":"read","arguments":"{\\"path\\":\\"x\\"}"}}')
    assert direct and direct["toolCallId"] == "call-direct"
    assert nested and nested["toolCallId"] == "call-function"


@pytest.mark.parametrize(
    "text",
    [
        '{"tool":"bash"}',
        '{"tool":"bash","args":{}}',
        '{"function":{"name":"bash","arguments":"{}"}}',
        '{"tool":"bash","args":{"command":""}}',
        '{"tool":"bash","args":{"command":"  \t\n"}}',
        '{"tool":"bash","args":{"command":42}}',
    ],
)
def test_tool_parser_rejects_bash_without_a_nonempty_command(tmp_path: Path, text: str):
    registry = ModelRegistry.create(AuthStorage.in_memory())
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    agent = AgentSession(
        SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory(), registry, _Loader(), model, "medium"
    )

    assert agent._try_parse_tool_call(text) is None


def test_tool_parser_preserves_valid_bash_command(tmp_path: Path):
    registry = ModelRegistry.create(AuthStorage.in_memory())
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    agent = AgentSession(
        SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory(), registry, _Loader(), model, "medium"
    )

    assert agent._try_parse_tool_call('{"tool":"bash","args":{"command":" echo ok "}}') == {
        "tool": "bash",
        "args": {"command": " echo ok "},
        "toolCallId": None,
    }


def test_valid_write_json_is_parsed_losslessly_before_compatibility_repair(tmp_path: Path):
    registry = ModelRegistry.create(AuthStorage.in_memory())
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    agent = AgentSession(
        SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory(), registry, _Loader(), model, "medium"
    )
    expected = '\ufeff<!doctype html><p title="“smart”">Żółć &amp; \u003c literal</p> <tool_call|> TOOL_CALL: <|tool_response>'
    wire = json.dumps({"tool": "write", "args": {"path": "page.html", "content": expected}}, ensure_ascii=False)
    # JSON's escaped less-than sequence must decode, while all literal content
    # remains byte-for-character unchanged after parsing.
    wire = wire.replace("<", "\\u003c")

    parsed = agent._try_parse_tool_call(wire)

    assert parsed == {"tool": "write", "args": {"path": "page.html", "content": expected}, "toolCallId": None}


@pytest.mark.asyncio
async def test_write_e2e_preserves_html_unicode_entities_bom_and_marker_strings(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    agent = AgentSession(
        SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory({"tools": {"maxSteps": 2}}),
        registry, _Loader(), model, "medium", tools=["write"],
    )
    expected = '\ufeff<!doctype html>\n<p title="“smart quotes”">Żółć &amp; &lt;tag&gt; <tag> <tool_call|> TOOL_CALL: <|tool_response></p>\n'
    wire = json.dumps({"tool": "write", "args": {"path": "page.html", "content": expected}}, ensure_ascii=False)
    agent.providers = {"openai": _FakeProvider([wire.replace("<tag>", "\\u003ctag>"), "DONE"])}

    await agent.prompt("write the page")

    assert (tmp_path / "page.html").read_text(encoding="utf-8") == expected
    tool_message = json.loads(next(m for m in agent.messages if m.get("role") == "toolResult")["content"])
    assert "content" not in tool_message["args"]
    assert tool_message["writeContentOmitted"] is True
    provider_tool_result = next(
        message
        for message in agent._flatten_messages_for_provider()
        if message["content"].startswith("<untrusted-tool-output>")
    )
    assert '"writeContentOmitted": true' in provider_tool_result["content"]
    assert expected not in provider_tool_result["content"]


@pytest.mark.asyncio
async def test_text_write_failure_then_correction_preserves_existing_file(tmp_path: Path) -> None:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    target = tmp_path / "page.txt"
    target.write_text("keep", encoding="utf-8")
    agent = AgentSession(
        SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory({"tools": {"maxSteps": 3}}),
        registry, _Loader(), model, "medium", tools=["write"],
    )
    agent.providers = {"openai": _FakeProvider([
        '{"tool":"write","args":{"path":"page.txt"}}',
        '{"tool":"write","args":{"path":"page.txt","content":"corrected"}}',
        "DONE",
    ])}

    await agent.prompt("write safely")

    assert target.read_text(encoding="utf-8") == "corrected"
    results = [json.loads(message["content"]) for message in agent.messages if message.get("role") == "toolResult"]
    assert results[0]["ok"] is False
    assert "requires explicit string" in results[0]["error"]
    assert results[1]["ok"] is True


@pytest.mark.asyncio
async def test_openai_raw_tool_call_id_is_propagated_to_tool_events(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    agent = AgentSession(
        SessionManager.in_memory(str(tmp_path)),
        SettingsManager.in_memory({"tools": {"maxSteps": 3}}),
        registry, _Loader(), model, "medium", tools=["read"],
    )
    agent.providers = {"openai": _FakeProvider([
        ChatResult(
            text='{"tool":"read","args":{"path":"a.txt"}}',
            raw={"id": "chatcmpl-not-a-tool", "choices": [{"message": {"tool_calls": [
                {"id": "call_openai_123", "type": "function", "function": {"name": "read", "arguments": '{"path":"a.txt"}'}}
            ]}}]},
            usage={}, stop_reason="tool_calls",
        ),
        "DONE",
    ])}
    events: list[dict[str, object]] = []
    agent.subscribe(events.append)

    await agent.prompt("read it")

    lifecycle = [event for event in events if event["type"] in {"tool_call_start", "tool_call_end"}]
    assert [event["toolCallId"] for event in lifecycle] == ["call_openai_123", "call_openai_123"]


@pytest.mark.asyncio
async def test_missing_native_id_is_persisted_and_replayed_for_gemini(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("gemini", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("gemini", "gemini-2.5-pro")
    assert model is not None
    agent = AgentSession(SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory({"tools": {"maxSteps": 2}}), registry, _Loader(), model, "medium", tools=["read"])
    provider = _RecordingNative([
        ChatResult(text='{"tool":"bash","args":{"command":"false"}}', raw={}, usage={}, native_tool_calls=[{"id": "", "name": "read", "arguments": {"path": "a.txt"}}]),
        ChatResult(text="DONE", raw={}, usage={}),
    ])
    agent.providers = {"gemini": provider}
    events: list[dict[str, object]] = []
    agent.subscribe(events.append)
    await agent.prompt("read")
    start = next(event for event in events if event["type"] == "tool_call_start")
    assert start["tool"] == "read"
    assert str(start["toolCallId"]).startswith("native-")
    replay = provider.requests[1]
    assistant = next(message for message in replay if message.get("_nativeToolCalls"))
    result = next(message for message in replay if message.get("_nativeToolCallId"))
    fallback_id = str(start["toolCallId"])
    assert assistant["_nativeToolCalls"][0]["id"] == result["_nativeToolCallId"] == fallback_id

    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return {"candidates": []}

    class Client:
        payload: dict[str, Any] = {}

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def post(self, *args: Any, **kwargs: Any) -> Response:
            Client.payload = kwargs["json"]
            return Response()

    from one.providers import gemini as gemini_module

    monkeypatch.setattr(gemini_module.httpx, "AsyncClient", Client)
    await GeminiAdapter().chat("key", "gemini-2.5-pro", replay, "off")
    function_call = next(
        part["functionCall"]
        for content in Client.payload["contents"]
        for part in content["parts"]
        if "functionCall" in part
    )
    function_response = next(
        part["functionResponse"]
        for content in Client.payload["contents"]
        for part in content["parts"]
        if "functionResponse" in part
    )
    assert function_call["id"] == function_response["id"] == fallback_id


@pytest.mark.asyncio
async def test_multiple_native_calls_execute_sequentially(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    agent = AgentSession(SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory({"tools": {"maxSteps": 2}}), registry, _Loader(), model, "medium", tools=["bash"])
    agent.providers = {"openai": _FakeProvider([
        ChatResult(text="", raw={}, usage={}, native_tool_calls=[{"id": "a", "name": "bash", "arguments": {"command": "true"}}, {"id": "b", "name": "bash", "arguments": {"command": "true"}}]),
        "done",
    ])}
    events: list[dict[str, object]] = []
    agent.subscribe(events.append)
    await agent.prompt("go")
    starts = [event for event in events if event["type"] == "tool_call_start"]
    assert [event["toolCallId"] for event in starts] == ["a", "b"]


@pytest.mark.asyncio
async def test_codex_native_batch_replays_ordered_pairs(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    session = SessionManager.in_memory(str(tmp_path))
    agent = _chatgpt_agent(session, tmp_path)
    provider = _RecordingCodex([
        ChatResult(text="", raw={}, usage={}, native_tool_calls=[
            {"id": "call-a", "name": "read", "arguments": {"path": "a.txt"}},
            {"id": "call-b", "name": "read", "arguments": {"path": "b.txt"}},
        ]),
        ChatResult(text="done", raw={}, usage={}),
    ])
    agent.providers = {"chatgpt": provider}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("read both")

    assert [event["toolCallId"] for event in events if event["type"] == "tool_call_start"] == ["call-a", "call-b"]
    replay = [item for item in provider.payloads[1]["input"] if item["type"] != "message"]
    assert [(item["type"], item["call_id"]) for item in replay] == [
        ("function_call", "call-a"), ("function_call", "call-b"),
        ("function_call_output", "call-a"), ("function_call_output", "call-b"),
    ]


@pytest.mark.asyncio
async def test_codex_native_ask_user_is_executed_and_replayed(tmp_path: Path):
    async def approve(_: str, __: dict[str, Any]) -> tuple[bool, str]:
        return True, ""

    agent = _chatgpt_agent(
        SessionManager.in_memory(str(tmp_path)), tmp_path, tools=["ask_user"], approval_callback=approve,
    )
    provider = _RecordingCodex([
        ChatResult(text="", raw={}, usage={}, native_tool_calls=[
            {"id": "ask-1", "name": "ask_user", "arguments": {"question": "Continue?"}},
        ]),
        ChatResult(text="done", raw={}, usage={}),
    ])
    agent.providers = {"chatgpt": provider}
    events: list[dict[str, Any]] = []
    asked = asyncio.Event()
    question_id: str | None = None
    agent.subscribe(events.append)

    def answer_question(event: dict[str, Any]) -> None:
        nonlocal question_id
        if event.get("type") == "ask_user":
            question_id = event["id"]
            asked.set()

    agent.subscribe(answer_question)
    prompt_task = asyncio.create_task(agent.prompt("ask me"))
    await asked.wait()
    assert question_id is not None
    agent.answer_question(question_id, "yes")
    await prompt_task

    assert not [event for event in events if event.get("reason") == "control_tool_in_native_batch"]
    lifecycle = [event for event in events if event["type"] in {"tool_call_start", "tool_call_end"}]
    assert [event["toolCallId"] for event in lifecycle] == ["ask-1", "ask-1"]
    replay = [item for item in provider.payloads[1]["input"] if item["type"] != "message"]
    assert [(item["type"], item["call_id"]) for item in replay] == [
        ("function_call", "ask-1"), ("function_call_output", "ask-1"),
    ]
    assert "yes" in replay[-1]["output"]


@pytest.mark.asyncio
async def test_codex_native_batch_abort_persists_ordered_cancelled_pairs(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("c\n", encoding="utf-8")
    session = SessionManager.in_memory(str(tmp_path))
    agent = _chatgpt_agent(session, tmp_path)
    provider = _RecordingCodex([ChatResult(text="", raw={}, usage={}, native_tool_calls=[
        {"id": "call-a", "name": "read", "arguments": {"path": "a.txt"}},
        {"id": "call-b", "name": "read", "arguments": {"path": "b.txt"}},
        {"id": "call-c", "name": "read", "arguments": {"path": "c.txt"}},
    ])])
    agent.providers = {"chatgpt": provider}
    execute = agent._execute_tool_by_name

    async def abort_after_first(name: str, args: dict[str, Any], timeout_sec: int | None = None) -> dict[str, Any]:
        result = await execute(name, args, timeout_sec=timeout_sec)
        await agent.abort()
        return result

    agent._execute_tool_by_name = abort_after_first  # type: ignore[method-assign]
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("read all")

    assert len(provider.payloads) == 1
    lifecycle = [event for event in events if event["type"] in {"tool_call_start", "tool_call_end"}]
    assert [(event["type"], event["toolCallId"]) for event in lifecycle] == [
        ("tool_call_start", "call-a"),
        ("tool_call_end", "call-a"),
        ("tool_call_start", "call-b"),
        ("tool_call_end", "call-b"),
        ("tool_call_start", "call-c"),
        ("tool_call_end", "call-c"),
    ]
    starts = [event for event in lifecycle if event["type"] == "tool_call_start"]
    ends = [event for event in lifecycle if event["type"] == "tool_call_end"]
    assert ends[0]["ok"] is True
    assert all(event["cancelled"] is True for event in starts[1:])
    assert all(event["args"] == {"path": name} for event, name in zip(starts[1:], ["b.txt", "c.txt"], strict=True))
    assert all(event["result"]["cancelled"] is True and event["aborted"] is True for event in ends[1:])
    for start, end in zip(starts[1:], ends[1:], strict=True):
        assert (start["tool"], start["toolCallId"], start["args"]) == (
            end["tool"], end["toolCallId"], end["result"]["args"],
        )
    persisted = session.build_session_context()["messages"]
    outputs = [message for message in persisted if message.get("_nativeToolCallId")]
    assert [message["_nativeToolCallId"] for message in outputs] == ["call-a", "call-b", "call-c"]
    assert json.loads(outputs[0]["content"])["ok"] is True
    assert all(json.loads(message["content"])["cancelled"] is True for message in outputs[1:])
    replay = [item for item in provider._build_payload(agent.model.id, agent._flatten_messages_for_provider(), "medium", True)["input"] if item["type"] != "message"]
    assert [(item["type"], item["call_id"]) for item in replay] == [
        ("function_call", "call-a"), ("function_call", "call-b"), ("function_call", "call-c"),
        ("function_call_output", "call-a"), ("function_call_output", "call-b"), ("function_call_output", "call-c"),
    ]


@pytest.mark.asyncio
async def test_codex_native_batch_budget_stop_cancels_remaining_calls(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    session = SessionManager.in_memory(str(tmp_path))
    agent = _chatgpt_agent(session, tmp_path)
    agent.settings_manager = SettingsManager.in_memory({"tools": {"maxSteps": 3}, "budget": {"maxTimeSec": 1}})
    provider = _RecordingCodex([ChatResult(text="", raw={}, usage={}, native_tool_calls=[
        {"id": "call-a", "name": "read", "arguments": {"path": "a.txt"}},
        {"id": "call-b", "name": "read", "arguments": {"path": "b.txt"}},
    ])])
    agent.providers = {"chatgpt": provider}
    execute = agent._execute_tool_by_name

    async def exhaust_budget_after_first(
        name: str, args: dict[str, Any], timeout_sec: int | None = None,
    ) -> dict[str, Any]:
        result = await execute(name, args, timeout_sec=timeout_sec)
        agent._session_started_at = time.monotonic() - 2
        return result

    agent._execute_tool_by_name = exhaust_budget_after_first  # type: ignore[method-assign]
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.prompt("read both")

    assert len(provider.payloads) == 1
    lifecycle = [event for event in events if event["type"] in {"tool_call_start", "tool_call_end"}]
    assert [(event["type"], event["toolCallId"]) for event in lifecycle] == [
        ("tool_call_start", "call-a"),
        ("tool_call_end", "call-a"),
        ("tool_call_start", "call-b"),
        ("tool_call_end", "call-b"),
    ]
    completed = lifecycle[1]
    assert completed["ok"] is True
    cancelled_start = lifecycle[2]
    assert cancelled_start["cancelled"] is True
    assert cancelled_start["args"] == {"path": "b.txt"}
    cancelled = lifecycle[3]
    assert (cancelled_start["tool"], cancelled_start["toolCallId"], cancelled_start["args"]) == (
        cancelled["tool"], cancelled["toolCallId"], cancelled["result"]["args"],
    )
    assert cancelled["result"]["cancelled"] is True
    assert cancelled["result"]["budgetExceeded"] is True
    assert "aborted" not in cancelled
    budget_events = [event for event in events if event["type"] == "budget_exceeded"]
    assert budget_events[-1]["kind"] == "time_budget"
    outputs = [message for message in session.build_session_context()["messages"] if message.get("_nativeToolCallId")]
    assert [message["_nativeToolCallId"] for message in outputs] == ["call-a", "call-b"]
    assert json.loads(outputs[0]["content"])["ok"] is True
    assert json.loads(outputs[1]["content"])["cancelled"] is True
    final = [message for message in agent.messages if message.get("role") == "assistant"][-1]
    assert final["stopReason"] == "budget_exceeded"


@pytest.mark.asyncio
async def test_conflicting_native_ids_repair_without_execution(tmp_path: Path):
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    agent = AgentSession(SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory({"tools": {"maxSteps": 2}}), registry, _Loader(), model, "medium", tools=["bash"])
    agent.providers = {"openai": _FakeProvider([
        ChatResult(text="", raw={}, usage={}, native_tool_calls=[
            {"id": "same", "name": "bash", "arguments": {"command": "true"}},
            {"id": "same", "name": "bash", "arguments": {"command": "false"}},
        ]), "repaired",
    ])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    await agent.prompt("go")
    assert not [event for event in events if event["type"] == "tool_call_start"]
    assert any(event.get("reason") == "conflicting_native_tool_call_id" for event in events)


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
    assert out_file.read_text(encoding="utf-8") == "def x():\n    return 1\n"


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
    assert written == (
        "def is_palindrome(s):\n"
        '    processed_s = "".join(filter(str.isalnum), s)).lower()\n'
        "    return processed_s == processed_s[::-1]\n"
    )


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
    assert written == (
        "def is_palindrome(s):\n"
        '    processed_s = "".join(ch for ch in s if ch.isalnum()).lower()\n'
        "    return processed_s == processed_s[::-1]\n"
    )


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
