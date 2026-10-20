"""Demo detector (M1): exercises the live path bar close -> detector -> event -> UI.

It is **not a strategy** and has no edge. On each closed, live-observed 1m bar it emits a
`SIGNAL_CREATED` event with stage `WATCH` when the bar's body |close/open - 1| exceeds a threshold.

Hard rules (M1-T2 brief, CLAUDE.md):
- only `WATCH`, never `CONFIRMED`; never sized; never routed to any book;
- `strategy_id="demo_detector"`, `evidence_grade="X"`, `run_mode=LIVE`;
- fail closed: no event unless the service is running and every stream of that (market, symbol)
  is HEALTHY, and only for bars closed on the WebSocket (`ws_live`) — a REST-recovered or
  backfilled bar is late by definition and never yields a live signal;
- every API field derived from these events is labeled `Provenance.DEMO`.

The `SignalRecord` contract requires risk/geometry fields (stop, R:R, cost, break-even win rate)
that the demo detector does not compute. They are filled with transparent placeholders (stop at the
opposite extreme of the trigger bar, ratios 0.0) and listed in `market_conditions["not_computed"]`;
the API never exposes them. A contract change to make them optional for demo records is requested
in the M1-T2 report.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from tsclaude.contracts.events import AggregateType, Event, EventType, NewEvent
from tsclaude.contracts.ids import new_ulid
from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.contracts.signals import (
    Condition,
    EntryRule,
    EntryRuleKind,
    RunMode,
    Side,
    SignalRecord,
    SignalStatus,
)
from tsclaude.contracts.timeutil import MS_PER_MINUTE

log = logging.getLogger(__name__)

STRATEGY_ID = "demo_detector"
STRATEGY_VERSION = "0"
EVIDENCE_GRADE = "X"
DEFAULT_THRESHOLD = 0.003  # 0.3 % body on a 1m bar
NOT_COMPUTED = "stop_price,rr_gross,rr_net,est_cost_bps,break_even_win_rate"

type HealthCheck = Callable[[Market, str], tuple[bool, str | None]]
type Append = Callable[[NewEvent], Event]


@dataclass(frozen=True)
class DemoDetectorConfig:
    enabled: bool = True
    threshold: float = DEFAULT_THRESHOLD  # fraction, e.g. 0.003 = 0.3 %
    valid_for_minutes: int = 5

    def __post_init__(self) -> None:
        if not 0 < self.threshold < 1:
            raise ValueError("demo detector threshold must be in (0, 1)")
        if self.valid_for_minutes < 1:
            raise ValueError("valid_for_minutes must be >= 1")


@dataclass
class DemoStats:
    bars_seen: int = 0
    emitted: int = 0
    skipped_unhealthy: int = 0
    skipped_not_live: int = 0
    last_skip_reason: str | None = None


def params_hash(config: DemoDetectorConfig) -> str:
    return f"threshold={config.threshold};valid_for_minutes={config.valid_for_minutes}"


class DemoDetector:
    """Stateless per bar; `health(market, symbol)` returns (all streams HEALTHY, reason)."""

    def __init__(
        self,
        config: DemoDetectorConfig,
        append: Append,
        health: HealthCheck,
        now_ms: Callable[[], int],
        new_id: Callable[[int], str] = new_ulid,
    ) -> None:
        self.config = config
        self._append = append
        self._health = health
        self._now = now_ms
        self._new_id = new_id
        self.stats = DemoStats()

    def on_bar(self, bar: Bar, *, session_id: str | None) -> Event | None:
        """Evaluate one closed bar; returns the stored event if a WATCH was emitted."""
        if not self.config.enabled:
            return None
        self.stats.bars_seen += 1
        if bar.interval is not Interval.M1 or bar.source is not BarSource.WS_LIVE:
            self.stats.skipped_not_live += 1
            self.stats.last_skip_reason = f"bar source {bar.source} is not ws_live"
            return None
        healthy, reason = self._health(bar.market, bar.symbol)
        if not healthy:
            self.stats.skipped_unhealthy += 1
            self.stats.last_skip_reason = reason or "streams not healthy"
            log.info(
                "demo detector disabled for symbol (fail closed)",
                extra={"market": str(bar.market), "symbol": bar.symbol, "reason": reason},
            )
            return None
        move = bar.close / bar.open - 1.0
        if abs(move) <= self.config.threshold:
            return None
        record = self._record(bar, move, session_id)
        event = self._append(
            NewEvent(
                type=EventType.SIGNAL_CREATED,
                aggregate_type=AggregateType.SIGNAL,
                aggregate_id=record.signal_id,
                ts_ms=record.created_at_ms,
                payload={
                    **record.model_dump(mode="json"),
                    "demo": True,
                    "session_id": session_id,
                    "move": move,
                    "threshold": self.config.threshold,
                },
            )
        )
        self.stats.emitted += 1
        return event

    def _record(self, bar: Bar, move: float, session_id: str | None) -> SignalRecord:
        now = max(self._now(), bar.close_time_ms)
        side = Side.LONG if move > 0 else Side.SHORT
        # Placeholder stop: the trigger bar's opposite extreme (strictly on the right side because
        # the body is non-zero). Not a risk decision; see the module docstring.
        stop = bar.low if side is Side.LONG else bar.high
        condition = Condition(
            name="abs_body_return_1m",
            operator=">",
            threshold=self.config.threshold,
            observed=abs(move),
            satisfied=True,
        )
        return SignalRecord(
            signal_id=self._new_id(now),
            run_mode=RunMode.LIVE,
            market=bar.market,
            symbol=bar.symbol,
            timeframe=Interval.M1,
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            params_hash=params_hash(self.config),
            prereg_id=None,
            evidence_grade=EVIDENCE_GRADE,
            side=side,
            stage=SignalStatus.WATCH,
            decision_time_ms=bar.close_time_ms,
            created_at_ms=now,
            snapshot_id=f"bar:{bar.market}:{bar.symbol}:{bar.open_time_ms}",
            entry_conditions=(condition,),
            entry_rule=EntryRule(
                kind=EntryRuleKind.MARKET_NEXT_OPEN,
                valid_until_ms=(
                    bar.close_time_ms + 1 + self.config.valid_for_minutes * MS_PER_MINUTE
                ),
            ),
            ref_price=bar.close,
            stop_price=stop,
            stop_basis="demo placeholder: opposite extreme of the trigger bar (no risk decision)",
            take_profit_price=None,
            exit_policy="none (demo detector: never routed to any book)",
            rr_gross=0.0,
            rr_net=0.0,
            est_cost_bps=0.0,
            break_even_win_rate=0.0,
            rationale=(
                f"DEMO: 1m body {move:+.3%} exceeded ±{self.config.threshold:.3%}. "
                "Exercises the live pipeline only; no edge, not a recommendation."
            ),
            market_conditions={
                "demo": True,
                "not_computed": NOT_COMPUTED,
                "session_id": session_id or "",
            },
        )
