"""M2-E1: no simulated fill on candles without trades (D-12, ADR-0014 rule 4; live evidence O-4) and
registered exchange interruptions (ADR-0013, D-09/D-10). Synthetic bars only."""

from __future__ import annotations

from decimal import Decimal as D
from typing import Any

from tsclaude.contracts.market import Bar, Market
from tsclaude.contracts.signals import EntryRuleKind, Side
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.sim.book import Book
from tsclaude.sim.clock import HistoricalClock
from tsclaude.sim.costs import CostModel
from tsclaude.sim.engine import SIM_ENGINE_VERSION, Simulator
from tsclaude.sim.fills import ExitReason
from tsclaude.sim.orders import EntryRequest, ExitRequest
from tsclaude.sim.testing import T0, ScriptedSource, bar, events_of, minute, run_book, spot_spec

SPOT = spot_spec()
UNTIL = minute(30)


def no_trade(i: int, price: float) -> Bar:
    """A published zero-trade candle: flat at the previous close, every volume 0 (class Z1)."""
    return bar(i, price, price, price, price).model_copy(
        update={
            "trades": 0,
            "volume": 0.0,
            "quote_volume": 0.0,
            "taker_buy_volume": 0.0,
            "taker_buy_quote_volume": 0.0,
        }
    )


def entry(kind: EntryRuleKind = EntryRuleKind.MARKET_NEXT_OPEN, **kw: Any) -> EntryRequest:
    args: dict[str, Any] = {
        "request_id": "r1",
        "market": Market.SPOT,
        "symbol": "BTCUSDT",
        "side": Side.LONG,
        "kind": kind,
        "stop_price": D("990"),
        "take_profit_price": D("1020"),
        "valid_until_ms": UNTIL,
    }
    args.update(kw)
    return EntryRequest(**args)


def opened(result: Any) -> list[dict[str, Any]]:
    return events_of(result, "sim_position_opened")


def closed(result: Any) -> list[dict[str, Any]]:
    return events_of(result, "sim_position_closed")


# ---------------------------------------------------------------------- entries
def test_a_market_entry_waits_for_the_next_candle_with_trades() -> None:
    bars = [bar(0, 1000, 1001, 999, 1000), no_trade(1, 1000), no_trade(2, 1000)]
    bars += [bar(3, 1004, 1006, 1003, 1005), bar(4, 1005, 1006, 1004, 1005)]
    book, res = run_book(bars, {(T0, "BTCUSDT"): [entry()]}, specs=[SPOT])
    fills = events_of(res, "sim_order_filled")
    assert len(fills) == 1
    assert fills[0]["bar_open_ms"] == minute(3)  # never on the zero-trade candles 1 and 2
    assert D(fills[0]["raw_price"]) == D("1004")  # the first traded open, not the filler 1000
    assert book.summary()["no_trade_deferrals"] == 1  # one order, counted once
    book.finish(minute(4) + MS_PER_MINUTE - 1)


def test_a_limit_entry_never_fills_at_a_frozen_price_and_rests_at_the_next_trade() -> None:
    """Buy limit 1001: the filler at 1000 would be 'marketable', but nobody traded. At the next
    candle with trades the order is resting: a gap below the limit fills at the limit (maker), not
    at the better open."""
    bars = [bar(0, 1000, 1001, 999, 1000), no_trade(1, 1000)]
    bars += [bar(2, 998, 1000, 997, 999), bar(3, 999, 1000, 998, 999)]
    req = entry(EntryRuleKind.LIMIT, entry_price=D("1001"))
    _, res = run_book(bars, {(T0, "BTCUSDT"): [req]}, specs=[SPOT])
    fills = events_of(res, "sim_order_filled")
    assert [f["bar_open_ms"] for f in fills] == [minute(2)]
    assert D(fills[0]["raw_price"]) == D("1001")
    assert fills[0]["liquidity"] == "maker"


def test_a_stop_entry_triggers_only_on_trades_and_fills_at_a_gapped_open() -> None:
    bars = [bar(0, 1000, 1001, 999, 1000), no_trade(1, 1000)]
    bars += [bar(2, 1008, 1010, 1007, 1009), bar(3, 1009, 1010, 1008, 1009)]
    req = entry(EntryRuleKind.STOP, entry_price=D("1003"), take_profit_price=D("1030"))
    _, res = run_book(bars, {(T0, "BTCUSDT"): [req]}, specs=[SPOT])
    fills = events_of(res, "sim_order_filled")
    assert [f["bar_open_ms"] for f in fills] == [minute(2)]
    assert D(fills[0]["raw_price"]) == D("1008")  # gap through the stop: the open, not the level


