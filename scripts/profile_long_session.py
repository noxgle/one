#!/usr/bin/env python3
"""Profile a long ``one`` session through the real RPC runtime.

The script intentionally uses the user's configured default model/provider. It
does not modify the real session directory: unless ``--session-dir`` is passed,
it creates a disposable diagnostic directory under ``/tmp``. The collected
metrics help distinguish provider/context/persistence slowdown from a TUI-only
problem. Run the same workload in RPC mode first, then repeat it manually in
the TUI if needed.

Examples:

    .venv/bin/python scripts/profile_long_session.py --cycles 30
    .venv/bin/python scripts/profile_long_session.py --prompt-file task.txt
    .venv/bin/python scripts/profile_long_session.py --model llama.cpp/model-id
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any


def _default_prompt(cycles: int) -> str:
    return (
        "Work autonomously in the current project and do not modify files. "
        f"Perform exactly {cycles} short diagnostic cycles. In each cycle, use the bash tool "
        "to run one harmless command such as `printf 'cycle N\\n'`, inspect the result, "
        "and briefly report what happened. Do not stop early unless a tool fails. "
        "After the final cycle, summarize the run."
    )


def _session_files(session_dir: Path) -> list[Path]:
    return sorted(session_dir.rglob("*.jsonl")) if session_dir.exists() else []


def _file_metrics(session_dir: Path) -> dict[str, int]:
    files = _session_files(session_dir)
    return {
        "sessionFiles": len(files),
        "sessionBytes": sum(p.stat().st_size for p in files if p.is_file()),
        "sessionLines": sum(p.read_text(encoding="utf-8").count("\n") for p in files if p.is_file()),
    }


def _request(req_id: str, request_type: str, **payload: Any) -> bytes:
    return (json.dumps({"id": req_id, "type": request_type, **payload}, ensure_ascii=False) + "\n").encode()


async def _read_line(process: asyncio.subprocess.Process, timeout: float) -> dict[str, Any] | None:
    if process.stdout is None:
        return None
    try:
        raw = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
    except TimeoutError:
        return None
    if not raw:
        return None
    try:
        value = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {"type": "stdout_text", "raw": raw.decode("utf-8", errors="replace")[:500]}
    return value if isinstance(value, dict) else {"type": "non_object_output"}


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(__file__).resolve().parents[1]
    if args.session_dir:
        session_dir = Path(args.session_dir).expanduser().resolve()
        session_dir.mkdir(parents=True, exist_ok=True)
        disposable = False
    else:
        session_dir = Path(tempfile.mkdtemp(prefix="one-long-session-"))
        disposable = True

    prompt = Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else args.prompt
    if not prompt:
        prompt = _default_prompt(args.cycles)

    command = [sys.executable, "-m", "one.cli.main", "--mode", "rpc", "--session-dir", str(session_dir)]
    if args.model:
        command.extend(["--model", args.model])
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(repo),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    process.stdin.write(_request("prompt", "prompt", message=prompt))
    await process.stdin.drain()

    counts: Counter[str] = Counter()
    event_times: list[dict[str, Any]] = []
    malformed: list[str] = []
    agent_finished = False
    deadline = started + args.timeout

    while time.monotonic() < deadline:
        item = await _read_line(process, max(0.1, deadline - time.monotonic()))
        if item is None:
            break
        event_type = str(item.get("type") or "unknown")
        counts[event_type] += 1
        if event_type == "stdout_text":
            malformed.append(str(item.get("raw", "")))
        if event_type in {
            "agent_start",
            "turn_start",
            "tool_call_start",
            "tool_call_end",
            "compaction_start",
            "compaction_end",
            "turn_end",
            "agent_end",
        }:
            event_times.append({"type": event_type, "elapsedSec": round(time.monotonic() - started, 3)})
        if event_type == "agent_end":
            agent_finished = True
            break

    responses: dict[str, dict[str, Any]] = {}
    if agent_finished:
        for req_id, req_type in (("stats", "get_session_stats"), ("context", "get_context_usage")):
            process.stdin.write(_request(req_id, req_type))
            await process.stdin.drain()
        response_deadline = time.monotonic() + 10
        while len(responses) < 2 and time.monotonic() < response_deadline:
            item = await _read_line(process, max(0.1, response_deadline - time.monotonic()))
            if item is None:
                break
            if item.get("type") == "response" and item.get("id") in {"stats", "context"}:
                responses[str(item["id"])] = item

    if process.stdin is not None:
        process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            process.kill()
            await process.wait()

    elapsed = time.monotonic() - started
    metrics = _file_metrics(session_dir)
    stats = responses.get("stats", {}).get("data") or {}
    context = responses.get("context", {}).get("data") or {}
    return {
        "completed": agent_finished,
        "elapsedSec": round(elapsed, 3),
        "timeoutSec": args.timeout,
        "model": args.model or "configured default",
        "sessionDir": str(session_dir),
        "promptChars": len(prompt),
        "events": dict(counts),
        "milestones": event_times,
        "session": metrics,
        "sessionStats": stats,
        "contextUsage": context.get("contextUsage"),
        "stdoutText": malformed,
        "disposable": disposable,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--prompt", help="Prompt to run against the configured model")
    source.add_argument("--prompt-file", help="Read the prompt from a UTF-8 file")
    parser.add_argument("--cycles", type=int, default=30, help="Cycles used by the default diagnostic prompt")
    parser.add_argument("--model", help="Optional provider/model override; otherwise use the configured default")
    parser.add_argument("--session-dir", help="Directory for the diagnostic session; default is a new /tmp directory")
    parser.add_argument("--timeout", type=float, default=1800, help="Maximum run time in seconds (default: 1800)")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.cycles < 1 or args.timeout <= 0:
        print("--cycles must be positive and --timeout must be greater than zero", file=sys.stderr)
        return 2
    report = asyncio.run(_run(args))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("one long-session diagnostic")
        print(f"completed: {report['completed']}")
        print(f"elapsed: {report['elapsedSec']}s")
        print(f"model: {report['model']}")
        print(f"session directory: {report['sessionDir']}")
        print(f"session files/lines/bytes: {report['session'].get('sessionFiles', 0)}/"
              f"{report['session'].get('sessionLines', 0)}/"
              f"{report['session'].get('sessionBytes', 0)}")
        print(f"context usage: {json.dumps(report.get('contextUsage'), ensure_ascii=False)}")
        print(f"events: {json.dumps(report['events'], ensure_ascii=False, sort_keys=True)}")
        if report["stdoutText"]:
            print(f"non-event stdout lines: {len(report['stdoutText'])}")
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
