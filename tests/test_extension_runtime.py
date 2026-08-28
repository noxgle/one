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
    # Explicit tmp models path so we don't pollute real user config.
    models_dir = str(tmp_path / "models")
    Path(models_dir).mkdir(parents=True, exist_ok=True)
    registry = ModelRegistry.create(auth, models_path=models_dir + "/models.json")
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    # Explicit in-memory SettingsManager with tmp cwd/agent_dir.
    settings = SettingsManager(
        cwd=str(tmp_path),
        agent_dir=str(tmp_path / "agent"),
        in_memory=True,
        initial=settings_override or {"tools": {"maxSteps": 4, "timeoutSec": 5}},
    )
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


# ---------------------------------------------------------------------------
# Phase 30.9 — chaining, precedence, and error paths for call_before_tool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_output_args_integration_changes_tool_call_start_and_result(tmp_path: Path):
    """output['args'] assignment changes the tool_call_start event AND the actual tool result."""
    (tmp_path / "original.txt").write_text("original\n", encoding="utf-8")
    (tmp_path / "replaced.txt").write_text("replaced\n", encoding="utf-8")
    ext = _write_ext(
        tmp_path,
        "mutate_output.py",
        f"""
from pathlib import Path

LOG = Path({str(_log_file(tmp_path))!r})

def _log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\\n")

def register(ctx):
    def before(input, output):
        _log("before:" + input["tool"])
        # Mutate via output["args"] (not return)
        output["args"] = {{"path": "replaced.txt"}}
    def after(input, output):
        _log("after:" + input["tool"] + ":" + str(output["output"]))
    return {{"tool.execute.before": before, "tool.execute.after": after}}
""",
    )
    agent = _mk_agent(tmp_path, extensions=[ext], tools=["read"])
    agent.providers = {"openai": _Provider(['{"tool":"read","args":{"path":"original.txt"}}', "DONE"])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.bind_extensions()
    await agent.prompt("go")

    assert "before:read" in _read_log(tmp_path)
    tcs = [e for e in events if e.get("type") == "tool_call_start" and e.get("tool") == "read"]
    assert tcs, "tool_call_start should exist"
    assert tcs[0]["args"] == {"path": "replaced.txt"}
    # The after hook should have seen "replaced" text.
    assert any(line.startswith("after:read:") and "replaced" in line for line in _read_log(tmp_path))


@pytest.mark.asyncio
async def test_multiple_hook_chaining_with_async_hook(tmp_path: Path):
    """Multiple hooks chain: hook 2 observes hook 1 effective args; async hook participates."""
    # Two separate extensions — single extension dict can't have duplicate keys
    ext1 = _write_ext(
        tmp_path,
        "chain_a.py",
        f"""
from pathlib import Path

LOG = Path({str(_log_file(tmp_path))!r})

def _log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\\n")

def before(input, output):
    _log("h1_in:" + str(input["args"]))
    output["args"] = {{"path": "b.txt"}}

def register(ctx):
    return {{"tool.execute.before": before}}
""",
    )
    ext2 = _write_ext(
        tmp_path,
        "chain_b.py",
        f"""
from pathlib import Path

LOG = Path({str(_log_file(tmp_path))!r})

def _log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\\n")

async def before(input, output):
    _log("h2_in:" + str(input["args"]))
    _log("h2_out_before:" + str(output["args"]))
    output["args"] = {{"path": output["args"]["path"], "suffix": True}}
    return {{"path": output["args"]["path"], "suffix": True}}

def register(ctx):
    return {{"tool.execute.before": before}}
""",
    )
    agent = _mk_agent(tmp_path, extensions=[ext1, ext2], tools=["read"])
    # Provider returns original args; hooks will chain and transform them.
    agent.providers = {"openai": _Provider(['{"tool":"read","args":{"path":"a.txt"}}', "DONE"])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.bind_extensions()
    await agent.prompt("go")

    log_lines = _read_log(tmp_path)
    assert "h1_in:{'path': 'a.txt'}" in log_lines
    assert "h2_in:{'path': 'b.txt'}" in log_lines
    assert "h2_out_before:{'path': 'b.txt'}" in log_lines
    tcs = [e for e in events if e.get("type") == "tool_call_start" and e.get("tool") == "read"]
    assert tcs
    assert tcs[0]["args"] == {"path": "b.txt", "suffix": True}


@pytest.mark.asyncio
async def test_returned_dict_precedence_over_output_assignment(tmp_path: Path):
    """SAME hook: invalid output['args'] but returned dict wins; next hook observes returned dict."""
    ext1 = _write_ext(
        tmp_path,
        "precedence_a.py",
        f"""
from pathlib import Path

LOG = Path({str(_log_file(tmp_path))!r})

def _log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\\n")

def before(input, output):
    # Assign invalid value — would fail, but returned dict overrides
    output["args"] = "invalid-string"
    _log("h1")
    # Returned dict overrides this hook's own output["args"] assignment
    return {{"path": "b.txt", "mode": "override"}}

def register(ctx):
    return {{"tool.execute.before": before}}
""",
    )
    ext2 = _write_ext(
        tmp_path,
        "precedence_b.py",
        f"""
from pathlib import Path

LOG = Path({str(_log_file(tmp_path))!r})

def _log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\\n")

def before(input, output):
    # Must observe the returned dict from h1 (chaining works)
    _log("h2_in:" + str(input["args"]))
    output["args"] = {{"path": input["args"]["path"], "mode": input["args"]["mode"], "chained": True}}

def register(ctx):
    return {{"tool.execute.before": before}}
""",
    )
    agent = _mk_agent(tmp_path, extensions=[ext1, ext2], tools=["read"])
    agent.providers = {"openai": _Provider(['{"tool":"read","args":{"path":"a.txt"}}', "DONE"])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.bind_extensions()
    await agent.prompt("go")

    log_lines = _read_log(tmp_path)
    assert "h1" in log_lines
    # h2 must see the returned dict from h1 (chaining proves precedence)
    assert "h2_in:{'path': 'b.txt', 'mode': 'override'}" in log_lines
    tcs = [e for e in events if e.get("type") == "tool_call_start" and e.get("tool") == "read"]
    assert tcs
    assert tcs[0]["args"] == {"path": "b.txt", "mode": "override", "chained": True}
    # No error events — returned dict was valid
    assert not any(e.get("type") == "extension_load_error" for e in events)


@pytest.mark.asyncio
async def test_invalid_output_assignment_raises_type_error_and_denies(tmp_path: Path):
    """Invalid output['args'] (non-dict, no dict return) → TypeError, deny, no execution."""
    ext1 = _write_ext(
        tmp_path,
        "bad_output.py",
        f"""
from pathlib import Path

LOG = Path({str(_log_file(tmp_path))!r})

def _log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\\n")

def before(input, output):
    output["args"] = "not-a-dict"
    # ret is None — candidate becomes the invalid string → TypeError

def register(ctx):
    return {{"tool.execute.before": before}}
""",
    )
    ext2 = _write_ext(
        tmp_path,
        "bad_output_2.py",
        f"""
from pathlib import Path

LOG = Path({str(_log_file(tmp_path))!r})

def _log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\\n")

def before(input, output):
    _log("h2-called")

def register(ctx):
    return {{"tool.execute.before": before}}
""",
    )
    agent = _mk_agent(tmp_path, extensions=[ext1, ext2], tools=["read"])
    agent.providers = {"openai": _Provider(['{"tool":"read","args":{"path":"a.txt"}}', "DONE"])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.bind_extensions()
    await agent.prompt("go")

    # TypeError extension_load_error with actionable message
    ext_errors = [e for e in events if e.get("type") == "extension_load_error"]
    assert len(ext_errors) == 1
    assert ext_errors[0]["hook"] == "tool.execute.before"
    assert ext_errors[0]["errorType"] == "TypeError"
    msg = ext_errors[0]["error"]
    assert 'output["args"]' in msg
    assert "str" in msg

    # Approval rejected and failed tool_call_end
    rejected = [e for e in events if e.get("type") == "tool_approval_rejected"]
    assert len(rejected) == 1
    ended = [e for e in events if e.get("type") == "tool_call_end"]
    assert ended and ended[-1]["ok"] is False
    # No tool_call_start (tool was never executed)
    assert not any(e.get("type") == "tool_call_start" for e in events)
    # h2 was never called — short-circuit on first hook error
    log_lines = _read_log(tmp_path)
    assert "h2-called" not in log_lines


@pytest.mark.asyncio
async def test_invalid_non_dict_return_raises_type_error_and_denies(tmp_path: Path):
    """Non-None non-dict return → TypeError, deny, no execution, later hooks not called."""
    # Two extensions — first returns invalid type, second should not be called
    ext1 = _write_ext(
        tmp_path,
        "bad_return.py",
        f"""
from pathlib import Path

LOG = Path({str(_log_file(tmp_path))!r})

def _log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\\n")

def before(input, output):
    return 42  # invalid return type

def register(ctx):
    return {{"tool.execute.before": before}}
""",
    )
    ext2 = _write_ext(
        tmp_path,
        "bad_return_2.py",
        f"""
from pathlib import Path

LOG = Path({str(_log_file(tmp_path))!r})

def _log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\\n")

def before(input, output):
    _log("h2-called")

def register(ctx):
    return {{"tool.execute.before": before}}
""",
    )
    agent = _mk_agent(tmp_path, extensions=[ext1, ext2], tools=["read"])
    agent.providers = {"openai": _Provider(['{"tool":"read","args":{"path":"a.txt"}}', "DONE"])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.bind_extensions()
    await agent.prompt("go")

    # TypeError extension_load_error
    ext_errors = [e for e in events if e.get("type") == "extension_load_error"]
    assert len(ext_errors) == 1
    assert ext_errors[0]["hook"] == "tool.execute.before"
    assert "TypeError" in ext_errors[0].get("errorType", "")

    # Approval rejected and failed tool_call_end
    rejected = [e for e in events if e.get("type") == "tool_approval_rejected"]
    assert len(rejected) == 1
    ended = [e for e in events if e.get("type") == "tool_call_end"]
    assert ended and ended[-1]["ok"] is False
    # No tool_call_start (tool was never executed)
    assert not any(e.get("type") == "tool_call_start" for e in events)
    # hook_2 was never called (short-circuit)
    log_lines = _read_log(tmp_path)
    assert "h2-called" not in log_lines


@pytest.mark.asyncio
async def test_missing_output_args_raises_type_error_and_denies(tmp_path: Path):
    """Hook deletes output['args'] key and returns None → actionable TypeError, deny."""
    ext = _write_ext(
        tmp_path,
        "delete_args.py",
        f"""
from pathlib import Path

LOG = Path({str(_log_file(tmp_path))!r})

def _log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\\n")

def before(input, output):
    del output["args"]
    # ret is None — output["args"] key is missing → TypeError

def register(ctx):
    return {{"tool.execute.before": before}}
""",
    )
    agent = _mk_agent(tmp_path, extensions=[ext], tools=["read"])
    agent.providers = {"openai": _Provider(['{"tool":"read","args":{"path":"a.txt"}}', "DONE"])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.bind_extensions()
    await agent.prompt("go")

    # TypeError extension_load_error with actionable message about missing key
    ext_errors = [e for e in events if e.get("type") == "extension_load_error"]
    assert len(ext_errors) == 1
    assert ext_errors[0]["hook"] == "tool.execute.before"
    assert "TypeError" in ext_errors[0].get("errorType", "")
    msg = ext_errors[0]["error"]
    assert "output" in msg.lower() and "args" in msg.lower()

    # Approval rejected and failed tool_call_end
    rejected = [e for e in events if e.get("type") == "tool_approval_rejected"]
    assert len(rejected) == 1
    ended = [e for e in events if e.get("type") == "tool_call_end"]
    assert ended and ended[-1]["ok"] is False
    # No tool_call_start (tool was never executed)
    assert not any(e.get("type") == "tool_call_start" for e in events)


@pytest.mark.asyncio
async def test_earlier_valid_return_cannot_mask_later_invalid_output(tmp_path: Path):
    """Regression: earlier hook returns valid dict, later hook assigns invalid with no return
    → MUST deny. Proves that a valid returned dict from an earlier hook does not protect
    against an invalid output["args"] set by a subsequent hook.
    """
    ext1 = _write_ext(
        tmp_path,
        "valid_return.py",
        """
def register(ctx):
    def before(input, output):
        # Returns valid dict — out_args becomes this dict
        return {"path": "a.txt"}
    return {"tool.execute.before": before}
""",
    )
    ext2 = _write_ext(
        tmp_path,
        "invalid_output.py",
        f"""
from pathlib import Path

LOG = Path({str(_log_file(tmp_path))!r})

def _log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\\n")

def before(input, output):
    _log("h2_in:" + str(input["args"]))
    # Assign invalid value and return None — candidate = "invalid-string" → TypeError
    output["args"] = "invalid-string"
    # No return → None → output["args"] is the candidate, which is not a dict

def register(ctx):
    return {{"tool.execute.before": before}}
""",
    )
    agent = _mk_agent(tmp_path, extensions=[ext1, ext2], tools=["read"])
    agent.providers = {"openai": _Provider(['{"tool":"read","args":{"path":"a.txt"}}', "DONE"])}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    await agent.bind_extensions()
    await agent.prompt("go")

    # TypeError extension_load_error
    ext_errors = [e for e in events if e.get("type") == "extension_load_error"]
    assert len(ext_errors) == 1
    assert ext_errors[0]["hook"] == "tool.execute.before"
    assert "TypeError" in ext_errors[0].get("errorType", "")
    msg = ext_errors[0]["error"]
    assert 'output["args"]' in msg

    # Approval rejected and failed tool_call_end
    rejected = [e for e in events if e.get("type") == "tool_approval_rejected"]
    assert len(rejected) == 1
    ended = [e for e in events if e.get("type") == "tool_call_end"]
    assert ended and ended[-1]["ok"] is False
    # No tool_call_start (tool was never executed)
    assert not any(e.get("type") == "tool_call_start" for e in events)
    # h2 was called (it ran and observed the valid dict from h1)
    log_lines = _read_log(tmp_path)
    assert "h2_in:{'path': 'a.txt'}" in log_lines
