"""Market-data half of a risk decision: an as-of-time view, and the tracker that builds it.

A risk decision for a check at time ``ts`` may only use market data observed at or before its
**cutoff** (`cutoff_ms`): the decision time itself for the submit stage (the bar close the signal
was computed from), and the previous 1m close (``ts - 1``) for the fill stage. `MarketView` carries
its ``as_of_ms``; the engine refuses a view stamped after the cutoff (look-ahead guard) or too
old (staleness), so the view is always "<= previous close".

`MarketStateTracker` is fed in real time (market-data snapshots polled by the paper service,
closed 1m bars) and answers `view_at(cutoff, keys)`. A view is the latest snapshot observed at or
before the cutoff, made *more conservative* by anything learned later about times before the
cutoff (a stream reported unhealthy since a time <= cutoff counts as unhealthy).

Views are serialized into the paper journal (`to_payload` / `from_payload`) so a replay
re-evaluates the risk rules on exactly the market state the live session saw.
"""

from __future__ import annotations

import bisect
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from typing import Any

from tsclaude.contracts.market import Bar, Market, StreamState
from tsclaude.contracts.provenance import Provenance
from tsclaude.marketdata.models import MarketSnapshot
from tsclaude.sim.money import BPS, SIM_CONTEXT, dstr, q8, to_decimal

type SymbolKey = tuple[Market, str]


def cutoff_ms(stage: str, ts_ms: int) -> int:
    """Latest data time a check at `ts_ms` may use (see module docstring)."""
    if stage == "submit":
        return ts_ms
    if stage == "fill":
        return ts_ms - 1
    raise ValueError(f"unknown risk stage {stage!r}")


@dataclass(frozen=True, slots=True)
class SymbolMarket:
    """What the risk rules know about one (market, symbol) at a cutoff."""

    market: Market
    symbol: str
    unhealthy_streams: tuple[str, ...]  # stream ids not HEALTHY (empty = all healthy)
    stream_count: int  # streams known for the symbol (0 = unknown -> not healthy)
    open_gap_minutes: int
    last_bar_close_ms: int | None
    last_close: Decimal | None
    last_quote_volume: Decimal | None
    spread_bps: Decimal | None
    spread_as_of_ms: int | None
    median_spread_bps: Decimal | None
    spread_samples: int

    @property
    def streams_healthy(self) -> bool:
        return self.stream_count > 0 and not self.unhealthy_streams

    def to_payload(self) -> dict[str, Any]:
        def d(v: Decimal | None) -> str | None:
            return None if v is None else dstr(v)

        return {
            "market": str(self.market),
            "symbol": self.symbol,
            "unhealthy_streams": list(self.unhealthy_streams),
            "stream_count": self.stream_count,
            "open_gap_minutes": self.open_gap_minutes,
            "last_bar_close_ms": self.last_bar_close_ms,
            "last_close": d(self.last_close),
            "last_quote_volume": d(self.last_quote_volume),
            "spread_bps": d(self.spread_bps),
            "spread_as_of_ms": self.spread_as_of_ms,
            "median_spread_bps": d(self.median_spread_bps),
            "spread_samples": self.spread_samples,
        }

    @classmethod
    def from_payload(cls, p: Mapping[str, Any]) -> SymbolMarket:
        def d(v: Any) -> Decimal | None:
            return None if v is None else Decimal(str(v))

        return cls(
            market=Market(p["market"]),
            symbol=str(p["symbol"]),
            unhealthy_streams=tuple(str(s) for s in p["unhealthy_streams"]),
            stream_count=int(p["stream_count"]),
            open_gap_minutes=int(p["open_gap_minutes"]),
            last_bar_close_ms=p["last_bar_close_ms"],
            last_close=d(p["last_close"]),
            last_quote_volume=d(p["last_quote_volume"]),
            spread_bps=d(p["spread_bps"]),
            spread_as_of_ms=p["spread_as_of_ms"],
            median_spread_bps=d(p["median_spread_bps"]),
            spread_samples=int(p["spread_samples"]),
        )


