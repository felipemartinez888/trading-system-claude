"""Risk engine inside the simulator: blocks become events, the kill switch halts and flattens,
manual reset is logged, stale streams block (fault injection), downtime resolution."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal as D

import pytest
from risk_helpers import symbol_market

from tsclaude.contracts.market import Market
from tsclaude.contracts.signals import EntryRuleKind, Side
from tsclaude.risk.config import RiskConfig
from tsclaude.risk.engine import RiskEngine
from tsclaude.risk.hook import EngineRiskHook
from tsclaude.risk.market import MarketView, cutoff_ms
from tsclaude.sim.book import Book, BookConfig, RiskCheck, RiskDecision
from tsclaude.sim.clock import HistoricalClock
from tsclaude.sim.costs import CostModel
from tsclaude.sim.engine import Simulator
from tsclaude.sim.fills import ExitReason
from tsclaude.sim.orders import EntryRequest
from tsclaude.sim.testing import T0, ScriptedSource, events_of, minute, series, spot_spec

FLAT = (1000, 1001, 999, 1000)


def entry(rid: str, valid: int = 30, tp: str | None = "1030") -> EntryRequest:
    return EntryRequest(
        rid,
        Market.SPOT,
        "BTCUSDT",
        Side.LONG,
        EntryRuleKind.MARKET_NEXT_OPEN,
        stop_price=D("990"),
        take_profit_price=None if tp is None else D(tp),
        valid_until_ms=minute(valid),
    )


def healthy_provider(**kw: object) -> Callable[[RiskCheck], MarketView]:
    """A view provider that always reports a healthy market as of the check's cutoff."""

    def provide(check: RiskCheck) -> MarketView:
        cut = cutoff_ms(check.stage, check.ts_ms)
        sym = symbol_market(last_bar_close_ms=cut, spread_as_of_ms=cut - 10, **kw)
        return MarketView(cut - 10, 0, {(sym.market, sym.symbol): sym})

    return provide


def engine_book(
    provider: Callable[[RiskCheck], MarketView], *, kill: D | None = None
) -> tuple[Book, EngineRiskHook]:
    hook = EngineRiskHook(RiskEngine(RiskConfig(), CostModel()), provider)
    book = Book(
        "baseline",
        D(10_000),
        CostModel(),
        hook,
        instruments=[spot_spec()],
        kill_switch_drawdown=kill,
    )
    return book, hook


def test_engine_hook_allows_healthy_entry_and_records_both_stages() -> None:
    book, hook = engine_book(healthy_provider())
    sim = Simulator(
        HistoricalClock(series([FLAT] * 4)), [book], ScriptedSource({(T0, "BTCUSDT"): [entry("a")]})
    )
    res = sim.run()
    assert len(book.trades) + book.open_positions == 1
    stages = [(r["stage"], r["allow"]) for r in hook.drain()]
    assert stages == [("submit", True), ("fill", True)]
    assert not events_of(res, "sim_request_rejected")


def test_stale_stream_blocks_entry_with_event() -> None:
    """Fault injection: the kline stream is STALE at the cutoff -> RISK_BLOCKED at submit."""
    book, _ = engine_book(healthy_provider(unhealthy_streams=("spot:BTCUSDT:kline_1m",)))
    sim = Simulator(
        HistoricalClock(series([FLAT] * 4)), [book], ScriptedSource({(T0, "BTCUSDT"): [entry("a")]})
    )
    res = sim.run()
    (rej,) = events_of(res, "sim_request_rejected")
    assert rej["request_id"] == "a"
    assert rej["reason"].startswith("risk_blocked:RISK_BLOCKED:data_freshness:streams_unhealthy")
    assert book.trades == []
    assert book.open_positions == 0


def test_fill_stage_reblocks_when_market_turns_bad_after_submit() -> None:
    """Healthy at the decision, stream STALE by the previous close of the execution bar."""

    def provider(check: RiskCheck) -> MarketView:
        bad = check.stage == "fill"
        cut = cutoff_ms(check.stage, check.ts_ms)
        sym = symbol_market(
            last_bar_close_ms=cut,
            unhealthy_streams=("spot:BTCUSDT:book_ticker",) if bad else (),
        )
        return MarketView(cut, 0, {(sym.market, sym.symbol): sym})

    book, _ = engine_book(provider)
    sim = Simulator(
        HistoricalClock(series([FLAT] * 4)), [book], ScriptedSource({(T0, "BTCUSDT"): [entry("a")]})
    )
    res = sim.run()
    (rej,) = events_of(res, "sim_order_rejected")
    assert rej["reason"].startswith("risk_blocked:RISK_BLOCKED:data_freshness")
    assert book.summary()["rejections_by_reason"]