def test_an_entry_the_frozen_price_never_reached_is_not_reported_as_deferred() -> None:
    """Buy stop 1030 over a filler at 1000: the filler could not have triggered it, so the wait
    changed nothing and the trade is not flagged (ADR-0014 rule 5 counts real waits only)."""
    bars = [bar(0, 1000, 1001, 999, 1000), no_trade(1, 1000)]
    bars += [bar(2, 1020, 1035, 1019, 1032), bar(3, 1032, 1055, 1031, 1050)]
    req = entry(EntryRuleKind.STOP, entry_price=D("1030"), take_profit_price=D("1050"))
    book, res = run_book(bars, {(T0, "BTCUSDT"): [req]}, specs=[SPOT])
    assert events_of(res, "sim_order_filled")[0]["bar_open_ms"] == minute(2)  # the entry
    exits = closed(res)
    assert len(exits) == 1
    assert "deferred_no_trade" not in exits[0]
    assert book.summary()["no_trade_deferrals"] == 0


def test_validity_is_never_extended_by_zero_trade_candles() -> None:
    """The order may execute until the close of minute 2; minutes 1-2 had no trades, so it expires
    and never fills at the traded minute 3."""
    bars = [bar(0, 1000, 1001, 999, 1000), no_trade(1, 1000), no_trade(2, 1000)]
    bars += [bar(3, 1004, 1006, 1003, 1005)]
    req = entry(valid_until_ms=minute(2) + MS_PER_MINUTE - 1)
    _, res = run_book(bars, {(T0, "BTCUSDT"): [req]}, specs=[SPOT])
    assert events_of(res, "sim_order_filled") == []
    expired = events_of(res, "sim_order_expired")
    assert [e["reason"] for e in expired] == ["valid_until_passed:no_trade"]


# ---------------------------------------------------------------------- held positions
def _held_bars() -> list[Bar]:
    return [bar(0, 1000, 1001, 999, 1000), bar(1, 1000, 1005, 998, 1002)]  # entry at minute 1 open


def test_a_stop_is_never_hit_on_a_frozen_candle_and_fills_at_the_gapped_open() -> None:
    bars = [
        *_held_bars(),
        no_trade(2, 1002),
        bar(3, 985, 986, 980, 982),
        bar(4, 982, 983, 981, 982),
    ]
    _, res = run_book(bars, {(T0, "BTCUSDT"): [entry()]}, specs=[SPOT])
    exits = closed(res)
    assert len(exits) == 1
    assert exits[0]["exit_bar_open_ms"] == minute(3)
    assert exits[0]["exit_reason"] == "stop_loss"
    assert exits[0]["exit_price"].startswith("98")  # the gapped open 985 (minus slippage), not 990
    assert exits[0]["gap_fill"] is True


def test_a_due_exit_signal_waits_for_trades_and_the_trade_is_flagged() -> None:
    bars = [*_held_bars(), no_trade(2, 1002), no_trade(3, 1002), bar(4, 1003, 1004, 1002, 1003)]
    script: dict[tuple[int, str], list[Any]] = {
        (T0, "BTCUSDT"): [entry()],
        (minute(1), "BTCUSDT"): [ExitRequest("x1", Market.SPOT, "BTCUSDT")],
    }
    book, res = run_book(bars, script, specs=[SPOT])
    exits = closed(res)
    assert [e["exit_bar_open_ms"] for e in exits] == [minute(4)]
    assert exits[0]["exit_reason"] == "exit_signal"
    assert exits[0]["deferred_no_trade"] is True
    assert book.trades[0].deferred_no_trade is True
    assert book.summary()["deferred_no_trade_trades"] == 1


def test_trades_without_zero_trade_candles_carry_no_new_fields() -> None:
    """Backtest artefacts are unchanged when nothing was deferred (flags serialized only if set)."""
    bars = [*_held_bars(), bar(2, 1002, 1025, 1001, 1020)]
    _, res = run_book(bars, {(T0, "BTCUSDT"): [entry()]}, specs=[SPOT])
    payload = closed(res)[0]
    assert "deferred_no_trade" not in payload
    assert "spans_exchange_downtime" not in payload


# ---------------------------------------------------------------------- registered interruptions
OUTAGE = (Market.SPOT, minute(3), minute(6), "test-outage")


def _run_with_outage(bars: list[Bar], script: dict[tuple[int, str], list[Any]]) -> tuple[Book, Any]:
    book = Book("baseline", D(10_000), CostModel(), instruments=[SPOT])
    sim = Simulator(
        HistoricalClock(bars), [book], ScriptedSource(script), exchange_interruptions=[OUTAGE]
    )
    return book, sim.run()


