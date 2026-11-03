"""Versioned, detached run snapshots stored beside a persistent session.

This journal does not restore execution handles or mutate a live RunContext.
Callers supply JSON-safe snapshots containing no secrets or monotonic timestamps.
"""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from one.core.persistence import append_private_text
from one.core.run_context import SAFE_RUN_REASONS, NodeState

RUN_HISTORY_VERSION = 1
_NONTERMINAL = {NodeState.PENDING, NodeState.RUNNING, NodeState.CANCELLING}
_STATES = {state.value for state in NodeState}


class RunHistory:
    """Append-only snapshots; the last complete record for each run wins.

    Reads are historical views: pending, running and cancelling nodes become
    interrupted in the returned data, never in the journal or a live context.
    One writer per session is required, as with session persistence.
    """

    def __init__(self, session_file: str | Path, session_id: str) -> None:
        if not session_id:
            raise ValueError("session_id must not be empty")
        self.path = Path(str(session_file) + ".runs")
        self.session_id = session_id

    def _check_path(self) -> None:
        if self.path.is_symlink():
            raise ValueError("Invalid run history sidecar")

    def append(self, snapshot: dict[str, Any]) -> None:
        """Persist a detached snapshot with runId, nodes and optional aggregates."""
        self._check_path()
        if not self._valid_snapshot(snapshot):
            raise ValueError("Invalid run snapshot")
        record = {"type": "run_snapshot", "version": RUN_HISTORY_VERSION,
                  "sessionId": self.session_id, "snapshot": snapshot}
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        # Separate a crash-truncated final record from the next valid record.
        prefix = ""
        try:
            with self.path.open("rb") as stream:
                stream.seek(0, 2)
                if stream.tell():
                    stream.seek(-1, 2)
                    if stream.read(1) != b"\n":
                        prefix = "\n"
        except FileNotFoundError:
            pass
        append_private_text(self.path, prefix + encoded + "\n")

    @staticmethod
    def _valid_snapshot(snapshot: Any) -> bool:
        return (isinstance(snapshot, dict) and isinstance(snapshot.get("runId"), str)
                and bool(snapshot["runId"]) and isinstance(snapshot.get("nodes"), list)
                and bool(snapshot["nodes"]) and all(
                    isinstance(node, dict) and isinstance(node.get("state"), str)
                    and node["state"] in _STATES for node in snapshot["nodes"]
                ))

    def read(self) -> list[dict[str, Any]]:
        """Return latest complete snapshots, ignoring malformed/truncated records."""
        self._check_path()
        try:
            stream = self.path.open("rb")
        except FileNotFoundError:
            return []
        runs: dict[str, dict[str, Any]] = {}
        with stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    break
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict) or record.get("type") != "run_snapshot":
                    continue
                if record.get("sessionId") != self.session_id:
                    continue
                if type(record.get("version")) is not int or record["version"] != RUN_HISTORY_VERSION:
                    continue
                snapshot = record.get("snapshot")
                if not self._valid_snapshot(snapshot):
                    continue
                interrupted = False
                for node in snapshot["nodes"]:
                    if node["state"] in _NONTERMINAL:
                        node["state"] = NodeState.INTERRUPTED.value
                        node["reason"] = "runtime_shutdown"
                        interrupted = True
                    elif "reason" in node and (not isinstance(node["reason"], str) or node["reason"] not in SAFE_RUN_REASONS):
                        node["reason"] = None
                    if node["state"] == NodeState.INTERRUPTED:
                        interrupted = True
                if interrupted:
                    snapshot["historical"] = True
                    snapshot["externalState"] = "unknown"
                runs[snapshot["runId"]] = snapshot
        return list(runs.values())
