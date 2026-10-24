# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from one.core.session_manager import SessionManager
from one.core.subagent_history import PAGE_CHARS, SubagentHistory
from one.core.subagent_monitor import SubagentMonitor
from one.core.tui_runtime_host import TuiRuntimeHost


def read_all(history: SubagentHistory, child: str) -> str:
    cursor, part = 0, 0
    pieces = []
    for _ in range(1000):
        page = history.page(child, cursor, part)
        assert len(json.dumps(page).encode()) < 192 * 1024
        pieces.extend(entry["text"] for entry in page["entries"])
        if not page["hasMore"]:
            return "\n".join(pieces)
        assert (page["cursor"], page["part"]) != (cursor, part)
        cursor, part = page["cursor"], page["part"]
    raise AssertionError("History cursor did not terminate")


def test_history_survives_preview_eviction_and_resume(tmp_path: Path) -> None:
    path = str(tmp_path / "parent.jsonl")
    monitor = SubagentMonitor(path)
    monitor.register("child", "task", "model")
    for index in range(100):
        monitor.update("child", text=f"before-{index}\n")
        monitor.tool_started("child", "read", tool_call_id=str(index), args={"path": str(index)})
        monitor.tool_finished("child", "read", True, index, f"result-{index}\n" + "ż" * 1000,
                              tool_call_id=str(index))
    monitor.observe_final("child", "Final summary")
    monitor.complete("child", "completed")
    assert monitor.snapshot()["children"][0]["timelineTruncated"]
    text = read_all(monitor.history, "child")
    assert text.index("before-0") < text.index("result-0") < text.index("before-99")
    assert text.count("Final summary") == 1
    monitor.history.close()
    restored = SubagentMonitor(path)
    assert restored.snapshot()["children"][0]["status"] == "completed"
    assert read_all(restored.history, "child") == text
    assert restored.history.path.stat().st_mode & 0o777 == 0o600
    restored.history.close()


def test_preset_snapshot_survives_history_reload_without_changing_legacy_records(tmp_path: Path) -> None:
    path = str(tmp_path / "parent.jsonl")
    monitor = SubagentMonitor(path)
    monitor.register("legacy", "task", "provider/parent")
    monitor.register("preset", "task", "provider/override", preset_id="review", preset_name="Review")
    monitor.complete("legacy", "completed")
    monitor.complete("preset", "completed")
    monitor.history.close()

    restored = SubagentMonitor(path)
    children = {child["sessionId"]: child for child in restored.list_children()["children"]}
    assert "presetId" not in children["legacy"] and "presetName" not in children["legacy"]
    assert children["preset"]["presetId"] == "review"
    assert children["preset"]["presetName"] == "Review"
    assert children["preset"]["model"] == "provider/override"
    restored.history.close()


def test_large_unicode_response_is_paged_and_live_cursor_continues() -> None:
    monitor = SubagentMonitor()
    monitor.register("child", "task", "model")
    original = "żółć" * 20_000
    monitor.update("child", text=original)
    page = monitor.history.page("child", 0, 0)
    assert max(len(entry["text"]) for entry in page["entries"]) <= PAGE_CHARS
    text = read_all(monitor.history, "child").replace("\n", "")
    assert original in text
    tail = monitor.history.page("child", 0, 1)
    assert original[-100:] in "".join(entry["text"] for entry in tail["entries"])
    while tail["hasMore"]:
        tail = monitor.history.page("child", tail["cursor"], tail["part"])
    monitor.update("child", text="LIVE NEW")
    continuation = monitor.history.page("child", tail["cursor"], tail["part"])
    assert "LIVE NEW" in str(continuation)
    monitor.history.close()


def test_history_redacts_split_credentials_and_omits_raw_content() -> None:
    monitor = SubagentMonitor()
    monitor.register("child", "Bearer task-secret", "model")
    monitor.update("child", text="Bearer ")
    monitor.update("child", text="very-secret-token")
    monitor.tool_started("child", "write", args={"path": "file", "content": "private-body", "apiKey": "secret-key"})
    text = read_all(monitor.history, "child")
    assert "very-secret-token" not in text
    assert "private-body" not in text
    assert "secret-key" not in text
    assert "task-secret" not in str(monitor.list_children())
    monitor.history.close()


