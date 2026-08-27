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


def test_edit_with_oldString_newString_keys(tmp_path: Path):
    """The documented schema uses oldString/newString keys."""
    write_tool(str(tmp_path), "a.txt", "hello world\n")
    edit_tool(str(tmp_path), "a.txt", [{"oldString": "hello", "newString": "goodbye"}])
    after = read_tool(str(tmp_path), "a.txt")
    assert "goodbye" in after["content"][0]["text"]
    assert "hello" not in after["content"][0]["text"]


def test_edit_multi_collision_no_sequential_corruption(tmp_path: Path):
    """When an earlier replacement creates text matching a later oldText, the later
    edit must still match the ORIGINAL text, not the mutated intermediate result.

    File: "foo\\nbar\\n"
    Edit 1: foo -> bar X
    Edit 2: bar -> Y

    Correct result: "bar X\\nY\\n"  (bar on line 2 replaced in original)
    Broken result:  "Y X\\nbar\\n"  (sequential replace hits the bar inside 'bar X')
    """
    write_tool(str(tmp_path), "a.txt", "foo\nbar\n")
    edits = [
        {"oldString": "foo", "newString": "bar X"},
        {"oldString": "bar", "newString": "Y"},
    ]
    edit_tool(str(tmp_path), "a.txt", edits)
    after = read_tool(str(tmp_path), "a.txt")
    assert after["content"][0]["text"] == "bar X\nY\n", (
        f"Expected 'bar X\\nY\\n', got {after['content'][0]['text']!r}"
    )


def test_edit_missing_keys_raises_value_error(tmp_path: Path):
    """An edit dict without oldString/oldText or newString/newText must raise ValueError."""
    write_tool(str(tmp_path), "a.txt", "hello\n")
    with pytest.raises(ValueError) as excinfo:
        edit_tool(str(tmp_path), "a.txt", [{"path": "a.txt", "content": "nope"}])
    assert "oldString" in str(excinfo.value)
    assert "oldText" in str(excinfo.value)


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
    assert result["ok"] is True
    assert result["timedOut"] is False
    assert result["cancelled"] is False
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
    assert result["ok"] is False
    assert result["timedOut"] is True
    assert result["errorType"] == "TimeoutError"
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
    # execute_bash must return the structured cancellation dict from bash_tool — do not silently pass on CancelledError.
    result = await asyncio.wait_for(task, timeout=5)
    assert isinstance(result, dict)
    assert result.get("ok") is False
    assert result.get("timedOut") is False
    assert result.get("cancelled") is True
    assert result.get("errorType") == "CancelledError"
    exit_code = result.get("exitCode")
    assert exit_code is not None and exit_code != 0
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

    # Bash tool_call_end: ok:false, aborted:true, result.cancelled:true, result.timedOut:false, no timeout classification
    tool_call_ends = [e for e in events if e.get("type") == "tool_call_end"]
    bash_end = next((e for e in tool_call_ends if e.get("tool") == "bash"), None)
    assert bash_end is not None
    assert bash_end["ok"] is False
    assert bash_end.get("aborted") is True
    result = bash_end["result"]
    assert result["cancelled"] is True
    assert result["timedOut"] is False
    # No timeout classification: errorType must not be TimeoutError
    assert result.get("errorType") != "TimeoutError"


def test_bash_tool_fullscreen_flagged_and_hinted(tmp_path: Path):
    """Curses full-screen output must be replaced by a TTY hint, raw output kept in a log file."""
    import asyncio

    from one.tools.bash import bash_tool

    cmd = (
        "python3 -c "
        "'import sys;sys.stdout.write(chr(27)+\"[?1049h\"+\"hi\"+chr(27)+\"[?1049l\");'"
    )
    result = asyncio.run(bash_tool(str(tmp_path), cmd))
    shown = result["content"][0]["text"]
    assert result["fullscreen"] is True
    assert result["details"]["fullscreen"] is True
    assert "TTY" in shown
    assert "top -b -n 1" in shown
    assert "\x1b" not in shown
    assert "\x1b[?1049h" in result["output"]  # raw kept
    assert result["fullOutputPath"] is not None
    raw = Path(result["fullOutputPath"]).read_text(encoding="utf-8")
    assert "\x1b[?1049h" in raw and "hi" in raw
    # raw log should be outside the repo, in the temp dir of the OS
    assert "one-bash-" in result["fullOutputPath"]


def test_bash_tool_short_colored_output_not_fullscreen(tmp_path: Path):
    """Short colored output (SGR only) must NOT be flagged as full-screen."""
    import asyncio

    from one.tools.bash import bash_tool

    cmd = "python3 -c 'import sys;sys.stdout.write(chr(27)+\"[31mred\"+chr(27)+\"[0m\");'"
    result = asyncio.run(bash_tool(str(tmp_path), cmd))
    assert result["fullscreen"] is False
    assert result["content"][0]["text"] == "red"
    assert "\x1b" not in result["content"][0]["text"]
    assert "\x1b[31m" in result["output"]
    assert result["fullOutputPath"] is None


def test_bash_tool_cursor_control_flagged_fullscreen(tmp_path: Path):
    """Cursor-control sequences (clear screen / home) indicate a full-screen app."""
    import asyncio

    from one.tools.bash import bash_tool

    cmd = "python3 -c 'import sys;sys.stdout.write(chr(27)+\"[2J\"+chr(27)+\"[H\"+\"hello\");'"
    result = asyncio.run(bash_tool(str(tmp_path), cmd))
    assert result["fullscreen"] is True
    shown = result["content"][0]["text"]
    assert "TTY" in shown
    assert "\x1b" not in shown
    assert "\x1b[2J" in result["output"]
    assert result["fullOutputPath"] is not None


