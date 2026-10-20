"""Golden trades: every expected number is computed by hand in the docstring.

Common setup unless stated otherwise
------------------------------------
- Book equity 10 000 USDT, risk_per_trade 0.5 % -> risk budget 50 USDT; 1x notional cap.
- Instrument tick 0.1, lot 0.001, min notional 0.
- Fees (default `CostModel`): spot maker = taker = 0.10 %; USD-M maker 0.02 %, taker 0.05 %.
- Slippage (default `FixedSlippage`): half-spread 2 bps + impact 1 bps = 3 bps, taker fills only.
  Buy price = ceil_tick(raw x 1.0003), sell price = floor_tick(raw x 0.9997).
- Bar i opens at T0 + i minutes. Requests are submitted at the close of bar 0, so the earliest
  execution is the open of bar 1.
- Intrabar path: O -> (H or L nearer to O; low on ties) -> other extreme -> C.
"""

from __future__ import annotations

from decimal import Decimal as D

from tsclaude.contracts.market import Interval, Market
from tsclaude.contracts.signals import EntryRuleKind, Side
from tsclaude.sim.costs import CostModel, Liquidity
from tsclaude.sim.fills import ExitReason
from tsclaude.sim.orders import EntryRequest
from tsclaude.sim.testing import (
    T0,
    events_of,
    minute,
    run_book,
    series,
    spot_spec,
    usdm_spec,
)

SPOT = spot_spec()
USDM = usdm_spec()
UNTIL = minute(30)


def market_long(**kw: object) -> EntryRequest:
    args: dict[str, object] = {
        "request_id": "r1",
        "market": Market.SPOT,
        "symbol": "BTCUSDT",
        "side": Side.LONG,
        "kind": EntryRuleKind.MARKET_NEXT_OPEN,
        "stop_price": D("990"),
        "take_profit_price": D("1020"),
        "valid_until_ms": UNTIL,
    }
    args.update(kw)
    return EntryRequest(**args)  # type: ignore[arg-type]


# Shared long entry used by G1, G5, G6, G7 (spot, market at bar 1 open = 1000):
#   entry price = ceil_tick(1000 x 1.0003 = 1000.3)            = 1000.3
#   risk/unit   = 1000.3 - 990                                  = 10.3
#   qty         = floor_lot(50 / 10.3 = 4.85436...)             = 4.854
#       (cash cap 10000 / (1000.3 x 1.001) = 9.987, notional cap 9.997: risk binds)
#   notional    = 4.854 x 1000.3                                = 4855.4562
#   entry fee   = 4855.4562 x 0.001                             = 4.8554562
#   1R          = 4.854 x 10.3                                  = 49.9962
ENTRY_BARS = [(1000, 1001, 999, 1000), (1000, 1005, 998, 1002)]
SCRIPT = {(T0, "BTCUSDT"): [market_long()]}