def test_a_position_held_into_an_interruption_closes_at_the_first_traded_open_after_it() -> None:
    """Minutes 3-5 registered: minute 3 a filler, 4-5 absent. Even a traded-looking candle inside a
    registered interval cannot fill; the position closes at the minute-6 open, flagged."""
    bars = [*_held_bars(), bar(2, 1002, 1003, 1001, 1002), no_trade(3, 1002)]
    bars += [bar(6, 995, 996, 994, 995), bar(7, 995, 996, 994, 995)]
    book, res = _run_with_outage(bars, {(T0, "BTCUSDT"): [entry()]})
    exits = closed(res)
    assert [e["exit_bar_open_ms"] for e in exits] == [minute(6)]
    assert exits[0]["exit_reason"] == ExitReason.EXCHANGE_DOWNTIME
    assert exits[0]["spans_exchange_downtime"] is True
    assert exits[0]["spans_gap"] is True  # counted in the existing flagged-trade share
    assert "deferred_no_trade" not in exits[0]  # the exit was never due inside the interval
    assert book.summary()["exchange_downtime_trades"] == 1
    started = events_of(res, "sim_run_started") or [
        e["payload"] for e in res.events if e["type"].endswith("run_started")
    ]
    assert started[0]["engine_version"] == SIM_ENGINE_VERSION == "sim-v1"
    assert started[0]["exchange_interruptions"] == ["test-outage"]


def test_no_fill_inside_an_interruption_even_on_a_traded_candle() -> None:
    bars = [
        bar(0, 1000, 1001, 999, 1000),
        bar(1, 1000, 1001, 999, 1000),
        bar(2, 1000, 1001, 999, 1000),
    ]
    bars += [bar(3, 1000, 1002, 999, 1001), bar(6, 1003, 1004, 1002, 1003)]  # 3 looks traded
    _, res = _run_with_outage(bars, {(minute(2), "BTCUSDT"): [entry()]})
    fills = events_of(res, "sim_order_filled")
    assert [f["bar_open_ms"] for f in fills] == [minute(6)]  # the entry rested through the outage


def test_an_entry_whose_validity_ends_inside_an_interruption_is_cancelled_never_filled() -> None:
    """Valid until the close of minute 4, inside the registered minutes 3-5: the first traded open
    after the interruption (minute 6) is too late, so the order expires and nothing fills."""
    bars = [
        bar(0, 1000, 1001, 999, 1000),
        bar(1, 1000, 1001, 999, 1000),
        bar(2, 1000, 1001, 999, 1000),
    ]
    bars += [bar(3, 1000, 1002, 999, 1001), bar(6, 1003, 1004, 1002, 1003)]
    req = entry(valid_until_ms=minute(4) + MS_PER_MINUTE - 1)
    _, res = _run_with_outage(bars, {(minute(2), "BTCUSDT"): [req]})
    assert events_of(res, "sim_order_filled") == []
    assert len(events_of(res, "sim_order_expired")) == 1


def test_a_stop_crossed_by_a_traded_looking_candle_inside_an_interruption_does_not_fill() -> None:
    """Minute 3 is inside the registered interval and its low 980 crosses the stop 990: no
    counterparty existed, so the stop cannot fill there; the position closes at the minute-6
    open."""
    bars = [*_held_bars(), bar(2, 1002, 1003, 1001, 1002), bar(3, 1000, 1001, 980, 985)]
    bars += [bar(6, 995, 996, 994, 995), bar(7, 995, 996, 994, 995)]
    _, res = _run_with_outage(bars, {(T0, "BTCUSDT"): [entry()]})
    exits = closed(res)
    assert [e["exit_bar_open_ms"] for e in exits] == [minute(6)]
    assert exits[0]["exit_reason"] == ExitReason.EXCHANGE_DOWNTIME


def test_a_position_opened_after_an_interruption_is_not_closed_by_it() -> None:
    bars = [bar(0, 1000, 1001, 999, 1000), no_trade(3, 1000)]
    bars += [bar(6, 1000, 1001, 999, 1000), bar(7, 1000, 1005, 998, 1002)]
    bars += [bar(8, 1002, 1003, 1001, 1002), bar(9, 1002, 1003, 1001, 1002)]
    _, res = _run_with_outage(bars, {(minute(6), "BTCUSDT"): [entry()]})
    assert [f["bar_open_ms"] for f in events_of(res, "sim_order_filled")] == [minute(7)]
    assert closed(res) == []  # held after the interval: no forced exit


