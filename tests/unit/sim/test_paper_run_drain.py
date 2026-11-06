"""M3-PB.3: `PaperTradingService.run` must not split a queued minute.

The async driver used to take one subscription update per iteration, offer it and poll. With a
backlog (updates queued while the consumer was busy or starting) the wall clock is already past the
first queued minute's settle deadline, so the first poll released that minute with only the first
symbol's bar and the other symbol's bar of the same minute became `late` (never processed). The
driver now offers the awaited update and then every update already queued (bounded by
`drain_max`, only if the subscription has `get_nowait`) before it snapshots and polls once; when
the bound is reached it neither snapshots nor polls in that iteration (a minute is never released
incomplete because of the batch limit) and yields.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Sequence

import pytest
from paper_helpers import BTC, ETH, ScriptedStrategy, drive, new_service, record, two_symbol_bars
from risk_helpers import snapshot

from tsclaude.contracts.market import Bar
from tsclaude.marketdata.bars import BarClosed
from tsclaude.sim.clock import HistoricalClock, order_key
from tsclaude.sim.paper import PaperTradingService
from tsclaude.sim.testing import minute

SETTLE_MS = 90_000


class _QueueSubscription:
    """A queue-backed subscription: `get` awaits (one item or, when empty, None, after which the
    driver is told to stop), `get_nowait` is non-blocking (oldest first, None when empty)."""

    def __init__(self, items: Sequence[object], stop: asyncio.Event) -> None:
        self.items: deque[object] = deque(items)
        self.stop = stop
        self.gets = 0
        self.nowaits = 0

    async def get(self, timeout: float | None = None) -> object | None:
        await asyncio.sleep(0)
        self.gets += 1
        if self.items:
            return self.items.popleft()
        self.stop.set()  # backlog consumed: this iteration polls once more, then the loop ends
        return None

    def get_nowait(self) -> object | None:
        self.nowaits += 1
        return self.items.popleft() if self.items else None


class _AwaitOnlySubscription(_QueueSubscription):
    """A subscription without `get_nowait` (the driver must not try to drain it)."""

    def __getattribute__(self, name: str) -> object:
        if name == "get_nowait":
            raise AttributeError(name)
        return super().__getattribute__(name)


def _backlog(bars: dict, minutes: Sequence[int]) -> list[object]:
    """Subscription order of a backlog: per minute, ETH's bar before BTC's (as `drive`)."""
    return [BarClosed(bars[key][i]) for i in minutes for key in (ETH, BTC)]


def _run(
    svc: PaperTradingService,
    items: Sequence[object],
    now_ms: int,
    *,
    nowait: bool = True,
    **kw: object,
) -> _QueueSubscription:
    """Run the driver over a queued backlog with the wall clock fixed at `now_ms`."""
    polls: list[int] = []
    original = svc.poll

    def poll(now: int) -> int:
        polls.append(now)
        return original(now)

    svc.poll = poll  # type: ignore[method-assign]

    async def main() -> _QueueSubscription:
        stop = asyncio.Event()
        sub = (_QueueSubscription if nowait else _AwaitOnlySubscription)(items, stop)
        await svc.run(
            sub,
            lambda: snapshot(now_ms - 200, symbols=(BTC, ETH)),
            lambda: now_ms,
            stop,
            poll_s=0.0,
            **kw,  # type: ignore[arg-type]
        )
        return sub

    sub = asyncio.run(asyncio.wait_for(main(), timeout=10))
    sub.polls = polls  # type: ignore[attr-defined]
    return sub


def _released_keys(svc: PaperTradingService) -> list[tuple[int, str, str]]:
    return [order_key(b) for b in svc.clock.released]


def _historical(bars: dict, minutes: Sequence[int]) -> list[tuple[int, str, str]]:
    delivered: list[Bar] = [bars[k][i] for i in minutes for k in (BTC, ETH)]
    return [order_key(b) for b in HistoricalClock(delivered)]


# ---------------------------------------------------------------- the backlog loss (red before)
def test_run_releases_every_queued_bar_of_a_backlog_past_its_deadlines() -> None:
    """Three minutes queued, the wall clock past every deadline. Before M3-PB.3 the driver released
    each minute with ETH only and BTC's bar of every queued minute was `late` (6 offered, 3
    released, 3 late, 3 incomplete minutes); now every bar is released, in the historical clock
    order, with no late bar and no incomplete minute."""
    bars = two_symbol_bars(3)
    svc, journal = new_service()
    now = minute(3) + SETTLE_MS  # past the deadline of minutes 0, 1 and 2
    _run(svc, _backlog(bars, range(3)), now)
    assert svc.clock.stats.late == 0, [order_key(b) for b in svc.clock.late]
    assert svc.clock.stats.incomplete_minutes == 0
    assert svc.clock.stats.released == 6
    assert _released_keys(svc) == _historical(bars, range(3))
    assert len(journal.batches()) == 6
    assert svc.fault is None


def test_run_backlog_stop_loss_fills_in_its_own_minute() -> None:
    """Golden exit: a BTC long (stop 990) is open when a backlog of minutes 4 and 5 queues (the
    wall clock at minute 5's deadline); BTC's minute-5 bar trades through the stop and minute 6,
    delivered on time afterwards, opens below it, at 988.5. Before M3-PB.3 BTC's minute-5 bar was
    late and the stop filled a minute late, gapped through at 988.5; now it fills in minute 5 at
    the stop, 990.0."""
    btc = {
        5: (1000, 1001, 989, 989.5),
        6: (988.5, 989, 988, 988.5),
    }
    bars = two_symbol_bars(7, btc=btc)
    strat = ScriptedStrategy({(minute(1), "BTCUSDT"): [record(1, 1)]})
    svc, journal = new_service(strat)
    drive(svc, bars, range(4))  # on time: the baseline entry fills before the backlog
    now = minute(6) + SETTLE_MS  # minute 5's deadline: minutes 4 and 5 are both due
    _run(svc, _backlog(bars, range(4, 6)), now)
    drive(svc, bars, [6], not_before_ms=now)
    fills = [
        (e.aggregate_id, e.payload["role"], e.payload["bar_open_ms"], e.payload["raw_price"])
        for batch in journal.batches()
        for e in batch
        if e.type == "sim_order_filled"
    ]
    stops = [f for f in fills if f[1] == "stop_loss"]
    assert stops, fills
    assert {(at, raw) for _, _, at, raw in stops} == {(minute(5), "990.0")}, stops
    assert svc.clock.stats.late == 0


# ---------------------------------------------------------------- bound and fallback
def test_run_drain_bound_defers_the_poll_instead_of_splitting_a_minute() -> None:
    """`drain_max=2`: the first iteration takes ETH 0 and drains BTC 0 and ETH 1, which reaches
    the bound; polling then would release minute 1 with ETH only (BTC 1 late). The driver defers
    the snapshot and the poll until an iteration drains fewer than the bound."""
    bars = two_symbol_bars(3)
    svc, _ = new_service()
    now = minute(3) + SETTLE_MS
    sub = _run(svc, _backlog(bars, range(3)), now, drain_max=2)
    assert svc.clock.stats.late == 0
    assert svc.clock.stats.incomplete_minutes == 0
    assert _released_keys(svc) == _historical(bars, range(3))
    # 6 items: [ETH0 +BTC0 +ETH1] deferred, [BTC1 +ETH2 +BTC2] deferred, [None] polls
    assert sub.polls == [now]  # type: ignore[attr-defined]


def test_run_without_a_bound_hit_polls_every_iteration() -> None:
    """On-time delivery (an empty queue after each awaited update) polls once per update, as
    before: the drain changes nothing when nothing is queued."""
    bars = two_symbol_bars(2)
    svc, _ = new_service()
    sub = _run(svc, _backlog(bars, range(1)), minute(1) + SETTLE_MS)
    # ETH 0 + BTC 0 drained, then the empty get: two polls
    assert sub.polls == [minute(1) + SETTLE_MS] * 2  # type: ignore[attr-defined]
    assert svc.clock.stats.released == 2


@pytest.mark.parametrize("bad", [0, -1])
def test_run_rejects_a_non_positive_drain_bound(bad: int) -> None:
    svc, _ = new_service()
    with pytest.raises(ValueError, match="drain_max"):
        _run(svc, [], minute(1), drain_max=bad)


def test_run_without_get_nowait_keeps_one_update_per_poll() -> None:
    """A subscription without `get_nowait` cannot be drained: the driver falls back to one update
    per poll (never raises). This documents the limit: such a subscription still splits a backlog
    past its deadlines (BTC 0 and BTC 1 late), which is why the market-data `Subscription` has
    `get_nowait`."""
    bars = two_symbol_bars(2)
    svc, _ = new_service()
    sub = _run(svc, _backlog(bars, range(2)), minute(2) + SETTLE_MS, nowait=False)
    assert sub.nowaits == 0
    assert sub.gets == 5  # four updates, then the empty get
    assert [order_key(b) for b in svc.clock.late] == [order_key(bars[BTC][i]) for i in (0, 1)]
