from __future__ import annotations

import asyncio
import tempfile

from .common import truncate_tail


async def bash_tool(cwd: str, command: str, timeout: int | None = None, command_prefix: str | None = None) -> dict:
    final_command = f"{command_prefix}; {command}" if command_prefix else command
    proc = await asyncio.create_subprocess_shell(
        final_command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    output = b""
    full_path = None
    try:
        if timeout:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        else:
            out, _ = await proc.communicate()
        output += out or b""
    except asyncio.TimeoutError:
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
    shown = trunc["content"] or "(no output)"

    if trunc["truncated"]:
        with tempfile.NamedTemporaryFile(delete=False, prefix="one-bash-", suffix=".log", mode="w", encoding="utf-8") as f:
            f.write(text)
            full_path = f.name
        shown += f"\n\n[Output truncated. Full output: {full_path}]"

    if proc.returncode not in (0, None):
        raise RuntimeError(shown + f"\n\nCommand exited with code {proc.returncode}")

    return {
        "output": text,
        "exitCode": proc.returncode,
        "cancelled": False,
        "truncated": trunc["truncated"],
        "fullOutputPath": full_path,
        "content": [{"type": "text", "text": shown}],
        "details": {"truncation": trunc if trunc["truncated"] else None, "fullOutputPath": full_path},
    }
