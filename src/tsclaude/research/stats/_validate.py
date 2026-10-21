"""Shared argument validation for the statistics core (fail loudly, never coerce silently)."""

from __future__ import annotations

import math
import operator
from typing import Literal

Side = Literal["two-sided", "lower", "upper"]
SIDES: tuple[str, ...] = ("two-sided", "lower", "upper")


def as_int(value: object, name: str) -> int:
    """Return ``value`` as a Python ``int``; booleans and non-integral numbers are rejected."""
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an integer, not bool")
    try:
        return operator.index(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError(f"{name} must be an integer, got {type(value).__name__}") from exc


def as_float(value: object, name: str) -> float:
    """Return ``value`` as a finite ``float``."""
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a real number, not bool")
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be a real number, got {type(value).__name__}") from exc
    if not math.isfinite(out):
        raise ValueError(f"{name} must be finite, got {out}")
    return out


def check_count(k: object, n: object) -> tuple[int, int]:
    """Validate a binomial observation ``0 <= k <= n``, ``n >= 1``."""
    ki = as_int(k, "k")
    ni = as_int(n, "n")
    if ni < 1:
        raise ValueError(f"n must be >= 1, got {ni}")
    if not 0 <= ki <= ni:
        raise ValueError(f"k must satisfy 0 <= k <= n, got k={ki}, n={ni}")
    return ki, ni


def check_prob(p: object, name: str = "p") -> float:
    """Validate a probability in the closed interval [0, 1]."""
    out = as_float(p, name)
    if not 0.0 <= out <= 1.0:
        raise ValueError(f"{name} must be in [0, 1], got {out}")
    return out


def check_open_prob(p: object, name: str) -> float:
    """Validate a probability in the open interval (0, 1)."""
    out = as_float(p, name)
    if not 0.0 < out < 1.0:
        raise ValueError(f"{name} must be in (0, 1), got {out}")
    return out


def check_side(side: str) -> Side:
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}, got {side!r}")
    return side  # type: ignore[return-value]