def test_more_than_twenty_completed_children_remain_accessible() -> None:
    monitor = SubagentMonitor()
    for index in range(45):
        monitor.register(str(index), f"task {index}", "model")
        monitor.complete(str(index), "completed")
    ids = [child["sessionId"] for offset in (0, 20, 40) for child in monitor.list_children(offset)["children"]]
    assert ids == [str(index) for index in range(45)]
    assert "Completed" in read_all(monitor.history, "0")
    monitor.history.close()


def test_resume_marks_unfinished_children_interrupted(tmp_path: Path) -> None:
    path = str(tmp_path / "parent.jsonl")
    monitor = SubagentMonitor(path)
    monitor.register("child", "task", "model")
    monitor.history.close()
    restored = SubagentMonitor(path)
    child = restored.list_children()["children"][0]
    assert child["status"] == "interrupted"
    assert child["startedMonotonic"] is None
    restored.history.close()


@pytest.mark.parametrize("cursor,part", [(-1, 0), (0, -1), (True, 0), ("0", 0)])
def test_history_rejects_invalid_cursors(cursor: object, part: object) -> None:
    history = SubagentHistory()
    with pytest.raises(ValueError):
        history.page("child", cursor, part)
    history.close()


def test_sidecar_cleanup_and_symlink_rejection(tmp_path: Path) -> None:
    manager = SessionManager.create(str(tmp_path), str(tmp_path))
    manager._rewrite()
    monitor = SubagentMonitor(manager.session_file)
    monitor.register("child", "task", "model")
    path = monitor.history.path
    monitor.history.close()
    SessionManager.delete(manager.session_file, str(tmp_path))
    assert not path.exists()
    target = tmp_path / "target"
    target.write_text("untouched")
    path.symlink_to(target)
    with pytest.raises(ValueError, match="sidecar"):
        SubagentHistory(str(path).removesuffix(".agents"))
    assert target.read_text() == "untouched"


@pytest.mark.asyncio
async def test_runtime_pages_are_epoch_bound_detached_and_parent_scoped() -> None:
    monitor = SubagentMonitor()
    monitor.register("child", "task", "model")
    monitor.update("child", text="visible")
    host = object.__new__(TuiRuntimeHost)
    host._epoch = 2
    host._session = lambda: SimpleNamespace(subagent_monitor=monitor)
    result = await host.dispatch("agent_history_page", {"session_id": "child", "cursor": 0, "part": 0, "epoch": 2})
    assert result["epoch"] == 2
    assert "visible" in str(result)
    result["entries"].clear()
    assert "visible" in str(monitor.history.page("child"))
    with pytest.raises(ValueError, match="Session changed"):
        await host.dispatch("agent_history_page", {"session_id": "child", "cursor": 0, "part": 0, "epoch": 1})
    with pytest.raises(ValueError, match="unavailable"):
        monitor.history.page("/arbitrary/path")
    listed = await host.dispatch("agents_page", {"offset": 0, "epoch": 2})
    assert listed["children"][0]["sessionId"] == "child"
    monitor.history.close()


def test_result_visibility_and_child_isolation() -> None:
    monitor = SubagentMonitor()
    monitor.register("one", "task", "model")
    monitor.register("two", "task", "model")
    monitor.tool_started("one", "read")
    monitor.tool_finished("one", "read", True, 1, "one-private-result")
    monitor.update("two", text="two-only")
    assert "one-private-result" in read_all(monitor.history, "one")
    hidden = monitor.history.page("one", show_results=False)
    assert "one-private-result" not in str(hidden)
    assert "two-only" not in read_all(monitor.history, "one")
    assert "one-private-result" not in read_all(monitor.history, "two")
    monitor.history.close()


def test_final_summary_after_tool_is_not_duplicated() -> None:
    monitor = SubagentMonitor()
    monitor.register("child", "task", "model")
    monitor.update("child", text="Before")
    monitor.tool_started("child", "read")
    monitor.tool_finished("child", "read", False, 1, "read failed")
    monitor.observe_final("child", "Final result")
    monitor.observe_final("child", "Final result")
    assert read_all(monitor.history, "child").count("Final result") == 1
    monitor.complete("child", "failed", error="timed out")
    assert "timed out" in read_all(monitor.history, "child")
    monitor.history.close()


