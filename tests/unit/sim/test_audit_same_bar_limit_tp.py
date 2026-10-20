"""Causality audit M2-T2 F1, resolved by Lead decision L-04 (no same-bar TP for intrabar entries).

Long limit 995 (stop 985, TP 1010). Bar 1 = (O 1000, H 1011, L 994.9, C 1000).
The assumed path is O -> nearer extreme -> other -> C. Here O - L = 5.1 < H - O = 11, so the old
simulator assumed 1000 -> 994.9 (limit fills at 995) -> 1011 (TP 1010 traded through) -> 1000 and
booked a +1.2995 R winner inside one minute, with ambiguous_bar=False.

The other ordering, 1000 -> 1011 -> 994.9 -> 1000, is equally consistent with the OHLC bar: the
high comes before the fill, so there is no take-profit. OHLC alone cannot tell the two apart.

L-04: an entry filled inside a 1m bar may be stopped out in that bar (flagged ambiguous_bar) but
never books its take-profit in it; the TP is eligible from the next 1m bar.
"""

from __future__ import annotations

from decimal import Decimal as D

from tsclaude.contracts.market import Market
from tsclaude.contracts.signals import EntryRuleKind, Side
from tsclaude.sim.fills import ExitReason
from tsclaude.sim.orders import EntryRequest
from tsclaude.sim.testing import T0, events_of, minute, random_scenario, run_book, series, spot_spec

REQ = EntryRequest(
    request_id="audit1",
    market=Market.SPOT,
    symbol="BTCUSDT",
    side=Side.LONG,
    kind=EntryRuleKind.LIMIT,
    entry_price=D("995"),
    stop_price=D("985"),
    take_profit_price=D("1010"),
    valid_until_ms=minute(30),
)


def test_same_bar_limit_entry_never_books_its_take_profit_in_the_fill_bar() -> None:
    bars = series([(1000, 1001, 999, 1000), (1000, 1011, 994.9, 1000)])
    book, res = run_book(bars, {(T0, "BTCUSDT"): [REQ]}, specs=[spot_spec()])
    (fill,) = events_of(res, "sim_order_filled")
    assert (fill["role"], fill["price"], fill["at_open"]) == ("entry", "995.0", False)
    assert book.trades == []
    assert book.open_positions == 1


def test_take_profit_becomes_eligible_from_the_next_bar() -> None:
    """Same entry; bar 2 (1000, 1011, 999, 1005) trades through 1010 -> TP at 1010 in bar 2.

    MFE counts only the fill-bar close in the fill bar, then the bar 2 path up to the TP price:
    qty 5 (50 / 10), MFE = (1010 - 995) / 10 = 1.5 R; MAE from the fill-bar low 994.9 = 0.01 R.
    """
    bars = series([(1000, 1001, 999, 1000), (1000, 1011, 994.9, 1000), (1000, 1011, 999, 1005)])
    book, _ = run_book(bars, {(T0, "BTCUSDT"): [REQ]}, specs=[spot_spec()])
    (t,) = book.trades
    assert t.exit_reason is ExitReason.TAKE_PROFIT
    assert t.exit_bar_open_ms == minute(2)
    assert not t.ambiguous_bar
    assert (t.mfe_r, t.mae_r) == (D("1.50000000"), D("0.01000000"))


def test_no_fill_bar_take_profit_for_intrabar_entries_in_random_scenarios() -> None:
    intrabar_entries = 0
    for seed in range(200):
        sc = random_scenario(seed)
        book, _ = run_book(sc.bars, sc.script, specs=sc.specs)
        for t in book.trades:
            if t.entry_at_open:
                continue
            intrabar_entries += 1
            if t.entry_bar_open_ms == t.exit_bar_open_ms:
                assert t.exit_reason is ExitReason.STOP_LOSS, t
                assert t.ambiguous_bar, t
    assert intrabar_entries > 50
