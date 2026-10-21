"""Signal source adapter: closed 1m bars -> derived strategy bars -> strategy -> sim requests.

The simulator delivers every closed 1m bar once (`sim.engine.SignalSource`). Per (market, symbol)
this adapter aggregates them into strategy bars (`features.bars.BarAggregator`, complete buckets
only). When a strategy bar completes -- at the close of its last minute, never earlier -- the
strategy is evaluated on the **trailing window** of the last ``strategy.window_bars`` strategy bars
(the live computation path of ADR-0006; the window ends at the decision bar, so later data cannot
exist in it). CONFIRMED proposals become market `EntryRequest`s; the simulator stamps the decision
time and executes no earlier than the next 1m open.

Gaps: when a strategy bucket is incomplete (a missing minute, or an empty bucket) the symbol's
strategy-bar history is reset, so the warm-up restarts after every gap and no feature is ever
computed over bars that are not contiguous.

Run boundaries: no entry is proposed for decision bars closing before ``trade_from_ms`` (warm-up)
or at/after ``flat_from_ms``; at the first 1m bar closing at/after ``flat_from_ms`` an
`ExitRequest` is issued for every symbol, so positions close at the next open and the run ends
flat whenever data allows.

`schedule` turns the adapter into the **random-entry control**: instead of the strategy's entry
rule, it enters at the scheduled decision bars with the strategy's own exit geometry
(`Strategy.exit_geometry`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from tsclaude.contracts.market import Bar, Market
from tsclaude.contracts.signals import Side, SignalStatus
from tsclaude.features.bars import BarAggregator
from tsclaude.features.core import BarArrays
from tsclaude.sim.engine import SimView
from tsclaude.sim.money import to_decimal
from tsclaude.sim.orders import EntryRequest, ExitRequest
from tsclaude.strategies.base import PlanningCosts, Proposal, Strategy

type SymbolKey = tuple[Market, str]
type Request = EntryRequest | ExitRequest


class _History:
    """Growable column store of closed strategy bars (amortized O(1) append, O(1) windows)."""

    def __init__(self) -> None:
        self.n = 0
        self._ints: NDArray[np.int64] = np.zeros((2, 1024), dtype=np.int64)
        self._floats: NDArray[np.float64] = np.zeros((5, 1024), dtype=np.float64)

    def append(self, b: Bar) -> None:
        if self.n == self._ints.shape[1]:
            self._ints = np.concatenate([self._ints, np.zeros_like(self._ints)], axis=1)
            self._floats = np.concatenate([self._floats, np.zeros_like(self._floats)], axis=1)
        self._ints[:, self.n] = (b.open_time_ms, b.close_time_ms)
        self._floats[:, self.n] = (b.open, b.high, b.low, b.close, b.volume)
        self.n += 1

    def window(self, length: int) -> BarArrays:
        lo = max(0, self.n - length)
        i, f = self._ints[:, lo : self.n], self._floats[:, lo : self.n]
        return BarArrays(i[0], i[1], f[0], f[1], f[2], f[3], f[4])


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    decision_ms: int  # close time of the strategy bar the proposal was computed from
    delivered_ms: int  # close time of the 1m bar that completed the strategy bar
    market: Market
    symbol: str
    bar_index: int  # index of the decision bar in the symbol's strategy-bar history
    proposal: Proposal
    request_id: str | None  # set when submitted to the simulator


class StrategySource:
    def __init__(
        self,
        strategy: Strategy[Any],
        costs: Mapping[SymbolKey, PlanningCosts],
        *,
        trade_from_ms: int,
        flat_from_ms: int,
        schedule: Mapping[SymbolKey, frozenset[int]] | None = None,
        request_prefix: str = "s",
    ) -> None:
        if flat_from_ms <= trade_from_ms:
            raise ValueError("flat_from_ms must be after trade_from_ms")
        self.strategy = strategy
        self.costs = dict(costs)
        self.trade_from_ms = trade_from_ms
        self.flat_from_ms = flat_from_ms
        self.schedule = None if schedule is None else dict(schedule)
        self.prefix = request_prefix
        self._agg: dict[SymbolKey, BarAggregator] = {}
        self._hist: dict[SymbolKey, _History] = {}
        self._flattened: set[SymbolKey] = set()
        self.records: list[ProposalRecord] = []
        self.breaks = 0  # history resets caused by incomplete strategy buckets
        self.decision_bars: dict[SymbolKey, list[int]] = {}  # tradable decision closes

    @property
    def incomplete_buckets(self) -> int:
        return sum(a.incomplete for a in self._agg.values())

    def on_bar(self, bar: Bar, view: SimView) -> Sequence[Request]:
        key = (bar.market, bar.symbol)
        costs = self.costs.get(key)
        if costs is None:
            return ()  # unknown instrument: no orders (fail closed)
        out: list[Request] = []
        if bar.close_time_ms >= self.flat_from_ms and key not in self._flattened:
            self._flattened.add(key)
            out.append(ExitRequest(f"{self.prefix}:flat:{bar.symbol}", bar.market, bar.symbol))
        agg = self._agg.get(key)
        if agg is None:
            agg = self._agg[key] = BarAggregator(self.strategy.interval)
            self._hist[key] = _History()
        before = agg.incomplete
        derived = agg.push(bar)
        if agg.incomplete != before:
            # a strategy bucket was dropped (missing minutes): the history breaks here and the
            # warm-up restarts, so no feature window ever joins bars across a gap
            self._hist[key] = _History()
            self.breaks += 1
        if derived is None:
            return out
        hist = self._hist[key]
        hist.append(derived)
        decision = derived.close_time_ms
        if not self.trade_from_ms <= decision < self.flat_from_ms:
            return out
        window = hist.window(self.strategy.window_bars)
        if len(window) < self.strategy.warmup_bars:
            return out
        self.decision_bars.setdefault(key, []).append(decision)
        if self.schedule is None:
            proposals = list(self.strategy.on_bar(window, costs))
        elif decision in self.schedule.get(key, frozenset()):
            geo = self.strategy.exit_geometry(window, Side.LONG, costs)
            proposals = [] if geo is None else [geo]
        else:
            proposals = []
        for prop in proposals:
            rid: str | None = None
            if prop.stage is SignalStatus.CONFIRMED:
                rid = f"{self.prefix}:{bar.market}:{bar.symbol}:{decision}"
                out.append(self._entry(rid, bar, prop))
            self.records.append(
                ProposalRecord(
                    decision, bar.close_time_ms, bar.market, bar.symbol, hist.n - 1, prop, rid
                )
            )
        return out

    def _entry(self, rid: str, bar: Bar, prop: Proposal) -> EntryRequest:
        interval_ms = self.strategy.interval.ms
        return EntryRequest(
            request_id=rid,
            market=bar.market,
            symbol=bar.symbol,
            side=prop.side,
            kind=prop.entry_kind,
            entry_price=None if prop.entry_price is None else to_decimal(prop.entry_price),
            stop_price=to_decimal(prop.stop_price),
            take_profit_price=(
                None if prop.take_profit_price is None else to_decimal(prop.take_profit_price)
            ),
            valid_until_ms=bar.close_time_ms + prop.valid_bars * interval_ms,
            time_exit_bars=prop.time_exit_bars,
            strategy_interval=self.strategy.interval,
            signal_id=rid,
        )
