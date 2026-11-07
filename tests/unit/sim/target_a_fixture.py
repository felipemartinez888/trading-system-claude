"""Target A fixture: a fully synthetic, deterministic and causal 1m bar series that satisfies the
CONFIRMED conditions of the unmodified `bracket_reference` (default parameters).

Nothing here is market data and nothing is random. The series is built from a 15m plan and expanded
to 1m bars whose 15m aggregates reproduce the plan exactly (each 15m bar is three straight 1m legs
of five minutes through its waypoints; 1m highs and lows are the leg endpoints, so no 1m bar
leaves its own range).

The plan repeats a *calm → breakout* cycle of `CYCLE` 15m bars:

1. `BURST` bars of wide range (`HIGH_RANGE`), so the later ATR rank is measured against real
   volatility;
2. a linear decline of the range to `LOW_RANGE`: the ATR of the bar before the breakout is the
   lowest of its 96-bar window (compression, rank <= 0.35);
3. one breakout bar: a large up move above the prior 20-bar high on a volume spike.

`LOW_RANGE` is deliberately not tiny: the production cost filter (`max_cost_r` 0.2 with the paper
cost model) only confirms a signal when the stop is far enough from the price, i.e. when the 15m ATR
is at least ~0.87 % of the price. This is a property of the rules, not something the fixture bends.

The strategy never sees a bar before it closes: every `Bar` is a *closed* bar, delivered by the
caller in order. Warm-up bars all close before the first decision the test lets through.
"""

from __future__ import annotations

from dataclasses import dataclass

from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.contracts.timeutil import MS_PER_MINUTE

#: 2025-01-01T00:00:00Z, aligned to a 15m boundary
T0 = 1_735_689_600_000
M15 = 15 * MS_PER_MINUTE
SYMBOL = "BTCUSDT"
START_PRICE = 30_000.0

CYCLE = 130  # 15m bars per calm -> breakout cycle
BURST = 30  # wide-range bars at the start of a cycle
HIGH_RANGE = 0.016  # 15m range as a fraction of price, burst
LOW_RANGE = 0.0100  # 15m range at the end of the decline
DRIFT = 0.0001  # per-bar log drift (keeps the close above the EMA)
BREAKOUT_MOVE = 0.03
BASE_VOLUME = 100.0
BREAKOUT_VOLUME = 300.0
N_CYCLES = 9  # 9 * 130 = 1,170 15m bars
TAIL = 40  # extra 15m bars after the last breakout, so its shadow fill can resolve


@dataclass(frozen=True, slots=True)
class Plan15:
    index: int
    open: float
    waypoints: tuple[float, float]
    close: float
    volume: float


def _range_at(k: int) -> float:
    pos = k % CYCLE
    if pos < BURST:
        return HIGH_RANGE
    decline = CYCLE - 1 - BURST  # bars of decline, then the breakout bar
    frac = min(1.0, (pos - BURST) / decline)
    return HIGH_RANGE + (LOW_RANGE - HIGH_RANGE) * frac


def is_breakout(k: int) -> bool:
    return k % CYCLE == CYCLE - 1


def build_plan() -> list[Plan15]:
    plan: list[Plan15] = []
    price = START_PRICE
    for k in range(CYCLE * N_CYCLES + TAIL):
        a = _range_at(k)
        volume = BASE_VOLUME * (1.05 if k % 2 == 0 else 0.95)
        if is_breakout(k):
            close = price * (1 + BREAKOUT_MOVE)
            # a small dip, a rise to just under the close, then a last up leg: the final 1m bar
            # opens below its close (a flat decision bar would hide a stale or open price)
            wp = (price * (1 - 0.1 * a), close * 0.998)
            volume = BREAKOUT_VOLUME
        elif k % 2 == 0:  # up bar: dip, rise to the high, settle
            close = price * (1 + 0.4 * a + DRIFT)
            wp = (price * (1 - 0.3 * a), close * (1 + 0.3 * a))
        else:  # down bar: pop, fall to the low, settle
            close = price * (1 - 0.4 * a + DRIFT)
            wp = (price * (1 + 0.3 * a), close * (1 - 0.3 * a))
        plan.append(Plan15(k, price, wp, close, volume))
        price = close
    return plan


def _leg(p0: float, p1: float, steps: int) -> list[float]:
    return [p0 + (p1 - p0) * (j + 1) / steps for j in range(steps)]


def expand(plan: list[Plan15], symbol: str = SYMBOL) -> list[Bar]:
    """Three straight 1m legs per 15m bar (5+5+5 minutes) through its two waypoints."""
    bars: list[Bar] = []
    for p in plan:
        pts = [p.open, p.waypoints[0], p.waypoints[1], p.close]
        sizes = (5, 5, 5)
        closes: list[float] = []
        start = p.open
        for end, size in zip(pts[1:], sizes, strict=True):
            closes.extend(_leg(start, end, size))
            start = end
        assert len(closes) == 15
        prev = p.open
        for j, c in enumerate(closes):
            t = T0 + p.index * M15 + j * MS_PER_MINUTE
            hi, lo = max(prev, c), min(prev, c)
            vol = p.volume / 15.0
            bars.append(
                Bar(
                    market=Market.SPOT,
                    symbol=symbol,
                    interval=Interval.M1,
                    open_time_ms=t,
                    close_time_ms=t + MS_PER_MINUTE - 1,
                    open=prev,
                    high=hi,
                    low=lo,
                    close=c,
                    volume=vol,
                    quote_volume=vol * c,
                    trades=1,
                    taker_buy_volume=vol / 2,
                    taker_buy_quote_volume=vol * c / 2,
                    source=BarSource.WS_LIVE,
                    ingested_at_ms=t + MS_PER_MINUTE,
                )
            )
            prev = c
    return bars


def fixture_bars() -> list[Bar]:
    return expand(build_plan())


def trade_from_ms() -> int:
    """The first cycle that starts after the strategy's production warm-up (741 15m bars)."""
    return T0 + 6 * CYCLE * M15  # bar 780
