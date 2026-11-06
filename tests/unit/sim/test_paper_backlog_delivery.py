"""M3-PB.3 Test C: a restarted consumer's queued backlog, one update per poll versus drained.

Production consumed one subscription update per poll. After a restart the first poll comes after a
startup wait, so the bars that closed meanwhile are queued, and a queued minute already past its
settle deadline was released with only its first bar: the other bars of that minute became late
and were never processed, also for an instrument with an open position (a missed exit
evaluation). `reconnect(delivery=...)` models both consumers; ``"drained"`` (offer everything
queued, then poll once) is the fixed one.

Historical case 603669584 / 125 (reviewers, 2026-11-03): a baseline ETH stop-loss filled a minute
late, at 988.5 instead of 990.0. The restart delay, startup wait and intra-minute order used then
are unknown. Searched (bounded): restart delays {1 ms, 30 s, 1, 1.5, 2, 3, 5, 10 min} x startup
waits {0, 1, 2, 2.5, 3, 4, 5 min} in `reconnect`, and waits {1, 1.5, 2, 3 min} on an instant
restart. Findings, all pinned below:

- `reconnect` (downtime restart, ETH before BTC): the failed write is minute 24's, released after
  its close, so minute 24 closes before any restart and is resolved by the backfill, never
  queued; and with ETH first the bar a split minute loses is always BTC's. The exact symptom
  cannot occur there. Equivalent regression: delay 30 s, wait 3 min loses BTC's minute-26 bar
  (BTC held by the baseline and shadow books) under ``"per_update"``, none under ``"drained"``.
- Exact reproduction: instant restart (the subscription re-delivers from the restored position, as
  `redeliver`, so minute 24 is queued), BTC's bar before ETH's within a minute, startup wait 90 s:
  ``"per_update"`` fills the ETH stop at the open of minute 25, 988.5 (raw 988.8);
  ``"drained"`` fills it in minute 24 at 990.0 (raw 990.3), as the uninterrupted session does.
  With ETH first the same restart loses BTC's minute-24 bar instead and the stop is unaffected.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

import pytest
from paper_helpers import BTC, ETH, drive
from paper_scenarios import (
    Delivery,
    Restart,
    _canonical,
    check_backlog_delivery,
    deliver_backlog,
    reconnect,
    recovery_outcome,
    restart_causality,
    restore_after_crash,
)
from risk_helpers import snapshot

from tsclaude.contracts.market import Market
from tsclaude.sim.clock import order_key
from tsclaude.sim.testing import minute

SEED, FAIL_BATCH = 603669584, 125


def _restart(delay_ms: int) -> Restart:
    r = restore_after_crash(SEED, FAIL_BATCH, delay_ms)
    assert isinstance(r, Restart)
    return r


def _late(r: Restart) -> list[tuple[int, str]]:
    return [((b.open_time_ms - minute(0)) // 60_000, b.symbol) for b in r.restored.clock.late]


def _exits(batches: Sequence[Sequence[object]]) -> list[tuple[str, str, int, str, str]]:
    out = []
    for batch in batches:
        for e in batch:
            p = e.payload  # type: ignore[attr-defined]
            if e.type == "sim_order_filled" and p["role"] != "entry":  # type: ignore[attr-defined]
                at = (p["bar_open_ms"] - minute(0)) // 60_000
                out.append((e.aggregate_id, p["role"], at, p["raw_price"], p["price"]))  # type: ignore[attr-defined]
    return out


def _causality(r: Restart) -> list[str]:
    return restart_causality(
        r.journal.batches(),
        r.durable,
        r.restart_ms,
        r.closed_through_ms,
        live_from_ms=r.restart_ms,
        down_from_ms=r.crash_ms,
    )


# ---------------------------------------------------------------- the harness mode
def test_the_historical_crash_is_in_minute_24_with_an_eth_stop_resting() -> None:
    """The scenario: the failed write is minute 24's (released 1.5 s after its close), the journal
    ends with minute 23, and the baseline book holds ETH (SIG 2, stop 990.3) and BTC (SIG 4)."""
    r = _restart(0)
    assert r.crash_ms == minute(24) + 60_000 + 1_499
    assert r.last_key == (minute(23), "spot", "ETHUSDT")
    baseline = r.restored.session.sim.books["baseline"]
    assert baseline.holder(Market.SPOT, "ETHUSDT") == "SIG00000000000000000000002"
    assert baseline.holder(Market.SPOT, "BTCUSDT") == "SIG00000000000000000000004"
    # the uninterrupted session stops ETH out in minute 24 at 990.0
    assert ("baseline", "stop_loss", 24, "990.3", "990.0") in _exits(r.ref_journal.batches())


@pytest.mark.parametrize(
    ("seed", "fail_batch", "delay_ms"),
    [(SEED, FAIL_BATCH, 30_000), (13073, 124, 30_000), (465923499, 162, 600_000)],
)
@pytest.mark.parametrize("delivery", ["per_update", "drained"])
def test_without_a_startup_wait_both_modes_equal_the_default_model(
    seed: int, fail_batch: int, delay_ms: int, delivery: Delivery
) -> None:
    """With no startup wait nothing is queued: either mode journals exactly what the default
    `reconnect` journals (the mode adds the backlog, nothing else)."""
    base = restore_after_crash(seed, fail_batch, delay_ms)
    mode = restore_after_crash(seed, fail_batch, delay_ms)
    assert isinstance(base, Restart)
    assert isinstance(mode, Restart)
    reconnect(base)
    reconnect(mode, delivery=delivery, startup_wait_ms=0)
    assert _canonical(mode.journal.batches()) == _canonical(base.journal.batches())
    assert mode.restored.observing_from_ms == base.restored.observing_from_ms
    assert mode.first_poll_ms == mode.restart_ms
    assert base.first_poll_ms is None


def test_a_startup_wait_needs_a_delivery_mode() -> None:
    r = _restart(30_000)
    with pytest.raises(ValueError, match="delivery"):
        reconnect(r, startup_wait_ms=60_000)
    with pytest.raises(ValueError, match="startup_wait_ms"):
        reconnect(_restart(30_000), delivery="drained", startup_wait_ms=-1)


def test_the_anchor_is_fixed_at_the_first_poll() -> None:
    """The runtime starts monitoring at its first poll: the queued minutes are unobserved."""
    r = _restart(30_000)
    reconnect(r, delivery="drained", startup_wait_ms=180_000)
    assert r.first_poll_ms == r.restart_ms + 180_000
    assert r.restored.observing_from_ms == minute(29)  # first open after 25:31.5 + 3 min
    assert recovery_outcome(r) == []


# ---------------------------------------------------------------- equivalent regression (reconnect)
def test_per_update_backlog_loses_an_exposed_bar_and_drained_does_not() -> None:
    """603669584 / 125, restart 30 s after the crash (25:31.5), first poll 3 min later (28:31.5):
    minutes 25-27 are queued. The first poll awaits the backfill (so minute 25 is not split); one
    update per poll then releases minute 26 (past its deadline, 27:00 + 90 s = 28:30) with ETH's
    bar only, so BTC's minute-26 bar is late and never processed, while
    the baseline and shadow books hold BTC (SIG 4, from before the restore to the end). The
    recovery check reports the lost live bar. Drained: nothing late, all checks clean."""
    per_update = _restart(30_000)
    reconnect(per_update, delivery="per_update", startup_wait_ms=180_000)
    assert _late(per_update) == [(26, "BTCUSDT")]
    assert recovery_outcome(per_update) == [
        f"live bars not processed: {[(minute(26), 'spot', 'BTCUSDT')]}"
    ]
    for name in ("baseline", "shadow"):
        book = per_update.restored.session.sim.books[name]
        assert book.holder(Market.SPOT, "BTCUSDT") == "SIG00000000000000000000004"
    assert (Market.SPOT, "BTCUSDT") in per_update.exposed

    drained = _restart(30_000)
    reconnect(drained, delivery="drained", startup_wait_ms=180_000)
    assert _late(drained) == []
    assert drained.restored.clock.stats.released == per_update.restored.clock.stats.released + 1
    assert recovery_outcome(drained) == []
    assert _causality(drained) == []
    assert check_backlog_delivery(SEED, FAIL_BATCH, 30_000, 180_000) == []
    assert check_backlog_delivery(SEED, FAIL_BATCH, 30_000, 180_000, "per_update") == [
        f"late bars: {[(minute(26), 'spot', 'BTCUSDT')]}",
        "offered 29, released 28, buffered 0",
    ]


@pytest.mark.parametrize(
    ("delay_ms", "wait_ms", "lost"),
    [
        (1, 180_000, []),  # minute 25 due, but the first poll awaits the backfill
        (1, 240_000, [26]),
        (1, 300_000, [26, 27]),
        (30_000, 300_000, [26, 27, 28]),
        (90_000, 180_000, [27]),
        (300_000, 300_000, [32]),
        (600_000, 300_000, []),  # blocked awaiting a backfill: nothing released, nothing late
    ],
)
def test_per_update_loses_btc_of_every_split_minute_drained_none(
    delay_ms: int, wait_ms: int, lost: list[int]
) -> None:
    for delivery, expected in (("per_update", lost), ("drained", [])):
        r = _restart(delay_ms)
        reconnect(r, delivery=delivery, startup_wait_ms=wait_ms)  # type: ignore[arg-type]
        assert _late(r) == [(i, "BTCUSDT") for i in expected], delivery


# ---------------------------------------------------------------- exact historical symptom
def _instant_restart(
    order: Sequence[tuple[Market, str]], delivery: Delivery, wait_ms: int
) -> Restart:
    """Restart at the crash instant (no downtime): the subscription re-delivers every minute from
    the restored position (`redeliver`), the first poll comes `wait_ms` later, and the queued bars
    are in `order` within each minute. Later minutes arrive on time."""
    r = _restart(0)
    first_poll = r.restart_ms + wait_ms
    last = r.restored.session.last_minute_ms
    assert last is not None
    minutes = list(range((last - minute(0)) // 60_000, 40))
    queued = [i for i in minutes if minute(i) + 59_999 < first_poll]
    for i in queued:
        r.restored.on_snapshot(snapshot(minute(i) + 59_799, symbols=(BTC, ETH)))
    bars = [r.bars[k][i] for i in queued for k in order if (i, k[1]) not in r.missing]
    deliver_backlog(r.restored, bars, first_poll, delivery)
    drive(r.restored, r.bars, minutes[len(queued) :], missing=r.missing, not_before_ms=first_poll)
    return r


def test_historical_603669584_125_reproduces_per_update_and_is_fixed_drained() -> None:
    """Exact: BTC before ETH, startup wait 90 s (the first poll at 26:31.5, past minute 24's
    deadline 26:30). Minute 23 is re-delivered and late by design in both (journaled before the
    crash). One update per poll releases minute 24 with BTC only: ETH's minute-24 bar, which
    trades through the stop, is late, and the stop gaps through at minute 25's open, 988.5.
    Drained, the stop fills in minute 24 at 990.0 and the ledgers equal the uninterrupted ones."""
    per_update = _instant_restart((BTC, ETH), "per_update", 90_000)
    assert _late(per_update) == [(23, "BTCUSDT"), (23, "ETHUSDT"), (24, "ETHUSDT")]
    assert _exits(per_update.journal.batches()[per_update.durable :]) == [
        ("baseline", "stop_loss", 25, "988.8", "988.5")
    ]
    assert per_update.restored.ledger_hashes() != per_update.ref.ledger_hashes()

    drained = _instant_restart((BTC, ETH), "drained", 90_000)
    assert _late(drained) == [(23, "BTCUSDT"), (23, "ETHUSDT")]
    assert _exits(drained.journal.batches()[drained.durable :]) == [
        ("baseline", "stop_loss", 24, "990.3", "990.0")
    ]
    assert drained.restored.ledger_hashes() == drained.ref.ledger_hashes()


def test_historical_case_with_eth_first_loses_btc_instead() -> None:
    """Same restart in the harness order (ETH first): BTC's minute-24 bar is lost instead, which
    is why `reconnect` alone cannot show the historical ETH symptom."""
    per_update = _instant_restart((ETH, BTC), "per_update", 90_000)
    assert _late(per_update) == [(23, "ETHUSDT"), (23, "BTCUSDT"), (24, "BTCUSDT")]
    assert _exits(per_update.journal.batches()[per_update.durable :]) == [
        ("baseline", "stop_loss", 24, "990.3", "990.0")
    ]
    drained = _instant_restart((ETH, BTC), "drained", 90_000)
    assert _late(drained) == [(23, "ETHUSDT"), (23, "BTCUSDT")]
    assert drained.restored.ledger_hashes() == drained.ref.ledger_hashes()


# ---------------------------------------------------------------- bounded property (offline)
def _cases(n: int) -> list[tuple[int, int, int, int]]:
    rng = random.Random(20261104)
    return [
        (
            rng.randint(0, 10**9),
            rng.randint(0, 200),
            rng.randint(1, 600_000),
            rng.choice([0, 60_000, 150_000, 240_000, 360_000]),
        )
        for _ in range(n)
    ]


CASES = _cases(40)


def test_drained_backlog_never_makes_a_bar_late() -> None:
    """Seeded sample of restarts after downtime with a startup wait: drained, no delivered bar is
    late or lost. The same sample under one update per poll does lose bars (harness fidelity)."""
    problems = {c: check_backlog_delivery(*c) for c in CASES}
    assert {c: p for c, p in problems.items() if p} == {}
    lossy = [c for c in CASES if check_backlog_delivery(*c, "per_update")]
    assert lossy, "the sample never split a queued minute"


def test_drained_backlog_shape_holds_for_both_intra_minute_orders() -> None:
    """Instant restart with every wait of the bounded search: drained leaves only the by-design
    re-delivery of the journaled minute 23 late, in either order."""
    for order in ((ETH, BTC), (BTC, ETH)):
        for wait in (60_000, 90_000, 120_000, 180_000):
            r = _instant_restart(order, "drained", wait)
            assert {m for m, _ in _late(r)} == {23}, (order, wait)
            assert r.restored.ledger_hashes() == r.ref.ledger_hashes(), (order, wait)
            assert order_key(r.restored.clock.released[0])[0] == minute(24)
