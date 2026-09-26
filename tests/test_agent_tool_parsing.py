from __future__ import annotations

import json
from pathlib import Path

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.core.types import ModelInfo
from one.providers.base import ChatResult
from tests.support.agents import _FakeProvider, _Loader


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
    assert tool_message["args"]["content"] == "[omitted from model context]"
    assert tool_message["args"]["writeContentOmitted"] is True
    provider_tool_result = next(
        message
        for message in agent._flatten_messages_for_provider()
        if message["content"].startswith("<untrusted-tool-output>")
    )
    assert "[omitted from model context]" in provider_tool_result["content"]
    assert expected not in provider_tool_result["content"]


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