def test_bash_tool_batch_mode_not_flagged(tmp_path: Path):
    import asyncio

    from one.tools.bash import bash_tool

    cmd = "command -v top >/dev/null 2>&1 && top -b -n 1 | head -5 || echo no-top"
    result = asyncio.run(bash_tool(str(tmp_path), cmd))
    assert result["fullscreen"] is False
    assert result["content"][0]["text"].strip() != ""
    assert "\x1b" not in result["content"][0]["text"]


def test_sanitize_display_text_fullscreen_dump_sanitized():
    """htop-style raw dump: no control chars survive, box drawing preserved."""
    from one.tools.common import sanitize_display_text

    dump = "\x1b[?1049h\x1b[H\x1b[2J" + "PID\x1b[12;34H USER\x1b[1;1H" + "\u2502\u2500\u251c" + "\x9e\x1b[?25l" + "done"
    clean = sanitize_display_text(dump)
    assert "\x1b" not in clean
    assert not any(ord(c) < 0x20 and c not in "\t\n" for c in clean)
    assert not any(0x7F <= ord(c) <= 0x9F for c in clean)
    assert "PID" in clean and "done" in clean
    assert "\u2502" in clean and "\u2500" in clean
    assert "\ufffd" in clean  # the C1 byte was replaced


def test_plan_tool_stores_text() -> None:
    from one.tools.plan import plan_tool

    result = plan_tool("1. read file\n2. edit content")
    assert result["ok"] is True
    assert "Plan stored" in result["result"]


def test_plan_tool_rejects_empty() -> None:
    from one.tools.plan import plan_tool

    with pytest.raises(ValueError):
        plan_tool("")
    with pytest.raises(ValueError):
        plan_tool("   ")


def test_sanitize_display_text_plain_and_safe_controls_passthrough():
    from one.tools.common import sanitize_display_text

    assert sanitize_display_text("hello world") == "hello world"
    assert sanitize_display_text("a\tb\nc") == "a\tb\nc"


def test_sanitize_display_text_normalizes_carriage_returns_and_stray_esc():
    from one.tools.common import sanitize_display_text

    assert sanitize_display_text("a\r\nb\rc") == "a\nb\nc"
    assert "\r" not in sanitize_display_text("x\ry")
    # stray ESC is stripped by strip_ansi (called first), so no ESC survives
    assert "\x1b" not in sanitize_display_text("pre\x1bpost")
    # strip_ansi removes it, so sanitize_display_text sees "prepost" — clean
    assert sanitize_display_text("pre\x1bpost") == "prepost"


def test_edit_path_inside_edits_recovered(tmp_path: Path):
    """When the model puts 'path' inside edits[0] instead of top-level, recover it."""
    write_tool(str(tmp_path), "a.txt", "hello world\n")
    result = edit_tool(str(tmp_path), "", [{"oldString": "hello", "newString": "goodbye", "path": "a.txt"}])
    assert "Successfully replaced 1 block" in result["content"][0]["text"]
    after = read_tool(str(tmp_path), "a.txt")
    assert "goodbye" in after["content"][0]["text"]
    assert "hello" not in after["content"][0]["text"]


def test_edit_missing_path_raises(tmp_path: Path):
    """When path is empty AND no path inside edits[0], raise ValueError about top-level."""
    write_tool(str(tmp_path), "a.txt", "hello\n")
    with pytest.raises(ValueError, match="top-level"):
        edit_tool(str(tmp_path), "", [{"oldString": "hello", "newString": "goodbye"}])


def test_edit_edits_not_list_raises(tmp_path: Path):
    """When edits is passed as a dict instead of a list, raise ValueError about list."""
    write_tool(str(tmp_path), "a.txt", "hello\n")
    with pytest.raises(ValueError, match="list"):
        edit_tool(str(tmp_path), "a.txt", {"oldString": "x", "newString": "y"})


@pytest.mark.asyncio
async def test_bash_tool_timeout_exitCode_never_none_or_zero(tmp_path: Path):
    """After kill/communicate the exitCode must be a negative return code or -1 fallback — never None or zero."""
    from one.tools.bash import bash_tool

    result = await bash_tool(str(tmp_path), "sleep 100", timeout=0.05)
    assert result["ok"] is False
    assert result["timedOut"] is True
    assert result["errorType"] == "TimeoutError"
    exit_code = result["exitCode"]
    assert exit_code is not None, "timeout exitCode must never be None"
    assert exit_code != 0, "timeout exitCode must never be zero"
    assert isinstance(exit_code, int), "exitCode must be int"


@pytest.mark.asyncio
async def test_bash_tool_cancel_returns_structured_result(tmp_path: Path):
    """bash_tool catches CancelledError and returns structured result (not raising)."""
    from one.tools.bash import bash_tool

    async def run_with_cancel():
        task = asyncio.create_task(bash_tool(str(tmp_path), "sleep 100"))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            return await task
        except asyncio.CancelledError:
            # If bash_tool re-raises, we catch here
            return None

    result = await run_with_cancel()
    # bash_tool should catch CancelledError and return a structured dict
    assert result is not None
    assert isinstance(result, dict)
    assert result.get("ok") is False
    assert result.get("timedOut") is False
    assert result.get("cancelled") is True
    assert result.get("errorType") == "CancelledError"
    exit_code = result.get("exitCode")
    assert exit_code is not None
    assert exit_code != 0