def test_g1_market_long_take_profit() -> None:
    """Market long, take-profit hit by trade-through.

    Bar 1 (1000, 1005, 998, 1002): path 1000 -> 998 -> 1005 -> 1002, no SL (990) / TP (>= 1020.1).
    Bar 2 (1002, 1021, 1001, 1019): high 1021 >= 1020 + 1 tick -> TP fills at 1020, maker.
        exit notional = 4.854 x 1020 = 4951.08;   exit fee = 4.95108
        gross = 4951.08 - 4855.4562 = 95.6238;    fees = 4.8554562 + 4.95108 = 9.8065362
        net   = 95.6238 - 9.8065362 = 85.8172638; R = 85.8172638 / 49.9962 = 1.71647573
        MAE = (1000.3 - 998) / 10.3 = 0.22330097 R
        MFE = (1020.1 - 1000.3) / 10.3 = 1.92233010 R (path up to the TP trigger level 1020.1)
        final equity = 10000 + 85.8172638 = 10085.8172638
    """
    bars = series([*ENTRY_BARS, (1002, 1021, 1001, 1019)])
    book, res = run_book(bars, SCRIPT, specs=[SPOT])
    (t,) = book.trades
    assert t.entry_price == D("1000.3")
    assert t.qty == D("4.854")
    assert t.risk_amount == D("49.9962")
    assert t.exit_reason is ExitReason.TAKE_PROFIT
    assert t.exit_price == D("1020")
    assert t.exit_bar_open_ms == minute(2)
    assert t.gross_pnl == D("95.6238")
    assert t.fees == D("9.8065362")
    assert t.net_pnl == D("85.8172638")
    assert t.r_multiple == D("1.71647573")
    assert t.mae_r == D("0.22330097")
    assert t.mfe_r == D("1.92233010")
    assert not t.ambiguous_bar
    assert not t.gap_fill
    assert book.equity() == D("10085.81726380")
    assert book.ledger.reconcile().ok
    fills = events_of(res, "sim_order_filled")
    assert [f["liquidity"] for f in fills] == ["taker", "maker"]
    # Entry slippage split: raw notional 1000 x 4.854 = 4854
    #   half-spread 4854 x 2 bps = 0.9708; impact 4854 x 1 bps = 0.4854
    #   realized (1000.3 - 1000) x 4.854 = 1.4562 -> rounding adjustment 1.4562 - 1.4562 = 0
    assert (fills[0]["half_spread_cost"], fills[0]["impact_cost"]) == ("0.97080000", "0.48540000")
    assert fills[0]["rounding_adjustment"] == "0.00000000"
    assert (fills[1]["half_spread_cost"], fills[1]["impact_cost"]) == ("0.00000000", "0.00000000")
    assert t.slippage_cost == D("1.4562")
    assert events_of(res, "sim_order_cancelled") == [
        {"book": "baseline", "order_id": "baseline:r1:sl", "reason": "oco"}
    ]


def test_g2_market_short_stop_loss_usdm() -> None:
    """Market short on USD-M, stop-loss hit intrabar.

    Request at bar 0 close: short, stop 2020, TP 1950. Bar 1 opens 2000.
        entry = floor_tick(2000 x 0.9997 = 1999.4) = 1999.4; risk/unit = 2020 - 1999.4 = 20.6
        qty = floor_lot(50 / 20.6 = 2.42718...) = 2.427; 1R = 2.427 x 20.6 = 49.9962
        entry notional = 2.427 x 1999.4 = 4852.5438; taker fee 0.05 % = 2.4262719
    Bar 1 (2000, 2005, 1995, 2003): tie -> path 2000 -> 1995 -> 2005 -> 2003; no exit.
        mark: cash 10000 - 4852.5438 - 2.4262719 = 5145.0299281
              short value = margin + unrealized = 2 x 4852.5438 - 2.427 x 2003 = 4843.8066
              equity = 9988.8365281
    Bar 2 (2010, 2030, 2008, 2025): opens below the stop (no gap), path reaches 2020.
        exit = ceil_tick(2020 x 1.0003 = 2020.606) = 2020.7; notional = 2.427 x 2020.7 = 4904.2389
        exit fee = 2.45211945; gross = -(4904.2389 - 4852.5438) = -51.6951
        net = -51.6951 - 2.4262719 - 2.45211945 = -56.57349135; R = -1.13155583
        MAE = (2020 - 1999.4) / 20.6 = 1 R; MFE = (1999.4 - 1995) / 20.6 = 0.21359223 R
        slippage cost: entry 0.6 x 2.427 = 1.4562; exit 0.7 x 2.427 = 1.6989; total 3.1551
    """
    bars = series(
        [(2000, 2010, 1999, 2000), (2000, 2005, 1995, 2003), (2010, 2030, 2008, 2025)],
        market=Market.USDM,
    )
    req = market_long(
        market=Market.USDM,
        side=Side.SHORT,
        stop_price=D("2020"),
        take_profit_price=D("1950"),
    )
    book, _ = run_book(bars, {(T0, "BTCUSDT"): [req]}, specs=[USDM])
    assert book.equity_curve[2].equity == D("9988.83652810")
    (t,) = book.trades
    assert (t.side, t.entry_price, t.qty) == (Side.SHORT, D("1999.4"), D("2.427"))
    assert t.exit_reason is ExitReason.STOP_LOSS
    assert t.exit_price == D("2020.7")
    assert t.gross_pnl == D("-51.6951")
    assert t.net_pnl == D("-56.57349135")
    assert t.r_multiple == D("-1.13155583")
    assert t.mae_r == D("1.00000000")
    assert t.mfe_r == D("0.21359223")
    assert t.slippage_cost == D("3.1551")
    assert not t.ambiguous_bar
    assert not t.gap_fill
    assert book.equity() == D("9943.42650865")
    assert book.ledger.reconcile().ok


