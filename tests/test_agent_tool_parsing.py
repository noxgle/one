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
