from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

TEMPERATURE_PRESETS: tuple[tuple[str, float], ...] = (
    ("coder", 0.2),
    ("balanced", 0.5),
    ("creative", 0.8),
    ("experimental", 1.2),
)
TEMPERATURE_MODES = tuple(name for name, _ in TEMPERATURE_PRESETS)
TEMPERATURE_MIN = 0.0
TEMPERATURE_MAX = 1.2
TEMPERATURE_STEP = 0.1
DEFAULT_TEMPERATURE = 0.5


def normalize_temperature(value: Any) -> float:
    """Validate and round a user temperature to the supported tenth."""
    if isinstance(value, bool):
        raise ValueError("Temperature must be a number from 0.0 to 1.2")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Temperature must be a number from 0.0 to 1.2") from None
    if not decimal.is_finite() or not Decimal("0.0") <= decimal <= Decimal("1.2"):
        raise ValueError("Temperature must be between 0.0 and 1.2")
    normalized = float(decimal.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    # Do not leak IEEE negative zero into provider payloads or persisted state.
    return 0.0 if normalized == 0 else normalized


def temperature_for_mode(mode: Any) -> float:
    key = str(mode).lower()
    for name, value in TEMPERATURE_PRESETS:
        if name == key:
            return value
    raise ValueError(f"Invalid temperature mode: {mode}. Allowed: {', '.join(TEMPERATURE_MODES)}")


def nearest_temperature_mode(value: Any) -> str:
    temperature = normalize_temperature(value)
    # min retains the declared preset order for equal distances.
    return min(TEMPERATURE_PRESETS, key=lambda item: abs(item[1] - temperature))[0]
