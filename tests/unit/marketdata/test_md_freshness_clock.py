from __future__ import annotations

import asyncio

import pytest
from md_helpers import T0, Sink

from tsclaude.contracts.events import EventType
from tsclaude.contracts.market import Market, StreamState
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.marketdata.config import FreshnessConfig
from tsclaude.marketdata.types import StreamKey, StreamKind
from tsclaude.quality.clock import ClockMonitor, ClockSample
from tsclaude.quality.freshness import FreshnessMonitor

KL = StreamKey(Market.SPOT, "BTCUSDT", StreamKind.KLINE_1M)
BK = StreamKey(Market.SPOT, "BTCUSDT", StreamKind.BOOK_TICKER)
MK = StreamKey(Market.USDM, "BTCUSDT", StreamKind.MARK_PRICE)


def _states(sink: Sink) -> list[tuple[str, str, str]]:
    return [
        (e.payload["stream"], e.payload["from"], e.payload["to"])
        for e in sink.of(EventType.STREAM_STATE_CHANGED)
    ]


def test_budgets_from_config() -> None:
    cfg = FreshnessConfig()
    assert cfg.budget_ms(StreamKind.KLINE_1M) == 75_000
    assert cfg.budget_ms(StreamKind.BOOK_TICKER) == 10_000
    assert cfg.budget_ms(StreamKind.MARK_PRICE) == 5_000


def test_full_lifecycle_with_events(clock: ManualClock, sink: Sink) -> None:
    repaired = {"ok": True}
    mon = FreshnessMonitor(
        [KL, BK, MK],
        FreshnessConfig().budget_ms,
        sink,
        clock.now_ms,
        repaired=lambda _k: repaired["ok"],
    )
    assert mon.state(KL) is StreamState.CONNECTING
    mon.on_open(KL)
    assert mon.state(KL) is StreamState.CONNECTING  # open is not data
    mon.on_message(KL, fresh=False)  # forming candle proves the stream works
    assert mon.state(KL) is StreamState.HEALTHY

    # kline budget counts from healthy_since / last closed bar, not from forming updates
    clock.advance(70_000)
    mon.on_message(KL, fresh=False)
    mon.tick()
    assert mon.state(KL) is StreamState.HEALTHY
    clock.advance(6_000)
    mon.tick()
    assert mon.state(KL) is StreamState.STALE
    mon.on_message(KL, fresh=False)
    assert mon.state(KL) is StreamState.STALE  # still no closed bar
    mon.on_message(KL, fresh=True)
    assert mon.state(KL) is StreamState.HEALTHY

    mon.on_close(KL, "connection_lost")
    repaired["ok"] = False  # e.g. a minute missed while disconnected is being recovered
    assert mon.state(KL) is StreamState.DISCONNECTED
    mon.on_close(KL, "again")  # idempotent, no extra event
    mon.on_open(KL)
    assert mon.state(KL) is StreamState.RECOVERING
    mon.on_message(KL, fresh=True)
    assert mon.state(KL) is StreamState.RECOVERING  # repair not finished
    mon.tick()
    assert mon.state(KL) is StreamState.RECOVERING
    repaired["ok"] = True
    mon.tick()
    assert mon.state(KL) is StreamState.HEALTHY

    assert _states(sink) == [
        (KL.id, "CONNECTING", "HEALTHY"),
        (KL.id, "HEALTHY", "STALE"),
        (KL.id, "STALE", "HEALTHY"),
        (KL.id, "HEALTHY", "DISCONNECTED"),
        (KL.id, "DISCONNECTED", "RECOVERING"),
        (KL.id, "RECOVERING", "HEALTHY"),
    ]
    assert mon.health(KL).disconnects == 1
    ev = sink.of(EventType.STREAM_STATE_CHANGED)[1]
    assert ev.payload["reason"] == "freshness_budget_exceeded"
    assert ev.payload["budget_ms"] == 75_000


def test_book_and_mark_budgets(clock: ManualClock, sink: Sink) -> None:
    mon = FreshnessMonitor([BK, MK], FreshnessConfig().budget_ms, sink, clock.now_ms)
    mon.on_message(BK)
    mon.on_message(MK)
    clock.advance(5_001)
    mon.tick()
    assert mon.state(MK) is StreamState.STALE
    assert mon.state(BK) is StreamState.HEALTHY
    clock.advance(5_000)
    mon.tick()
    assert mon.state(BK) is StreamState.STALE
    # streams without history recover on the first message after reconnect
    mon.on_close(BK, "x")
    mon.on_open(BK)
    mon.on_message(BK)
    assert mon.state(BK) is StreamState.HEALTHY


