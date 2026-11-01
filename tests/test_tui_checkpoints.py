# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import pytest

from one.modes.tui_mode import TuiSessionView, _OneTextualApp
from tests.support.tui import _mk_app_session


class Facade:
    def __init__(self):
        self.calls = []
        self.view = None
        self.stale = False

    async def command(self, name, payload):
        self.calls.append((name, payload))
        if self.stale:
            self.view._snapshot["epoch"] += 1
        if payload["action"] == "prepare":
            return {"epoch": 0, "token": "abc", "paths": [{"path": "a b", "action": "restore"}], "warning": "Careful"}
        return {"epoch": 0, "enabled": True, "checkpoints": [{"id": "task", "status": "complete", "paths": ["a"]}]}


@pytest.mark.asyncio
async def test_checkpoint_commands(tmp_path):
    facade = Facade()
    view = TuiSessionView(facade)
    facade.view = view
    app = _OneTextualApp(view)
    output = []
    app._write = lambda text, *args: output.append(text)
    await app._checkpoints_command('/checkpoints undo task "a b"')
    assert facade.calls == [("checkpoints", {"action": "prepare", "epoch": 0, "id": "task", "paths": ["a b"]})]
    assert "restore: a b" in "\n".join(output)
    assert "Careful" in output
    assert "/checkpoints confirm abc" in "\n".join(output)
    for command, action in [("confirm abc", "confirm"), ("cancel", "cancel"), ("delete task", "delete"),
                            ("enable", "enable"), ("list", "list")]:
        await app._checkpoints_command("/checkpoints " + command)
        assert facade.calls[-1][1]["action"] == action
    assert "task (complete): a" in output
    count = len(facade.calls)
    await app._checkpoints_command("/checkpoints confirm")
    assert len(facade.calls) == count
    facade.stale = True
    await app._checkpoints_command("/checkpoints undo task")
    assert "stale" in output[-1]


@pytest.mark.asyncio
async def test_checkpoint_legacy_unavailable(tmp_path):
    app = _OneTextualApp(_mk_app_session(tmp_path))
    output = []
    app._write = lambda text, *args: output.append(text)
    await app._checkpoints_command("/checkpoints list")
    assert "detached TUI runtime" in output[-1]