LIMIT = market_long(
    kind=EntryRuleKind.LIMIT,
    entry_price=D("995"),
    stop_price=D("985"),
    take_profit_price=D("1010"),
    valid_until_ms=minute(2) + 59_999,  # through the close of bar 2
)


def test_g3_limit_touched_is_not_filled() -> None:
    """Long limit 995: bars 1 and 2 have low exactly 995.0 (touch, no trade-through) -> no fill.

    The order expires at the close of bar 2 (its validity end). Bar 3 trades through (994.9) but
    arrives after expiry, so it must not fill either. Equity stays 10000.
    """
    bars = series(
        [
            (1000, 1001, 999, 1000),
            (1000, 1001, 995, 998),
            (998, 999, 995, 996),
            (996, 997, 994.9, 995),
        ]
    )
    book, res = run_book(bars, {(T0, "BTCUSDT"): [LIMIT]}, specs=[SPOT])
    assert book.trades == []
    assert events_of(res, "sim_order_filled") == []
    (exp,) = events_of(res, "sim_order_expired")
    assert exp["reason"] == "valid_until_passed"
    assert book.equity() == D("10000.00000000")


def test_g4_limit_traded_through_fills_at_limit_as_maker() -> None:
    """Long limit 995; bar 2 low 994.9 = limit - 1 tick -> fills at exactly 995.0, maker.

    risk/unit = 995 - 985 = 10 -> qty = 50 / 10 = 5.000; notional 4975; fee 0.1 % = 4.975.
    Bar 2 path: 998 -> 999 -> 994.9 -> 996 (high nearer to the open); position marked at 996:
    equity = (10000 - 4975 - 4.975) + 5 x 996 = 10000.025.
    """
    bars = series([(1000, 1001, 999, 1000), (1000, 1001, 995, 998), (998, 999, 994.9, 996)])
    book, res = run_book(bars, {(T0, "BTCUSDT"): [LIMIT]}, specs=[SPOT])
    (fill,) = events_of(res, "sim_order_filled")
    assert fill["price"] == "995.0"
    assert fill["liquidity"] == "maker"
    assert fill["bar_open_ms"] == minute(2)
    assert fill["at_open"] is False
    assert fill["qty"] == "5.000"
    assert fill["fee"] == "4.97500000"
    assert book.equity() == D("10000.02500000")


def test_g4b_resting_limit_gapped_through_fills_at_limit_not_open() -> None:
    """A resting long limit 995 whose bar opens at 990 fills at 995 (never the better open)."""
    bars = series([(1000, 1001, 999, 1000), (1000, 1001, 996, 998), (990, 992, 989, 991)])
    _, res = run_book(bars, {(T0, "BTCUSDT"): [LIMIT]}, specs=[SPOT])
    (fill,) = events_of(res, "sim_order_filled")
    assert (fill["price"], fill["liquidity"], fill["at_open"]) == ("995.0", "maker", True)


