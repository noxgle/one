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
    def __init__(self, extensions: list[dict[str, Any]] | None = None, cwd: str | None = None) -> None:
        self._extensions = extensions or []
        self.cwd = cwd or "."

    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
        return "You are a coding agent."

    def get_extensions(self) -> dict[str, Any]:
        return {"extensions": self._extensions}


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


def _mk_agent(
    tmp_path: Path,
    extensions: list[dict[str, Any]] | None = None,
    tools: list[str] | None = None,
    settings_override: dict[str, Any] | None = None,
) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory(settings_override or {"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    return AgentSession(session, settings, registry, _Loader(extensions, str(tmp_path)), model, "medium", tools=tools)


def _write_ext(tmp_path: Path, name: str, code: str) -> dict[str, Any]:
    p = tmp_path / name
    p.write_text(code, encoding="utf-8")
    return {"path": str(p)}


def _log_file(tmp_path: Path) -> Path:
    return tmp_path / "ext.log"


def _read_log(tmp_path: Path) -> list[str]:
    p = _log_file(tmp_path)
    if not p.exists():
        return []
    return p.read_text(encoding="utf-8").splitlines()


@pytest.mark.asyncio
async def test_before_hook_mutates_args_and_after_hook_fires(tmp_path: Path):
    (tmp_path / "a.txt").write_text("AAA\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("BBB\n", encoding="utf-8")
    ext = _write_ext(
        tmp_path,
        "mutate.py",
        f"""
from pathlib import Path

LOG = Path({str(_log_file(tmp_path))!r})

def _log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\\n")

def register(ctx):
    def before(input, output):
        _log("before:" + input["tool"])
        return {{"path": "b.txt"}}
    def after(input, output):
        _log("after:" + input["tool"] + ":" + str(output["output"]))
    return {{"tool.execute.before": before, "tool.execute.after": after}}
""",
    )
    agent = _mk_agent(tmp_path, extensions=[ext], tools=["read"])
    agent.providers = {"openai": _Provider(['{"tool":"read","args":{"path":"a.txt"}}', "DONE"])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.bind_extensions()
    await agent.prompt("go")

    # The before hook replaced the path: the tool read b.txt, not a.txt.
    assert "before:read" in _read_log(tmp_path)
    assert any(
        e.get("type") == "tool_call_start"
        and e.get("tool") == "read"
        and e.get("args") == {"path": "b.txt"}
        for e in events
    )
    # The after hook saw the actual result text.
    assert any(line.startswith("after:read:") and "BBB" in line for line in _read_log(tmp_path))
    assert not any(e.get("type") == "extension_load_error" for e in events)


@pytest.mark.asyncio
async def test_before_hook_deny_uses_approval_rejected_contract(tmp_path: Path):
    ext = _write_ext(
        tmp_path,
        "deny.py",
        """
from one.resources.extension_runtime import ExtensionDenied

def register(ctx):
    def before(input, output):
        raise ExtensionDenied("nope")
    return {"tool.execute.before": before}
""",
    )
    agent = _mk_agent(tmp_path, extensions=[ext], tools=["read"])
    agent.providers = {"openai": _Provider(['{"tool":"read","args":{"path":"a.txt"}}', "DONE"])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.bind_extensions()
    await agent.prompt("go")

    rejected = [e for e in events if e.get("type") == "tool_approval_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["tool"] == "read"
    assert rejected[0]["reason"].startswith("Extension deny denied:")
    ended = [e for e in events if e.get("type") == "tool_call_end"]
    assert ended and ended[-1]["ok"] is False
    # The tool was never executed (no tool_call_start emitted).
    assert not any(e.get("type") == "tool_call_start" for e in events)
    # The rejection was recorded as a toolResult message.
    assert any(m.get("role") == "toolResult" for m in agent.messages)
    # The deny (an exception in the hook) is also reported as an error event.
    hook_errors = [e for e in events if e.get("type") == "extension_load_error"]
    assert len(hook_errors) == 1
    assert hook_errors[0]["stage"] == "hook"
    assert hook_errors[0]["hook"] == "tool.execute.before"


@pytest.mark.asyncio
async def test_chat_message_hook_fires(tmp_path: Path):
    ext = _write_ext(
        tmp_path,
        "chat.py",
        f"""
from pathlib import Path

LOG = Path({str(_log_file(tmp_path))!r})

def register(ctx):
    def chat(input, output):
        with LOG.open("a", encoding="utf-8") as f:
            f.write("chat:" + str(input["message"].get("content")) + "\\n")
    return {{"chat.message": chat}}
""",
    )
    agent = _mk_agent(tmp_path, extensions=[ext])
    agent.providers = {"openai": _Provider(["DONE"])}

    await agent.bind_extensions()
    await agent.prompt("hello there")

    assert "chat:hello there" in _read_log(tmp_path)


@pytest.mark.asyncio
async def test_compacting_hook_adds_context_and_overrides_prompt(tmp_path: Path):
    ext = _write_ext(
        tmp_path,
        "compact.py",
        """
def register(ctx):
    def compact(input, output):
        output["context"].append({"type": "text", "content": "EXTRA-INFO"})
        output["prompt"] = "override-summary"
    return {"experimental.session.compacting": compact}
""",
    )
    agent = _mk_agent(
        tmp_path,
        extensions=[ext],
        settings_override={"compaction": {"recentTokens": 100, "minKeptMessages": 2, "summarizeWithModel": False}},
    )
    # Enough messages so compaction drops something.
    for i in range(40):
        agent.messages.append({"role": "user", "content": f"message {i} " + "x" * 80, "timestamp": 0})

    await agent.bind_extensions()
    result = await agent.compact()

    assert result.get("skipped") is False
    first = agent.messages[0]
    assert first.get("customType") == "compaction_summary"
    content = str(first.get("content", ""))
    assert "override-summary" in content
    assert "EXTRA-INFO" in content


@pytest.mark.asyncio
async def test_broken_extensions_emit_errors_and_session_continues(tmp_path: Path):
    broken_register = _write_ext(tmp_path, "broken.py", "def register(ctx):\n    raise RuntimeError('boom')\n")
    no_register = _write_ext(tmp_path, "noreg.py", "x = 1\n")
    syntax_error = _write_ext(tmp_path, "syntax.py", "def register(ctx):\n    return {\n")
    agent = _mk_agent(tmp_path, extensions=[broken_register, no_register, syntax_error])
    agent.providers = {"openai": _Provider(["DONE"])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.bind_extensions()
    await agent.prompt("go")  # must not raise

    errors = [e for e in events if e.get("type") == "extension_load_error" and e.get("stage") == "load"]
    assert len(errors) == 3
    assert {e["path"] for e in errors} == {broken_register["path"], no_register["path"], syntax_error["path"]}
    # agent_end means the turn completed normally.
    assert any(e.get("type") == "agent_end" for e in events)


@pytest.mark.asyncio
async def test_unknown_hooks_ignored_with_error_event(tmp_path: Path):
    ext = _write_ext(
        tmp_path,
        "unknown.py",
        """
def register(ctx):
    def before(input, output):
        pass
    return {"totally.unknown.hook": before}
""",
    )
    agent = _mk_agent(tmp_path, extensions=[ext])
    agent.providers = {"openai": _Provider(["DONE"])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.bind_extensions()
    await agent.prompt("go")

    errors = [e for e in events if e.get("type") == "extension_load_error"]
    assert len(errors) == 1
    assert errors[0]["stage"] == "bind"
    assert errors[0]["errorType"] == "UnknownHook"
    assert any(e.get("type") == "agent_end" for e in events)


@pytest.mark.asyncio
async def test_non_callable_hook_reported(tmp_path: Path):
    ext = _write_ext(
        tmp_path,
        "bad_hook.py",
        """
def register(ctx):
    return {"tool.execute.before": 42}
""",
    )
    agent = _mk_agent(tmp_path, extensions=[ext])
    agent.providers = {"openai": _Provider(["DONE"])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.bind_extensions()
    await agent.prompt("go")

    errors = [e for e in events if e.get("type") == "extension_load_error"]
    assert len(errors) == 1
    assert errors[0]["stage"] == "bind"
    assert errors[0]["errorType"] == "TypeError"
    assert any(e.get("type") == "agent_end" for e in events)


@pytest.mark.asyncio
async def test_async_register_and_async_hooks(tmp_path: Path):
    ext = _write_ext(
        tmp_path,
        "async_ext.py",
        f"""
from pathlib import Path

LOG = Path({str(_log_file(tmp_path))!r})

async def register(ctx):
    async def before(input, output):
        with LOG.open("a", encoding="utf-8") as f:
            f.write("async-before\\n")
    return {{"tool.execute.before": before}}
""",
    )
    agent = _mk_agent(tmp_path, extensions=[ext], tools=["read"])
    agent.providers = {"openai": _Provider(['{"tool":"read","args":{"path":"missing"}}', "DONE"])}

    await agent.bind_extensions()
    await agent.prompt("go")

    assert "async-before" in _read_log(tmp_path)


@pytest.mark.asyncio
async def test_dispose_hook_called(tmp_path: Path):
    ext = _write_ext(
        tmp_path,
        "dispose_ext.py",
        f"""
from pathlib import Path

LOG = Path({str(_log_file(tmp_path))!r})

def register(ctx):
    def dispose():
        with LOG.open("a", encoding="utf-8") as f:
            f.write("dispose\\n")
    return {{"dispose": dispose}}
""",
    )
    agent = _mk_agent(tmp_path, extensions=[ext])

    await agent.bind_extensions()
    assert _read_log(tmp_path) == []
    await agent.dispose()
    assert "dispose" in _read_log(tmp_path)
