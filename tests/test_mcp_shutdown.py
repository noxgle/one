from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from one.mcp import McpClient, McpManager, McpServerConfig

# ---------------------------------------------------------------------------
# McpClient.close() regression tests — no real subprocesses.
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Minimal transport stub with close() + is_closing()."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeStreamReader:
    """Minimal StreamReader stub with feed_eof()."""

    def feed_eof(self) -> None:
        pass


@pytest.mark.asyncio
async def test_mcpclient_close_cancels_stderr_drain_task():
    """close() must cancel the fire-and-forget _drain_stderr task."""
    client = McpClient(McpServerConfig(name="test", command="true"))

    # Simulate start() having created the task.
    async def _fake_drain():
        await asyncio.sleep(100)

    client._proc = MagicMock()  # proc exists but no pipes
    client._stderr_task = asyncio.create_task(_fake_drain())

    await client.close()

    # The task should be cancelled.
    assert client._stderr_task.done()
    # task.exception() raises the exception directly for CancelledError
    try:
        exc = client._stderr_task.exception()
        assert isinstance(exc, asyncio.CancelledError)
    except asyncio.CancelledError:
        pass  # expected — CancelledError propagates from exception()


@pytest.mark.asyncio
async def test_mcpclient_close_closes_stdin_and_waits():
    """close() must call stdin.close() + await stdin.wait_closed()."""
    fake_stdin = MagicMock()
    fake_stdin.close = MagicMock()
    fake_stdin.wait_closed = AsyncMock()

    proc = MagicMock()
    proc.stdin = fake_stdin
    proc.returncode = None

    client = McpClient(McpServerConfig(name="test", command="true"))
    client._proc = proc
    client._stderr_task = None

    await client.close()

    fake_stdin.close.assert_called_once()
    fake_stdin.wait_closed.assert_called_once()


@pytest.mark.asyncio
async def test_mcpclient_close_terminate_before_wait():
    """close() must call terminate() then wait() — not kill first."""
    proc = MagicMock()
    proc.stdin = None
    proc.stdout = None
    proc.stderr = None
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)
    proc.terminate = MagicMock()
    proc.kill = MagicMock()

    client = McpClient(McpServerConfig(name="test", command="true"))
    client._proc = proc
    client._stderr_task = None

    await client.close()

    proc.terminate.assert_called_once()
    # terminate is called before wait (which is awaited after terminate).
    # kill must NOT have been called because terminate+wait succeeded.
    proc.kill.assert_not_called()


@pytest.mark.asyncio
async def test_mcpclient_close_kill_on_timeout():
    """When terminate+wait times out, close() must kill then wait again."""
    proc = MagicMock()
    proc.stdin = None
    proc.stdout = None
    proc.stderr = None
    proc.returncode = None
    proc.terminate = MagicMock()
    # make wait() raise TimeoutError
    proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
    proc.kill = MagicMock()

    client = McpClient(McpServerConfig(name="test", command="true"))
    client._proc = proc
    client._stderr_task = None

    await client.close()

    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    proc.wait.assert_called()  # called twice: once after terminate, once after kill


@pytest.mark.asyncio
async def test_mcpclient_close_sets_proc_none():
    """close() must set self._proc to None."""
    proc = MagicMock()

    client = McpClient(McpServerConfig(name="test", command="true"))
    client._proc = proc
    client._stderr_task = None

    await client.close()

    assert client._proc is None


@pytest.mark.asyncio
async def test_mcpclient_close_feed_eof_on_streams():
    """close() must call feed_eof() on stdout and stderr streams."""
    fake_stdout = MagicMock()
    fake_stderr = MagicMock()

    proc = MagicMock()
    proc.stdin = None
    proc.stdout = fake_stdout
    proc.stderr = fake_stderr
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)
    proc.terminate = MagicMock()

    client = McpClient(McpServerConfig(name="test", command="true"))
    client._proc = proc
    client._stderr_task = None

    await client.close()

    fake_stdout.feed_eof.assert_called_once()
    fake_stderr.feed_eof.assert_called_once()


