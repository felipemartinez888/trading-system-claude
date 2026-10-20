"""No look-ahead: execution never at or inside the bar a decision was computed from."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal as D

from tsclaude.contracts.market import Bar, Market
from tsclaude.contracts.signals import EntryRuleKind, Side
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.sim.book import Book
from tsclaude.sim.clock import HistoricalClock
from tsclaude.sim.costs import CostModel, FeeSchedule, SlippageEstimate
from tsclaude.sim.engine import Simulator, SimView, earliest_execution_ms
from tsclaude.sim.orders import EntryRequest, ExitRequest
from tsclaude.sim.testing import (
    T0,
    events_of,
    minute,
    random_scenario,
    run_book,
    series,
    spot_spec,
)

SPOT = spot_spec()


def req(i: int, **kw: object) -> EntryRequest:
    args: dict[str, object] = {
        "request_id": f"r{i}",
        "market": Market.SPOT,
        "symbol": "BTCUSDT",
        "side": Side.LONG,
        "kind": EntryRuleKind.MARKET_NEXT_OPEN,
        "stop_price": D("900"),
        "valid_until_ms": minute(i + 5),
    }
    args.update(kw)
    return EntryRequest(**args)  # type: ignore[arg-type]


def test_earliest_execution_is_the_next_open() -> None:
    close = minute(0) + MS_PER_MINUTE - 1
    assert earliest_execution_ms(close, 0) == minute(1)
    assert earliest_execution_ms(close, 1) == minute(2)  # any latency pushes to the following open
    assert earliest_execution_ms(close, 60_000) == minute(2)
    assert earliest_execution_ms(close, 60_001) == minute(3)


def test_order_never_fills_at_or_inside_its_decision_bar_random_scenarios() -> None:
    """Every entry fill is on a bar whose open is after the decision close (60 seeded runs)."""
    fills_checked = 0
    for seed in range(60):
        sc = random_scenario(seed)
        _, res = run_book(sc.bars, sc.script, specs=sc.specs)
        submitted = {s["order_id"]: s for s in events_of(res, "sim_order_submitted")}
        for f in events_of(res, "sim_order_filled"):
            if f["role"] != "entry":
                continue
            sub = submitted[f["order_id"]]
            assert f["bar_open_ms"] > sub["decision_time_ms"]
            assert f["bar_open_ms"] >= sub["earliest_exec_ms"]
            fills_checked += 1
    assert fills_checked > 100


def test_limit_at_the_decision_bars_own_low_cannot_fill_inside_that_bar() -> None:
    """A source sees bar 0 (low 990) and places a limit at 995: only bar 1+ can fill it.

    Bar 1 never trades below 999 -> no fill at all, although bar 0 traded far through 995.
    """
    bars = series([(1000, 1001, 990, 1000), (1000, 1002, 999, 1001), (1001, 1003, 1000, 1002)])
    entry = req(0, kind=EntryRuleKind.LIMIT, entry_price=D("995"))
    book, res = run_book(bars, {(T0, "BTCUSDT"): [entry]}, specs=[SPOT])
    assert events_of(res, "sim_order_filled") == []
    assert book.trades == []


def test_decision_latency_delays_execution() -> None:
    bars = series([(1000, 1001, 999, 1000), (1000, 1001, 999, 1000), (1010, 1011, 1009, 1010)])
    _, res = run_book(bars, {(T0, "BTCUSDT"): [req(0)]}, specs=[SPOT], decision_latency_ms=1_000)
    (fill,) = events_of(res, "sim_order_filled")
    assert fill["bar_open_ms"] == minute(2)
    assert fill["raw_price"] == "1010.0"  # bar floats convert via repr


class FutureOracle:
    """Canary: a source holding the *whole* bar list, buying whenever the next open is higher.

    Prices only move between bars (each bar is flat, O=H=L=C) and alternate +10 / -10. Knowing the
    next jump is worthless if execution is at the next open: the position is bought after the +10
    jump and held through the -10 jump. A simulator that filled at the decision close would make
    +10 per trade instead.
    """

    def __init__(self, bars: Sequence[Bar]) -> None:
        self.by_open = {b.open_time_ms: b for b in bars}
        self.n = 0

    def on_bar(self, bar: Bar, view: SimView) -> Sequence[EntryRequest | ExitRequest]:
        nxt = self.by_open.get(bar.open_time_ms + MS_PER_MINUTE)
        if nxt is None or nxt.open <= bar.close:
            return []
        if view.position_qty("oracle", bar.market, bar.symbol) != 0:
            return []
        self.n += 1
        return [
            EntryRequest(
                request_id=f"o{self.n}",
                market=bar.market,
                symbol=bar.symbol,
                side=Side.LONG,
                kind=EntryRuleKind.MARKET_NEXT_OPEN,
                stop_price=D(str(bar.close)) - D("100"),
                valid_until_ms=bar.close_time_ms + MS_PER_MINUTE,
                time_exit_bars=1,
            )
        ]


@dataclass(frozen=True)
class ZeroSlippage:
    def estimate(
        self, market: Market, symbol: str, notional_hint: D, bar_quote_volume: D
    ) -> SlippageEstimate:
        return SlippageEstimate(D(0), D(0), basis="zero_for_test")


#: Fee- and slippage-free, so the canary measures only the timing of fills.
ZERO_COSTS = CostModel(
    fees={m: FeeSchedule(D(0), D(0)) for m in Market}, slippage=ZeroSlippage(), version="zero"
)


def test_future_knowing_source_cannot_get_a_better_fill() -> None:
    prices = [1000 + (10 if i % 2 else 0) for i in range(40)]
    bars = series([(p, p, p, p) for p in prices])
    book = Book("oracle", D(10_000), ZERO_COSTS, instruments=[SPOT])
    res = Simulator(HistoricalClock(bars), [book], FutureOracle(bars)).run()
    assert len(book.trades) >= 15
    for t in book.trades:
        assert t.entry_price == D("1010")  # bought only after the jump it "knew"
        assert t.exit_price == D("1000")
        assert t.gross_pnl < 0
    assert D(res.summaries["oracle"]["equity"]) < D(10_000)


def test_source_only_ever_sees_closed_bars_once_in_order() -> None:
    from tsclaude.sim.testing import ScriptedSource

    a = series([(1000, 1001, 999, 1000)] * 5, symbol="AAA")
    b = series([(50, 51, 49, 50)] * 5, symbol="BBB")
    src = ScriptedSource({})
    specs = [spot_spec("AAA"), spot_spec("BBB")]
    book = Book("baseline", D(1000), CostModel(), instruments=specs)
    Simulator(HistoricalClock(b + a), [book], src).run()
    assert src.seen == sorted(src.seen)
    assert len(src.seen) == len(set(src.seen)) == 10
