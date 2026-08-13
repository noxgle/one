from __future__ import annotations

import asyncio
import os
import re
import signal
import tempfile

from .common import strip_ansi, truncate_tail

# Alternate-screen (full-screen/raw TUI) markers emitted by curses apps
# (top, htop, less, vim, ...). Some programs emit only the entry marker.
_FULLSCREEN_MARKERS = ("\x1b[?1049h", "\x1b[?1049l", "\x1b[?1047h", "\x1b[?1047l", "\x1b[?47h", "\x1b[?47l")

_FULLSCREEN_CTRL_RE = re.compile(r"\x1b\[[0-9;?]*[HfJKG]")


def _looks_fullscreen(text: str) -> bool:
    """Heuristic: curses-style full-screen app output (interactive TTY required)."""
    if not text:
        return False
    if any(marker in text for marker in _FULLSCREEN_MARKERS):
        return True
    # Cursor-control sequences (home / position / clear screen / erase line /
    # insert-delete) are emitted by full-screen apps while repainting; ordinary
    # colored output (SGR `\x1b[...m`) never contains them.
    if _FULLSCREEN_CTRL_RE.search(text):
        return True
    # Fallback for exotic dumps without cursor control: dense escape output
    # with little actual text. Requires >= 3 escape sequences so short colored
    # strings like `\x1b[31mred\x1b[0m` are never flagged.
    stripped = strip_ansi(text)
    raw_bytes = len(text.encode("utf-8", errors="ignore"))
    stripped_bytes = len(stripped.encode("utf-8", errors="ignore"))
    if raw_bytes == 0:
        return False
    sequences = re.findall(r"\x1b\[[0-?]*[ -/]*[@-~]", text)
    return len(sequences) >= 3 and (raw_bytes - stripped_bytes) / raw_bytes > 0.3


async def bash_tool(cwd: str, command: str, timeout: int | None = None, command_prefix: str | None = None) -> dict:
    final_command = f"{command_prefix}; {command}" if command_prefix else command
    proc = await asyncio.create_subprocess_shell(
        final_command,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )

    output = b""
    full_path = None
    try:
        if timeout:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        else:
            out, _ = await proc.communicate()
        output += out or b""
    except asyncio.CancelledError:
        _kill_process_group(proc.pid)
        proc.kill()
        await asyncio.sleep(0.1)
        return {
            "output": output.decode("utf-8", errors="ignore"),
            "exitCode": proc.returncode or -1,
            "cancelled": True,
            "truncated": False,
            "fullscreen": False,
            "fullOutputPath": None,
            "content": [{"type": "text", "text": "(cancelled)"}],
            "details": None,
        }
    except asyncio.TimeoutError:
        _kill_process_group(proc.pid)
        proc.kill()
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=1)
            output += out or b""
        except asyncio.TimeoutError:
            # Avoid hanging forever on buggy shell/process states.
            pass
        output += b"\n\nCommand timed out"

    text = output.decode("utf-8", errors="ignore")
    trunc = truncate_tail(text)
    fullscreen = _looks_fullscreen(text)
    shown = strip_ansi(trunc["content"]) or "(no output)"

    if fullscreen or trunc["truncated"]:
        with tempfile.NamedTemporaryFile(delete=False, prefix="one-bash-", suffix=".log", mode="w", encoding="utf-8") as f:
            f.write(text)
            full_path = f.name

    if fullscreen:
        shown = (
            "[Program requires an interactive terminal (TTY) - full-screen output is not shown here. "
            f"Raw output saved to: {full_path}. "
            "Run it in a real terminal or use batch mode (e.g. 'top -b -n 1').]"
        )
    elif trunc["truncated"]:
        shown += f"\n\n[Output truncated. Full output: {full_path}]"

    if proc.returncode is not None and proc.returncode > 0:
        raise RuntimeError(shown + f"\n\nCommand exited with code {proc.returncode}")

    return {
        "output": text,
        "exitCode": proc.returncode,
        "cancelled": False,
        "truncated": trunc["truncated"],
        "fullscreen": fullscreen,
        "fullOutputPath": full_path,
        "content": [{"type": "text", "text": shown}],
        "details": {
            "truncation": trunc if trunc["truncated"] else None,
            "fullscreen": fullscreen,
            "fullOutputPath": full_path,
        },
    }


def _kill_process_group(pid: int | None) -> None:
    """Kill the process group rooted at *pid* to reap shell children."""
    if pid is None:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
