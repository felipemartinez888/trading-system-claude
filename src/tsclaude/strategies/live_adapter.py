"""`ReferenceLiveStrategy`: `bracket_reference` as a paper-only live signal source (MVP-3).

It implements the paper engine's `LiveStrategy` protocol by **reusing the backtest adapter
unchanged** (`research.source.StrategySource`): the same 1m -> strategy-interval aggregation
(complete buckets only), the same history reset after any incomplete bucket, the same trailing
window of `window_bars` strategy bars ending at the decision bar, and the same planning costs. Live
and backtest proposals on identical closed bars are therefore identical by construction, and the
parity tests check it.

What it emits:
- only **CONFIRMED** proposals become intents: one `SignalRecord` each, decided at the close of the
  1m bar that completed the strategy bar. The paper engine routes it through the risk engine
  (baseline book) like any other signal;
- **WATCH** proposals are counted and never emitted (no intent, no Confirm, no order);
- every signal is labelled REFERENCE / NOT VALIDATED (`evidence_grade` "X", no preregistration,
  rationale and market conditions say so). It is not a research trial and touches no research
  registry, preregistration or holdout data.

Causality:
- `trade_from_ms` is the session's start minute: no decision bar closing before it can produce an
  intent, so the warm-up (closed bars from the live bar store, `warm_up`) and any queued bar from
  before the start only build history;
- a minute already fed (warm-up overlapping the live queue) is skipped, never re-aggregated;
- a missing minute (for example one resolved by an unobserved backfill, which never reaches a
  strategy) breaks the history and restarts the warm-up, as in backtests.

Fail closed: the paper runtime builds it only through `create()`, which uses the default
`BracketReferenceParams` (no configuration path exists for other params). The constructor, used by
the parity tests with small-window params, accepts a time exit since PT-1: the signal record
carries `time_exit_bars` into the paper engine.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from tsclaude.contracts.ids import new_ulid
from tsclaude.contracts.market import Bar, Interval, Market
from tsclaude.contracts.signals import (
    Condition,
    EntryRule,
    RunMode,
    SignalRecord,
    SignalStatus,
)
from tsclaude.research.source import ProposalRecord, StrategySource
from tsclaude.sim.costs import CostModel
from tsclaude.sim.engine import SimView
from tsclaude.strategies.base import PlanningCosts, Proposal
from tsclaude.strategies.bracket_reference import BracketReference, BracketReferenceParams

type SymbolKey = tuple[Market, str]

LABEL = "REFERENCE"
VALIDATION = "NOT_VALIDATED"
EVIDENCE_GRADE = "X"  # no validation evidence (RESEARCH_PROTOCOL §6)
RATIONALE = (
    "REFERENCE / NOT VALIDATED: bracket_reference is a pipeline strategy, not claimed profitable; "
    "paper trading only."
)
#: far beyond any session: a live source never runs into the backtest's flat-at-end boundary
_NEVER_MS = 2**62
MAX_RECENT = 50
BREAK_REASON = (
    "an incomplete 15m bucket (a missing 1m bar, or one the paper engine processed unobserved) "
    "restarted this instrument's history"
)
WARMUP_NOTE = (
    "bracket_reference needs about 741 contiguous 15m bars (about 7.8 days of unbroken 1m bars) "
    "per instrument before its first decision. At paper start it warms up from the live bar store, "
    "which the market-data startup backfill fills for the last 9 days (configs/data_quality.yaml); "
    "a missing minute in that history leaves it short. After the start, any minute the strategy "
    "does not see (a missing bar, or a REST-recovered or downtime-backfill bar, which the paper "
    "engine processes unobserved) restarts the warm-up: no reference signals for about 7.8 days."
)


@dataclass
class ReferenceStats:
    bars_seen: int = 0
    warmup_bars: int = 0
    skipped_not_newer: int = 0  # already fed, or late/out of order for that symbol
    watch_proposals: int = 0
    confirmed_intents: int = 0
    recent: list[dict[str, Any]] = field(default_factory=list)


class ReferenceLiveStrategy:
    """`LiveStrategy` (sim.paper) emitting the CONFIRMED proposals of `bracket_reference`."""

    def __init__(
        self,
        strategy: BracketReference,
        costs: Mapping[SymbolKey, PlanningCosts],
        *,
        trade_from_ms: int,
    ) -> None:
        self.strategy = strategy
        self.trade_from_ms = trade_from_ms
        self._source = StrategySource(
            strategy,
            costs,
            trade_from_ms=trade_from_ms,
            flat_from_ms=_NEVER_MS,
            request_prefix="ref",
        )
        self._costs = dict(costs)
        self._last_open: dict[SymbolKey, int] = {}
        self._breaks: dict[SymbolKey, dict[str, Any]] = {}
        self.stats = ReferenceStats()

    @classmethod
    def create(
        cls,
        cost_model: CostModel,
        instruments: Iterable[SymbolKey],
        *,
        trade_from_ms: int,
    ) -> ReferenceLiveStrategy:
        """Default `bracket_reference` with the planning costs of the paper cost model."""
        costs = {(m, s): PlanningCosts.from_cost_model(cost_model, m, s) for m, s in instruments}
        strategy = BracketReference()
        if strategy.params != BracketReferenceParams():  # pragma: no cover - defaults by design
            raise ValueError("paper reference strategy: only default bracket_reference params")
        return cls(strategy, costs, trade_from_ms=trade_from_ms)

    # ------------------------------------------------------------------ feeding
    def warm_up(self, bars: Iterable[Bar]) -> int:
        """Feed closed history bars (sorted per symbol by the caller or not: they are ordered
        here). Only bars closing before `trade_from_ms` are used; they never produce intents."""
        fed = 0
        for bar in sorted(bars, key=lambda b: (b.open_time_ms, str(b.market), b.symbol)):
            if bar.close_time_ms >= self.trade_from_ms:
                continue
            if self._feed(bar):
                fed += 1
        self.stats.warmup_bars += fed
        return fed

    def on_bar(self, bar: Bar, view: SimView) -> Sequence[SignalRecord]:
        """The `LiveStrategy` call: intents for CONFIRMED proposals decided at this bar."""
        self.stats.bars_seen += 1
        before = len(self._source.records)
        if not self._feed(bar, view):
            return []
        out: list[SignalRecord] = []
        for rec in self._source.records[before:]:
            if rec.proposal.stage is not SignalStatus.CONFIRMED:
                self.stats.watch_proposals += 1  # never executable
                continue
            record = self.signal_record(rec)
            out.append(record)
            self.stats.confirmed_intents += 1
            self.stats.recent.append(_summary(record))
            del self.stats.recent[:-MAX_RECENT]
        return out

    def _feed(self, bar: Bar, view: SimView | None = None) -> bool:
        if bar.interval is not Interval.M1:
            return False
        key = (bar.market, bar.symbol)
        if key not in self._costs:
            return False  # not an instrument of this session (fail closed: no intents)
        last = self._last_open.get(key)
        if last is not None and bar.open_time_ms <= last:
            self.stats.skipped_not_newer += 1  # warm-up overlap, or a late bar (history gap)
            return False
        self._last_open[key] = bar.open_time_ms
        # the backtest adapter never reads the view; it is passed through unchanged
        before = self._source.breaks
        self._source.on_bar(bar, cast(SimView, view))
        if self._source.breaks != before:
            info = self._breaks.setdefault(key, {"count": 0})
            info["count"] += self._source.breaks - before
            info["detected_at_ms"] = bar.close_time_ms
            info["phase"] = "warm-up" if bar.close_time_ms < self.trade_from_ms else "live"
        return True

    # ------------------------------------------------------------------ mapping
    def signal_record(self, rec: ProposalRecord) -> SignalRecord:
        """The paper intent for one CONFIRMED proposal (deterministic: same bars, same record)."""
        prop = rec.proposal
        interval = self.strategy.interval
        costs = self._costs[(rec.market, rec.symbol)]
        risk = abs(prop.ref_price - prop.stop_price)
        reward = abs(prop.take_profit_price - prop.ref_price) if prop.take_profit_price else 0.0
        rr_gross = reward / risk if risk > 0 else 0.0
        cost_r = (
            prop.planned_cost_r
            if prop.planned_cost_r is not None
            else costs.cost_r(prop.ref_price, prop.stop_price)
        )
        rr_net = (rr_gross - cost_r) / (1.0 + cost_r)
        p_star = self.strategy.design_p_star(costs)
        if p_star is None:
            p_star = (1.0 + cost_r) / (1.0 + rr_gross) if rr_gross > 0 else 1.0
        return SignalRecord(
            signal_id=self.signal_id(rec.market, rec.symbol, rec.decision_ms),
            run_mode=RunMode.LIVE,
            market=rec.market,
            symbol=rec.symbol,
            timeframe=interval,
            strategy_id=self.strategy.strategy_id,
            strategy_version=self.strategy.version,
            params_hash=self.strategy.params_hash,
            prereg_id=None,
            evidence_grade=EVIDENCE_GRADE,
            side=prop.side,
            stage=SignalStatus.CONFIRMED,
            decision_time_ms=rec.decision_ms,
            created_at_ms=rec.decision_ms,
            snapshot_id=f"reference:{rec.market}:{rec.symbol}:{rec.decision_ms}",
            entry_conditions=_conditions(prop),
            entry_rule=EntryRule(
                kind=prop.entry_kind,
                price=prop.entry_price,
                # identical to the backtest request (`StrategySource._entry`)
                valid_until_ms=rec.delivered_ms + prop.valid_bars * interval.ms,
            ),
            ref_price=prop.ref_price,
            stop_price=prop.stop_price,
            stop_basis=f"bracket_reference: {self.strategy.params.stop_atr} x ATR below close",
            take_profit_price=prop.take_profit_price,
            # the same value the backtest request carries (`StrategySource._entry`)
            time_exit_bars=prop.time_exit_bars,
            exit_policy=prop.exit_policy,
            rr_gross=rr_gross,
            rr_net=rr_net,
            est_cost_bps=costs.round_trip_frac * 10_000.0,
            break_even_win_rate=min(1.0, max(0.0, p_star)),
            rationale=RATIONALE,
            market_conditions={
                **{k: v for k, v in prop.conditions.items()},
                "planning_costs": costs.basis,
                # written last: no feature name can ever replace the labels
                "label": LABEL,
                "validation": VALIDATION,
            },
        )

    def signal_id(self, market: Market, symbol: str, decision_ms: int) -> str:
        """Deterministic ULID: time = decision, randomness = 80 bits of a content hash."""
        digest = hashlib.sha256(
            f"{self.strategy.strategy_hash}|{market}|{symbol}|{decision_ms}".encode()
        ).hexdigest()
        return new_ulid(decision_ms, int(digest[:20], 16))

    # ------------------------------------------------------------------ reporting
    @property
    def history_breaks(self) -> int:
        return self._source.breaks

    def warmup_progress(self) -> dict[str, dict[str, Any]]:
        """Contiguous strategy bars currently held per instrument (a history break resets it)
        and whether a decision is possible yet."""
        out: dict[str, dict[str, Any]] = {}
        histories = getattr(self._source, "_hist", {})  # read-only view of the backtest adapter
        for key in sorted(self._costs, key=lambda k: (str(k[0]), k[1])):
            held = int(getattr(histories.get(key), "n", 0))
            info = self._breaks.get(key, {})
            out[f"{key[0]}:{key[1]}"] = {
                "strategy_bars": held,
                "needed": self.strategy.warmup_bars,
                "ready": held >= self.strategy.warmup_bars,
                # B-3: why and when this instrument's history last restarted (None: never)
                "history_breaks": int(info.get("count", 0)),
                "last_break_detected_at_ms": info.get("detected_at_ms"),
                "last_break_phase": info.get("phase"),
                "last_break_reason": BREAK_REASON if info else None,
            }
        return out

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.strategy.strategy_id,
            "version": self.strategy.version,
            "params_hash": self.strategy.params_hash,
            "interval": str(self.strategy.interval),
            "label": LABEL,
            "validation": VALIDATION,
            "evidence_grade": EVIDENCE_GRADE,
            "trade_from_ms": self.trade_from_ms,
            "warmup_strategy_bars": self.strategy.warmup_bars,
            "warmup_1m_bars_fed": self.stats.warmup_bars,
            "bars_seen": self.stats.bars_seen,
            "skipped_not_newer": self.stats.skipped_not_newer,
            "warmup_needed_strategy_bars": self.strategy.warmup_bars,
            "warmup_progress": self.warmup_progress(),
            "note": WARMUP_NOTE,
            "history_breaks": self.history_breaks,
            "watch_proposals": self.stats.watch_proposals,
            "confirmed_intents": self.stats.confirmed_intents,
            "recent_signals": list(self.stats.recent),
        }


def _conditions(prop: Proposal) -> tuple[Condition, ...]:
    return tuple(
        Condition(name=k, operator="observed", threshold=v, observed=v, satisfied=True)
        for k, v in sorted(prop.conditions.items())
    )


def _summary(record: SignalRecord) -> dict[str, Any]:
    return {
        "signal_id": record.signal_id,
        "market": str(record.market),
        "symbol": record.symbol,
        "decision_time_ms": record.decision_time_ms,
        "side": str(record.side),
        "ref_price": record.ref_price,
        "stop_price": record.stop_price,
        "take_profit_price": record.take_profit_price,
        "valid_until_ms": record.entry_rule.valid_until_ms,
        "label": LABEL,
        "validation": VALIDATION,
    }


__all__ = ["LABEL", "VALIDATION", "ReferenceLiveStrategy", "ReferenceStats"]
