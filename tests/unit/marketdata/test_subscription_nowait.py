"""`Subscription.get_nowait()` and `Subscription.closed` (M3-PB.3, approved design).

`get_nowait` returns the oldest queued update without awaiting (FIFO, same order as `get()`), None
when the queue is empty (never raises), and leaves the close sentinel queued (returning None) so the
next `get()` still reports the closure. It never blocks, never yields to the event loop and never
changes `dropped`. No network.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, cast

from md_helpers import T0

from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.marketdata.bars import BarClosed
from tsclaude.marketdata.service import Subscription

if TYPE_CHECKING:
    from tsclaude.marketdata.service import MarketDataService


class _Owner:
    """Stands in for `MarketDataService` as the subscription's owner (only `_subscribers`)."""

    def __init__(self) -> None:
        self._subscribers: set[Subscription] = set()

    def subscribe(self, maxsize: int = 10_000) -> Subscription:
        sub = Subscription(cast("MarketDataService", self), maxsize)
        self._subscribers.add(sub)
        return sub


def _update(i: int) -> BarClosed:
    open_ms = T0 + i * MS_PER_MINUTE
    bar = Bar(
        market=Market.SPOT,
        symbol="BTCUSDT",
        interval=Interval.M1,
        open_time_ms=open_ms,
        close_time_ms=open_ms + MS_PER_MINUTE - 1,
        open=100.0 + i,
        high=101.0 + i,
        low=99.0 + i,
        close=100.5 + i,
        volume=10.0,
        quote_volume=1_000.0,
        trades=5,
        taker_buy_volume=4.0,
        taker_buy_quote_volume=400.0,
        source=BarSource.WS_LIVE,
        ingested_at_ms=open_ms + MS_PER_MINUTE,
    )
    return BarClosed(bar)


def _fill(sub: Subscription, n: int) -> list[BarClosed]:
    items = [_update(i) for i in range(n)]
    for item in items:
        sub._put(item)
    return items


async def test_returns_available_item_then_none() -> None:
    sub = _Owner().subscribe()
    (item,) = _fill(sub, 1)
    assert sub.get_nowait() is item
    assert sub.get_nowait() is None
    assert sub.closed is False


async def test_empty_queue_returns_none_without_raising() -> None:
    sub = _Owner().subscribe()
    for _ in range(3):
        assert sub.get_nowait() is None
    assert sub.closed is False
    assert sub.dropped == 0


async def test_closed_empty_subscription_keeps_sentinel_for_get() -> None:
    owner = _Owner()
    sub = owner.subscribe()
    assert sub.closed is False
    sub.close()
    assert sub.closed is True
    assert sub not in owner._subscribers
    # the sentinel stays queued however often get_nowait is called
    for _ in range(3):
        assert sub.get_nowait() is None
    assert sub.closed is True
    # the next get() still reports the closure, immediately
    assert await sub.get(timeout=1.0) is None


async def test_closed_with_pending_items_drains_then_keeps_sentinel() -> None:
    sub = _Owner().subscribe()
    items = _fill(sub, 3)
    sub.close()
    assert sub.closed is True
    assert [sub.get_nowait() for _ in range(3)] == items
    assert sub.get_nowait() is None
    assert sub.get_nowait() is None
    # async iteration still terminates on the kept sentinel
    seen = [u async for u in sub]
    assert seen == []


async def test_closed_sentinel_kept_for_async_iteration_after_partial_drain() -> None:
    sub = _Owner().subscribe()
    items = _fill(sub, 4)
    sub.close()
    assert sub.get_nowait() is items[0]
    seen = [u async for u in sub]
    assert seen == items[1:]


async def test_sentinel_stays_at_head_even_if_an_item_follows_it() -> None:
    # the service never publishes to a closed subscription; a direct _put after close() must still
    # not move the sentinel behind the item
    sub = _Owner().subscribe()
    sub.close()
    sub._put(_update(0))
    assert sub.get_nowait() is None
    assert sub.get_nowait() is None
    assert await sub.get(timeout=1.0) is None


async def test_fifo_across_multiple_pending_updates() -> None:
    sub = _Owner().subscribe()
    items = _fill(sub, 25)
    drained = []
    while (u := sub.get_nowait()) is not None:
        drained.append(u)
    assert drained == items


async def test_interleaves_with_get_in_fifo_order() -> None:
    sub = _Owner().subscribe()
    a, b, c, d, e = _fill(sub, 5)
    assert await sub.get(timeout=1.0) is a
    assert sub.get_nowait() is b
    assert await sub.get(timeout=1.0) is c
    assert sub.get_nowait() is d
    assert sub.get_nowait() is e
    assert sub.get_nowait() is None
    # an empty queue: get() times out, get_nowait() returns None
    assert await sub.get(timeout=0.01) is None
    assert sub.get_nowait() is None


async def test_does_not_change_dropped() -> None:
    sub = _Owner().subscribe(maxsize=2)
    items = _fill(sub, 3)  # the third overflows
    assert sub.dropped == 1
    assert sub.get_nowait() is items[0]
    assert sub.dropped == 1
    assert sub.get_nowait() is items[1]
    assert sub.get_nowait() is None
    assert sub.get_nowait() is None
    assert sub.dropped == 1
    sub.close()
    assert sub.get_nowait() is None
    assert sub.dropped == 1


async def test_never_yields_to_the_event_loop() -> None:
    assert not inspect.iscoroutinefunction(Subscription.get_nowait)
    sub = _Owner().subscribe()
    items = _fill(sub, 5)
    ran = asyncio.Event()

    async def other() -> None:
        ran.set()

    task = asyncio.create_task(other())
    # no await between here and the assertion: if get_nowait yielded, `other` would have run
    got = [sub.get_nowait() for _ in range(5)]
    empty = sub.get_nowait()
    sub.close()
    after_close = sub.get_nowait()
    assert not ran.is_set()
    assert not inspect.isawaitable(empty)
    assert got == items
    assert empty is None
    assert after_close is None
    await task
    assert ran.is_set()


async def test_bounded_drain_takes_exactly_first_n_and_leaves_rest_in_order() -> None:
    sub = _Owner().subscribe()
    items = _fill(sub, 10)
    n = 4
    drained = []
    for _ in range(n):
        u = sub.get_nowait()
        if u is None:
            break
        drained.append(u)
    assert drained == items[:n]
    rest = [await sub.get(timeout=1.0) for _ in range(len(items) - n)]
    assert rest == items[n:]
    assert sub.get_nowait() is None
    assert sub.dropped == 0
