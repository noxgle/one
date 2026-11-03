# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Any


def _count(value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("token counts must be non-negative integers")


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    """Explicit USD per million tokens for one provider/model and pricing version.

    No catalog is assumed: callers must supply a trustworthy, applicable snapshot.
    Cache rates of None mean unknown, not free.
    """

    provider: str
    model: str
    version: str
    input: Decimal
    output: Decimal
    cache_read: Decimal | None = None
    cache_write: Decimal | None = None
    cache_mode: str = "read_write"
    source: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.cache_mode, str) or self.cache_mode not in {"none", "read", "read_write"}:
            raise ValueError("cache_mode must be none, read or read_write")
        if self.source is not None and (not isinstance(self.source, str) or not self.source.strip()):
            raise ValueError("price source must be a non-empty string")
        if self.cache_mode == "none" and (self.cache_read is not None or self.cache_write is not None):
            raise ValueError("cache rates conflict with cache mode none")
        if self.cache_mode == "read" and self.cache_write is not None:
            raise ValueError("cache write rate conflicts with cache mode read")
        for value in (self.provider, self.model, self.version):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("provider, model and price version must be non-empty strings")
        for value in (self.input, self.output, self.cache_read, self.cache_write):
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value < 0):
                raise ValueError("rates must be non-negative finite Decimals")
        if self.input is None or self.output is None:
            raise ValueError("input and output rates are required")

    def bound(self, input_tokens: int, output_tokens: int) -> Decimal | None:
        """Bound any cache mix only within the modes covered by this snapshot.

        Prices for all enabled cache modes must be known; the caller must
        verify that no other tier or cache duration has a higher rate.
        """
        _count(input_tokens)
        _count(output_tokens)
        if input_tokens and ((self.cache_mode != "none" and self.cache_read is None)
                             or (self.cache_mode == "read_write" and self.cache_write is None)):
            return None
        rates = [rate for rate in (self.input, self.cache_read, self.cache_write) if rate is not None]
        return _cost(((input_tokens, max(rates)), (output_tokens, self.output)))

    @classmethod
    def from_config(cls, provider: str, model: str, value: Any) -> PriceSnapshot:
        """Load explicit models.json pricing, never inferred or builtin rates.

        Schema 1 is USD per million tokens. source/version identify an
        operator-verified schedule covering ALL request tiers used by this
        model entry (including long context and cache retention). Use decimal
        strings to avoid floating-point loss. Missing cache rates are unknown;
        cacheMode=none explicitly asserts that caching does not apply.
        """
        required = {"schemaVersion", "version", "source", "currency", "unit", "cacheMode", "input", "output"}
        if not isinstance(value, dict) or not required <= value.keys() or value.keys() - (
            required | {"cacheRead", "cacheWrite"}
        ):
            raise ValueError("pricing requires schemaVersion, version, source, currency, unit, cacheMode, input, output")
        if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
            raise ValueError("unsupported pricing schemaVersion (expected 1)")
        if value["currency"] != "USD" or value["unit"] != "perMillionTokens":
            raise ValueError("pricing requires USD perMillionTokens")
        if not isinstance(value["source"], str) or not value["source"].strip():
            raise ValueError("pricing source must be a non-empty string")

        def rate(key: str) -> Decimal | None:
            raw = value.get(key)
            if raw is None and key in {"cacheRead", "cacheWrite"}:
                return None
            if not isinstance(raw, str) and type(raw) is not int:
                raise ValueError(f"pricing {key} must be a decimal string or integer")
            try:
                return Decimal(raw)
            except ArithmeticError as error:
                raise ValueError(f"invalid pricing {key}") from error

        return cls(provider, model, value["version"], rate("input"), rate("output"),
                   rate("cacheRead"), rate("cacheWrite"), value["cacheMode"], value["source"])


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Normalized totals: cache counts are disjoint subsets of input.

    Output includes any reasoning tokens. Adapters must normalize their own
    documented semantics before constructing this object; missing totals are unknown.
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int

    def __post_init__(self) -> None:
        for count in (self.input_tokens, self.output_tokens, self.cache_read_tokens, self.cache_write_tokens):
            _count(count)
        if self.cache_read_tokens + self.cache_write_tokens > self.input_tokens:
            raise ValueError("cache tokens exceed input total")

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def cost(self, price: PriceSnapshot | None) -> Decimal | None:
        if price is None or (self.cache_read_tokens and price.cache_read is None) or (
            self.cache_write_tokens and price.cache_write is None
        ):
            return None
        return _cost((
            (self.input_tokens - self.cache_read_tokens - self.cache_write_tokens, price.input),
            (self.output_tokens, price.output),
            (self.cache_read_tokens, price.cache_read or Decimal(0)),
            (self.cache_write_tokens, price.cache_write or Decimal(0)),
        ))


def _cost(parts: tuple[tuple[int, Decimal], ...]) -> Decimal:
    # Round upward if unusually large inputs exceed the working precision.
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_CEILING
        return sum((Decimal(count) * rate for count, rate in parts), Decimal(0)) / Decimal(1_000_000)
