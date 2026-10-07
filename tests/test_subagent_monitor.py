"""Tests for subagent monitor."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one

from __future__ import annotations

import threading

from one.core.subagent_monitor import MAX_COMPLETED_CHILDREN, MAX_OUTPUT_BYTES, MAX_TIMELINE_ENTRIES, SubagentMonitor


def test_snapshot_is_copied_and_notifications_are_identity_only() -> None:
    monitor = SubagentMonitor()
    notices: list[dict[str, object]] = []
    off = monitor.subscribe(notices.append)
    monitor.register("child", "task", "model", started_at=1.0)
    monitor.update("child", text="visible", tool="bash", activity="using tool")
    snapshot = monitor.snapshot()
    snapshot["children"][0]["outputTail"] = "mutated"  # type: ignore[index]

    fresh = monitor.snapshot()
    assert fresh["children"][0]["outputTail"] == "visible"  # type: ignore[index]
    assert all(set(notice) == {"type", "sessionId", "revision"} for notice in notices)
    off()
    monitor.complete("child", "completed")
    assert len(notices) == 2


def test_output_is_utf8_bounded_sanitized_and_redacts_split_secret() -> None:
    monitor = SubagentMonitor()
    monitor.register("child", "task\x1b[31m", "model")
    monitor.update("child", text="Bearer ")
    monitor.update("child", text="very-secret-token")
    monitor.update("child", text="\x1b[2J\n" + "ż" * (MAX_OUTPUT_BYTES + 100))
    child = monitor.snapshot()["children"][0]  # type: ignore[index]
    assert len(child["outputTail"].encode()) <= MAX_OUTPUT_BYTES
    assert child["outputTruncated"] is True
    assert "very-secret-token" not in child["outputTail"]
    assert "\x1b" not in child["task"]


def test_retains_active_children_and_evicts_oldest_completed_by_completion_time(monkeypatch) -> None:
    monitor = SubagentMonitor()
    monitor.register("active", "task", "model")
    for number in range(MAX_COMPLETED_CHILDREN + 1):
        monitor.register(f"done-{number}", "task", "model")
    times = iter(range(MAX_COMPLETED_CHILDREN + 1))
    monkeypatch.setattr("one.core.subagent_monitor.time.monotonic", lambda: next(times))
    for number in reversed(range(MAX_COMPLETED_CHILDREN + 1)):
        child = f"done-{number}"
        monitor.complete(child, "completed")
    children = monitor.snapshot()["children"]  # type: ignore[index]
    ids = {child["sessionId"] for child in children}
    assert "active" in ids
    assert "done-20" not in ids
    assert "done-0" in ids
    assert len(children) == MAX_COMPLETED_CHILDREN + 1


def test_parallel_updates_are_isolated() -> None:
    monitor = SubagentMonitor()
    monitor.register("one", "one", "model")
    monitor.register("two", "two", "model")

    def write(session_id: str, text: str) -> None:
        for _ in range(100):
            monitor.update(session_id, text=text)

    threads = [threading.Thread(target=write, args=("one", "A")), threading.Thread(target=write, args=("two", "B"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    children = {child["sessionId"]: child for child in monitor.snapshot()["children"]}  # type: ignore[index]
    assert set(children["one"]["outputTail"]) == {"A"}
    assert set(children["two"]["outputTail"]) == {"B"}


def test_mixed_timeline_preserves_response_tool_response_order() -> None:
    monitor = SubagentMonitor()
    monitor.register("child", "task", "model")
    monitor.update("child", text="Before ")
    monitor.update("child", text="read")
    monitor.tool_started("child", "read", tool_call_id="read-1", args={"path": "notes.txt", "offset": 2, "limit": 4})
    monitor.tool_finished("child", "read", True, 3, "file contents", tool_call_id="read-1")
    monitor.update("child", text="After read")

    child = monitor.snapshot()["children"][0]  # type: ignore[index]
    assert child["timeline"] == [
        {"kind": "response", "text": "Before read"},
        {"kind": "tool", "tool": "read", "toolCallId": "read-1", "args": "path=notes.txt, offset=2, limit=4", "effectiveTimeout": None,
         "state": "completed", "ok": True, "elapsedMs": 3, "output": "file contents"},
        {"kind": "response", "text": "After read"},
    ]
    assert child["outputTail"] == "Before readAfter read"


def test_adjacent_response_deltas_do_not_consume_timeline_entry_cap() -> None:
    monitor = SubagentMonitor()
    monitor.register("child", "task", "model")
    for _ in range(MAX_TIMELINE_ENTRIES * 2):
        monitor.update("child", text="x")

    child = monitor.snapshot()["children"][0]  # type: ignore[index]
    assert child["timeline"] == [{"kind": "response", "text": "x" * (MAX_TIMELINE_ENTRIES * 2)}]
    assert child["timelineTruncated"] is False


def test_observe_final_reconciles_streamed_text_and_summary_newline() -> None:
    monitor = SubagentMonitor()
    monitor.register("child", "task", "model")
    monitor.update("child", text="streamed prose\n")

    monitor.observe_final("child", "streamed prose")

    assert monitor.snapshot()["children"][0]["outputTail"] == "streamed prose\n"  # type: ignore[index]


def test_observe_final_appends_only_unshared_summary_content_once() -> None:
    monitor = SubagentMonitor()
    monitor.register("child", "task", "model")
    monitor.update("child", text="The final ")

    monitor.observe_final("child", "final answer")
    monitor.observe_final("child", "final answer")

    child = monitor.snapshot()["children"][0]  # type: ignore[index]
    assert child["outputTail"] == "The final answer"
    assert child["timeline"] == [{"kind": "response", "text": "The final answer"}]


def test_observe_final_replaces_trailing_line_break_for_partial_overlap_once() -> None:
    monitor = SubagentMonitor()
    monitor.register("child", "task", "model")
    monitor.update("child", text="The final \n")

    monitor.observe_final("child", "final answer")
    monitor.observe_final("child", "final answer")

    assert monitor.snapshot()["children"][0]["outputTail"] == "The final answer"  # type: ignore[index]


def test_preview_budget_is_aggregate_across_response_and_tool_output() -> None:
    monitor = SubagentMonitor()
    monitor.register("child", "task", "model")
    monitor.update("child", text="a" * (MAX_OUTPUT_BYTES // 2 + 100))
    monitor.tool_started("child", "read")
    monitor.tool_finished("child", "read", True, 1, "b" * (MAX_OUTPUT_BYTES // 2 + 100))
    monitor.update("child", text="latest")

    child = monitor.snapshot()["children"][0]  # type: ignore[index]
    visible = sum(
        len(str(item.get("text") if item.get("kind") == "response" else item.get("output") or "").encode())
        for item in child["timeline"]
    )
    assert visible <= MAX_OUTPUT_BYTES
    assert child["previewTruncated"] is True
    assert child["timeline"][-1] == {"kind": "response", "text": "latest"}


def test_tool_timeline_is_bounded_copied_and_allowlisted() -> None:
    monitor = SubagentMonitor()
    notices: list[dict[str, object]] = []
    monitor.subscribe(notices.append)
    monitor.register("child", "task", "model")
    for number in range(MAX_TIMELINE_ENTRIES + 3):
        monitor.tool_started("child", "read")
        monitor.tool_finished("child", "read", number % 2 == 0, number, "Bearer secret-token\x1b[2J")

    child = monitor.snapshot()["children"][0]  # type: ignore[index]
    assert len(child["timeline"]) == MAX_TIMELINE_ENTRIES
    assert child["timelineTruncated"] is True
    assert child["currentTool"] is None
    assert child["activity"] == "responding"
    assert sum(len(item.get("tool", "").encode()) + len(item.get("output", "").encode()) for item in child["timeline"]) + len(child["outputTail"].encode()) <= MAX_OUTPUT_BYTES
    assert all(set(item) <= {"kind", "tool", "toolCallId", "args", "effectiveTimeout", "state", "ok", "elapsedMs", "output"} for item in child["timeline"])
    assert all("secret-token" not in str(item) and "\x1b" not in str(item) for item in child["timeline"])
    child["timeline"][0]["tool"] = "mutated"
    assert monitor.snapshot()["children"][0]["timeline"][0]["tool"] != "mutated"  # type: ignore[index]
    assert all(set(notice) == {"type", "sessionId", "revision"} for notice in notices)


def test_visible_tool_result_allowlists_main_tui_fields() -> None:
    event = {
        "ok": True,
        "result": {
            "outputText": "visible Bearer secret-token",
            "result": "fallback",
            "args": {"token": "must-not-appear"},
            "rawResult": {"evidence": "must-not-appear"},
            "content": [{"image": "must-not-appear"}],
            "details": "must-not-appear",
            "path": "/must-not-appear",
        },
    }
    assert "secret-token" not in SubagentMonitor.visible_tool_result(event)
    assert "must-not-appear" not in SubagentMonitor.visible_tool_result(event)
    assert SubagentMonitor.visible_tool_result({"ok": False, "result": {"error": "failed"}}) == "failed"


def test_tool_call_ids_update_one_row_and_repeated_names_pair_correctly() -> None:
    monitor = SubagentMonitor()
    monitor.register("child", "task", "model")
    monitor.tool_started("child", "bash", tool_call_id="one", args={"command": "first"}, effective_timeout=120)
    monitor.tool_started("child", "bash", tool_call_id="two", args={"command": "second"})
    assert len(monitor.snapshot()["children"][0]["timeline"]) == 2  # type: ignore[index]
    monitor.tool_finished("child", "bash", True, 228, "done", tool_call_id="two")
    monitor.tool_finished("child", "bash", False, 4, "failed", tool_call_id="one")
    timeline = monitor.snapshot()["children"][0]["timeline"]  # type: ignore[index]
    assert len(timeline) == 2
    assert timeline[0]["elapsedMs"] == 4 and timeline[0]["output"] == "failed"
    assert timeline[1]["elapsedMs"] == 228 and timeline[1]["output"] == "done"


def test_display_tool_args_is_bounded_redacted_and_omits_write_content() -> None:
    args = SubagentMonitor.display_tool_args({
        "command": "printf hello", "path": "file.txt", "password": "nope", "content": "body" * 1000,
        "payload": {"image": "nope"}, "evidenceData": "nope", "authorization": "Bearer nope",
    })
    assert "command=printf hello" in args and "path=file.txt" in args
    assert "nope" not in args and "bodybody" not in args
    assert "content=<4000 chars>" in args and "payload=<content omitted>" in args and "evidenceData=<4 chars>" in args
    assert len(args) <= 1024


def test_display_tool_args_normalizes_exact_sensitive_and_content_aliases() -> None:
    args = SubagentMonitor.display_tool_args({
        "max_token": 128,
        "image_count": 2,
        "text_length": 42,
        "blob_size": 1024,
        "apiKey": "api-secret",
        "access-token": "access-secret",
        "rawResult": {"private": "result"},
        "patchText": "patch body",
        "content": "body",
    })

    assert "max_token=128" in args
    assert "image_count=2" in args
    assert "text_length=42" in args
    assert "blob_size=1024" in args
    assert "apiKey=[REDACTED]" in args
    assert "access-token=[REDACTED]" in args
    assert "rawResult=<content omitted>" in args
    assert "patchText=<10 chars>" in args
    assert "content=<4 chars>" in args
    assert "api-secret" not in args and "access-secret" not in args and "patch body" not in args
