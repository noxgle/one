"""Regression checks for limitations blocking prompt lifecycle integration."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import pytest

from one.core.checkpoints import CheckpointError, CheckpointManager


def test_empty_task_does_not_shadow_previous_undoable_task(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = CheckpointManager(tmp_path / "storage", workspace, "session")
    try:
        path = workspace / "file"
        path.write_text("before")
        manager.begin_task("first")
        manager.capture_preimage(["file"])
        path.write_text("after")
        manager.record_postimage()
        manager.end_task()
        manager.begin_task("empty")
        manager.end_task()

        assert manager.list_checkpoints()[-1] == {"id": "empty", "status": "complete", "paths": []}
        token = manager.prepare_undo("first", ["file"])["token"]
        assert manager.confirm_undo(token) == ["file"]
        assert path.read_text() == "before"
        assert manager.list_checkpoints()[0]["status"] == "undone"
    finally:
        manager.close()


def test_reopened_manager_rejects_reused_prompt_turn_id(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage = tmp_path / "storage"
    manager = CheckpointManager(storage, workspace, "session")
    try:
        manager.begin_task("turn-1")
        manager.end_task()
    finally:
        manager.close()

    reopened = CheckpointManager(storage, workspace, "session")
    try:
        with pytest.raises(CheckpointError, match="duplicate task ID"):
            reopened.begin_task("turn-1")
    finally:
        reopened.close()


def test_incomplete_empty_task_still_blocks_previous_undo(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = CheckpointManager(tmp_path / "storage", workspace, "session")
    try:
        path = workspace / "file"
        path.write_text("before")
        manager.begin_task("first")
        manager.capture_preimage(["file"])
        path.write_text("after")
        manager.record_postimage()
        manager.end_task()
        manager.begin_task("incomplete")
        manager.fail_mutation()
        manager.end_task()
        with pytest.raises(CheckpointError, match="latest task"):
            manager.prepare_undo("first", ["file"])
    finally:
        manager.close()