def test_g4d_resting_limit_filled_on_a_bar_opening_beyond_the_stop_is_stopped_at_the_open() -> None:
    """Long limit 995 (stop 985, no TP). Bar 1 (1000, 1001, 996, 998): no trade-through.

    Bar 2 opens at 980, already below the stop. The resting limit fills at 995 (maker, never the
    better open) and the position, held from that open, is stopped at the gapped open:
        qty = 50 / (995 - 985) = 5.000; entry notional 4975; maker fee 4.975
        exit = floor_tick(980 x 0.9997 = 979.706) = 979.7; notional 4898.5; fee 4.8985
        gross = -76.5; net = -76.5 - 4.975 - 4.8985 = -86.3735; R = -86.3735 / 50 = -1.72747
    """
    bars = series([(1000, 1001, 999, 1000), (1000, 1001, 996, 998), (980, 982, 978, 981)])
    req = market_long(
        kind=EntryRuleKind.LIMIT,
        entry_price=D("995"),
        stop_price=D("985"),
        take_profit_price=None,
    )
    book, _ = run_book(bars, {(T0, "BTCUSDT"): [req]}, specs=[SPOT])
    (t,) = book.trades
    assert (t.entry_price, t.entry_liquidity) == (D("995.0"), Liquidity.MAKER)
    assert t.exit_reason is ExitReason.STOP_LOSS
    assert t.gap_fill is True
    assert t.exit_price == D("979.7")
    assert t.net_pnl == D("-86.3735")
    assert t.r_multiple == D("-1.72747000")
    assert t.entry_bar_open_ms == t.exit_bar_open_ms == minute(2)


def test_g4c_marketable_limit_on_arrival_is_taker_capped_at_limit() -> None:
    """Long limit 1010 arriving at open 1000 is marketable: taker at ceil(1000 x 1.0003) = 1000.3.

    With limit 1000.1 the slipped price 1000.3 is capped at the limit: fill 1000.1, taker fee.
    """
    bars = series([(1000, 1001, 999, 1000), (1000, 1005, 999.5, 1002)])
    for limit, expected in (("1010", "1000.3"), ("1000.1", "1000.1")):
        req = market_long(kind=EntryRuleKind.LIMIT, entry_price=D(limit), take_profit_price=None)
        _, res = run_book(bars, {(T0, "BTCUSDT"): [req]}, specs=[SPOT])
        (fill,) = events_of(res, "sim_order_filled")
        assert (fill["price"], fill["liquidity"], fill["at_open"]) == (expected, "taker", True)


def test_g5_stop_gap_through_fills_at_open() -> None:
    """Shared long entry; bar 2 opens at 985, below the 990 stop -> filled at the open.

        exit = floor_tick(985 x 0.9997 = 984.7045) = 984.7; notional 4.854 x 984.7 = 4779.7338
        exit fee = 4.7797338; gross = 4779.7338 - 4855.4562 = -75.7224
        net = -75.7224 - 4.8554562 - 4.7797338 = -85.35759; R = -85.35759 / 49.9962 = -1.70728155
        MAE = (1000.3 - 985) / 10.3 = 1.48543689 R (worse than -1R: the gap is real risk)
    Equity curve: T0 10000 (origin), bar0 10000, bar1 5139.6883438 + 4.854 x 1002 = 10003.3963438,
    bar2 9914.64241 -> max drawdown (10003.3963438 - 9914.64241) / 10003.3963438 = 0.00887238.
    """
    bars = series([*ENTRY_BARS, (985, 986, 980, 983)])
    book, _ = run_book(bars, SCRIPT, specs=[SPOT])
    (t,) = book.trades
    assert t.exit_reason is ExitReason.STOP_LOSS
    assert t.gap_fill is True
    assert t.exit_at_open is True
    assert t.exit_price == D("984.7")
    assert t.net_pnl == D("-85.35759")
    assert t.r_multiple == D("-1.70728155")
    assert t.mae_r == D("1.48543689")
    assert [p.equity for p in book.equity_curve] == [
        D("10000.00000000"),
        D("10000.00000000"),
        D("10003.39634380"),
        D("9914.64241000"),
    ]
    s = book.summary()
    assert s["max_drawdown"] == "0.00887238"
    assert s["max_drawdown_peak_ts_ms"] == minute(1) + 59_999
    assert s["max_drawdown_trough_ts_ms"] == minute(2) + 59_999


