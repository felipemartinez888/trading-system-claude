"""LiveClock ordering rules, journals (memory, event store fail-closed) and the async driver."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from paper_helpers import BTC, ETH, ScriptedStrategy, config, new_service, record, two_symbol_bars

from tsclaude.contracts.market import Market
from tsclaude.marketdata.bars import BarClosed
from tsclaude.sim.clock import Clock, HistoricalClock
from tsclaude.sim.journal import (
    STEP_TYPE,
    EventStoreJournal,
    JournalEntry,
    JournalError,
    JournalUnavailable,
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


def test_event_store_journal_fails_closed_until_contract_types_exist(tmp_path: Path) -> None:
    missing = missing_contract_types()
    if not missing:
        pytest.skip("contract already defines the paper journal types")
    assert STEP_TYPE in missing
    assert "aggregate_type:sim" in missing
    with EventStore(tmp_path / "state.db") as store, pytest.raises(JournalUnavailable) as err:
        EventStoreJournal(store)
    assert "sim_order_filled" in str(err.value)


@pytest.mark.skipif(bool(missing_contract_types()), reason="needs the M3-T1 contract change")
def test_event_store_journal_round_trip_and_restore(tmp_path: Path) -> None:
    from paper_helpers import drive

    from tsclaude.sim.paper import PaperTradingService

    with EventStore(tmp_path / "state.db") as store:
        journal = EventStoreJournal(store)
        strat = ScriptedStrategy({(minute(1), "BTCUSDT"): [record(1, 1)]})
        svc, _ = new_service(strat, journal=journal)  # type: ignore[arg-type]
        drive(svc, _bars(6), list(range(6)))
        assert store.verify().ok
        restored = PaperTradingService.restore(config(), journal)
        assert restored.ledger_hashes() == svc.ledger_hashes()


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