@dataclass(frozen=True, slots=True)
class MarketView:
    """Market state at a cutoff (only the symbols a decision needs)."""

    as_of_ms: int  # time of the newest observation used (<= the cutoff it was built for)
    clock_offset_ms: int | None  # server - local; None = never sampled
    symbols: Mapping[SymbolKey, SymbolMarket] = field(default_factory=dict)

    def get(self, market: Market, symbol: str) -> SymbolMarket | None:
        return self.symbols.get((market, symbol))

    def to_payload(self) -> dict[str, Any]:
        return {
            "as_of_ms": self.as_of_ms,
            "clock_offset_ms": self.clock_offset_ms,
            "symbols": [
                self.symbols[k].to_payload()
                for k in sorted(self.symbols, key=lambda k: (str(k[0]), k[1]))
            ],
        }

    @classmethod
    def from_payload(cls, p: Mapping[str, Any]) -> MarketView:
        syms = [SymbolMarket.from_payload(s) for s in p["symbols"]]
        return cls(
            as_of_ms=int(p["as_of_ms"]),
            clock_offset_ms=p["clock_offset_ms"],
            symbols={(s.market, s.symbol): s for s in syms},
        )


# ---------------------------------------------------------------- tracker
@dataclass(frozen=True, slots=True)
class _Obs:
    """One snapshot, reduced to what the risk rules need."""

    as_of_ms: int
    running: bool
    clock_offset_ms: int | None
    streams: Mapping[SymbolKey, tuple[tuple[str, bool, int], ...]]  # (id, healthy, since_ms)
    gaps: Mapping[SymbolKey, int]


def spread_bps(bid: float | Decimal, ask: float | Decimal) -> Decimal:
    """(ask - bid) / mid in basis points, quantized to 1e-8; non-positive or crossed -> error."""
    b, a = to_decimal(bid), to_decimal(ask)
    if b <= 0 or a <= 0 or a < b:
        raise ValueError(f"invalid book top bid={bid} ask={ask}")
    with localcontext(SIM_CONTEXT):
        return q8((a - b) / ((a + b) / 2) * BPS)


def _median(values: list[Decimal]) -> Decimal:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    with localcontext(SIM_CONTEXT):
        return s[mid] if n % 2 else q8((s[mid - 1] + s[mid]) / 2)


