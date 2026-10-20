"""Clock, ledger, fill rules, costs, sizing limits and fail-closed behaviour."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal as D

import pytest

from tsclaude.contracts.market import Bar, Interval, Market
from tsclaude.contracts.signals import EntryRuleKind, RunMode, Side
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.sim.book import Book, BookConfig, RiskCheck, RiskDecision
from tsclaude.sim.clock import Clock, ClockError, HistoricalClock
from tsclaude.sim.costs import CostModel, Direction, FeeSchedule, Liquidity
from tsclaude.sim.engine import Simulator, SimView
from tsclaude.sim.fills import BarPrices, ExitReason, resolve_exit, split_at
from tsclaude.sim.ledger import CASH, Ledger, LedgerError
from tsclaude.sim.money import to_decimal
from tsclaude.sim.orders import EntryRequest, ExitRequest, InstrumentSpec
from tsclaude.sim.testing import (
    T0,
    bar,
    events_of,
    minute,
    run_book,
    series,
    spot_spec,
    usdm_spec,
)

SPOT = spot_spec()
FLAT = [(1000, 1001, 999, 1000)] * 4


def long_req(rid: str = "r1", **kw: object) -> EntryRequest:
    args: dict[str, object] = {
        "request_id": rid,
        "market": Market.SPOT,
        "symbol": "BTCUSDT",
        "side": Side.LONG,
        "kind": EntryRuleKind.MARKET_NEXT_OPEN,
        "stop_price": D("990"),
        "valid_until_ms": minute(10),
    }
    args.update(kw)
    return EntryRequest(**args)  # type: ignore[arg-type]


def reasons(res: object, kind: str) -> list[str]:
    return [p["reason"] for p in events_of(res, kind)]  # type: ignore[arg-type]


# ---------------------------------------------------------------- clock
def test_clock_merges_symbols_in_global_order_and_reports_gaps() -> None:
    a = series([(1, 1, 1, 1)] * 3, symbol="AAA")
    b = [bar(0, 2, 2, 2, 2, symbol="BBB"), bar(3, 2, 2, 2, 2, symbol="BBB")]
    clock = HistoricalClock(b + a, mode=RunMode.REPLAY)
    assert isinstance(clock, Clock)
    assert clock.mode is RunMode.REPLAY
    order = [(x.open_time_ms, x.symbol) for x in clock]
    assert order == sorted(order)
    assert len(clock) == 5
    (gap,) = clock.gaps
    assert (gap.symbol, gap.missing_bars) == ("BBB", 2)


def test_clock_rejects_unsorted_duplicates_and_non_1m() -> None:
    b0, b1 = bar(0, 1, 1, 1, 1), bar(1, 1, 1, 1, 1)
    with pytest.raises(ClockError, match="unsorted"):
        HistoricalClock([b1, b0])
    with pytest.raises(ClockError, match="duplicate"):
        HistoricalClock([b0, b0])
    five = b0.model_copy(update={"interval": Interval.M5, "close_time_ms": T0 + 5 * 60_000 - 1})
    with pytest.raises(ClockError, match="1m"):
        HistoricalClock([five])


def test_simulator_rejects_out_of_order_feed_and_emits_gap_event() -> None:
    bars = [bar(0, 1, 1, 1, 1), bar(3, 1, 1, 1, 1)]
    book = Book("b", D(100), CostModel(), instruments=[SPOT])
    sim = Simulator(HistoricalClock(bars), [book])
    sim.feed(bars)
    with pytest.raises(ClockError, match="out of order"):
        sim.step(bars[0])
    res = sim.finish()
    (gap,) = events_of(res, "sim_bar_gap")
    assert gap["missing_bars"] == 2
    # one equity point per minute that had bars, plus the origin
    assert [p.ts_ms for p in book.equity_curve] == [T0, T0 + 59_999, minute(3) + 59_999]


# ---------------------------------------------------------------- ledger
def test_ledger_rejects_unbalanced_unquantized_and_unknown_accounts() -> None:
    led = Ledger()
    with pytest.raises(LedgerError, match="unbalanced"):
        led.post(0, "x", [(CASH, D("1.00000000")), ("capital", D("-0.99999999"))])
    with pytest.raises(LedgerError, match="quantized"):
        led.post(0, "x", [(CASH, D("0.000000001")), ("capital", D("-0.000000001"))])
    with pytest.raises(LedgerError, match="unknown account"):
        led.post(0, "x", [("bogus", D("1")), ("capital", D("-1"))])
    with pytest.raises(LedgerError, match="empty"):
        led.post(0, "x", [(CASH, D("1")), (CASH, D("-1"))])
    led.post(0, "capital", [(CASH, D("100")), ("capital", D("-100"))])
    led.post(1, "fee", [("fees", D("0.5")), (CASH, D("-0.5"))])
    rep = led.reconcile()
    assert rep.ok
    assert rep.assets == rep.equity_from_entries == D("99.5")


def test_reconcile_detects_tampering() -> None:
    led = Ledger()
    led.post(0, "capital", [(CASH, D("100")), ("capital", D("-100"))])
    led._balances[CASH] = D("101")  # simulate a bug that mutates a running balance
    rep = led.reconcile()
    assert not rep.ok
    assert any("running balances" in p for p in rep.problems)


# ---------------------------------------------------------------- fills
def _prices(o: float, h: float, lo: float, c: float) -> BarPrices:
    return BarPrices.from_bar(bar(0, o, h, lo, c))


def test_assumed_path_visits_the_nearer_extreme_first_low_on_ties() -> None:
    assert _prices(100, 103, 99, 101).path() == (D("100.0"), D("99.0"), D("103.0"), D("101.0"))
    assert _prices(100, 101, 97, 99).path() == (D("100.0"), D("101.0"), D("97.0"), D("99.0"))
    assert _prices(100, 102, 98, 100).path()[1] == D("98.0")  # tie -> low first


def test_split_at_handles_gaps_and_misses() -> None:
    path = (D(100), D(99), D(103), D(101))
    assert split_at(path, D(102), down=False) == ((D(100), D(99), D(102)), (D(102), D(103), D(101)))
    assert split_at(path, D(101), down=True) == ((D(100),), path)  # already beyond at the start
    assert split_at(path, D(98), down=True) is None


def test_short_take_profit_needs_trade_through_and_stop_touch_counts() -> None:
    tick = D("0.1")
    # short, stop 110, TP 90: low exactly 90.0 is a touch -> no TP
    touch = resolve_exit(
        (D(100), D(90), D(101), D(95)),
        Side.SHORT,
        D(110),
        D(90),
        tick,
        held_at_open=True,
        market_exit_due=None,
        tp_liquidity=Liquidity.MAKER,
    )
    assert touch is None
    through = resolve_exit(
        (D(100), D("89.9"), D(101), D(95)),
        Side.SHORT,
        D(110),
        D(90),
        tick,
        held_at_open=True,
        market_exit_due=None,
        tp_liquidity=Liquidity.MAKER,
    )
    assert through is not None
    assert through.reason is ExitReason.TAKE_PROFIT
    assert through.raw_price == D(90)
    stop_touch = resolve_exit(
        (D(100), D(110), D(101), D(105)),
        Side.SHORT,
        D(110),
        None,
        tick,
        held_at_open=True,
        market_exit_due=None,
        tp_liquidity=Liquidity.MAKER,
    )
    assert stop_touch is not None
    assert stop_touch.reason is ExitReason.STOP_LOSS
    assert stop_touch.raw_price == D(110)
    assert not stop_touch.gap


def test_gap_beyond_stop_beats_a_due_time_exit_at_the_same_open() -> None:
    fill = resolve_exit(
        (D(980), D(985), D(975), D(982)),
        Side.LONG,
        D(990),
        None,
        D("0.1"),
        held_at_open=True,
        market_exit_due=ExitReason.TIME_EXIT,
        tp_liquidity=Liquidity.MAKER,
    )
    assert fill is not None
    assert (fill.reason, fill.raw_price, fill.gap) == (ExitReason.STOP_LOSS, D(980), True)


def test_same_bar_stop_entry_with_stop_inside_bar_range_is_stopped_and_flagged() -> None:
    """Long stop entry 1010, stop 1000; bar 1 (1005, 1012, 999, 1006).

    Path 1005 -> 999 -> 1012 -> 1006 (low nearer): the entry triggers on the way up, after the
    low. The path is an assumption, so the stop inside the bar range is treated as reachable after
    the entry: stop-loss in the entry bar, ambiguous_bar=True.
      entry = ceil_tick(1010 x 1.0003 = 1010.303) = 1010.4; qty = floor(50 / 10.4) = 4.807
      exit = floor_tick(1000 x 0.9997 = 999.7) = 999.7
    """
    bars = series([(1000, 1001, 999, 1000), (1005, 1012, 999, 1006)])
    req = long_req(kind=EntryRuleKind.STOP, entry_price=D("1010"), stop_price=D("1000"))
    book, _ = run_book(bars, {(T0, "BTCUSDT"): [req]}, specs=[SPOT])
    (t,) = book.trades
    assert (t.entry_price, t.qty, t.exit_price) == (D("1010.4"), D("4.807"), D("999.7"))
    assert t.exit_reason is ExitReason.STOP_LOSS
    assert t.ambiguous_bar
    assert t.entry_bar_open_ms == t.exit_bar_open_ms == minute(1)


def test_same_bar_limit_entry_does_not_see_the_high_before_its_fill() -> None:
    """Long limit 995 (TP 1000, stop 985); bar 1 (998, 1001, 994, 999): path 998 -> 1001 -> 994.

    The high (TP level 1000.1) came before the limit filled at 995 -> no same-bar TP.
    """
    bars = series([(1000, 1001, 999, 1000), (998, 1001, 994, 999)])
    req = long_req(
        kind=EntryRuleKind.LIMIT,
        entry_price=D("995"),
        stop_price=D("985"),
        take_profit_price=D("1000"),
    )
    book, res = run_book(bars, {(T0, "BTCUSDT"): [req]}, specs=[SPOT])
    assert book.trades == []
    assert book.open_positions == 1
    (fill,) = events_of(res, "sim_order_filled")
    assert fill["price"] == "995.0"


# ---------------------------------------------------------------- costs and rounding
def test_cost_model_stress_scales_fees_and_slippage() -> None:
    cm = CostModel().scaled(D("1.5"))
    assert cm.fee_rate(Market.USDM, Liquidity.TAKER) == D("0.00075")
    ex = cm.price(
        market=Market.SPOT,
        symbol="X",
        direction=Direction.BUY,
        liquidity=Liquidity.TAKER,
        raw_price=D(1000),
        tick_size=D("0.1"),
        notional_hint=D(1),
        bar_quote_volume=D(1),
    )
    assert ex.slippage is not None
    assert ex.slippage.total_bps == D("4.5")
    assert ex.price == D("1000.5")  # ceil(1000 x 1.00045 = 1000.45)
    with pytest.raises(KeyError):
        CostModel(fees={Market.SPOT: FeeSchedule(D(0), D(0))}).fee_rate(
            Market.USDM, Liquidity.MAKER
        )


def test_instrument_rounding_and_float_conversion() -> None:
    spec = InstrumentSpec(Market.SPOT, "X", D("0.5"), D("0.01"))
    assert spec.round_price(D("10.2"), Direction.BUY) == D("10.5")
    assert spec.round_price(D("10.2"), Direction.SELL) == D("10.0")
    assert spec.round_qty(D("1.239")) == D("1.23")
    assert spec.round_qty(D("-1")) == D(0)
    assert to_decimal(0.1) == D("0.1")
    with pytest.raises(ValueError, match="non-finite"):
        to_decimal(float("nan"))


def test_request_validation() -> None:
    with pytest.raises(ValueError, match="below"):
        long_req(kind=EntryRuleKind.LIMIT, entry_price=D("980"))
    with pytest.raises(ValueError, match="need entry_price"):
        long_req(kind=EntryRuleKind.STOP)
    with pytest.raises(ValueError, match="must not carry"):
        long_req(entry_price=D("1000"))
    with pytest.raises(ValueError, match="wrong side"):
        long_req(kind=EntryRuleKind.LIMIT, entry_price=D("995"), take_profit_price=D("994"))


# ---------------------------------------------------------------- sizing limits
def test_sizing_clips_by_max_notional_cash_and_min_notional() -> None:
    bars = series(FLAT)
    # Tight stop: risk qty = 50 / 0.4 = 125 -> capped by 1x notional cap 10000/1000.3 and cash.
    tight = long_req(stop_price=D("999.9"))
    book, res = run_book(bars, {(T0, "BTCUSDT"): [tight]}, specs=[SPOT])
    (opened,) = events_of(res, "sim_position_opened")
    assert opened["sizing_binding"] == "cash"
    assert opened["qty"] == "9.987"  # floor(10000 / (1000.3 x 1.001) = 9.98701...)
    assert D(opened["notional"]) + D(opened["entry_fee"]) <= D(10_000)
    # Absolute notional cap.
    cfg = BookConfig(max_notional_abs=D("2000"))
    _, res = run_book(bars, {(T0, "BTCUSDT"): [tight]}, specs=[SPOT], config=cfg)
    (opened,) = events_of(res, "sim_position_opened")
    assert opened["sizing_binding"] == "max_notional"
    assert opened["qty"] == "1.999"  # floor(2000 / 1000.3)
    # Min notional rejects.
    big_min = InstrumentSpec(Market.SPOT, "BTCUSDT", D("0.1"), D("0.001"), D("6000"))
    book, res = run_book(bars, {(T0, "BTCUSDT"): [long_req()]}, specs=[big_min])
    assert reasons(res, "sim_order_rejected") == ["below_min_notional"]
    assert book.ledger.reconcile().ok
    assert book.cash == D(10_000)


def test_short_requires_usdm_and_one_position_per_symbol() -> None:
    bars = series(FLAT)
    short_spot = long_req(side=Side.SHORT, stop_price=D("1010"))
    dup = [long_req("a"), long_req("b"), long_req("a")]
    _, res = run_book(bars, {(T0, "BTCUSDT"): [short_spot, *dup]}, specs=[SPOT])
    assert reasons(res, "sim_request_rejected") == [
        "short_requires_usdm",
        "symbol_busy",
        "duplicate_request_id",
    ]


# ---------------------------------------------------------------- fail closed
def test_risk_hook_veto_error_and_invalid_decision_block_entries() -> None:
    bars = series(FLAT)
    script = {(T0, "BTCUSDT"): [long_req()]}

    def veto(check: RiskCheck) -> RiskDecision:
        return RiskDecision(False, "daily_loss")

    def boom(check: RiskCheck) -> RiskDecision:
        raise RuntimeError("risk state unknown")

    def garbage(check: RiskCheck) -> RiskDecision:
        return "yes"  # type: ignore[return-value]

    def veto_at_fill(check: RiskCheck) -> RiskDecision:
        return RiskDecision(check.stage == "submit", "stale_data")

    expected = {
        veto: ("sim_request_rejected", "risk_blocked:daily_loss"),
        boom: ("sim_request_rejected", "risk_blocked:risk_error:RuntimeError"),
        garbage: ("sim_request_rejected", "risk_blocked:invalid_decision"),
        veto_at_fill: ("sim_order_rejected", "risk_blocked:stale_data"),
    }
    for hook, (kind, reason) in expected.items():
        book, res = run_book(bars, script, specs=[SPOT], risk_hook=hook)
        assert reasons(res, kind) == [reason]
        assert book.trades == []
        assert book.open_positions == 0
        assert book.cash == D(10_000)


class _BrokenSource:
    def on_bar(self, bar: Bar, view: SimView) -> Sequence[EntryRequest | ExitRequest]:
        raise ValueError("feature pipeline failed")


def test_source_error_is_an_event_and_produces_no_orders() -> None:
    bars = series(FLAT)
    book = Book("b", D(1000), CostModel(), instruments=[SPOT])
    res = Simulator(HistoricalClock(bars), [book], _BrokenSource()).run()
    errors = [e for e in res.events if e["type"] == "sim_source_error"]
    assert len(errors) == 4
    assert events_of(res, "sim_order_submitted") == []


def test_unknown_book_and_unknown_instrument_are_rejected() -> None:
    bars = series(FLAT)
    script = {
        (T0, "BTCUSDT"): [
            long_req("a", books=("nope",)),
            long_req("b", symbol="ETHUSDT"),
        ]
    }
    _, res = run_book(bars, script, specs=[SPOT])
    assert reasons(res, "sim_request_rejected") == ["unknown_book", "unknown_instrument"]


def test_exit_request_closes_at_next_open_or_cancels_pending() -> None:
    bars = series([*FLAT, (1003, 1004, 1002, 1003)])
    script: dict[tuple[int, str], list[EntryRequest | ExitRequest]] = {
        (T0, "BTCUSDT"): [long_req()],
        (minute(2), "BTCUSDT"): [ExitRequest("x1", Market.SPOT, "BTCUSDT")],
    }
    book, _ = run_book(bars, script, specs=[SPOT])
    (t,) = book.trades
    assert (t.exit_reason, t.exit_bar_open_ms) == (ExitReason.EXIT_SIGNAL, minute(3))
    pend = {
        (T0, "BTCUSDT"): [long_req(kind=EntryRuleKind.LIMIT, entry_price=D("995"))],
        (minute(1), "BTCUSDT"): [ExitRequest("x1", Market.SPOT, "BTCUSDT")],
    }
    _, res = run_book(bars, pend, specs=[SPOT])
    assert reasons(res, "sim_order_cancelled") == ["exit_signal"]


def test_end_of_data_cancels_pending_and_keeps_positions_marked() -> None:
    bars = series([(1000, 1001, 999, 1000), (1000, 1005, 999, 1004)])
    script = {(T0, "BTCUSDT"): [long_req()]}
    usdm = usdm_spec("ETHUSDT")
    eth = series([(10, 10, 10, 10)] * 2, symbol="ETHUSDT", market=Market.USDM)
    script[(T0, "ETHUSDT")] = [
        long_req(
            "e",
            market=Market.USDM,
            symbol="ETHUSDT",
            kind=EntryRuleKind.LIMIT,
            entry_price=D("9"),
            stop_price=D("8"),
            valid_until_ms=minute(50),
        )
    ]
    book, res = run_book(bars + eth, script, specs=[SPOT, usdm])
    assert reasons(res, "sim_order_cancelled") == ["end_of_data"]
    assert book.open_positions == 1
    assert book.equity() == book.equity_curve[-1].equity
    assert book.equity() != D(10_000)


def test_one_minute_close_marks_every_book_once() -> None:
    bars = series(FLAT) + series(FLAT, symbol="ETHUSDT")
    books = [Book(n, D(1000), CostModel(), instruments=[SPOT]) for n in ("a", "b")]
    Simulator(HistoricalClock(bars), books).run()
    for b in books:
        assert [p.ts_ms for p in b.equity_curve] == [T0] + [
            minute(i) + MS_PER_MINUTE - 1 for i in range(4)
        ]


class _GarbageSource:
    def on_bar(self, bar: Bar, view: SimView) -> Sequence[EntryRequest | ExitRequest]:
        return ["not a request"]  # type: ignore[list-item]


def test_malformed_request_is_rejected_not_raised() -> None:
    bars = series(FLAT)
    book = Book("b", D(1000), CostModel(), instruments=[SPOT])
    res = Simulator(HistoricalClock(bars), [book], _GarbageSource()).run()
    assert reasons(res, "sim_request_rejected") == ["invalid_request"] * 4