def test_g5b_stop_entry_gap_fills_at_open() -> None:
    """Long stop entry 1010 (stop 1000); bar 1 opens at 1015 above it -> filled at the open.

    entry = ceil_tick(1015 x 1.0003 = 1015.3045) = 1015.4; qty = floor(50 / 15.4 = 3.2467) = 3.246.
    """
    bars = series([(1000, 1001, 999, 1000), (1015, 1016, 1014, 1015)])
    req = market_long(
        kind=EntryRuleKind.STOP,
        entry_price=D("1010"),
        stop_price=D("1000"),
        take_profit_price=None,
    )
    _, res = run_book(bars, {(T0, "BTCUSDT"): [req]}, specs=[SPOT])
    (fill,) = events_of(res, "sim_order_filled")
    assert (fill["price"], fill["qty"], fill["at_open"]) == ("1015.4", "3.246", True)


def test_g6_sl_and_tp_in_same_bar_resolves_sl_first_and_flags() -> None:
    """Shared long entry; bar 2 (1002, 1021, 989, 1000) reaches both 990 and 1020.1.

    SL first: exit = floor_tick(990 x 0.9997 = 989.703) = 989.7; notional 4.854 x 989.7 = 4804.0038
        fee 4.8040038; net = 4804.0038 - 4855.4562 - 4.8554562 - 4.8040038 = -61.11186
        R = -61.11186 / 49.9962 = -1.22233010; ambiguous_bar = True
    TP-first counterfactual = G1's result: R 1.71647573.
    MAE = (1000.3 - 990) / 10.3 = 1 R; MFE = (1005 - 1000.3) / 10.3 = 0.45631068 R (bar 1 high).
    """
    bars = series([*ENTRY_BARS, (1002, 1021, 989, 1000)])
    book, _ = run_book(bars, SCRIPT, specs=[SPOT])
    (t,) = book.trades
    assert t.exit_reason is ExitReason.STOP_LOSS
    assert t.ambiguous_bar is True
    assert t.exit_price == D("989.7")
    assert t.net_pnl == D("-61.11186")
    assert t.r_multiple == D("-1.22233010")
    assert t.alt_r_if_tp_first == D("1.71647573")
    assert t.mae_r == D("1.00000000")
    assert t.mfe_r == D("0.45631068")
    assert book.summary()["ambiguous_trades"] == 1


def test_g7_time_exit_after_n_strategy_bars() -> None:
    """Shared long entry with time_exit_bars=3 (1m): due at bar 1 open + 3 min = bar 4 open.

    Bar 4 opens 1004: exit = floor_tick(1004 x 0.9997 = 1003.6988) = 1003.6
        notional 4.854 x 1003.6 = 4871.4744; fee 4.8714744; gross 16.0182
        net = 16.0182 - 4.8554562 - 4.8714744 = 6.2912694; R = 6.2912694 / 49.9962 = 0.12583495
    """
    bars = series([*ENTRY_BARS, (1002, 1003, 1001, 1002), (1002, 1004, 1001, 1003)])
    bars += series([(1004, 1005, 1003, 1004)], first_index=4)
    script = {(T0, "BTCUSDT"): [market_long(time_exit_bars=3)]}
    book, _ = run_book(bars, script, specs=[SPOT])
    (t,) = book.trades
    assert t.exit_reason is ExitReason.TIME_EXIT
    assert t.exit_bar_open_ms == minute(4)
    assert t.exit_price == D("1003.6")
    assert t.net_pnl == D("6.2912694")
    assert t.r_multiple == D("0.12583495")


