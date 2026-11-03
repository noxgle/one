# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from one.core.run_context import MAX_TREE_PAGE_SIZE, RunContext, RunLimits


@pytest.mark.asyncio
async def test_tree_snapshot_accounting_pagination_and_detachment() -> None:
    ctx = RunContext("root", RunLimits(max_tokens=1000, max_cost_usd=Decimal("10")))
    child = ctx.register(ctx.root_id, "child")
    grandchild = ctx.register(child.node_id, "grandchild")
    ctx.reserve("a", ctx.root_id, 10, Decimal("1"))
    ctx.settle("a", 7, Decimal("0.5"))
    ctx.reserve("b", child.node_id, 20, Decimal("2"))
    ctx.settle("b", None, Decimal("0.3"))
    ctx.reserve("c", grandchild.node_id, 30, Decimal("3"))
    ctx.reserve("d", grandchild.node_id, 5, Decimal("1"))
    ctx.release("d")
    page = ctx.tree_snapshot(limit=1)
    usage = page["nodes"][0]["subtreeUsage"]
    assert (usage["tokens"], usage["costUsd"], usage["reservedTokens"], usage["reservedCostUsd"]) == (7, "0.8", 30, "3")
    assert (usage["unknownTokens"], usage["uncertainTokensBound"]) == (1, 20)
    assert page["limits"]["maxTokens"] == 1000
    assert page["nextOffset"] == 1 and page["totalNodes"] == 3
    middle = ctx.tree_snapshot(offset=1, limit=1)
    assert middle["revision"] == page["revision"]
    assert middle["nodes"][0]["parentNodeId"] == ctx.root_id
    assert middle["nodes"][0]["ownUsage"]["costUsd"] == "0.3"
    page["nodes"][0]["subtreeUsage"]["tokens"] = -1
    assert ctx.tree_snapshot()["nodes"][0]["subtreeUsage"]["tokens"] == 7
    assert ctx.tree_snapshot(offset=99)["nodes"] == []
    ctx.start(child.node_id)
    assert ctx.tree_snapshot()["revision"] > middle["revision"]


@pytest.mark.asyncio
async def test_tree_snapshot_validation_redaction_and_deep_tree() -> None:
    ctx = RunContext("root", RunLimits())
    parent = ctx.root_id
    for i in range(1100):
        parent = ctx.register(parent, "session", node_id=f"node-{i}").node_id
    ctx.cancel_subtree(parent, "authorization: Bearer secret-value")
    page = ctx.tree_snapshot(offset=1100, limit=1)
    assert page["nodes"][0]["depth"] == 1100
    assert "secret-value" not in json.dumps(page)
    assert len(ctx.tree_snapshot()["nodes"]) == MAX_TREE_PAGE_SIZE
    for kwargs in ({"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": True},
                   {"limit": MAX_TREE_PAGE_SIZE + 1}):
        with pytest.raises(ValueError):
            ctx.tree_snapshot(**kwargs)


@pytest.mark.asyncio
async def test_split_credentials_in_reasons_never_escape_across_pages() -> None:
    ctx = RunContext("root", RunLimits())
    first = ctx.register(ctx.root_id, "first")
    second = ctx.register(ctx.root_id, "second")
    ctx.cancel_subtree(first.node_id, "authorization: Bearer split-")
    ctx.cancel_subtree(second.node_id, "secret-value")
    pages = [ctx.tree_snapshot(offset=i, limit=1) for i in range(3)]
    assert [page["nodes"][0]["reason"] for page in pages] == [None, None, None]
    assert "split-" not in json.dumps(pages) and "secret-value" not in json.dumps(pages)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["timeout", "parent_cancel", "user_cancel", "user_cancelled", "deadline"])
async def test_known_reasons_survive_tree_and_history(reason: str) -> None:
    ctx = RunContext("root", RunLimits())
    ctx.cancel_subtree(ctx.root_id, reason)
    assert ctx.tree_snapshot()["nodes"][0]["reason"] == reason
    assert ctx.history_snapshot()["nodes"][0]["reason"] == reason
