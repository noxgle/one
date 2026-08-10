from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from one.tools.edit import edit_tool
from one.tools.find import find_tool
from one.tools.grep import grep_tool
from one.tools.ls import ls_tool
from one.tools.read import read_tool
from one.tools.write import write_tool


def test_write_and_read_basic(tmp_path: Path):
    write_tool(str(tmp_path), "a.txt", "line1\nline2\nline3")
    out = read_tool(str(tmp_path), "a.txt", offset=1, limit=2)
    assert "line1" in out["content"][0]["text"]
    assert "Use offset=3 to continue." in out["content"][0]["text"]


def test_read_errors_and_truncation(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        read_tool(str(tmp_path), "missing.txt")

    # 2001 lines should trigger head truncation (default max lines = 2000).
    many_lines = "\n".join(f"line-{i}" for i in range(1, 2002))
    write_tool(str(tmp_path), "big.txt", many_lines)
    out = read_tool(str(tmp_path), "big.txt")
    text = out["content"][0]["text"]
    assert "Use offset=2001 to continue." in text
    assert out["details"]["truncation"] is not None

    with pytest.raises(ValueError):
        read_tool(str(tmp_path), "big.txt", offset=99999)


def test_edit_success_and_errors(tmp_path: Path):
    write_tool(str(tmp_path), "a.txt", "line1\nline2\nline3")

    edit_tool(str(tmp_path), "a.txt", [{"oldText": "line2", "newText": "LINE2"}])
    after = read_tool(str(tmp_path), "a.txt")
    assert "LINE2" in after["content"][0]["text"]
    assert "line2" not in after["content"][0]["text"]

    with pytest.raises(ValueError):
        edit_tool(str(tmp_path), "a.txt", [])

    with pytest.raises(FileNotFoundError):
        edit_tool(str(tmp_path), "missing.txt", [{"oldText": "x", "newText": "y"}])

    # Non-unique oldText should fail.
    write_tool(str(tmp_path), "dupe.txt", "x\nx\n")
    with pytest.raises(ValueError):
        edit_tool(str(tmp_path), "dupe.txt", [{"oldText": "x", "newText": "y"}])

    # Missing oldText should fail.
    with pytest.raises(ValueError):
        edit_tool(str(tmp_path), "a.txt", [{"oldText": "does-not-exist", "newText": "X"}])


def test_find_grep_ls_basic(tmp_path: Path):
    (tmp_path / "x.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "y.txt").write_text("hello world\n", encoding="utf-8")

    assert "x.py" in find_tool(str(tmp_path), "*.py")["content"][0]["text"]
    assert "hello world" in grep_tool(str(tmp_path), "hello")["content"][0]["text"]
    assert "x.py" in ls_tool(str(tmp_path))["content"][0]["text"]


def test_find_grep_ls_errors_and_empty(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        find_tool(str(tmp_path), "*.py", "missing")

    with pytest.raises(FileNotFoundError):
        grep_tool(str(tmp_path), "hello", "missing")

    with pytest.raises(FileNotFoundError):
        ls_tool(str(tmp_path), "missing")

    (tmp_path / "nested").mkdir()
    no_files = find_tool(str(tmp_path), "*.doesnotexist")["content"][0]["text"]
    assert no_files == "No files found"

    no_matches = grep_tool(str(tmp_path), "definitely-no-match")["content"][0]["text"]
    assert no_matches == "No matches"

    file_path = tmp_path / "single.txt"
    file_path.write_text("content", encoding="utf-8")
    ls_file = ls_tool(str(tmp_path), "single.txt")["content"][0]["text"]
    assert ls_file.endswith("single.txt")


def test_bash_tool_success(tmp_path: Path):
    import asyncio

    from one.tools.bash import bash_tool

    result = asyncio.run(bash_tool(str(tmp_path), "echo hello"))
    assert "hello" in result["content"][0]["text"]
    assert result["exitCode"] == 0
    assert result["truncated"] is False


def test_bash_tool_nonzero_exit_raises(tmp_path: Path):
    import asyncio

    from one.tools.bash import bash_tool

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(bash_tool(str(tmp_path), "echo bad && exit 7", timeout=2))
    msg = str(exc.value)
    assert "bad" in msg or "timed out" in msg.lower()
    assert "Command exited with code" in msg


# ---------------------------------------------------------------------------
# Helpers for agent-based tests
# ---------------------------------------------------------------------------


class _Loader:
    """Minimal resource_loader stub used by _mk_agent."""

    def get_system_prompt(self, selected_tools=None) -> str:
        return "test"


def _mk_agent(tmp_path: Path, settings_override: dict[str, Any] | None = None) -> Any:
    from one.core.agent_session import AgentSession
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.settings_manager import SettingsManager

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory(settings_override or {"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    return AgentSession(session, settings, registry, _Loader(), model, "medium")


def test_bash_tool_stdin_not_tty(tmp_path: Path):
    """Child processes must not inherit the app's terminal (avoids `top`-style hangs)."""
    import asyncio

    from one.tools.bash import bash_tool

    result = asyncio.run(bash_tool(str(tmp_path), "test -t 0; echo exit=$?"))
    assert "exit=1" in result["output"] or result["exitCode"] == 1


def test_bash_tool_cancel_kills_child(tmp_path: Path):
    import asyncio

    from one.tools.bash import bash_tool

    async def scenario():
        task = asyncio.create_task(bash_tool(str(tmp_path), "sleep 1000"))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        proc = await asyncio.create_subprocess_shell(
            "pgrep -f '[s]leep 1000' || true",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return out.decode(errors="ignore").strip()

    leftover = asyncio.run(scenario())
    assert leftover == ""


@pytest.mark.asyncio
async def test_execute_bash_default_timeout(tmp_path: Path):
    """execute_bash applies the configured tool timeout (default 30s) instead of hanging forever."""
    agent = _mk_agent(tmp_path, settings_override={"tools": {"maxSteps": 4, "timeoutSec": 2}})
    result = await agent.execute_bash("sleep 30")
    assert result["exitCode"] != 0
    assert "timed out" in result["output"].lower()
    # The sleep process must be gone.
    proc = await asyncio.create_subprocess_shell(
        "pgrep -f '[s]leep 30' || true",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    assert out.decode(errors="ignore").strip() == ""


@pytest.mark.asyncio
async def test_abort_kills_active_bash(tmp_path: Path):
    """abort() must cancel an in-flight /bash command (Ctrl+C in TUI)."""
    agent = _mk_agent(tmp_path)
    task = asyncio.create_task(agent.execute_bash("sleep 1000"))
    await asyncio.sleep(0.2)
    await agent.abort()
    try:
        await asyncio.wait_for(task, timeout=5)
    except asyncio.CancelledError:
        pass
    proc = await asyncio.create_subprocess_shell(
        "pgrep -f '[s]leep 1000' || true",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    assert out.decode(errors="ignore").strip() == ""


@pytest.mark.asyncio
async def test_abort_kills_bash_running_via_tool_loop(tmp_path: Path):
    """abort() must kill a bash subprocess launched through the agent's tool loop (Ctrl+C in TUI)."""
    agent = _mk_agent(tmp_path, settings_override={"retry": {"enabled": False}})

    class _ToolCallProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, api_key, model, messages, thinking_level, headers=None):
            from one.providers.base import ChatResult

            self.calls += 1
            if self.calls == 1:
                return ChatResult(
                    text='{"tool":"bash","args":{"command":"sleep 1000"}}',
                    raw={},
                    usage={},
                    stop_reason="tool_call",
                )
            return ChatResult(text="DONE", raw={}, usage={}, stop_reason="stop")

    agent.providers = {"openai": _ToolCallProvider()}
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)

    task = asyncio.create_task(agent.prompt("run sleep"))
    await asyncio.sleep(0.3)  # let the bash subprocess start
    await agent.abort()
    await task

    # The sleep subprocess must have been killed.
    proc = await asyncio.create_subprocess_shell(
        "pgrep -f '[s]leep 1000' || true",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    assert out.decode(errors="ignore").strip() == ""

    turn_end = [e for e in events if e.get("type") == "turn_end"]
    assert turn_end
    assert turn_end[-1]["aborted"] is True
    assert turn_end[-1]["reason"] == "abort"