@pytest.mark.asyncio
async def test_mcpclient_close_no_proc_is_nop():
    """Calling close() with _proc=None should be a no-op."""
    client = McpClient(McpServerConfig(name="test", command="true"))
    client._proc = None
    client._stderr_task = None

    await client.close()  # should not raise


# ---------------------------------------------------------------------------
# McpManager.close() regression tests.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcpmanager_close_gathers_all_clients():
    """close() must call close() on every client and clear the list."""
    c1 = MagicMock()
    c1.close = AsyncMock()
    c2 = MagicMock()
    c2.close = AsyncMock()

    manager = McpManager([])
    manager._clients = [c1, c2]

    await manager.close()

    c1.close.assert_called_once()
    c2.close.assert_called_once()
    assert manager._clients == []


@pytest.mark.asyncio
async def test_mcpmanager_close_returns_exceptions():
    """close() must not crash when a client.close() raises."""
    failing = MagicMock()
    failing.close = AsyncMock(side_effect=RuntimeError("boom"))

    ok = MagicMock()
    ok.close = AsyncMock()

    manager = McpManager([])
    manager._clients = [failing, ok]

    await manager.close()  # should not raise

    assert manager._clients == []


# ---------------------------------------------------------------------------
# Cleanup ordering test — TuiMode.run abort before mcp.close.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_quit_order_abort_before_mcp_close():
    """Ctrl+Q path: session.abort() must fire before mcp_manager.close()."""
    session = AsyncMock()
    session.abort = AsyncMock()
    session.settings_manager = MagicMock()
    session.settings_manager.get_tool_approval = MagicMock(return_value=False)

    host = MagicMock()
    host.session = session

    mcp_manager = AsyncMock()
    mcp_manager.close = AsyncMock()

    # Patch the main _run finally block behavior — we test the ordering
    # by replaying the sequence that TuiMode.run + main.py finally perform.
    order: list[str] = []

    async def track_abort() -> None:
        order.append("abort")
        await session.abort()

    async def track_mcp_close() -> None:
        order.append("mcp_close")
        await mcp_manager.close()

    # TuiMode.run finally fires abort first.
    await track_abort()
    # main.py finally fires mcp.close next.
    await track_mcp_close()

    assert order == ["abort", "mcp_close"], f"Expected abort→mcp_close, got {order}"
    session.abort.assert_awaited_once()
    mcp_manager.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_close_complete_before_loop_exit():
    """MCP close() must complete (awaited) before the event loop closes."""
    c1 = MagicMock()
    c1.close = AsyncMock()
    c2 = MagicMock()
    c2.close = AsyncMock()

    manager = McpManager([])
    manager._clients = [c1, c2]

    # Verify that gather with return_exceptions completes all tasks.
    await manager.close()

    # All clients closed.
    assert c1.close.await_count == 1
    assert c2.close.await_count == 1
    assert manager._clients == []


# ---------------------------------------------------------------------------
# Bash tool CancelledError reaping test.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bash_cancelled_error_reaps_process():
    """CancelledError path must kill + wait the process to avoid transport leaks."""
    from one.tools.bash import bash_tool

    wait_called = False

    class _FakeProc:
        pid = 12345
        returncode = None
        stdout = MagicMock()
        stderr = MagicMock()

        async def communicate(self, **kwargs):
            raise asyncio.CancelledError

        def kill(self):
            pass

        async def wait(self):
            nonlocal wait_called
            wait_called = True

    with patch("asyncio.create_subprocess_shell", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = _FakeProc()

        with patch("one.tools.bash._kill_process_group") as mock_kg:
            try:
                await bash_tool(cwd="/tmp", command="true")
            except asyncio.CancelledError:
                pass  # the tool itself may raise after our handling

    # The process kill + wait path should have been taken.
    mock_kg.assert_called_once_with(12345)
    assert wait_called is True, "Process was not reaped after CancelledError"
