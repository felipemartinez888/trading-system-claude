"""Clocks: the only way bars reach the simulator (ADR-0005, SIMULATION §1).

A `Clock` yields *closed* 1m `Bar`s in global time order: ascending `open_time_ms`, and within the
same minute ascending `(market, symbol)`. The simulator cannot tell clocks apart except through
`clock.mode`, which only affects labeling and is deliberately **not** part of the run fingerprint
(so a recorded live session replayed through `HistoricalClock` hashes identically).

`HistoricalClock` validates its input instead of repairing it: unsorted or duplicate bars raise
`ClockError`; gaps (missing minutes) are *reported* in `gaps` and never filled or skipped silently.
`LiveClock` arrives in M3 and must satisfy the same protocol.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tsclaude.contracts.market import Bar, Interval, Market
from tsclaude.contracts.provenance import Provenance
from tsclaude.contracts.signals import RunMode
from tsclaude.contracts.timeutil import MS_PER_MINUTE

type SymbolKey = tuple[Market, str]

#: Mode -> provenance label for values the simulator produces (ADR-0010).
MODE_PROVENANCE: dict[RunMode, Provenance] = {
    RunMode.BACKTEST: Provenance.BACKTEST,
    RunMode.REPLAY: Provenance.REPLAY,
    RunMode.LIVE: Provenance.SIMULATED,
}


class ClockError(ValueError):
    """The bar stream violates the clock contract (order, duplicates, interval)."""


@dataclass(frozen=True, slots=True)
class Gap:
    """Missing 1m bars between two consecutive bars of one (market, symbol)."""

    market: Market
    symbol: str
    after_open_ms: int  # open time of the last bar before the gap
    next_open_ms: int  # open time of the first bar after the gap

    @property
    def missing_bars(self) -> int:
        return (self.next_open_ms - self.after_open_ms) // MS_PER_MINUTE - 1


def bar_key(bar: Bar) -> SymbolKey:
    return (bar.market, bar.symbol)


def order_key(bar: Bar) -> tuple[int, str, str]:
    """Global delivery order of bars."""
    return (bar.open_time_ms, str(bar.market), bar.symbol)


@runtime_checkable
class Clock(Protocol):
    """Source of closed 1m bars in global time order."""

    @property
    def mode(self) -> RunMode: ...

    def __iter__(self) -> Iterator[Bar]: ...


def find_gaps(bars: Sequence[Bar]) -> list[Gap]:
    """Gaps inside one (market, symbol) series that is already sorted and duplicate-free."""
    gaps: list[Gap] = []
    for prev, cur in zip(bars, bars[1:], strict=False):
        if cur.open_time_ms - prev.open_time_ms > MS_PER_MINUTE:
            gaps.append(Gap(cur.market, cur.symbol, prev.open_time_ms, cur.open_time_ms))
    return gaps


class HistoricalClock:
    """Clock over in-memory bar sequences (backtests, walk-forward, replays).

    `bars` may mix symbols. Each (market, symbol) series must already be strictly increasing in
    `open_time_ms` in the order given; the clock merges the series into global order.
    """

    def __init__(self, bars: Iterable[Bar], mode: RunMode = RunMode.BACKTEST) -> None:
        series: dict[SymbolKey, list[Bar]] = {}
        for bar in bars:
            if bar.interval is not Interval.M1:
                raise ClockError(f"clock carries 1m bars only, got {bar.interval} for {bar.symbol}")
            seq = series.setdefault(bar_key(bar), [])
            if seq:
                last = seq[-1].open_time_ms
                if bar.open_time_ms == last:
                    raise ClockError(f"duplicate bar {bar.market}:{bar.symbol}@{bar.open_time_ms}")
                if bar.open_time_ms < last:
                    raise ClockError(
                        f"unsorted bars for {bar.market}:{bar.symbol}: "
                        f"{bar.open_time_ms} after {last}"
                    )
            seq.append(bar)
        self._series = series
        self._mode = mode
        self._gaps = tuple(
            gap for key in sorted(series, key=lambda k: (str(k[0]), k[1])) for gap in find_gaps(series[key])
        )

    @classmethod
    def from_chunks(
        cls, chunks: Iterable[Iterable[Bar]], mode: RunMode = RunMode.BACKTEST
    ) -> HistoricalClock:
        return cls((bar for chunk in chunks for bar in chunk), mode=mode)

    @property
    def mode(self) -> RunMode:
        return self._mode

    @property
    def gaps(self) -> tuple[Gap, ...]:
        return self._gaps

    @property
    def symbols(self) -> tuple[SymbolKey, ...]:
        return tuple(sorted(self._series, key=lambda k: (str(k[0]), k[1])))

    def __len__(self) -> int:
        return sum(len(s) for s in self._series.values())

    def __iter__(self) -> Iterator[Bar]:
        return heapq.merge(*self._series.values(), key=order_key)
