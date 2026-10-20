"""Provenance labels carried by every value the API exposes (ADR-0010)."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator


class Provenance(StrEnum):
    LIVE = "LIVE"  # observed live from the exchange during a monitoring session, fresh
    SYSTEM = "SYSTEM"  # the app's own runtime state (session, chain, coverage); not market data
    PARTIAL = "PARTIAL"  # forming candle / incomplete aggregate
    RECOVERED = "RECOVERED"  # filled after the fact from REST for a period we were observing
    HISTORICAL = "HISTORICAL"  # archive or backfill, never observed live
    REPLAY = "REPLAY"  # produced by replaying recorded data through the live code path
    BACKTEST = "BACKTEST"  # produced by a historical simulation
    SIMULATED = "SIMULATED"  # paper-trading result on live observations
    ESTIMATED = "ESTIMATED"  # model-based estimate (e.g. slippage), not an observation
    DEMO = "DEMO"  # fabricated demonstration data; never shown while monitoring
    STALE = "STALE"  # last known value, freshness budget exceeded
    UNAVAILABLE = "UNAVAILABLE"  # no value; `reason` explains why


class Sourced[T](BaseModel):
    """A value together with where it came from and how fresh it is."""

    model_config = ConfigDict(frozen=True)

    value: T | None
    provenance: Provenance
    as_of_ms: int | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if self.provenance is Provenance.UNAVAILABLE:
            if self.value is not None:
                raise ValueError("UNAVAILABLE values must not carry a value")
            if not self.reason:
                raise ValueError("UNAVAILABLE values must explain why (reason)")
        elif self.value is None:
            raise ValueError(f"{self.provenance} values must carry a value; use UNAVAILABLE")
        if self.provenance is Provenance.STALE and self.as_of_ms is None:
            raise ValueError("STALE values must state as_of_ms")
        return self

    @classmethod
    def unavailable(cls, reason: str) -> Sourced[T]:
        return cls(value=None, provenance=Provenance.UNAVAILABLE, reason=reason)