def test_an_interruption_made_only_of_fillers_still_flags_the_gap_share() -> None:
    """No absent minute at all (3-5 published as zero-trade fillers): the exposed position and
    the entry that rested through the interval are both flagged spans_gap (flagged-share cap)."""
    fillers = [no_trade(3, 1002), no_trade(4, 1002), no_trade(5, 1002)]
    bars = [*_held_bars(), bar(2, 1002, 1003, 1001, 1002), *fillers]
    bars += [bar(6, 995, 996, 994, 995), bar(7, 995, 996, 994, 995)]
    _, res = _run_with_outage(bars, {(T0, "BTCUSDT"): [entry()]})
    exits = closed(res)
    assert [e["exit_bar_open_ms"] for e in exits] == [minute(6)]
    assert exits[0]["spans_gap"] is True
    assert exits[0]["spans_exchange_downtime"] is True

    base = [bar(0, 1000, 1001, 999, 1000), bar(1, 1000, 1001, 999, 1000)]
    bars = [*base, bar(2, 1000, 1001, 999, 1000), no_trade(3, 1000), no_trade(4, 1000)]
    bars += [no_trade(5, 1000), bar(6, 1003, 1004, 1002, 1003), bar(7, 1003, 1025, 1002, 1020)]
    book, res = _run_with_outage(bars, {(minute(2), "BTCUSDT"): [entry()]})
    exits = closed(res)
    assert [e["exit_bar_open_ms"] for e in exits] == [minute(7)]  # take-profit after entry
    assert exits[0]["spans_gap"] is True
    assert exits[0]["spans_exchange_downtime"] is True
    assert book.summary()["flagged_trades"] == 1


def test_without_a_registry_nothing_changes_for_traded_candles() -> None:
    bars = [*_held_bars(), bar(2, 1002, 1003, 1001, 1002), bar(6, 995, 996, 994, 995)]
    _, res = run_book(bars, {(T0, "BTCUSDT"): [entry()]}, specs=[SPOT])
    assert closed(res) == []  # no registry: the position simply stays open (spans_gap only)


# ---------------------------------------------------------------------- paper / backtest parity
def test_paper_and_backtest_resolve_zero_trade_candles_identically() -> None:
    """One `Book` implementation serves both paths: the same bars and requests give the same fills
    whether the bars arrive one by one (paper-style stepping) or from a historical clock."""
    bars = [bar(0, 1000, 1001, 999, 1000), no_trade(1, 1000), bar(2, 1004, 1006, 1003, 1005)]
    bars += [no_trade(3, 1005), bar(4, 985, 986, 980, 982)]
    script = {(T0, "BTCUSDT"): [entry()]}
    _, backtest = run_book(bars, script, specs=[SPOT])
    stepped_book = Book("baseline", D(10_000), CostModel(), instruments=[SPOT])
    sim = Simulator(HistoricalClock([]), [stepped_book], ScriptedSource(script))
    for b in bars:
        sim.step(b)
    stepped = sim.finish()
    assert stepped.fingerprint == backtest.fingerprint
    assert [f["bar_open_ms"] for f in events_of(stepped, "sim_order_filled")] == [
        minute(2),
        minute(4),
    ]


def test_the_live_paper_path_defers_the_same_fill_and_replays_identically() -> None:
    """Through the real paper service (live clock, journal): a CONFIRMED market intent decided at
    minute 0 meets a zero-trade BTC candle at minute 1 and fills at the minute-2 open; the verified
    journal replay (the backtest-style recomputation) reproduces every batch."""
    from paper_helpers import BTC, ScriptedStrategy, drive, new_service, record, two_symbol_bars

    from tsclaude.sim.paper import replay

    bars = two_symbol_bars(5, btc={2: (1004, 1006, 1003, 1005)})
    z = bars[BTC][1]
    bars[BTC][1] = z.model_copy(
        update={
            "open": 1000.0, "high": 1000.0, "low": 1000.0, "close": 1000.0, "trades": 0,
            "volume": 0.0, "quote_volume": 0.0, "taker_buy_volume": 0.0,
            "taker_buy_quote_volume": 0.0,
        }
    )  # fmt: skip
    strategy = ScriptedStrategy({(minute(0), "BTCUSDT"): [record(1, 0)]})
    svc, journal = new_service(strategy)
    drive(svc, bars, range(5))
    fills = [
        e for e in svc.session.sim.events
        if e["type"] == "sim_order_filled" and e["aggregate_id"] == "shadow"
    ]  # fmt: skip
    assert fills, "the ungated shadow book must enter"
    assert fills[0]["payload"]["bar_open_ms"] == minute(2)  # never the zero-trade minute 1
    assert D(fills[0]["payload"]["raw_price"]) == D("1004")
    replayed = replay(config_of(svc), journal.batches())
    assert replayed.ledger_hashes() == svc.session.ledger_hashes()


def config_of(svc: Any) -> Any:
    return svc.config
