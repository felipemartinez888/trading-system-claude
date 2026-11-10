from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from api_testkit import T0, ContextFactory

from tsclaude.contracts.timeutil import ManualClock

#: Opt-in latency injection, off unless set (CI and normal runs are unaffected). It reproduces the
#: slow-runner conditions under which tests that slept a fixed real time before asserting failed:
#:   TSC_TEST_LATENCY_MS=40 TSC_TEST_LATENCY_MODE=commit  blocks every event-store commit (a slow
#:                                                        fsync: the event loop stands still)
#:   TSC_TEST_LATENCY_MS=80 TSC_TEST_LATENCY_MODE=round   delays every fake server-clock round
#: Tests that wait for what the runtime did (`Rig.settle`, `until`) pass under either.
LATENCY_MS = float(os.environ.get("TSC_TEST_LATENCY_MS", "0") or 0)
LATENCY_MODE = os.environ.get("TSC_TEST_LATENCY_MODE", "commit")


@pytest.fixture(autouse=True)
def injected_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    if LATENCY_MS <= 0:
        return
    if LATENCY_MODE == "commit":
        from tsclaude.store.eventstore import EventStore

        append_many = EventStore.append_many

        def slow_append(self: EventStore, events: Sequence[Any]) -> Any:
            time.sleep(LATENCY_MS / 1000)
            return append_many(self, events)

        monkeypatch.setattr(EventStore, "append_many", slow_append)
    elif LATENCY_MODE == "round":
        from m1t2_fakes import FakeMarketData

        resync_clock = FakeMarketData.resync_clock

        async def slow_round(self: FakeMarketData) -> bool:
            ok = await resync_clock(self)  # counted first, like a slow exchange answer
            await asyncio.sleep(LATENCY_MS / 1000)
            return ok

        monkeypatch.setattr(FakeMarketData, "resync_clock", slow_round)
    else:
        raise ValueError(f"unknown TSC_TEST_LATENCY_MODE {LATENCY_MODE!r}")


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def make_context(db_path: Path, clock: ManualClock) -> Iterator[ContextFactory]:
    factory = ContextFactory(db_path, clock)
    yield factory
    factory.close_all()
