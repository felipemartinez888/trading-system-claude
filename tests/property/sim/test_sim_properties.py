"""Hypothesis properties for the simulator (run in CI; hypothesis is not available offline).

- ledger always balances, equity == reconstruction from entries at every bar, cash never negative;
- size never exceeds risk / notional / cash limits, quantity always a positive lot multiple;
- chunking the bar stream never changes the fingerprint;
- limits never fill on a touch; SL and TP reachable in one bar always resolve SL-first.
"""

from __future__ import annotations

from decimal import Decimal as D

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tsclaude.contracts.signals import Side
from tsclaude.sim.book import Book, BookConfig
from tsclaude.sim.clock import HistoricalClock
from tsclaude.sim.costs import CostModel, Direction, Liquidity
from tsclaude.sim.engine import Simulator
from tsclaude.sim.fills import (
    BarPrices,
    ExitReason,
    resolve_entry,
    resolve_exit,
    resolve_fill_bar_exit,
)
from tsclaude.sim.orders import OrderType
from tsclaude.sim.testing import (
    InvariantSource,
    ScriptedSource,
    bar,
    check_book_invariants,
    chunked,
    random_scenario,
)

TICK = D("0.1")
SETTINGS = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@SETTINGS
@given(
    seed=st.integers(min_value=0, max_value=10**9),
    signal_pct=st.integers(min_value=2, max_value=30),
    gap_pct=st.integers(min_value=0, max_value=10),
    risk_bps=st.integers(min_value=10, max_value=500),
    notional_pct=st.integers(min_value=10, max_value=100),
    abs_cap=st.sampled_from([None, 500, 3000, 20000]),
    stress=st.sampled_from(["1", "1.5", "2"]),
)
def test_ledger_balances_and_sizes_respect_limits(
    seed: int,
    signal_pct: int,
    gap_pct: int,
    risk_bps: int,
    notional_pct: int,
    abs_cap: int | None,
    stress: str,
) -> None:
    sc = random_scenario(seed, n_bars=150, signal_prob=signal_pct / 100, gap_prob=gap_pct / 100)
    config = BookConfig(
        risk_per_trade=D(risk_bps) / D(10_000),
        max_notional_fraction=D(notional_pct) / D(100),
        max_notional_abs=None if abs_cap is None else D(abs_cap),
    )
    book = Book(
        "baseline", D(10_000), CostModel().scaled(D(stress)), instruments=sc.specs, config=config
    )
    src = InvariantSource(ScriptedSource(sc.script), [book])
    res = Simulator(HistoricalClock(sc.bars), [book], src).run()
    assert src.problems == []
    assert check_book_invariants(book, res) == []
    assert book.ledger.reconcile().ok


@SETTINGS
@given(
    seed=st.integers(min_value=0, max_value=10**9),
    sizes=st.lists(st.integers(min_value=1, max_value=60), min_size=1, max_size=40),
)
def test_chunking_never_changes_the_fingerprint(seed: int, sizes: list[int]) -> None:
    sc = random_scenario(seed, n_bars=120)

    def make() -> Simulator:
        book = Book("baseline", D(10_000), CostModel(), instruments=sc.specs)
        return Simulator(HistoricalClock(sc.bars), [book], ScriptedSource(sc.script))

    reference = make().run().fingerprint
    sim = make()
    for chunk in chunked(list(HistoricalClock(sc.bars)), sizes):
        sim.feed(chunk)
    assert sim.finish().fingerprint == reference


ohlc_ticks = st.tuples(
    st.integers(min_value=900, max_value=1100),  # open
    st.integers(min_value=0, max_value=60),  # high above max(open, close)
    st.integers(min_value=0, max_value=60),  # low below min(open, close)
    st.integers(min_value=900, max_value=1100),  # close
)


def _bar(t: tuple[int, int, int, int]) -> BarPrices:
    o, up, down, c = t
    h, lo = max(o, c) + up, min(o, c) - down
    return BarPrices.from_bar(bar(0, o * TICK, h * TICK, lo * TICK, c * TICK))


@SETTINGS
@given(t=ohlc_ticks, level=st.integers(min_value=800, max_value=1200), buy=st.booleans())
def test_limit_never_fills_without_trade_through(
    t: tuple[int, int, int, int], level: int, buy: bool
) -> None:
    prices = _bar(t)
    limit = level * TICK
    fill = resolve_entry(
        prices,
        OrderType.LIMIT,
        Direction.BUY if buy else Direction.SELL,
        limit,
        TICK,
        arrival=False,
        open_ok=True,
        intrabar_ok=True,
    )
    through = prices.low <= limit - TICK if buy else prices.high >= limit + TICK
    assert (fill is not None) == through
    if fill is not None:
        assert fill.raw_price == limit  # never a better price than the limit
        assert fill.liquidity is Liquidity.MAKER


@SETTINGS
@given(
    t=ohlc_ticks,
    stop_off=st.integers(min_value=1, max_value=80),
    tp_off=st.integers(min_value=1, max_value=80),
    long=st.booleans(),
)
def test_sl_and_tp_reachable_in_one_bar_is_always_sl_first(
    t: tuple[int, int, int, int], stop_off: int, tp_off: int, long: bool
) -> None:
    prices = _bar(t)
    o = prices.open
    sign = 1 if long else -1
    stop = o - sign * stop_off * TICK
    tp = o + sign * tp_off * TICK
    fill = resolve_exit(
        prices.path(),
        Side.LONG if long else Side.SHORT,
        stop,
        tp,
        TICK,
        held_at_open=True,
        market_exit_due=None,
        tp_liquidity=Liquidity.MAKER,
    )
    sl_hit = prices.low <= stop if long else prices.high >= stop
    tp_hit = prices.high >= tp + TICK if long else prices.low <= tp - TICK
    if sl_hit and tp_hit:
        assert fill is not None
        assert fill.reason is ExitReason.STOP_LOSS
        assert fill.ambiguous
    elif sl_hit:
        assert fill is not None
        assert fill.reason is ExitReason.STOP_LOSS
    elif tp_hit:
        assert fill is not None
        assert fill.reason is ExitReason.TAKE_PROFIT
    else:
        assert fill is None


@SETTINGS
@given(
    t=ohlc_ticks,
    stop_off=st.integers(min_value=-80, max_value=80),
    long=st.booleans(),
)
def test_fill_bar_exit_is_only_ever_a_flagged_stop(
    t: tuple[int, int, int, int], stop_off: int, long: bool
) -> None:
    """L-04: an intrabar entry's fill bar can only stop it out (flagged), never take profit."""
    prices = _bar(t)
    side = Side.LONG if long else Side.SHORT
    entry = prices.close
    stop = entry - (stop_off * TICK if long else -stop_off * TICK)
    fill = resolve_fill_bar_exit(side, entry, stop, prices.low, prices.high)
    in_range = prices.low <= stop if long else prices.high >= stop
    if in_range:
        assert fill is not None
        assert fill.reason is ExitReason.STOP_LOSS
        assert fill.ambiguous
        assert fill.raw_price == stop
    else:
        assert fill is None
