"""LiveClock ordering rules, journals (memory, event store fail-closed) and the async driver."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from paper_helpers import (
    BTC,
    ETH,
    ScriptedStrategy,
    config,
    new_service,
    record,
    sid,
    two_symbol_bars,
)

from tsclaude.contracts.market import Market
from tsclaude.marketdata.bars import BarClosed
from tsclaude.sim.clock import Clock, HistoricalClock
from tsclaude.sim.journal import (
    STEP_TYPE,
    EventStoreJournal,
    JournalEntry,
    JournalError,
    MemoryJournal,
    missing_contract_types,
)
from tsclaude.sim.live import LiveClock
from tsclaude.sim.testing import bar, minute
from tsclaude.store.eventstore import EventStore

M = 60_000


def _bars(n: int) -> dict[tuple[Market, str], list]:
    return two_symbol_bars(n)


def test_live_clock_releases_complete_minutes_in_global_order() -> None:
    bars = _bars(3)
    clock = LiveClock([BTC, ETH], settle_ms=90_000)
    assert isinstance(clock, Clock)
    assert clock.offer(bars[ETH][0]) == "buffered"
    assert clock.release(minute(1)) == []  # BTC minute 0 still missing, deadline not reached
    assert clock.offer(bars[BTC][0]) == "buffered"
    out = clock.release(minute(1))
    assert [(b.symbol, b.open_time_ms) for b in out] == [
        ("BTCUSDT", minute(0)),
        ("ETHUSDT", minute(0)),
    ]
    assert list(clock) == out  # the Clock protocol iterates what was released


def test_live_clock_deadline_releases_incomplete_minute_and_late_bars_are_dropped() -> None:
    bars = _bars(3)
    clock = LiveClock([BTC, ETH], settle_ms=90_000)
    clock.offer(bars[BTC][0])
    clock.offer(bars[BTC][1])
    assert clock.release(minute(1) + 89_999) == []
    out = clock.release(minute(1) + 90_000)
    assert [(b.symbol, b.open_time_ms) for b in out] == [("BTCUSDT", minute(0))]
    assert clock.stats.incomplete_minutes == 1
    assert clock.offer(bars[ETH][0]) == "late"  # minute 0 already released
    assert clock.late == [bars[ETH][0]]
    clock.offer(bars[ETH][1])
    assert [b.symbol for b in clock.release(minute(2))] == ["BTCUSDT", "ETHUSDT"]


def test_live_clock_duplicates_replacements_and_foreign_bars() -> None:
    bars = _bars(2)
    clock = LiveClock([BTC, ETH])
    clock.offer(bars[BTC][0])
    assert clock.offer(bars[BTC][0]) == "duplicate"
    newer = bars[BTC][0].model_copy(update={"close": 1000.5})
    assert clock.offer(newer, replaced=True) == "replaced"
    assert clock.offer(bar(0, 1, 1, 1, 1, symbol="XRPUSDT")) == "ignored"
    clock.offer(bars[ETH][0])
    out = clock.release(minute(1))
    assert out[0].close == 1000.5
    assert clock.offer(newer, replaced=True) == "late"


def test_live_clock_restart_with_partially_processed_minute() -> None:
    bars = _bars(2)
    clock = LiveClock([BTC, ETH], start_ms=minute(0), processed=[BTC])
    assert clock.offer(bars[BTC][0]) == "late"  # already processed before the restart
    clock.offer(bars[ETH][0])
    assert [b.symbol for b in clock.release(minute(1))] == ["ETHUSDT"]  # complete without BTC
    with pytest.raises(ValueError, match="start_ms"):
        LiveClock([BTC], processed=[BTC])


def test_live_clock_session_replays_identically_through_historical_clock() -> None:
    bars = _bars(4)
    clock = LiveClock([BTC, ETH], settle_ms=0)
    for i in range(4):
        clock.offer(bars[ETH][i])
        if i != 2:
            clock.offer(bars[BTC][i])
        clock.release(minute(i + 1))
    replayed = list(HistoricalClock(clock.released))
    assert replayed == list(clock.released)


# ---------------------------------------------------------------- journals
def test_memory_journal_requires_input_first_and_injects_failures() -> None:
    j = MemoryJournal(fail_when=lambda i, _b: i == 1)
    head = JournalEntry(STEP_TYPE, "sim", "paper", 1, {"kind": "bar"})
    with pytest.raises(JournalError, match="input entry"):
        j.append([JournalEntry("sim_x", "sim", "run", 1, {})])
    j.append([head])
    with pytest.raises(JournalError, match="injected"):
        j.append([head])
    assert len(j.batches()) == 1


def test_contract_defines_every_journal_type() -> None:
    assert missing_contract_types() == []


def test_event_store_journal_round_trip_and_restore_after_reopen(tmp_path: Path) -> None:
    """Production path: the paper session journals into the hash-chained event store; a fresh
    process (store reopened from disk) restores the identical ledgers and keeps the chain valid."""
    from paper_helpers import drive

    from tsclaude.contracts.events import EventType
    from tsclaude.sim.paper import PaperTradingService

    path = tmp_path / "state.db"
    strat = ScriptedStrategy(
        {
            (minute(1), "BTCUSDT"): [record(1, 1)],
            (minute(2), "ETHUSDT"): [record(2, 2, symbol="ETHUSDT", tp=1012.0)],  # vetoed
        }
    )
    bars = two_symbol_bars(10, {6: (1000, 1035, 999, 1030)}, {5: (1000, 1015, 999, 1010)})
    with EventStore(path) as store:
        svc = PaperTradingService.open(config(), store, strategy=strat)
        drive(svc, bars, list(range(6)))
        hashes = svc.ledger_hashes()
        assert store.verify().ok
        types = {e.type for e in store.read()}
        assert {EventType.PAPER_STEP, EventType.SIM_ORDER_FILLED, EventType.SIGNAL_CREATED} <= types
        (shadow,) = store.read(types=[EventType.SIGNAL_SHADOW_OUTCOME])
        assert shadow.aggregate_id == sid(2)
        assert shadow.payload["outcome"]["exit_reason"] == "take_profit"
    with EventStore(path) as store:  # restart: new process, same file
        restored = PaperTradingService.open(config(), store, strategy=strat)
        assert restored.ledger_hashes() == hashes
        s2 = restored.session.lifecycle.get(sid(2))
        assert s2 is not None
        assert s2.shadow_outcome is not None
        drive(restored, bars, list(range(6, 10)))
        assert store.verify().ok
        assert len(EventStoreJournal(store).batches()) == restored.session.batches == 20
        assert restored.latency_ms() == [1_500] * 20  # drive polls 1.5 s after each close


def test_event_store_journal_rejects_bad_batches(tmp_path: Path) -> None:
    with EventStore(tmp_path / "s.db") as store:
        j = EventStoreJournal(store)
        with pytest.raises(JournalError, match="input entry"):
            j.append([JournalEntry("sim_order_filled", "sim", "baseline", 1, {})])
        with pytest.raises(JournalError, match="append failed"):
            j.append([JournalEntry(STEP_TYPE, "sim", "paper", 1, {"x": float("nan")})])
        assert j.batches() == []


# ---------------------------------------------------------------- async driver
class _FakeSubscription:
    def __init__(self, items: list[object]) -> None:
        self.items = list(items)

    async def get(self, timeout: float | None = None) -> object | None:
        await asyncio.sleep(0)
        return self.items.pop(0) if self.items else None


def test_async_run_consumes_subscription_until_stopped() -> None:
    from risk_helpers import snapshot

    bars = _bars(3)
    items: list[object] = [BarClosed(bars[k][i]) for i in range(3) for k in (ETH, BTC)]
    svc, journal = new_service()
    clock_now = {"t": minute(0)}

    def now() -> int:
        clock_now["t"] += 30_000
        return clock_now["t"]

    async def main() -> None:
        stop = asyncio.Event()
        sub = _FakeSubscription(items)

        async def stopper() -> None:
            while sub.items or svc.session.batches < 6:
                await asyncio.sleep(0)
            stop.set()

        await asyncio.gather(
            svc.run(sub, lambda: snapshot(clock_now["t"], symbols=(BTC, ETH)), now, stop),
            stopper(),
        )

    asyncio.run(asyncio.wait_for(main(), timeout=5))
    assert svc.session.batches == 6
    assert len(journal.batches()) == 6