def test_g7b_time_exit_counts_strategy_bars_of_the_interval() -> None:
    """5m strategy, time_exit_bars=1, entry at T0+1m (inside the 5m bar [T0, T0+5m)).

    due = floor(T0+1m / 5m) x 5m + 1 x 5m = T0 + 5m -> exits at the open of bar 5, not bar 2.
    """
    rows = [(1000, 1001, 999, 1000)] * 7
    bars = series(rows)
    script = {(T0, "BTCUSDT"): [market_long(time_exit_bars=1, strategy_interval=Interval.M5)]}
    book, _ = run_book(bars, script, specs=[SPOT])
    (t,) = book.trades
    assert (t.exit_reason, t.exit_bar_open_ms) == (ExitReason.TIME_EXIT, minute(5))


def test_g8_expiry() -> None:
    """Expiry rules.

    (a) Market entry valid only until T0+30s cannot reach the bar 1 open -> rejected at submit.
    (b) Long limit 995 valid until bar 1 open + 30 s: bar 1 trades through intrabar (994.9) but the
        bar is not wholly inside the window (the touch time is unknown) -> no fill; expired at the
        bar 1 close.
    """
    bars = series([(1000, 1001, 999, 1000), (1000, 1001, 994.9, 998), (998, 999, 990, 991)])
    req_a = market_long(valid_until_ms=T0 + 30_000)
    book, res = run_book(bars, {(T0, "BTCUSDT"): [req_a]}, specs=[SPOT])
    assert [r["reason"] for r in events_of(res, "sim_request_rejected")] == [
        "validity_ends_before_execution"
    ]
    req_b = market_long(
        kind=EntryRuleKind.LIMIT,
        entry_price=D("995"),
        stop_price=D("985"),
        take_profit_price=None,
        valid_until_ms=minute(1) + 30_000,
    )
    book, res = run_book(bars, {(T0, "BTCUSDT"): [req_b]}, specs=[SPOT])
    assert book.trades == []
    assert events_of(res, "sim_order_filled") == []
    res_events = [(e["type"], e["ts_ms"]) for e in res.events if e["type"] == "sim_order_expired"]
    assert res_events == [("sim_order_expired", minute(1) + 59_999)]


def test_g9_fees_maker_vs_taker_usdm() -> None:
    """USD-M long limit 2000 (stop 1980, TP 2040); bar 1 low 1999.9 -> maker fill at 2000.

    qty = 50 / 20 = 2.5; entry notional 5000; maker fee 0.02 % = 1.
    Bar 2 high 2040.1 -> TP:
      maker TP: notional 5100, fee 1.02; net = 100 - 1 - 1.02 = 97.98; R = 97.98 / 50 = 1.9596
      taker TP (tp_as_taker): exit = floor_tick(2040 x 0.9997 = 2039.388) = 2039.3
        notional 5098.25, fee 0.05 % = 2.549125; net = 98.25 - 1 - 2.549125 = 94.700875
        R = 1.8940175
    """
    bars = series(
        [(2010, 2011, 2009, 2010), (2005, 2006, 1999.9, 2001), (2001, 2040.1, 2000, 2039)],
        market=Market.USDM,
    )
    req = market_long(
        market=Market.USDM,
        kind=EntryRuleKind.LIMIT,
        entry_price=D("2000"),
        stop_price=D("1980"),
        take_profit_price=D("2040"),
    )
    script = {(T0, "BTCUSDT"): [req]}
    book, res = run_book(bars, script, specs=[USDM])
    (t,) = book.trades
    fees = [(f["liquidity"], f["fee"]) for f in events_of(res, "sim_order_filled")]
    assert fees == [("maker", "1.00000000"), ("maker", "1.02000000")]
    assert (t.net_pnl, t.r_multiple) == (D("97.98"), D("1.95960000"))
    book, res = run_book(bars, script, specs=[USDM], cost_model=CostModel(tp_as_taker=True))
    (t,) = book.trades
    fees = [(f["liquidity"], f["fee"]) for f in events_of(res, "sim_order_filled")]
    assert fees == [("maker", "1.00000000"), ("taker", "2.54912500")]
    assert (t.exit_price, t.net_pnl, t.r_multiple) == (
        D("2039.3"),
        D("94.700875"),
        D("1.89401750"),
    )


