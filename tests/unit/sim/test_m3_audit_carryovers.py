"""M3-T1 audit carry-overs. Originally saved as failing tests by the M2-T2 causality re-audit
(after L-04 / N1-N6 fixes): two same-minute, cross-symbol leaks, plus the pending-entry gap flag.

Bars of one minute are delivered symbol by symbol (AAA before BBB). Both checks below use state
that, within a minute, already reflects bars of *other* symbols processed earlier.

R1 (funding timestamp guard, `Book.apply_funding`): "not in the future of the simulator" uses a
global `_now_ms` (latest close + 1 over all symbols). After AAA's minute-2 bar, funding stamped at
the minute-3 open is accepted for a BBB position whose minute-2 bar has not been processed; that
position then stops out inside minute 2, before the funding timestamp, yet the funding flow stays
in its net P&L. The guard must be per instrument (BBB's own last close + 1).

R2 (risk hook input, `RiskCheck.open_positions` at stage "fill"): AAA is stopped out *intrabar* in
minute 2; BBB's market entry executes at the minute-2 *open*, when AAA was still open. The hook is
told 0 open positions, so a max-concurrent-positions rule (M3) would admit BBB using a slot that is
freed only later in that minute. Sizing already guards this case for cash (`sizing_basis` takes
min(cash at mark, cash now)); the position count needs the same treatment.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from tsclaude.contracts.market import Market
from tsclaude.contracts.signals import EntryRuleKind, Side
from tsclaude.sim.book import Book, RiskCheck, RiskDecision
from tsclaude.sim.clock import HistoricalClock
from tsclaude.sim.costs import CostModel
from tsclaude.sim.engine import Simulator
from tsclaude.sim.orders import EntryRequest
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

FLAT = (1000, 1001, 999, 1000)


def _market(rid: str, market: Market, symbol: str) -> EntryRequest:
    return EntryRequest(
        rid,
        market,
        symbol,
        Side.LONG,
        EntryRuleKind.MARKET_NEXT_OPEN,
        stop_price=D("990"),
        valid_until_ms=minute(30),
    )


def test_r1_funding_cannot_be_posted_past_the_instruments_own_last_close() -> None:
    a = series([FLAT, FLAT, FLAT], symbol="AAA", market=Market.USDM)
    b = series([FLAT, FLAT, (1000, 1001, 980, 990)], symbol="BBB", market=Market.USDM)
    ordered = list(HistoricalClock(a + b))
    book = Book("b", D(10_000), CostModel(), instruments=[usdm_spec("AAA"), usdm_spec("BBB")])
    sim = Simulator(
        HistoricalClock(a + b),
        [book],
        ScriptedSource({(T0, "BBB"): [_market("mb", Market.USDM, "BBB")]}),
    )
    sim.feed(ordered[:4])  # minutes 0 and 1 for both symbols; BBB long opened at minute-1 open
    sim.feed(ordered[4:5])  # minute 2, AAA only: BBB's minute-2 bar is still unprocessed
    with pytest.raises(ValueError, match="future of usdm:BBB"):
        sim.apply_funding_rate("b", Market.USDM, "BBB", minute(3), D("0.0001"), D("1000"))


def test_r2_fill_stage_open_positions_excludes_exits_later_in_the_same_minute() -> None:
    seen: list[tuple[str, str, int]] = []

    def hook(check: RiskCheck) -> RiskDecision:
        seen.append((check.stage, check.request.request_id, check.open_positions))
        return RiskDecision(True)

    a = series([FLAT, FLAT, (1000, 1001, 980, 990)], symbol="AAA")  # AAA stopped intrabar, min 2
    b = series([FLAT, FLAT, FLAT], symbol="BBB")
    book = Book("b", D(10_000), CostModel(), hook, instruments=[spot_spec("AAA"), spot_spec("BBB")])
    script = {
        (T0, "AAA"): [_market("A", Market.SPOT, "AAA")],
        (minute(1), "BBB"): [_market("B", Market.SPOT, "BBB")],
    }
    Simulator(HistoricalClock(a + b), [book], ScriptedSource(script)).run()
    (fill_b,) = [s for s in seen if s[:2] == ("fill", "B")]
    assert fill_b[2] == 1, "AAA was still open at BBB's execution time (minute-2 open)"


def test_r1_funding_at_the_instruments_own_last_close_plus_one_is_accepted() -> None:
    """Per-instrument clock, boundary: BBB's minute-1 bar is processed, so the minute-2 open
    (BBB's last close + 1 ms) is a valid funding timestamp even though AAA is already further."""
    a = series([FLAT, FLAT, FLAT], symbol="AAA", market=Market.USDM)
    b = series([FLAT, FLAT, FLAT], symbol="BBB", market=Market.USDM)
    ordered = list(HistoricalClock(a + b))
    book = Book("b", D(10_000), CostModel(), instruments=[usdm_spec("AAA"), usdm_spec("BBB")])
    sim = Simulator(
        HistoricalClock(a + b),
        [book],
        ScriptedSource({(T0, "BBB"): [_market("mb", Market.USDM, "BBB")]}),
    )
    sim.feed(ordered[:5])
    qty = book.position_qty(Market.USDM, "BBB")
    assert qty > 0
    # long pays when the rate is positive: -(0.0001 x qty x 1000)
    paid = sim.apply_funding_rate("b", Market.USDM, "BBB", minute(2), D("0.0001"), D("1000"))
    assert paid == -(D("0.1") * qty)
    with pytest.raises(ValueError, match="not after previous"):
        sim.apply_funding_rate("b", Market.USDM, "BBB", minute(2), D("0.0001"), D("1000"))


def test_r2_submit_stage_counts_only_current_positions_and_fill_stage_frees_next_minute() -> None:
    """The same-minute exit keeps its slot at the fill stage only within that minute: an entry
    executing at the next minute's open sees the slot free."""
    seen: list[tuple[str, str, int, tuple[str, ...]]] = []

    def hook(check: RiskCheck) -> RiskDecision:
        syms = tuple(p.symbol for p in check.positions)
        seen.append((check.stage, check.request.request_id, check.open_positions, syms))
        return RiskDecision(True)

    a = series([FLAT, FLAT, (1000, 1001, 980, 990), FLAT], symbol="AAA")
    b = series([FLAT, FLAT, FLAT, FLAT], symbol="BBB")
    book = Book("b", D(10_000), CostModel(), hook, instruments=[spot_spec("AAA"), spot_spec("BBB")])
    script = {
        (T0, "AAA"): [_market("A", Market.SPOT, "AAA")],
        (minute(2), "BBB"): [_market("B", Market.SPOT, "BBB")],  # decided after AAA's exit
    }
    Simulator(HistoricalClock(a + b), [book], ScriptedSource(script)).run()
    assert ("submit", "B", 0, ()) in seen  # AAA closed in minute 2, before BBB's minute-2 close
    assert ("fill", "B", 0, ()) in seen  # executes at minute-3 open: the slot is free
    assert ("fill", "A", 0, ()) in seen


def test_pending_limit_that_rests_through_a_gap_is_flagged_spans_gap() -> None:
    """A resting long limit at 995 submitted at the minute-0 close; minutes 1-2 are missing and
    minute 3 opens through the limit (open 990 <= 995 - 0.1): resting gap-through fill at the limit
    price (maker), and the trade spans a data gap (L-04)."""
    rows = [FLAT, (990, 1000, 989.5, 999), (999, 1030, 998, 1025)]
    bars = series(rows[:1]) + series(rows[1:], first_index=3)
    entry = EntryRequest(
        "L",
        Market.SPOT,
        "BTCUSDT",
        Side.LONG,
        EntryRuleKind.LIMIT,
        entry_price=D("995"),
        stop_price=D("980"),
        take_profit_price=D("1020"),
        valid_until_ms=minute(30),
    )
    book, res = run_book(bars, {(T0, "BTCUSDT"): [entry]}, specs=[spot_spec()])
    (trade,) = book.trades
    assert trade.entry_price == D("995.0")
    assert trade.entry_at_open
    assert trade.spans_gap is True
    assert book.summary()["gap_trades"] == 1
    assert events_of(res, "sim_position_closed")[0]["spans_gap"] is True


def test_marketable_limit_arriving_without_a_gap_still_takes_the_open() -> None:
    """Control for the rule above: the first eligible minute is present and opens through the
    limit, so the order is marketable on arrival -> taker at the open (990 + slippage <= 995)."""
    rows = [FLAT, (990, 1000, 989.5, 999), (999, 1030, 998, 1025)]
    entry = EntryRequest(
        "L",
        Market.SPOT,
        "BTCUSDT",
        Side.LONG,
        EntryRuleKind.LIMIT,
        entry_price=D("995"),
        stop_price=D("980"),
        take_profit_price=D("1020"),
        valid_until_ms=minute(30),
    )
    book, _ = run_book(series(rows), {(T0, "BTCUSDT"): [entry]}, specs=[spot_spec()])
    (trade,) = book.trades
    assert trade.entry_at_open
    assert trade.entry_price < D("995")
    assert trade.spans_gap is False


def test_pending_entry_without_a_gap_is_not_flagged() -> None:
    rows = [FLAT, FLAT, FLAT, (990, 1000, 989.5, 999), (999, 1030, 998, 1025)]
    entry = EntryRequest(
        "L",
        Market.SPOT,
        "BTCUSDT",
        Side.LONG,
        EntryRuleKind.LIMIT,
        entry_price=D("995"),
        stop_price=D("980"),
        take_profit_price=D("1020"),
        valid_until_ms=minute(30),
    )
    book, _ = run_book(series(rows), {(T0, "BTCUSDT"): [entry]}, specs=[spot_spec()])
    (trade,) = book.trades
    assert trade.spans_gap is False