def test_exception_in_provider_blocks() -> None:
    def provider(check: RiskCheck) -> MarketView:
        raise RuntimeError("tracker exploded")

    book, hook = engine_book(provider)
    sim = Simulator(
        HistoricalClock(series([FLAT] * 3)), [book], ScriptedSource({(T0, "BTCUSDT"): [entry("a")]})
    )
    res = sim.run()
    (rej,) = events_of(res, "sim_request_rejected")
    assert "market_view:no_market_view" in rej["reason"]
    assert hook.drain()[0]["provider_error"] == "RuntimeError"


def test_kill_switch_halts_flattens_next_open_blocks_and_needs_manual_reset() -> None:
    """Risk 50 % with a far stop (850): the 1x notional cap binds, qty ~ 9.96 BTC at ~1000.3.
    Minute 3 gaps to 880 (close 885): equity ~ 10000 - 9.96 x 115 ~ 8850, drawdown ~ 11.5 %
    >= 10 % at the minute-3 mark -> halt, flatten at the minute-4 open (KILL_SWITCH exit)."""
    rows = [FLAT, FLAT, (1000, 1001, 999.5, 1000), (880, 890, 870, 885), FLAT, FLAT, FLAT, FLAT]
    req = EntryRequest(
        "big",
        Market.SPOT,
        "BTCUSDT",
        Side.LONG,
        EntryRuleKind.MARKET_NEXT_OPEN,
        stop_price=D("850"),
        valid_until_ms=minute(30),
    )
    later = entry("after", valid=40)
    later_ok = entry("after_reset", valid=40)
    book = Book(
        "baseline",
        D(10_000),
        CostModel(),
        instruments=[spot_spec()],
        config=BookConfig(risk_per_trade=D("0.5")),
        kill_switch_drawdown=D("0.10"),
    )
    script = {(T0, "BTCUSDT"): [req], (minute(4), "BTCUSDT"): [later]}
    bars = series(rows)
    sim = Simulator(HistoricalClock(bars), [book], ScriptedSource(script))
    sim.feed(bars[:6])
    halted = [e for e in sim.events if e["type"] == "sim_book_halted"]
    assert len(halted) == 1
    h = halted[0]["payload"]
    assert h["reason"] == "drawdown_kill_switch"
    assert h["flatten_at_ms"] == minute(4)  # halted at the minute-3 mark, flattened at next open
    assert D(h["drawdown"]) >= D("0.10")
    (trade,) = book.trades
    assert trade.exit_reason is ExitReason.KILL_SWITCH
    assert trade.exit_bar_open_ms == minute(4)
    assert trade.exit_at_open
    rejected = [e["payload"] for e in sim.events if e["type"] == "sim_request_rejected"]
    assert rejected[0]["request_id"] == "after"
    assert rejected[0]["reason"] == "risk_blocked:book_halted:drawdown_kill_switch"
    # manual reset (logged), then entries are accepted again
    sim.reset_halt("baseline", minute(6) - 1, "owner", "reviewed the crash")
    resets = [e["payload"] for e in sim.events if e["type"] == "sim_book_reset"]
    assert resets == [
        {
            "book": "baseline",
            "previous_reason": "drawdown_kill_switch",
            "operator": "owner",
            "note": "reviewed the crash",
            "peak_equity": resets[0]["peak_equity"],
        }
    ]
    assert book.halted is None
    sim.source = ScriptedSource({(minute(6), "BTCUSDT"): [later_ok]})
    sim.feed(bars[6:])
    filled = [e["payload"]["order_id"] for e in sim.events if e["type"] == "sim_order_filled"]
    assert "baseline:after_reset:entry" in filled


