# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext

import pytest

from one.core.run_context import RunContext, RunLimits
from one.core.run_pricing import PriceSnapshot, TokenUsage


@pytest.fixture
def price() -> PriceSnapshot:
    return PriceSnapshot("provider", "model", "2026-01", Decimal("2"), Decimal("4"), Decimal("1"), Decimal("3"))


def test_disjoint_cache_and_snapshot(price: PriceSnapshot) -> None:
    usage = TokenUsage(100, 50, 20, 10)
    assert usage.tokens == 150
    assert usage.cost(price) == Decimal("0.00039")
    assert price.bound(100, 50) == Decimal("0.0005")
    with pytest.raises(FrozenInstanceError):
        setattr(price, "version", "changed")


@pytest.mark.parametrize("kwargs", [
    {"provider": ""}, {"model": 1}, {"version": " "}, {"input": None}, {"output": Decimal("NaN")},
    {"cache_read": Decimal("-1")}, {"cache_write": 0},
])
def test_invalid_prices(kwargs: dict) -> None:
    fields = {"provider": "p", "model": "m", "version": "v1", "input": Decimal(1), "output": Decimal(1)}
    fields.update(kwargs)
    with pytest.raises(ValueError):
        PriceSnapshot(**fields)


@pytest.mark.parametrize("counts", [(True, 0, 0, 0), (-1, 0, 0, 0), (1, 0, 1, 1), (1, 0, None, 0)])
def test_invalid_usage(counts: tuple) -> None:
    with pytest.raises(ValueError):
        TokenUsage(*counts)


@pytest.mark.asyncio
async def test_priced_reservation_settlement_and_unknown(price: PriceSnapshot) -> None:
    ctx = RunContext("root", RunLimits(max_cost_usd=Decimal("0.0005")))
    ctx.reserve_priced("a", ctx.root_id, 100, 50, price, price_is_applicable=True)
    result = ctx.settle_usage("a", TokenUsage(100, 50, 20, 10))
    assert result.cost_usd == Decimal("0.00039") and result.price is price
    assert ctx.settle_usage("a", None) == result
    ctx.reserve_priced("c", ctx.root_id, 10, 0, price, price_is_applicable=True)
    uncertain = ctx.settle_usage("c", None)
    assert uncertain.state == "uncertain" and uncertain.cost_usd == Decimal("0.00003")
    with pytest.raises(ValueError, match="known cost"):
        ctx.reserve_priced("d", ctx.root_id, 1, 0, None, price_is_applicable=False)
    missing = PriceSnapshot("p", "m", "v", Decimal(1), Decimal(1))
    with pytest.raises(ValueError, match="known cost"):
        ctx.reserve_priced("e", ctx.root_id, 1, 0, missing, price_is_applicable=True)
    assert TokenUsage(1, 0, 1, 0).cost(missing) is None
    assert TokenUsage(1, 0, 0, 0).cost(None) is None


@pytest.mark.asyncio
async def test_unknown_price_without_limit() -> None:
    ctx = RunContext("root", RunLimits())
    ctx.reserve_priced("a", ctx.root_id, 5, 2, None, price_is_applicable=False)
    result = ctx.settle_usage("a", TokenUsage(4, 1, 0, 0))
    assert result.state == "uncertain" and result.cost_usd is None and result.tokens == 5


@pytest.fixture
def openai_gpt_61_terra_standard_short_30m_20261102() -> PriceSnapshot:
    """Offline example, supplied source retrieved 2026-11-02, USD per 1M tokens.

    https://developers.openai.com/api/docs/pricing: standard short only; this
    chooses the 30m write rate, not the 12h rate (2.50). Other modes and
    subsequent price changes are not covered. Never a production default.
    """
    return PriceSnapshot("openai", "gpt-6.1-terra", "2026-11-02-standard-short-30m",
                         Decimal("1.00"), Decimal("5.00"), Decimal("0.05"), Decimal("1.25"))


@pytest.mark.asyncio
async def test_offline_standard_short_30m(openai_gpt_61_terra_standard_short_30m_20261102: PriceSnapshot) -> None:
    price = openai_gpt_61_terra_standard_short_30m_20261102
    assert TokenUsage(1_000_000, 1_000_000, 200_000, 300_000).cost(price) == Decimal("5.885")
    assert price.bound(1_000_000, 1_000_000) == Decimal("6.25")
    ctx = RunContext("root", RunLimits(max_cost_usd=Decimal("6.25")))
    with pytest.raises(TypeError, match="price_is_applicable"):
        ctx.reserve_priced("missing", ctx.root_id, 1, 1, price)
    with pytest.raises(ValueError, match="applicability"):
        ctx.reserve_priced("bad", ctx.root_id, 1_000_000, 1_000_000, price, price_is_applicable=False)
    ctx.reserve_priced("ok", ctx.root_id, 1_000_000, 1_000_000, price, price_is_applicable=True)
    assert ctx.settle_usage("ok", TokenUsage(1_000_000, 1_000_000, 200_000, 300_000)).cost_usd == Decimal("5.885")


@pytest.mark.asyncio
async def test_ledger_cost_comparison_is_exact_despite_decimal_context() -> None:
    ctx = RunContext("root", RunLimits(max_cost_usd=Decimal("1")))
    with localcontext() as context:
        context.prec = 2
        ctx.reserve("a", ctx.root_id, 0, Decimal("0.999"))
        with pytest.raises(ValueError, match="cost budget"):
            ctx.reserve("b", ctx.root_id, 0, Decimal("0.002"))