def test_clock_median_offset_and_skew_events(clock: ManualClock, sink: Sink) -> None:
    mon = ClockMonitor(clock, _never, sink, samples_per_round=5, threshold_ms=1000)
    assert not mon.offset_known
    assert mon.server_now_ms() == T0

    def round_with(offsets: list[int]) -> None:
        now = clock.now_ms()
        mon.record([ClockSample(now - 10, now + 10, now + o) for o in offsets])

    round_with([200, 250, 300, 5000, -4000])  # median 250, outliers ignored
    assert mon.offset_ms == 250
    assert mon.server_now_ms() == T0 + 250
    assert sink.of(EventType.CLOCK_SKEW_CHANGED) == []
    round_with([1500, 1400, 1600, 1550, 1450])
    round_with([1500, 1500, 1500, 1500, 1500])  # still beyond: no new event
    round_with([-100, 0, 100, 50, -50])
    round_with([-1200, -1300, -1250, -1100, -1400])
    events = sink.of(EventType.CLOCK_SKEW_CHANGED)
    assert [(e.payload["exceeded"], e.payload["offset_ms"]) for e in events] == [
        (True, 1500),
        (False, 0),
        (True, -1250),
    ]


async def _never() -> int:
    raise ConnectionError("offline")


def test_clock_round_counts_failures(clock: ManualClock, sink: Sink) -> None:
    mon = ClockMonitor(clock, _never, sink, samples_per_round=3)
    assert asyncio.run(mon.sample_round()) is None
    assert mon.failures == 1
    assert not mon.offset_known


def test_clock_round_with_fetcher(clock: ManualClock, sink: Sink) -> None:
    calls = {"n": 0}

    async def fetch() -> int:
        calls["n"] += 1
        clock.advance(20)  # 20 ms round trip
        return clock.now_ms() - 10 + 3000  # server 3 s ahead

    mon = ClockMonitor(clock, fetch, sink, samples_per_round=5)
    rnd = asyncio.run(mon.sample_round())
    assert rnd is not None
    assert calls["n"] == 5
    assert rnd.offset_ms == 3000
    assert rnd.median_rtt_ms == 20
    assert mon.skew_exceeded
    assert len(sink.of(EventType.CLOCK_SKEW_CHANGED)) == 1


def test_clock_rejects_bad_config(clock: ManualClock, sink: Sink) -> None:
    with pytest.raises(ValueError, match="samples"):
        ClockMonitor(clock, _never, sink, samples_per_round=0)


def test_stale_to_healthy_requires_repair(clock: ManualClock, sink: Sink) -> None:
    """Review N2: STALE -> HEALTHY passes the same repair check as RECOVERING -> HEALTHY."""
    repaired = {"ok": True}
    mon = FreshnessMonitor(
        [KL], FreshnessConfig().budget_ms, sink, clock.now_ms, repaired=lambda _k: repaired["ok"]
    )
    mon.on_message(KL, fresh=True)
    clock.advance(76_000)
    mon.tick()
    assert mon.state(KL) is StreamState.STALE
    repaired["ok"] = False  # an overdue minute is not recovered yet
    mon.on_message(KL, fresh=True)
    assert mon.state(KL) is StreamState.STALE
    clock.advance(1_000)
    mon.tick()
    assert mon.state(KL) is StreamState.STALE
    repaired["ok"] = True
    mon.tick()
    assert mon.state(KL) is StreamState.HEALTHY
    assert _states(sink)[-1] == (KL.id, "STALE", "HEALTHY")


def test_pending_stale_recovery_expires_with_the_budget(clock: ManualClock, sink: Sink) -> None:
    repaired = {"ok": False}
    mon = FreshnessMonitor(
        [KL], FreshnessConfig().budget_ms, sink, clock.now_ms, repaired=lambda _k: repaired["ok"]
    )
    repaired["ok"] = True
    mon.on_message(KL, fresh=True)
    clock.advance(76_000)
    mon.tick()
    repaired["ok"] = False
    mon.on_message(KL, fresh=True)  # pending: repair not done
    clock.advance(76_000)  # that fresh message is now itself older than the budget
    repaired["ok"] = True
    mon.tick()
    assert mon.state(KL) is StreamState.STALE


def test_connecting_to_healthy_requires_repair(clock: ManualClock, sink: Sink) -> None:
    repaired = {"ok": False}
    mon = FreshnessMonitor(
        [KL], FreshnessConfig().budget_ms, sink, clock.now_ms, repaired=lambda _k: repaired["ok"]
    )
    mon.on_message(KL, fresh=False)
    assert mon.state(KL) is StreamState.CONNECTING
    repaired["ok"] = True
    mon.tick()
    assert mon.state(KL) is StreamState.HEALTHY
