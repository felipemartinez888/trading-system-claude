"""BarWriter / WriterSink: one writer thread, never two writes at once, writes are joined."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest
from md_helpers import MIN, T0, rest_kline

from tsclaude.contracts.market import Bar, BarSource, Market
from tsclaude.marketdata.convert import bar_from_rest
from tsclaude.marketdata.writer import (
    BarWriter,
    WriterBusy,
    WriterClosed,
    WriterSink,
    WriterStuck,
)
from tsclaude.store.livebars import Checkpoint, LiveBarStore, WriteResult


def _bar(i: int) -> Bar:
    return bar_from_rest(
        Market.SPOT, "BTCUSDT", rest_kline(T0 + i * MIN), BarSource.REST_BACKFILL, 1
    )


class GatedStore:
    """A store whose `write_many` waits for `gate` (so a test decides when a write ends)."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self.fail = fail
        self._lock = threading.Lock()

    def write_many(
        self,
        bars: Sequence[Bar],
        observed_session_id: str | None = None,
        *,
        checkpoint: Checkpoint | None = None,
    ) -> list[WriteResult]:
        with self._lock:
            self.calls += 1
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.entered.set()
        try:
            self.gate.wait(10)
            if self.fail is not None:
                raise self.fail
            return [WriteResult.__new__(WriteResult) for _ in bars]
        finally:
            with self._lock:
                self.concurrent -= 1


async def _until(predicate: object, limit: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + limit
    while not predicate():  # type: ignore[operator]
        assert loop.time() < end, "condition not reached"
        await asyncio.sleep(0.005)


def test_writes_are_single_flight_and_busy_until_the_store_returns() -> None:
    async def scenario() -> None:
        store = GatedStore()
        writer = BarWriter(store)
        future = writer.submit([_bar(0)])
        await _until(store.entered.is_set)
        assert writer.busy
        with pytest.raises(WriterBusy):
            writer.submit([_bar(1)])  # a second write can never overlap the first
        store.gate.set()
        assert len(await future) == 1
        assert await writer.wait_idle(1.0)
        assert not writer.busy
        assert store.max_concurrent == 1
        assert store.calls == 1
        assert await writer.join(2.0)
        assert not writer.alive

    asyncio.run(scenario())


def test_close_refuses_work_and_join_waits_for_the_write_in_flight() -> None:
    async def scenario() -> None:
        store = GatedStore()
        writer = BarWriter(store)
        future = writer.submit([_bar(0)])
        await _until(store.entered.is_set)
        writer.close()
        with pytest.raises(WriterClosed):
            writer.submit([_bar(1)])
        assert not await writer.join(0.05)  # still writing: the thread cannot be gone
        assert writer.alive
        assert writer.busy
        store.gate.set()
        assert await writer.join(2.0)  # the write finished, then the thread exited
        assert not writer.alive
        assert not writer.busy
        await future  # and its result was delivered

    asyncio.run(scenario())


def test_a_failing_write_reaches_the_caller_and_the_writer_stays_usable() -> None:
    async def scenario() -> None:
        store = GatedStore(fail=RuntimeError("disk full"))
        store.gate.set()
        writer = BarWriter(store)
        with pytest.raises(RuntimeError, match="disk full"):
            await writer.submit([_bar(0)])
        assert await writer.wait_idle(1.0)
        store.fail = None
        assert len(await writer.submit([_bar(1)])) == 1
        assert await writer.join(2.0)

    asyncio.run(scenario())


def test_slow_write_is_reported_and_joined_never_overlapped() -> None:
    async def scenario() -> None:
        store = GatedStore()
        writer = BarWriter(store)
        events: list[str] = []
        sink = WriterSink(writer, write_timeout_s=0.05, join_timeout_s=5.0, on_event=events.append)
        task = asyncio.create_task(sink.write([_bar(0)], None))
        await _until(lambda: sink.write_slow == 1)  # timeout hit while the store is still writing
        assert not task.done()
        assert writer.busy  # joined: the sink waits, the write is not abandoned
        assert any(e.startswith("write_slow") for e in events)
        with pytest.raises(WriterBusy):
            writer.submit([_bar(1)])  # nobody can start a second write meanwhile
        store.gate.set()
        assert len(await task) == 1
        assert store.max_concurrent == 1
        assert store.calls == 1
        assert await writer.join(2.0)

    asyncio.run(scenario())


def test_cancelled_caller_waits_for_the_write_before_the_cancellation_continues() -> None:
    async def scenario() -> None:
        store = GatedStore()
        writer = BarWriter(store)
        sink = WriterSink(writer, write_timeout_s=30.0, join_timeout_s=5.0)
        task = asyncio.create_task(sink.write([_bar(0)], None))
        await _until(store.entered.is_set)
        task.cancel()
        await asyncio.sleep(0.1)
        assert not task.done()  # cancelling the task did not "end" the write: it is being joined
        assert writer.busy
        store.gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not writer.busy  # the write was over before the task reported cancellation
        assert await writer.join(2.0)

    asyncio.run(scenario())


def test_a_stuck_writer_is_declared_and_never_given_a_second_write() -> None:
    async def scenario() -> None:
        store = GatedStore()
        writer = BarWriter(store)
        events: list[str] = []
        sink = WriterSink(writer, write_timeout_s=0.02, join_timeout_s=0.05, on_event=events.append)
        with pytest.raises(WriterStuck):
            await sink.write([_bar(0)], None)
        assert sink.stuck
        assert writer.busy
        assert writer.alive
        assert any(e.startswith("writer_stuck") for e in events)
        with pytest.raises(WriterStuck):
            await sink.write([_bar(1)], None)  # refused without touching the writer
        assert store.calls == 1
        assert not await writer.join(0.05)  # honest: the thread is still inside the store
        store.gate.set()  # the uninterruptible write finally ends
        assert await writer.join(2.0)
        assert store.max_concurrent == 1

    asyncio.run(scenario())


def test_writer_with_a_real_store_commits_and_closing_waits_for_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = LiveBarStore(tmp_path / "state.db")
        writer = BarWriter(store)
        sink = WriterSink(writer, write_timeout_s=5.0, join_timeout_s=5.0)
        results = await sink.write([_bar(i) for i in range(50)], None)
        assert len(results) == 50
        assert store.count() == 50
        assert await writer.join(2.0)  # the thread is gone before the store is closed
        store.close()
        assert store.closed

    asyncio.run(scenario())
