"""Decimal helpers for the simulator ledger (CLAUDE.md: money in ledgers is `Decimal` at 1e-8).

All simulator arithmetic runs inside `SIM_CONTEXT` (see `engine.Simulator`), so results never depend
on whatever global decimal context the host process configured.
"""

from __future__ import annotations

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal

QUANTUM = Decimal("0.00000001")
ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10_000)

#: 40 significant digits: quote amounts up to 1e15 with 8 decimals still leave ample headroom.
SIM_CONTEXT = Context(prec=40, rounding=ROUND_HALF_EVEN)


def to_decimal(value: Decimal | int | float | str) -> Decimal:
    """Exact, platform-independent conversion.

    Floats go through `repr` (shortest round-trip form, identical on every platform), so the bar
    price 100.1 becomes Decimal("100.1"), not its binary expansion.
    """
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):
        raise TypeError("booleans are not amounts")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float cannot become a Decimal amount")
        result = Decimal(repr(value))
    else:
        result = Decimal(value)
    if not result.is_finite():
        raise ValueError("amount must be finite")
    return result


def q8(value: Decimal, rounding: str = ROUND_HALF_EVEN) -> Decimal:
    """Quantize to 1e-8 (the ledger quantum)."""
    return value.quantize(QUANTUM, rounding=rounding)


def q8_up(value: Decimal) -> Decimal:
    """Quantize toward +infinity (used for amounts the book pays: fees, buy notionals)."""
    return value.quantize(QUANTUM, rounding=ROUND_CEILING)


def q8_down(value: Decimal) -> Decimal:
    """Quantize toward -infinity (used for amounts the book receives: sell proceeds)."""
    return value.quantize(QUANTUM, rounding=ROUND_FLOOR)


def is_q8(value: Decimal) -> bool:
    return value == value.quantize(QUANTUM)


def floor_to(value: Decimal, step: Decimal) -> Decimal:
    """Largest multiple of `step` that is <= value, carrying the step's exponent.

    The quantize makes the representation canonical (995 at tick 0.1 is always "995.0"), so event
    payloads and the run fingerprint never depend on how a price happened to be computed.
    """
    return ((value / step).to_integral_value(rounding=ROUND_FLOOR) * step).quantize(step)


def ceil_to(value: Decimal, step: Decimal) -> Decimal:
    """Smallest multiple of `step` that is >= value, carrying the step's exponent."""
    return ((value / step).to_integral_value(rounding=ROUND_CEILING) * step).quantize(step)


def dstr(value: Decimal) -> str:
    """Plain (non-exponent) string used in event payloads and canonical JSON."""
    return format(value, "f")