class MarketStateTracker:
    """Keeps a bounded history of market observations and answers as-of-cutoff views."""

    def __init__(
        self,
        *,
        median_window_ms: int = 3_600_000,
        min_samples: int = 30,
        history_ms: int = 2 * 3_600_000,
    ) -> None:
        self.median_window_ms = median_window_ms
        self.min_samples = min_samples
        self.history_ms = max(history_ms, median_window_ms)
        self._obs: deque[_Obs] = deque()
        self._obs_times: deque[int] = deque()
        self._spreads: dict[SymbolKey, deque[tuple[int, Decimal]]] = {}
        self._bars: dict[SymbolKey, deque[tuple[int, Decimal, Decimal]]] = {}

    # ------------------------------------------------------------ feeding
    def observe_snapshot(self, snap: MarketSnapshot) -> None:
        """Record one `MarketDataService.snapshot()` (call about once per second)."""
        if self._obs_times and snap.as_of_ms < self._obs_times[-1]:
            return  # out of order: ignore (never rewrite history)
        streams: dict[SymbolKey, list[tuple[str, bool, int]]] = {}
        for st in snap.streams:
            healthy = (
                snap.running
                and st.state.provenance is not Provenance.STALE
                and st.state.value is StreamState.HEALTHY
            )
            streams.setdefault((st.market, st.symbol), []).append((st.stream, healthy, st.since_ms))
        gaps: dict[SymbolKey, int] = {}
        for sym in snap.symbols:
            key = (sym.market, sym.symbol)
            gaps[key] = sym.open_gap_minutes
            top = sym.best_bid_ask
            if top.value is not None and top.provenance is not Provenance.STALE:
                book = top.value
                try:
                    bps = spread_bps(book.bid_price, book.ask_price)
                except ValueError:
                    continue  # crossed / empty book: no sample (the spread stays unknown)
                received = min(book.received_at_ms, snap.as_of_ms)
                samples = self._spreads.setdefault(key, deque())
                if not samples or received > samples[-1][0]:
                    samples.append((received, bps))
        offset = snap.clock_offset_ms
        self._obs.append(
            _Obs(
                as_of_ms=snap.as_of_ms,
                running=snap.running,
                clock_offset_ms=offset.value if offset.value is not None else None,
                streams={k: tuple(sorted(v)) for k, v in streams.items()},
                gaps=gaps,
            )
        )
        self._obs_times.append(snap.as_of_ms)
        self._prune(snap.as_of_ms)

    def observe_bar(self, bar: Bar) -> None:
        """Record a closed 1m bar (its data is usable at cutoffs >= its close time)."""
        key = (bar.market, bar.symbol)
        seq = self._bars.setdefault(key, deque())
        if seq and bar.close_time_ms <= seq[-1][0]:
            return
        seq.append((bar.close_time_ms, to_decimal(bar.close), to_decimal(bar.quote_volume)))

    def _prune(self, now_ms: int) -> None:
        horizon = now_ms - self.history_ms
        while len(self._obs) > 1 and self._obs_times[1] <= horizon:
            self._obs.popleft()
            self._obs_times.popleft()
        for samples in self._spreads.values():
            while samples and samples[0][0] < horizon:
                samples.popleft()
        for bars in self._bars.values():
            while len(bars) > 1 and bars[1][0] <= horizon:
                bars.popleft()

    # ------------------------------------------------------------ querying
    def view_at(self, cutoff: int, keys: Iterable[SymbolKey]) -> MarketView | None:
        """The market state usable at `cutoff`, or None if nothing was observed by then."""
        times = list(self._obs_times)
        i = bisect.bisect_right(times, cutoff) - 1
        if i < 0:
            return None
        obs = self._obs[i]
        later = [self._obs[j] for j in range(i + 1, len(self._obs))]
        symbols: dict[SymbolKey, SymbolMarket] = {}
        for key in keys:
            streams = obs.streams.get(key, ())
            unhealthy = {sid for sid, ok, _ in streams if not ok}
            for o in later:  # learned later: unhealthy since a time <= cutoff
                for sid, ok, since in o.streams.get(key, ()):
                    if not ok and since <= cutoff:
                        unhealthy.add(sid)
            gap = obs.gaps.get(key, 0)
            samples = [(t, v) for t, v in self._spreads.get(key, ()) if t <= cutoff]
            window = [v for t, v in samples if t > cutoff - self.median_window_ms]
            last_spread = samples[-1] if samples else None
            bars = [b for b in self._bars.get(key, ()) if b[0] <= cutoff]
            last_bar = bars[-1] if bars else None
            symbols[key] = SymbolMarket(
                market=key[0],
                symbol=key[1],
                unhealthy_streams=tuple(sorted(unhealthy)),
                stream_count=len(streams),
                open_gap_minutes=gap,
                last_bar_close_ms=None if last_bar is None else last_bar[0],
                last_close=None if last_bar is None else last_bar[1],
                last_quote_volume=None if last_bar is None else last_bar[2],
                spread_bps=None if last_spread is None else last_spread[1],
                spread_as_of_ms=None if last_spread is None else last_spread[0],
                median_spread_bps=_median(window) if len(window) >= self.min_samples else None,
                spread_samples=len(window),
            )
        return MarketView(obs.as_of_ms, obs.clock_offset_ms if obs.running else None, symbols)