def test_halt_cancels_resting_entries_and_reset_requires_halted_and_operator() -> None:
    book = Book("baseline", D(10_000), CostModel(), instruments=[spot_spec()])
    lim = EntryRequest(
        "lim",
        Market.SPOT,
        "BTCUSDT",
        Side.LONG,
        EntryRuleKind.LIMIT,
        entry_price=D("900"),
        stop_price=D("890"),
        valid_until_ms=minute(30),
    )
    bars = series([FLAT] * 4)
    sim = Simulator(HistoricalClock(bars), [book], ScriptedSource({(T0, "BTCUSDT"): [lim]}))
    sim.feed(bars[:2])
    sim.halt("baseline", minute(2) - 1, "manual")
    cancelled = [e["payload"] for e in sim.events if e["type"] == "sim_order_cancelled"]
    assert cancelled == [
        {"book": "baseline", "order_id": "baseline:lim:entry", "reason": "book_halted:manual"}
    ]
    with pytest.raises(ValueError, match="operator"):
        sim.reset_halt("baseline", minute(2) - 1, "")
    sim.reset_halt("baseline", minute(2) - 1, "owner")
    with pytest.raises(ValueError, match="not halted"):
        sim.reset_halt("baseline", minute(2) - 1, "owner")


def test_daily_loss_resets_at_utc_midnight_start_of_day_equity() -> None:
    """start_of_day_equity is the last mark before the UTC day of the check."""
    day = 86_400_000
    start = T0 + day - 3 * 60_000  # three minutes before midnight
    rows = [FLAT, FLAT, FLAT, FLAT, FLAT]
    bars = series(rows, start_ms=start)
    seen: list[tuple[str, int, D | None]] = []

    def hook(check: RiskCheck) -> RiskDecision:
        seen.append((check.stage, check.ts_ms, check.start_of_day_equity))
        return RiskDecision(True)

    book = Book("baseline", D(10_000), CostModel(), hook, instruments=[spot_spec()])
    script = {(start + 3 * 60_000, "BTCUSDT"): [entry("a", valid=10_000)]}
    Simulator(HistoricalClock(bars), [book], ScriptedSource(script)).run()
    assert seen[0][0] == "submit"
    assert seen[0][2] == book.equity_curve[3].equity  # mark at 23:59:59.999
    assert book.start_of_day_equity(start + 10 * day) == book.equity_curve[-1].equity


def test_downtime_resolution_closes_on_backfill_and_never_fills_entries() -> None:
    """A position open before the downtime is stopped on a backfilled bar (tagged), a resting
    limit that the backfilled path trades through does not fill, and the source is not called."""
    rows = [FLAT, FLAT, (995, 996, 985, 986), (980, 981, 970, 975), FLAT]
    bars = series(rows)
    lim = EntryRequest(
        "lim",
        Market.SPOT,
        "ETHUSDT",
        Side.LONG,
        EntryRuleKind.LIMIT,
        entry_price=D("990"),
        stop_price=D("960"),
        valid_until_ms=minute(3) - 1,
    )
    eth = series(rows, symbol="ETHUSDT")
    book = Book("baseline", D(10_000), CostModel(), instruments=[spot_spec(), spot_spec("ETHUSDT")])
    src = ScriptedSource({(T0, "BTCUSDT"): [entry("pos", tp=None)], (minute(1), "ETHUSDT"): [lim]})
    ordered = list(HistoricalClock(bars + eth))
    sim = Simulator(HistoricalClock(bars + eth), [book], src)
    sim.feed(ordered[:4])  # minutes 0-1 observed: BTC long opens at minute-1 open
    sim.feed(ordered[4:8], observed=False)  # minutes 2-3: downtime, backfilled
    sim.feed(ordered[8:])
    res = sim.finish()
    (trade,) = book.trades
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.resolved_from_backfill is True
    assert events_of(res, "sim_position_closed")[0]["resolved_from_backfill"] is True
    expired = events_of(res, "sim_order_expired")
    assert expired == [
        {
            "book": "baseline",
            "order_id": "baseline:lim:entry",
            "reason": "valid_until_passed:downtime",
        }
    ]
    assert (minute(2), "BTCUSDT") not in src.seen
    assert (minute(3), "ETHUSDT") not in src.seen
    changes = [
        e["payload"]["observed"] for e in res.events if e["type"] == "sim_observation_changed"
    ]
    assert changes == [False, True]