def test_g10_rounding_is_adverse() -> None:
    """Rounding to tick/lot always against the book.

    Long market, stop 990.25, TP 1019.95; bar 1 opens 1000.05.
      stop and TP are sell prices -> rounded down: 990.2 and 1019.9
      entry = ceil_tick(1000.05 x 1.0003 = 1000.350015) = 1000.4 (buy -> up)
      qty = floor_lot(50 / (1000.4 - 990.2) = 4.90196...) = 4.901 (down)
    Long limit 995.04 rounds up to 995.1 (buy); short limit 2000.06 rounds down to 2000.0 (sell)
    with its stop 2020.01 rounded up to 2020.1 and TP 1950.09 rounded up to 1950.1 (buys).
    """
    bars = series([(1000, 1001, 999, 1000), (1000.05, 1001, 999, 1000)])
    req = market_long(stop_price=D("990.25"), take_profit_price=D("1019.95"))
    _, res = run_book(bars, {(T0, "BTCUSDT"): [req]}, specs=[SPOT])
    (sub,) = events_of(res, "sim_order_submitted")
    assert (sub["stop_price"], sub["take_profit_price"]) == ("990.2", "1019.9")
    (fill,) = events_of(res, "sim_order_filled")
    assert (fill["price"], fill["qty"]) == ("1000.4", "4.901")

    long_limit = market_long(kind=EntryRuleKind.LIMIT, entry_price=D("995.04"), stop_price=D("985"))
    short_limit = market_long(
        request_id="r2",
        market=Market.USDM,
        side=Side.SHORT,
        kind=EntryRuleKind.LIMIT,
        entry_price=D("2000.06"),
        stop_price=D("2020.01"),
        take_profit_price=D("1950.09"),
    )
    script = {(T0, "BTCUSDT"): [long_limit, short_limit]}
    _, res = run_book(bars, script, specs=[SPOT, USDM])
    subs = {s["market"]: s for s in events_of(res, "sim_order_submitted")}
    assert subs["spot"]["price"] == "995.1"
    assert (subs["usdm"]["price"], subs["usdm"]["stop_price"]) == ("2000.0", "2020.1")
    assert subs["usdm"]["take_profit_price"] == "1950.1"


def test_g11_funding_flows_into_net_pnl() -> None:
    """G2's short with a funding payment of -1.5 USDT posted between bar 1 and bar 2.

    net = -56.57349135 - 1.5 = -58.07349135; R = -58.07349135 / 49.9962 = -1.16155811.
    """
    from tsclaude.sim.book import Book
    from tsclaude.sim.clock import HistoricalClock
    from tsclaude.sim.engine import Simulator
    from tsclaude.sim.testing import ScriptedSource

    bars = series(
        [(2000, 2010, 1999, 2000), (2000, 2005, 1995, 2003), (2010, 2030, 2008, 2025)],
        market=Market.USDM,
    )
    req = market_long(
        market=Market.USDM, side=Side.SHORT, stop_price=D("2020"), take_profit_price=D("1950")
    )
    book = Book("baseline", D(10000), CostModel(), instruments=[USDM])
    sim = Simulator(HistoricalClock(bars), [book], ScriptedSource({(T0, "BTCUSDT"): [req]}))
    sim.feed(bars[:2])
    sim.apply_funding("baseline", Market.USDM, "BTCUSDT", minute(2), D("-1.5"))
    sim.feed(bars[2:])
    res = sim.finish()
    (t,) = book.trades
    assert t.funding == D("-1.5")
    assert t.net_pnl == D("-58.07349135")
    assert t.r_multiple == D("-1.16155811")
    assert book.ledger.balance("funding") == D("1.5")
    assert book.ledger.reconcile().ok
    assert events_of(res, "sim_funding_applied")[0]["amount"] == "-1.50000000"
