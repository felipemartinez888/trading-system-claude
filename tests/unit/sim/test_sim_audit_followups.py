"""Causality audit M2-T2 follow-ups: N1 risk-hook timing, N2 reject reasons, N3 surviving mutations
(M13 multi-symbol sizing, M14 rounding direction on non-exact amounts, M15 TP gap at the open),
N4 funding, N6 spans_gap. Hand computations in the docstrings."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from tsclaude.contracts.market import Market
from tsclaude.contracts.signals import EntryRuleKind, Side
from tsclaude.sim.book import Book, RiskCheck, RiskDecision, funding_cash_flow
from tsclaude.sim.clock import HistoricalClock
from tsclaude.sim.costs import CostModel, Direction, Liquidity
from tsclaude.sim.engine import Simulator
from tsclaude.sim.fills import ExitReason
from tsclaude.sim.orders import EntryRequest, InstrumentSpec
from tsclaude.sim.testing import (
    T0,
    ScriptedSource,
    events_of,
    minute,
    run_book,
    series,
    spot_spec,
    usdm_spec,
)


def req(rid: str = "r1", **kw: object) -> EntryRequest:
    args: dict[str, object] = {
        "request_id": rid,
        "market": Market.SPOT,
        "symbol": "BTCUSDT",
        "side": Side.LONG,
        "kind": EntryRuleKind.MARKET_NEXT_OPEN,
        "stop_price": D("990"),
        "take_profit_price": D("1020"),
        "valid_until_ms": minute(30),
    }
    args.update(kw)
    return EntryRequest(**args)  # type: ignore[arg-type]


# ---------------------------------------------------------------- N3 / M13 + M15
def test_m13_sizing_in_a_minute_uses_previous_close_equity_not_live_equity() -> None:
    """Two spot symbols; within a minute AAA is processed before BBB.

    AAA: market long at bar 1 open (as G1): entry 1000.3, qty 4.854, notional 4855.4562,
         fee 4.8554562 -> cash 5139.6883438. Minute-1 close equity = 5139.6883438 + 4.854 x 1002
         = 10003.3963438.
    Bar 2 of AAA opens at 1030, above TP 1020 + 1 tick -> TP at 1020 (M15: never the better open
         1030): proceeds 4951.08, fee 4.95108 -> live cash/equity 10085.8172638.
    BBB: market long decided at bar 1 close, executes at bar 2 open 100 (tick 0.01):
         entry = ceil(100 x 1.0003) = 100.03, stop 99 -> risk/unit 1.03.
         previous-close sizing: floor(0.005 x 10003.3963438 / 1.03 = 48.5601...) = 48.560
         (live equity would give floor(48.9602...) = 48.960 -- the mutation M13 kills).
         available cash = min(cash at minute-1 close 5139.6883438, live cash) = 5139.6883438.
    """
    aaa = series(
        [(1000, 1001, 999, 1000), (1000, 1005, 998, 1002), (1030, 1031, 1029, 1030)],
        symbol="AAA",
    )
    bbb = series([(100, 100.5, 99.5, 100)] * 3, symbol="BBB")
    script = {
        (T0, "AAA"): [req("a", symbol="AAA")],
        (minute(1), "BBB"): [req("b", symbol="BBB", stop_price=D("99"), take_profit_price=None)],
    }
    specs = [spot_spec("AAA"), spot_spec("BBB", tick="0.01")]
    book, res = run_book(aaa + bbb, script, specs=specs)
    (t_a,) = book.trades
    assert t_a.exit_reason is ExitReason.TAKE_PROFIT
    assert (t_a.exit_price, t_a.exit_at_open, t_a.exit_bar_open_ms) == (D("1020"), True, minute(2))
    opened = {p["symbol"]: p for p in events_of(res, "sim_position_opened")}
    b = opened["BBB"]
    assert b["sizing_equity"] == "10003.39634380"
    assert b["sizing_cash"] == "5139.68834380"
    assert b["entry_price"] == "100.03"
    assert b["qty"] == "48.560"


def test_m15_short_take_profit_gapped_through_at_open_fills_at_tp() -> None:
    """USD-M short at bar 1 open 2000 (entry 1999.4, qty 2.427, as G2), TP 1950.

    Bar 2 opens at 1940, below TP - 1 tick -> TP fills at 1950 (maker), not at the open 1940.
        exit notional 2.427 x 1950 = 4732.65; gross = 4852.5438 - 4732.65 = 119.8938
    """
    bars = series(
        [(2000, 2010, 1999, 2000), (2000, 2005, 1995, 2003), (1940, 1945, 1935, 1942)],
        market=Market.USDM,
    )
    short = req(
        market=Market.USDM, side=Side.SHORT, stop_price=D("2020"), take_profit_price=D("1950")
    )
    book, _ = run_book(bars, {(T0, "BTCUSDT"): [short]}, specs=[usdm_spec()])
    (t,) = book.trades
    assert (t.exit_reason, t.exit_at_open, t.exit_price) == (
        ExitReason.TAKE_PROFIT,
        True,
        D("1950"),
    )
    assert t.exit_liquidity is Liquidity.MAKER
    assert t.gross_pnl == D("119.8938")


# ---------------------------------------------------------------- N3 / M14
FINE = InstrumentSpec(Market.SPOT, "ALTUSDT", D("0.000001"), D("0.001"))


def test_m14_fill_costs_round_adversely_on_non_exact_amounts() -> None:
    """qty 20383.204 x price 0.123453 = 2516.367683412 (9 decimals).

    buy notional  -> ceil  2516.36768342 (half-even would give ...41)
    sell proceeds of qty x 0.125004 = 2547.982032816 -> floor 2547.98203281 (half-even ...82)
    fees at 0.1 %: 2.51636768342 -> ceil 2.51636769; 2.54798203281 -> ceil 2.54798204
    """
    cm = CostModel()
    qty = D("20383.204")
    for direction, price, notional, fee in (
        (Direction.BUY, D("0.123453"), "2516.36768342", "2.51636769"),
        (Direction.SELL, D("0.125004"), "2547.98203281", "2.54798204"),
    ):
        ex = cm.price(
            market=Market.SPOT,
            symbol="ALTUSDT",
            direction=direction,
            liquidity=Liquidity.MAKER,
            raw_price=price,
            tick_size=FINE.tick_size,
            notional_hint=D(1),
            bar_quote_volume=D(1),
        )
        costs = cm.fill_costs(Market.SPOT, ex, qty)
        assert (str(costs.notional), str(costs.fee)) == (notional, fee)


def test_m14_golden_trade_with_non_exact_amounts() -> None:
    """Spot long limit 0.123453 (stop 0.121, TP 0.125004) on a 1e-6 tick, 1e-3 lot instrument.

    qty = floor_lot(50 / 0.002453 = 20383.2042...) = 20383.204
    Bar 1 low 0.123452 = limit - 1 tick -> maker fill; entry notional 2516.36768342 (ceil), fee
        2.51636769 (ceil). No TP in the fill bar (L-04).
    Bar 2 high 0.125005 = TP + 1 tick -> maker TP: proceeds 2547.98203281 (floor), fee 2.54798204
    gross = 2547.98203281 - 2516.36768342 = 31.61434939
    net = 31.61434939 - 2.51636769 - 2.54798204 = 26.54999966
    1R = q8(20383.204 x 0.002453 = 49.999999412) = 49.99999941 -> R = q8(0.53100000...) = 0.531
    """
    bars = series(
        [
            (0.124, 0.1241, 0.1239, 0.124),
            (0.1236, 0.1237, 0.123452, 0.1235),
            (0.1245, 0.125005, 0.1244, 0.1249),
        ],
        symbol="ALTUSDT",
    )
    entry = req(
        symbol="ALTUSDT",
        kind=EntryRuleKind.LIMIT,
        entry_price=D("0.123453"),
        stop_price=D("0.121"),
        take_profit_price=D("0.125004"),
    )
    book, _ = run_book(bars, {(T0, "ALTUSDT"): [entry]}, specs=[FINE])
    (t,) = book.trades
    assert t.qty == D("20383.204")
    assert t.exit_reason is ExitReason.TAKE_PROFIT
    assert t.gross_pnl == D("31.61434939")
    assert t.fees == D("5.06434973")
    assert t.net_pnl == D("26.54999966")
    assert t.risk_amount == D("49.99999941")
    assert t.r_multiple == D("0.53100000")
    assert book.ledger.reconcile().ok


# ---------------------------------------------------------------- N1
def test_n1_fill_stage_risk_check_is_stamped_with_the_execution_time() -> None:
    seen: list[tuple[str, int]] = []

    def hook(check: RiskCheck) -> RiskDecision:
        seen.append((check.stage, check.ts_ms))
        return RiskDecision(True)

    bars = series([(1000, 1001, 999, 1000), (1000, 1005, 998, 1002)])
    run_book(bars, {(T0, "BTCUSDT"): [req()]}, specs=[spot_spec()], risk_hook=hook)
    assert seen == [("submit", T0 + 59_999), ("fill", minute(1))]


# ---------------------------------------------------------------- N2
def test_n2_stop_breached_at_entry_has_its_own_counted_reason() -> None:
    """Market long, stop 990; bar 1 opens at 985 (already beyond the stop) -> rejected, counted."""
    bars = series([(1000, 1001, 999, 1000), (985, 986, 984, 985)])
    book, res = run_book(bars, {(T0, "BTCUSDT"): [req()]}, specs=[spot_spec()])
    assert [p["reason"] for p in events_of(res, "sim_order_rejected")] == ["stop_breached_at_entry"]
    assert book.summary()["rejections_by_reason"] == {"stop_breached_at_entry": 1}
    assert book.trades == []


# ---------------------------------------------------------------- N4
def test_n4_funding_sign_and_adverse_rounding_both_sides() -> None:
    """rate 0.0001234, qty 2.5, mark 2003.17: rate x notional = 0.0001234 x 5007.925 = 0.617977945

    long  (rate > 0 pays):     -0.617977945 -> floor -0.61797795 (pays the extra 1e-8)
    short (rate > 0 receives): +0.617977945 -> floor +0.61797794 (receives 1e-8 less)
    negative rate flips the sides.
    """
    rate, qty, mark = D("0.0001234"), D("2.5"), D("2003.17")
    assert funding_cash_flow(rate, Side.LONG, qty, mark) == D("-0.61797795")
    assert funding_cash_flow(rate, Side.SHORT, qty, mark) == D("0.61797794")
    assert funding_cash_flow(-rate, Side.LONG, qty, mark) == D("0.61797794")
    assert funding_cash_flow(-rate, Side.SHORT, qty, mark) == D("-0.61797795")


@pytest.mark.parametrize(
    ("side", "stop", "tp", "expected"),
    [(Side.LONG, "1980", "2040", "-0.50077500"), (Side.SHORT, "2020", "1950", "0.50077500")],
)
def test_n4_funding_golden_trade_both_sides(side: Side, stop: str, tp: str, expected: str) -> None:
    """USD-M market entry at bar 1 open 2000; funding at bar 2 open, rate +0.0001, mark 2003.1.

    Helper check, qty 2.5: 0.0001 x 2.5 x 2003.1 = 0.500775 -> long pays, short receives.
    In the run: long entry ceil(2000 x 1.0003) = 2000.6, short entry floor(2000 x 0.9997) = 1999.4,
    both qty floor(50 / 20.6 = 2.4271...) = 2.427:
        amount = -/+ 0.0001 x 2.427 x 2003.1 = -/+ 0.48615237 (exact at 1e-8)
    Time exit after 2 bars (bar 3 open); net = gross - fees + funding.
    """
    assert funding_cash_flow(D("0.0001"), side, D("2.5"), D("2003.1")) == D(expected)
    bars = series(
        [
            (2000, 2001, 1999, 2000),
            (2000, 2005, 1999, 2003),
            (2003, 2004, 2002, 2003),
            (2003, 2004, 2002, 2003),
        ],
        market=Market.USDM,
    )
    entry = req(
        market=Market.USDM,
        side=side,
        stop_price=D(stop),
        take_profit_price=D(tp),
        time_exit_bars=2,
    )
    book = Book("baseline", D(10_000), CostModel(), instruments=[usdm_spec()])
    sim = Simulator(HistoricalClock(bars), [book], ScriptedSource({(T0, "BTCUSDT"): [entry]}))
    sim.feed(bars[:2])
    amount = sim.apply_funding_rate(
        "baseline", Market.USDM, "BTCUSDT", minute(2), D("0.0001"), D("2003.1")
    )
    assert amount == (D("-0.48615237") if side is Side.LONG else D("0.48615237"))
    sim.feed(bars[2:])
    sim.finish()
    (t,) = book.trades
    assert t.funding == amount
    assert t.net_pnl == t.gross_pnl - t.fees + amount
    assert book.ledger.reconcile().ok


def test_n4_funding_timestamps_must_fall_within_the_open_position() -> None:
    bars = series(
        [(2000, 2001, 1999, 2000), (2000, 2005, 1999, 2003), (2003, 2004, 2002, 2003)],
        market=Market.USDM,
    )
    entry = req(market=Market.USDM, stop_price=D("1980"), take_profit_price=None)
    book = Book("baseline", D(10_000), CostModel(), instruments=[usdm_spec()])
    sim = Simulator(HistoricalClock(bars), [book], ScriptedSource({(T0, "BTCUSDT"): [entry]}))
    with pytest.raises(ValueError, match="no open"):
        sim.apply_funding("baseline", Market.USDM, "BTCUSDT", minute(1), D("-1"))
    sim.feed(bars[:2])  # entered at the bar 1 open (minute 1)
    with pytest.raises(ValueError, match="does not follow the entry"):
        sim.apply_funding("baseline", Market.USDM, "BTCUSDT", minute(1), D("-1"))
    with pytest.raises(ValueError, match="future"):
        sim.apply_funding("baseline", Market.USDM, "BTCUSDT", minute(3), D("-1"))
    sim.apply_funding("baseline", Market.USDM, "BTCUSDT", minute(2), D("-1"))
    with pytest.raises(ValueError, match="not after"):
        sim.apply_funding("baseline", Market.USDM, "BTCUSDT", minute(2), D("-1"))
    assert book.ledger.reconcile().ok
    spot_book = Book("s", D(100), CostModel(), instruments=[spot_spec()])
    with pytest.raises(ValueError, match="USD-M"):
        spot_book.apply_funding(Market.SPOT, "BTCUSDT", minute(2), D("1"))


# ---------------------------------------------------------------- N6
def test_n6_trade_spanning_a_data_gap_is_flagged_and_counted() -> None:
    """Long held from bar 1; bars 3-4 are missing; exits by TP on bar 5 -> spans_gap=True.

    A second trade with no missing bars in its life stays unflagged.
    """
    rows = [(1000, 1001, 999, 1000), (1000, 1005, 998, 1002), (1002, 1003, 1001, 1002)]
    bars = series(rows) + series([(1002, 1021, 1001, 1019)], first_index=5)
    bars += series([(1000, 1001, 999, 1000), (1000, 1021, 999, 1019)], first_index=6)
    script = {
        (T0, "BTCUSDT"): [req("gap")],
        (minute(5), "BTCUSDT"): [req("nogap")],
    }
    book, res = run_book(bars, script, specs=[spot_spec()])
    gap_t, clean_t = book.trades
    assert gap_t.spans_gap is True
    assert clean_t.spans_gap is False
    assert events_of(res, "sim_bar_gap")[0]["missing_bars"] == 2
    s = book.summary()
    assert (s["gap_trades"], s["flagged_trades"]) == (1, 1)
    closed = events_of(res, "sim_position_closed")
    assert [c["spans_gap"] for c in closed] == [True, False]
