# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from one.core.agent_runtime_facade import AgentRuntimeFacade
from one.core.run_context import NodeState, RunContext, RunLimits
from one.core.threaded_agent_runtime import RuntimeBootstrapSpec
from one.core.tui_runtime_host import TUI_COMMANDS, TuiRuntimeHost


@pytest.mark.asyncio
async def test_run_commands_cross_owner_loop_and_reject_stale_targets(tmp_path) -> None:
    async def factory(_spec):
        host = object.__new__(TuiRuntimeHost)
        host._epoch = 2
        context = RunContext("root", RunLimits())
        context.register(context.root_id, "child")
        context.register(context.root_id, "other")
        host._host = SimpleNamespace(session=SimpleNamespace(_run_context=context))
        return SimpleNamespace(dispatch=host.dispatch)

    facade = AgentRuntimeFacade(
        RuntimeBootstrapSpec(cwd=str(tmp_path), agent_dir=str(tmp_path)), factory,
        command_allowlist=TUI_COMMANDS,
    )
    await facade.start()
    try:
        page = (await facade.run_tree_page(2))["result"]
        run_id = page["runId"]
        child = page["nodes"][1]["nodeId"]
        assert page["epoch"] == 2 and page["revision"] > 0
        for epoch, run, node in [(1, run_id, child), (2, "old-run", child), (2, run_id, "foreign")]:
            result = (await facade.cancel_run_subtree(epoch, run, node))["result"]
            assert not result["accepted"]
        assert (await facade.run_tree_page(2))["result"]["nodes"][1]["state"] == "pending"
        assert (await facade.cancel_run_subtree(2, run_id, child))["result"]["accepted"]
        nodes = (await facade.run_tree_page(2))["result"]["nodes"]
        assert nodes[1]["state"] == "cancelling" and nodes[2]["state"] == "pending"
        assert (await facade.cancel_run(2, run_id))["result"]["accepted"]
    finally:
        await facade.close()
        assert facade.finalize()


@pytest.mark.asyncio
async def test_wide_tree_pagination_and_session_rebind() -> None:
    host = object.__new__(TuiRuntimeHost)
    host._epoch = 4
    ctx = RunContext("root", RunLimits())
    for i in range(120):
        node = ctx.register(ctx.root_id, "界" * 1024, node_id=f"node-{i}")
        if i == 0:
            ctx.cancel_subtree(node.node_id, "authorization: Bearer " + "secret-value")
    host._host = SimpleNamespace(session=SimpleNamespace(_run_context=ctx))
    page = await host.dispatch("run_tree_page", {"epoch": 4, "offset": 0, "limit": 100})
    assert len(json.dumps(page, ensure_ascii=False).encode()) < 48 * 1024
    assert page["nextOffset"] is not None and "secret-value" not in json.dumps(page)
    old_run = page["runId"]
    replacement = RunContext("new", RunLimits())
    host._host.session = SimpleNamespace(_run_context=replacement)
    host._epoch = 5
    for epoch, run in [(4, old_run), (5, old_run)]:
        result = await host.dispatch("cancel_run", {"epoch": epoch, "run_id": run})
        assert not result["accepted"]
    assert replacement.node(replacement.root_id).state == NodeState.PENDING
