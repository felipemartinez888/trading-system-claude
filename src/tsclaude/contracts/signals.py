"""Signal record contract v0 (REQ-SIG-03).

Lifecycle transitions are events; the current status is a projection.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tsclaude.contracts.market import Interval, Market


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"


class RunMode(StrEnum):
    LIVE = "LIVE"  # created during an observed monitoring session
    REPLAY = "REPLAY"
    BACKTEST = "BACKTEST"


class SignalStatus(StrEnum):
    WATCH = "WATCH"  # Early Watch — not an entry recommendation
    CONFIRMED = "CONFIRMED"  # Confirmed Entry — entry rule met and risk pre-check passed
    VETOED = "VETOED"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


TERMINAL_STATUSES = frozenset(
    {SignalStatus.VETOED, SignalStatus.CLOSED, SignalStatus.EXPIRED, SignalStatus.INVALIDATED}
)

ALLOWED_TRANSITIONS: dict[SignalStatus, frozenset[SignalStatus]] = {
    SignalStatus.WATCH: frozenset(
        {SignalStatus.CONFIRMED, SignalStatus.EXPIRED, SignalStatus.INVALIDATED}
    ),
    SignalStatus.CONFIRMED: frozenset(
        {SignalStatus.VETOED, SignalStatus.OPEN, SignalStatus.EXPIRED, SignalStatus.INVALIDATED}
    ),
    SignalStatus.OPEN: frozenset({SignalStatus.CLOSED}),
    SignalStatus.VETOED: frozenset(),
    SignalStatus.CLOSED: frozenset(),
    SignalStatus.EXPIRED: frozenset(),
    SignalStatus.INVALIDATED: frozenset(),
}


def can_transition(current: SignalStatus, target: SignalStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


class EntryRuleKind(StrEnum):
    MARKET_NEXT_OPEN = "market_next_open"
    LIMIT = "limit"
    STOP = "stop"


class AgentReviewState(StrEnum):
    NOT_REQUESTED = "NOT_REQUESTED"
    PENDING = "PENDING"
    REVIEWED = "REVIEWED"
    AI_UNAVAILABLE = "AI_UNAVAILABLE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    AGENT_OUTPUT_INVALID = "AGENT_OUTPUT_INVALID"


class AgentVerdict(StrEnum):
    APPROVED = "APPROVED"
    DOWNGRADED = "DOWNGRADED"
    VETOED_AGENT = "VETOED_AGENT"


class Condition(BaseModel):
    """A machine-readable predicate evaluated at decision time, e.g. atr_pct_rank < 0.2."""

    model_config = ConfigDict(frozen=True)

    name: str
    operator: str
    threshold: float | str | bool
    observed: float | str | bool
    satisfied: bool


class EntryRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: EntryRuleKind
    price: float | None = None  # required for limit/stop
    valid_until_ms: int

    @model_validator(mode="after")
    def _check(self) -> Self:
        needs_price = self.kind in (EntryRuleKind.LIMIT, EntryRuleKind.STOP)
        if needs_price and (self.price is None or not math.isfinite(self.price) or self.price <= 0):
            raise ValueError(f"{self.kind} entries need a positive price")
        if not needs_price and self.price is not None:
            raise ValueError("market entries must not carry a price")
        return self


class SignalRecord(BaseModel):
    """Immutable facts of a signal at creation. Status/outcome evolve via events."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = 1
    signal_id: str = Field(min_length=26, max_length=26)
    run_mode: RunMode
    market: Market
    symbol: str
    timeframe: Interval
    strategy_id: str
    strategy_version: str
    params_hash: str
    prereg_id: str | None
    evidence_grade: str  # A/B/C/D/X at time of creation (RESEARCH_PROTOCOL §6)
    side: Side
    stage: SignalStatus  # WATCH or CONFIRMED at creation
    decision_time_ms: int  # close time of the bar the decision used
    created_at_ms: int  # when persisted
    snapshot_id: str  # evidence packet reference
    entry_conditions: tuple[Condition, ...]
    entry_rule: EntryRule
    ref_price: float
    stop_price: float
    stop_basis: str
    take_profit_price: float | None
    exit_policy: str
    rr_gross: float
    rr_net: float
    est_cost_bps: float
    break_even_win_rate: float = Field(ge=0, le=1)  # p* for this signal's exit geometry (ADR-0012)
    rationale: str
    market_conditions: dict[str, float | str | bool]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.stage not in (SignalStatus.WATCH, SignalStatus.CONFIRMED):
            raise ValueError("signals are created as WATCH or CONFIRMED")
        if self.created_at_ms < self.decision_time_ms:
            raise ValueError("a signal cannot be persisted before its decision time")
        long = self.side is Side.LONG
        if long and not self.stop_price < self.ref_price:
            raise ValueError("long stop must be below reference price")
        if not long and not self.stop_price > self.ref_price:
            raise ValueError("short stop must be above reference price")
        tp = self.take_profit_price
        if tp is not None and (
            (long and tp <= self.ref_price) or (not long and tp >= self.ref_price)
        ):
            raise ValueError("take-profit is on the wrong side of the reference price")
        if self.entry_rule.valid_until_ms <= self.decision_time_ms:
            raise ValueError("entry validity window must end after decision time")
        return self
