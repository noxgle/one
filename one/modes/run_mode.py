from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _ask_user_queue(session: Any) -> asyncio.Queue:
    """Queue of ask_user events emitted by the session."""
    queue: asyncio.Queue = asyncio.Queue()

    def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "ask_user":
            queue.put_nowait(event)

    session.subscribe(on_event)
    return queue


async def _poll_answer_file(path: Path, question: str) -> str:
    """Write the question to the file, then poll until the content changes."""
    path.write_text(question, encoding="utf-8")
    while True:
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            content = ""
        if content and content != question:
            return content
        await asyncio.sleep(0.5)


async def _answer_loop(session: Any, answer_file: str) -> None:
    """Headless answer channel: question -> file, answer <- file (polled)."""
    path = Path(answer_file)
    queue = _ask_user_queue(session)
    while True:
        event = await queue.get()
        qid = str(event.get("id") or "")
        try:
            answer = await _poll_answer_file(path, str(event.get("question") or ""))
            session.answer_question(qid, answer)
        except Exception:
            try:
                session.answer_question(qid, "(answer channel error)")
            except Exception:
                pass


async def _canned_answer_loop(session: Any) -> None:
    """No answer channel configured: answer deterministically so the agent
    proceeds with best judgment instead of hanging."""
    queue = _ask_user_queue(session)
    while True:
        event = await queue.get()
        try:
            session.answer_question(
                str(event.get("id") or ""),
                "No answer channel configured (use --answer-file); proceed with best judgment.",
            )
        except Exception:
            pass


async def _steer_loop(session: Any, steer_file: str) -> None:
    """Headless steering channel: poll the file; any non-empty content is
    injected as a steering message, then the file is cleared so the next
    message can be detected."""
    path = Path(steer_file)
    while True:
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            content = ""
        if content:
            try:
                await session.steer(content)
            except Exception:
                pass
            try:
                path.write_text("", encoding="utf-8")
            except OSError:
                pass
        await asyncio.sleep(0.5)


async def run_run_mode(runtime_host: Any, options: dict[str, Any]) -> int:
    """Headless task mode: run the task to `finish`, print the result contract.

    Exit codes: 0 = finished with goal_success; 1 = finished with failure or
    not finished (step limit / error / abort); 2 = usage error.
    """
    session = runtime_host.session
    task = str(options.get("task") or "").strip()
    resume = bool(options.get("resume"))
    answer_file = str(options.get("answer_file") or "").strip() or None
    steer_file = str(options.get("steer_file") or "").strip() or None

    if resume and not task:
        task = session.get_last_user_text() or ""
        if not task:
            print("No previous user message to resume.")
            return 1
    if not task:
        print("Usage: one run <task>")
        return 2

    steer_task = asyncio.create_task(_steer_loop(session, steer_file)) if steer_file else None
    answer_task = asyncio.create_task(_answer_loop(session, answer_file) if answer_file else _canned_answer_loop(session))
    try:
        await session.prompt(task)
    finally:
        if steer_task is not None:
            steer_task.cancel()
            try:
                await steer_task
            except asyncio.CancelledError:
                pass
        answer_task.cancel()
        try:
            await answer_task
        except asyncio.CancelledError:
            pass

    result = session.get_last_finish_result()
    summary = result["summary"] or session.get_last_assistant_text() or "(no output)"

    if options.get("json"):
        print(
            json.dumps(
                {
                    "summary": summary,
                    "goalSuccess": result["goalSuccess"],
                    "finished": result["finished"],
                },
                ensure_ascii=False,
            )
        )
    else:
        print(summary)

    # Write end-of-task report to reports.jsonl in the agent dir.
    report_path = None
    agent_dir = str(options.get("agentDir") or "").strip()
    if agent_dir:
        try:
            report_dir = Path(agent_dir)
            report_dir.mkdir(parents=True, exist_ok=True)
            report_path = report_dir / "reports.jsonl"
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "task": task,
                "summary": summary,
                "goalSuccess": result["goalSuccess"],
                "finished": result["finished"],
                "exitCode": 0 if result["finished"] and result["goalSuccess"] else 1,
                "sessionId": getattr(session, "session_id", None),
                "sessionFile": getattr(session, "session_file", None),
                "stats": session.get_session_stats() if hasattr(session, "get_session_stats") else None,
            }
            with report_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            report_path = None
    if report_path is not None and not options.get("json"):
        print(f"[report] {report_path}")

    if result["finished"]:
        return 0 if result["goalSuccess"] else 1
    return 1
