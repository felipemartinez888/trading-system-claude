from __future__ import annotations

import asyncio

import pytest
from md_helpers import T0, load_fixture

from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.marketdata.binance.limits import (
    Backoff,
    RestBannedState,
    SlidingWindowLimiter,
    WeightLimiter,
)
from tsclaude.marketdata.binance.parse import parse_exchange_info


class FakeSleep:
    """Advances the ManualClock instead of waiting; records requested delays."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(max(1, round(seconds * 1000)))


def test_sliding_window_limits_control_messages() -> None:
    clock = ManualClock(T0)
    sleep = FakeSleep(clock)
    lim = SlidingWindowLimiter(4, 1_000, clock, sleep)

    async def burst() -> list[int]:
        stamps = []
        for _ in range(10):
            await lim.acquire()
            stamps.append(clock.now_ms())
        return stamps

    stamps = asyncio.run(burst())
    # never more than 4 in any rolling 1 s window
    for i in range(len(stamps)):
        in_window = [s for s in stamps if stamps[i] - 1000 < s <= stamps[i]]
        assert len(in_window) <= 4
    assert stamps[-1] - stamps[0] >= 2000


def test_try_acquire_and_validation() -> None:
    clock = ManualClock(T0)
    lim = SlidingWindowLimiter(2, 1_000, clock, FakeSleep(clock))
    assert lim.try_acquire()
    assert lim.try_acquire()
    assert not lim.try_acquire()
    assert lim.wait_ms() == 1_000
    clock.advance(1_000)
    assert lim.try_acquire()
    with pytest.raises(ValueError, match="positive"):
        SlidingWindowLimiter(0, 1_000, clock, FakeSleep(clock))


def test_weight_limiter_seeded_from_exchange_info_and_headers() -> None:
    clock = ManualClock(T0)
    sleep = FakeSleep(clock)
    wl = WeightLimiter(clock, sleep, budget_fraction=0.5)
    info = parse_exchange_info(Market.USDM, load_fixture("rest_usdm_exchange_info.json"))
    wl.seed(info.rate_limits)
    assert wl.seeded
    assert wl.windows == {"1M": (0, 2400)}  # ORDERS limits ignored

    asyncio.run(wl.acquire(5))
    assert wl.windows["1M"][0] == 5
    # the server's count is authoritative (other clients on the same IP)
    wl.observe_headers({"X-MBX-USED-WEIGHT-1M": "1198", "Content-Type": "application/json"})
    assert wl.windows["1M"][0] == 1198
    # budget is 1200: the next weight-5 request must wait for the next minute window
    asyncio.run(wl.acquire(5))
    assert sleep.calls
    assert clock.now_ms() % 60_000 == 0
    assert wl.windows["1M"][0] == 5


def test_unknown_header_window_is_tracked_without_limit() -> None:
    clock = ManualClock(T0)
    wl = WeightLimiter(clock, FakeSleep(clock))
    wl.observe_headers({"x-mbx-used-weight": "3", "x-mbx-used-weight-10s": "7"})
    assert wl.windows == {"10S": (7, None)}


def test_retry_after_blocks_and_ban_stops() -> None:
    clock = ManualClock(T0)
    sleep = FakeSleep(clock)
    wl = WeightLimiter(clock, sleep)
    wl.block_for(3_000)
    asyncio.run(wl.acquire(1))
    assert clock.now_ms() >= T0 + 3_000

    wl.ban(None)
    assert wl.banned
    with pytest.raises(RestBannedState):
        asyncio.run(wl.acquire(1))
    clock.advance(10**9)
    assert wl.banned  # no Retry-After: stopped until restart

    wl2 = WeightLimiter(clock, sleep)
    wl2.ban(60_000)
    assert wl2.banned
    clock.advance(60_000)
    assert not wl2.banned


def test_backoff_full_jitter_capped() -> None:
    seen: list[tuple[float, float]] = []

    def rng(lo: float, hi: float) -> float:
        seen.append((lo, hi))
        return hi

    b = Backoff(1.0, 60.0, rng)
    delays = [b.next_delay() for _ in range(9)]
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    assert all(lo == 0.0 for lo, _ in seen)
    b.reset()
    assert b.next_delay() == 1
    with pytest.raises(ValueError, match="base"):
        Backoff(10, 5)