def test_full_prompt_pages_survive_resume_and_legacy_fallback(tmp_path: Path) -> None:
    path = str(tmp_path / "parent.jsonl")
    prompt = "# Task\n\n" + "żółć code\n    indented\n" * 1000 + "END OF PROMPT"
    monitor = SubagentMonitor(path)
    monitor.register("child", prompt, "model")
    assert len(monitor.snapshot()["children"][0]["task"]) == 1024
    monitor.history.close()
    restored = SubagentMonitor(path)
    offset, pieces = 0, []
    while True:
        page = restored.history.prompt_page("child", offset)
        assert page["complete"]
        assert len(page["text"]) <= PAGE_CHARS
        pieces.append(page["text"])
        if not page["hasMore"]:
            break
        offset = page["nextOffset"]
    assert "".join(pieces) == prompt
    restored.history.metadata({"sessionId": "old", "task": "legacy summary"})
    old = restored.history.prompt_page("old")
    assert old["complete"] is False
    assert old["text"] == "legacy summary"
    with pytest.raises(ValueError):
        restored.history.prompt_page("child", -1)
    restored.history.close()


@pytest.mark.asyncio
async def test_runtime_prompt_page_is_epoch_bound_and_redacted() -> None:
    monitor = SubagentMonitor()
    monitor.register("child", "task Bearer secret-token\n" + "x" * 8000, "model")
    host = object.__new__(TuiRuntimeHost)
    host._epoch = 4
    host._session = lambda: SimpleNamespace(subagent_monitor=monitor)
    payload = {"session_id": "child", "offset": 0, "epoch": 4}
    result = await host.dispatch("agent_prompt_page", payload)
    assert result["complete"] and result["hasMore"]
    assert "secret-token" not in result["text"]
    with pytest.raises(ValueError, match="Session changed"):
        await host.dispatch("agent_prompt_page", {**payload, "epoch": 3})
    monitor.history.close()


def test_operation_updates_same_block_with_parallel_ids_and_timeout(tmp_path: Path) -> None:
    path = str(tmp_path / "parent.jsonl")
    monitor = SubagentMonitor(path)
    monitor.register("child", "task", "model")
    monitor.tool_started("child", "read", tool_call_id="one", args={"path": "one"})
    monitor.tool_started("child", "read", tool_call_id="two", args={"path": "two"})
    before = monitor.history.page("child")
    ids = [entry["id"] for entry in before["entries"] if entry["kind"] == "tool"]
    monitor.tool_finished("child", "read", False, 3, "failure-two", tool_call_id="two")
    monitor.tool_finished("child", "read", True, 4, "success-one", tool_call_id="one")
    after = monitor.history.page("child")
    operations = [entry for entry in after["entries"] if entry["kind"] == "tool"]
    assert [entry["id"] for entry in operations] == ids
    assert "✓" in operations[0]["text"] and "success-one" in operations[0]["text"]
    assert "×" in operations[1]["text"] and "failure-two" in operations[1]["text"]
    assert "○" not in str(after)
    hidden = monitor.history.page("child", show_results=False)
    assert "success-one" not in str(hidden) and "failure-two" not in str(hidden)
    monitor.tool_started("child", "bash", tool_call_id="timeout")
    monitor.complete("child", "failed", error="timeout")
    assert "○" not in read_all(monitor.history, "child")
    monitor.history.close()
    restored = SubagentMonitor(path)
    assert "○" not in read_all(restored.history, "child")
    restored.history.close()


def test_unified_inspector_prompt_then_thinking_and_operation() -> None:
    monitor = SubagentMonitor()
    monitor.register("child", "start " + "ż" * 13000 + " END", "model")
    monitor.thinking("child", "provider display summary")
    monitor.tool_started("child", "read", tool_call_id="one")
    monitor.tool_finished("child", "read", True, 1, "result", tool_call_id="one")
    cursor, part = 0, 0
    entries = []
    for _ in range(20):
        page = monitor.history.inspector_page("child", cursor, part)
        entries.extend(page["entries"])
        if not page["hasMore"]:
            break
        cursor, part = page["cursor"], page["part"]
    prompt = "".join(entry["text"] for entry in entries if entry["kind"] == "prompt")
    assert prompt.endswith(" END") and len(prompt) > 13000
    kinds = [entry["kind"] for entry in entries]
    assert kinds.index("prompt") < kinds.index("thinking") < kinds.index("tool")
    assert "provider display summary" in str(entries)
    monitor.history.close()
