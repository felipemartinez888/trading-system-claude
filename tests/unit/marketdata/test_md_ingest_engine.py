"""Chunked, newest-first, resumable backfill engine (synthetic data, no network)."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from md_helpers import MIN, T0, FakeFetcher, Sink, rest_kline

from tsclaude.contracts.events import EventType
from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.marketdata.backfill import (
    Backfiller,
    BackfillResult,
    BackfillSettings,
    PairProgress,
    RunLimits,
)
from tsclaude.marketdata.types import RestKline
from tsclaude.marketdata.writer import BarWriter, WriterSink
from tsclaude.quality.gaps import GapRegistry
from tsclaude.runtime.sessions import ObservationInterval
from tsclaude.store.livebars import Checkpoint, LiveBarStore

M, SYM = Market.SPOT, "BTCUSDT"
N = 250  # minutes in the window [T0, END)
END = T0 + N * MIN
SETTINGS = BackfillSettings(lookback_ms=N * MIN, max_backfill_ms=2 * N * MIN)


class Crash(BaseException):
    """Stands in for the process dying: nothing in the engine may catch it."""


def _fetcher(skip: set[int] | None = None, total: int = N + 20) -> FakeFetcher:
    f = FakeFetcher()
    for i in range(total):
        if skip and i in skip:
            continue
        f.put(M, SYM, rest_kline(T0 + i * MIN))
    return f


def _backfiller(
    bars: LiveBarStore,
    gaps: GapRegistry,
    fetch: Any,
    sink: Sink,
    clock: ManualClock,
    *,
    chunk: int = 100,
    settings: BackfillSettings = SETTINGS,
    **kw: Any,
) -> Backfiller:
    return Backfiller(
        bars,
        gaps,
        fetch,
        sink,
        clock.now_ms,
        settings,
        limits=RunLimits(chunk_minutes=chunk, max_consecutive_errors=kw.pop("max_errors", 3)),
        **kw,
    )


def _run(bf: Backfiller, **kw: Any) -> BackfillResult:
    return asyncio.run(bf.run(M, SYM, END, [], **kw))


def _opens(bars: LiveBarStore) -> list[int]:
    return [s.bar.open_time_ms for s in bars.range(M, SYM, T0, END)]


def test_chunks_are_processed_newest_first_and_bounded(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    fetch = _fetcher()
    res = _run(_backfiller(bars, gaps, fetch, sink, clock))
    assert [(lo, hi) for _m, _s, lo, hi in fetch.calls] == [
        (T0 + 150 * MIN, T0 + 250 * MIN),
        (T0 + 50 * MIN, T0 + 150 * MIN),
        (T0, T0 + 50 * MIN),
    ]
    assert (res.status, res.chunks_done, res.chunks_total) == ("complete", 3, 3)
    assert res.inserted == N
    assert _opens(bars) == [T0 + i * MIN for i in range(N)]
    done = sink.of(EventType.BACKFILL_COMPLETED)
    assert len(done) == 1
    assert done[0].payload["status"] == "complete"
    assert done[0].payload["chunks_done"] == 3


def test_commits_are_per_chunk_not_per_row(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    commits: list[str] = []
    bars._conn.set_trace_callback(
        lambda stmt: commits.append(stmt) if stmt.strip().upper().startswith("COMMIT") else None
    )
    _run(_backfiller(bars, gaps, _fetcher(), sink, clock, chunk=100))
    assert len(commits) == 3 + 1  # three chunks + the terminal checkpoint; per row it was 250


def test_chunk_size_changes_nothing_but_the_number_of_transactions(tmp_path: Path) -> None:
    obs = [ObservationInterval(T0 + 20 * MIN + 30_000, T0 + 139 * MIN + 10_000, "MON1", "user")]
    outcomes: list[tuple[Any, ...]] = []
    for chunk in (1000, 100, 7, 1):
        sink, clock = Sink(), ManualClock(T0)
        counter = iter(range(10_000))
        gaps = GapRegistry(sink, clock.now_ms, new_id=lambda _t, c=counter: f"gap-{next(c):04d}")
        with LiveBarStore(tmp_path / f"c{chunk}.db") as bars:
            # seed an old bar so part of the window is "holes after the first stored bar"
            seed = _backfiller(bars, gaps, _fetcher(), sink, clock, chunk=1000)
            asyncio.run(
                seed.run(M, SYM, T0 + 40 * MIN, [], should_stop=None)
            )  # stores minutes [..40)
            sink.events.clear()
            fetch = _fetcher(skip={60, 61, 62, 200})
            bf = _backfiller(bars, gaps, fetch, sink, clock, chunk=chunk)
            res = asyncio.run(bf.run(M, SYM, END, obs))
            stored = [(s.bar.open_time_ms, str(s.bar.source)) for s in bars.range(M, SYM, T0, END)]
            outcomes.append(
                (
                    stored,
                    gaps.missing_ranges(M, SYM),
                    (res.inserted_backfill, res.inserted_recovered, res.conflicts, res.invalid),
                    res.gaps_opened_minutes,
                    res.remaining_open_minutes,
                    res.status,
                )
            )
    assert all(o == outcomes[0] for o in outcomes[1:])
    assert outcomes[0][1]  # the four unreturned minutes stayed registered as gaps


def test_stop_between_chunks_leaves_whole_chunks_and_a_resumable_checkpoint(
    tmp_path: Path, clock: ManualClock
) -> None:
    sink = Sink()
    gaps = GapRegistry(sink, clock.now_ms, new_id=lambda _t: "gap")
    fetch = _fetcher()
    stop = {"flag": False}
    inner = fetch.__call__

    async def fetch_then_stop(m: Market, s: str, lo: int, hi: int) -> list[RestKline]:
        rows = await inner(m, s, lo, hi)
        if len(fetch.calls) == 2:
            stop["flag"] = True  # Stop arrives while chunk 2 is being fetched
        return rows

    with LiveBarStore(tmp_path / "state.db") as bars:
        prog = PairProgress(M, SYM, "startup")
        res = _run(
            _backfiller(bars, gaps, fetch_then_stop, sink, clock),
            progress=prog,
            should_stop=lambda: stop["flag"],
        )
        # chunk 2 was already fetched, so it is stored too; chunk 3 never starts
        assert (res.status, res.chunks_done) == ("cancelled", 2)
        assert _opens(bars) == [T0 + i * MIN for i in range(50, N)]  # a contiguous recent suffix
        cp = bars.get_checkpoint("startup", M, SYM)
        assert cp is not None
        assert (cp.status, cp.cursor_ms) == ("cancelled", T0 + 50 * MIN)
        assert prog.state == "cancelled"
        assert sink.of(EventType.GAP_DETECTED) == []  # the unfinished initial load is not a gap
        payload = sink.of(EventType.BACKFILL_COMPLETED)[0].payload
        assert payload["status"] == "cancelled"
        assert payload["chunks_done"] == 2
        interrupted = list(_opens(bars))

        # resume: only the remainder is fetched, checkpoint verdict ok, nothing re-inserted
        fetch2 = _fetcher()
        res2 = _run(_backfiller(bars, gaps, fetch2, sink, clock))
        assert res2.checkpoint == "ok"
        assert [(lo, hi) for _m, _s, lo, hi in fetch2.calls] == [(T0, T0 + 50 * MIN)]
        assert res2.inserted == 50
        assert res2.status == "complete"
        assert _opens(bars) == [T0 + i * MIN for i in range(N)]
        assert interrupted == _opens(bars)[50:]
        assert sink.of(EventType.GAP_DETECTED) == []


def test_crash_between_chunks_recovers_on_restart_without_gaps_or_duplicates(
    tmp_path: Path, clock: ManualClock
) -> None:
    path = tmp_path / "state.db"
    sink = Sink()
    gaps = GapRegistry(sink, clock.now_ms, new_id=lambda _t: "gap")
    fetch = _fetcher()
    inner = fetch.__call__

    async def die_on_third(m: Market, s: str, lo: int, hi: int) -> list[RestKline]:
        if len(fetch.calls) == 2:
            raise Crash  # the process dies after two committed chunks
        return await inner(m, s, lo, hi)

    store = LiveBarStore(path)
    with pytest.raises(Crash):
        _run(_backfiller(store, gaps, die_on_third, sink, clock))
    store.close()  # (the OS would release the file; WAL recovery keeps committed chunks)

    with LiveBarStore(path) as reopened:
        assert _opens(reopened) == [T0 + i * MIN for i in range(50, N)]
        cp = reopened.get_checkpoint("startup", M, SYM)
        assert cp is not None
        assert cp.status == "running"
        assert cp.cursor_ms == T0 + 50 * MIN  # never ahead of the stored rows
        fresh_gaps = GapRegistry(Sink(), clock.now_ms, new_id=lambda _t: "gap")
        res = _run(_backfiller(reopened, fresh_gaps, _fetcher(), Sink(), clock))
        assert res.checkpoint == "ok"
        assert res.inserted == 50
        assert res.duplicates == 0
        assert _opens(reopened) == [T0 + i * MIN for i in range(N)]
        assert fresh_gaps.missing_ranges(M, SYM) == []


def test_checkpoint_ahead_of_the_store_is_detected_and_the_store_wins(
    tmp_path: Path, clock: ManualClock
) -> None:
    path = tmp_path / "state.db"
    sink = Sink()
    gaps = GapRegistry(sink, clock.now_ms, new_id=lambda _t: "gap")
    with LiveBarStore(path) as bars:
        _run(_backfiller(bars, gaps, _fetcher(), sink, clock))
    raw = sqlite3.connect(path)  # rows vanish behind the checkpoint's back
    raw.execute("DELETE FROM bars_1m WHERE open_time_ms >= ? AND open_time_ms < ?",
                (T0 + 100 * MIN, T0 + 110 * MIN))  # fmt: skip
    raw.commit()
    raw.close()
    with LiveBarStore(path) as bars:
        fetch = _fetcher()
        res = _run(_backfiller(bars, gaps, fetch, sink, clock))
        assert res.checkpoint.startswith("mismatch:10")
        assert [(lo, hi) for _m, _s, lo, hi in fetch.calls] == [(T0 + 100 * MIN, T0 + 110 * MIN)]
        assert _opens(bars) == [T0 + i * MIN for i in range(N)]
        assert _run(_backfiller(bars, gaps, _fetcher(), sink, clock)).checkpoint == "ok"


def test_foreign_or_invalid_checkpoints_are_ignored(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    base = Checkpoint("startup", M, SYM, T0, END, T0 + 10 * MIN, 5, 1, "running", T0)
    bars.set_checkpoint(replace(base, schema_version=99))
    assert _run(_backfiller(bars, gaps, _fetcher(), sink, clock)).checkpoint == "ignored:schema"
    bars.set_checkpoint(replace(base, cursor_ms=END + MIN))
    assert _run(_backfiller(bars, gaps, _fetcher(), sink, clock)).checkpoint == "ignored:invalid"


def test_a_failed_chunk_is_recorded_and_only_it_is_fetched_next_time(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    fetch = _fetcher()
    inner = fetch.__call__

    async def flaky(m: Market, s: str, lo: int, hi: int) -> list[RestKline]:
        if lo == T0 + 50 * MIN:
            raise ConnectionError("reset by peer")
        return await inner(m, s, lo, hi)

    res = _run(_backfiller(bars, gaps, flaky, sink, clock))
    assert res.status == "partial"
    assert len(res.errors) == 1
    assert "reset by peer" in res.errors[0]
    assert res.chunks_done == 2  # the failed chunk is not "done"
    assert gaps.missing_ranges(M, SYM) == [(T0 + 50 * MIN, T0 + 150 * MIN)]
    retry = _fetcher()
    res2 = _run(_backfiller(bars, gaps, retry, sink, clock))
    assert [(lo, hi) for _m, _s, lo, hi in retry.calls] == [(T0 + 50 * MIN, T0 + 150 * MIN)]
    assert res2.status == "complete"
    assert gaps.missing_ranges(M, SYM) == []
    assert _opens(bars) == [T0 + i * MIN for i in range(N)]


def test_consecutive_errors_stop_the_pair_and_leave_the_rest_untried(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    fetch = _fetcher()
    fetch.fail = ConnectionError("down")
    res = _run(_backfiller(bars, gaps, fetch, sink, clock, chunk=50, max_errors=2))
    assert res.status == "failed"
    assert len(fetch.calls) == 2  # chunks 3.. were never attempted
    assert len(res.errors) == 2
    assert sink.of(EventType.BACKFILL_COMPLETED)[0].payload["status"] == "failed"
    assert bars.count() == 0


def test_a_hanging_fetch_times_out_instead_of_stalling(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    async def hang(m: Market, s: str, lo: int, hi: int) -> list[RestKline]:
        await asyncio.sleep(60)
        return []

    bf = Backfiller(
        bars,
        gaps,
        hang,
        sink,
        clock.now_ms,
        SETTINGS,
        limits=RunLimits(chunk_minutes=100, chunk_timeout_s=0.02, max_consecutive_errors=2),
    )
    started = time.monotonic()
    res = _run(bf)
    assert time.monotonic() - started < 5
    assert res.status == "failed"
    assert all("TimeoutError" in e for e in res.errors)


def test_time_budget_pauses_the_run_and_the_next_run_finishes_it(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    now = {"t": 0.0}
    fetch = _fetcher()
    inner = fetch.__call__

    async def slow(m: Market, s: str, lo: int, hi: int) -> list[RestKline]:
        now["t"] += 1.0  # each chunk "takes" one second of the budget
        return await inner(m, s, lo, hi)

    bf = Backfiller(
        bars,
        gaps,
        slow,
        sink,
        clock.now_ms,
        SETTINGS,
        limits=RunLimits(chunk_minutes=50, pair_budget_s=2.5),
        monotonic=lambda: now["t"],
    )
    res = _run(bf)
    assert (res.status, res.chunks_done, res.chunks_total) == ("paused", 3, 5)
    cp = bars.get_checkpoint("startup", M, SYM)
    assert cp is not None
    assert cp.status == "paused"
    now["t"] = 0.0
    res2 = _run(bf)
    assert (res2.status, res2.chunks_done) == ("complete", 2)
    assert _opens(bars) == [T0 + i * MIN for i in range(N)]


def test_bounded_catchup_registers_older_downtime_and_history_loads_it(
    bars: LiveBarStore, gaps: GapRegistry, sink: Sink, clock: ManualClock
) -> None:
    seed_settings = BackfillSettings(lookback_ms=10 * MIN, max_backfill_ms=2 * N * MIN)
    seed = _backfiller(bars, gaps, _fetcher(), sink, clock, chunk=1000, settings=seed_settings)
    asyncio.run(seed.run(M, SYM, T0 + 10 * MIN, []))  # the store ends at T0 + 9 min
    last = bars.last(M, SYM)
    assert last is not None
    assert last.bar.open_time_ms == T0 + 9 * MIN
    sink.events.clear()

    catchup = BackfillSettings(
        lookback_ms=60 * MIN, max_backfill_ms=2 * N * MIN, extend_to_last_stored=False
    )
    fetch = _fetcher()
    res = _run(_backfiller(bars, gaps, fetch, sink, clock, settings=catchup, job="catchup"))
    assert res.from_ms == END - 60 * MIN
    assert all(lo >= END - 60 * MIN for _m, _s, lo, _hi in fetch.calls)  # nothing older is fetched
    assert gaps.missing_ranges(M, SYM) == [(T0 + 10 * MIN, END - 60 * MIN)]
    detail = sink.of(EventType.GAP_DETECTED)[0].payload["detail"]
    assert "startup catch-up" in detail

    history = _fetcher()
    res2 = _run(_backfiller(bars, gaps, history, sink, clock, job="history"))
    assert res2.status == "complete"
    assert gaps.missing_ranges(M, SYM) == []
    assert _opens(bars) == [T0 + i * MIN for i in range(N)]


def test_engine_is_responsive_with_the_writer_thread_and_a_slow_store(tmp_path: Path) -> None:
    """With the real writer thread the event loop keeps running while every chunk is stored,
    even though each transaction takes 20 ms (counted yields, no timing thresholds)."""

    class SlowStore(LiveBarStore):
        def write_many(self, bars: Any, observed_session_id: Any = None, **kw: Any) -> Any:
            time.sleep(0.02)
            return super().write_many(bars, observed_session_id, **kw)

    async def scenario() -> tuple[int, BackfillResult]:
        clock, sink = ManualClock(T0), Sink()
        gaps = GapRegistry(sink, clock.now_ms, new_id=lambda _t: "gap")
        store = SlowStore(tmp_path / "state.db")
        writer = BarWriter(store)
        wsink = WriterSink(writer, write_timeout_s=5.0, join_timeout_s=5.0)
        bf = Backfiller(
            store,
            gaps,
            _fetcher(),
            sink,
            clock.now_ms,
            SETTINGS,
            limits=RunLimits(chunk_minutes=25),  # 10 chunks -> ~220 ms in the store
            sink=wsink,
        )
        ticks = 0
        running = True

        async def heartbeat() -> None:
            nonlocal ticks
            while running:
                ticks += 1
                await asyncio.sleep(0)

        beat = asyncio.create_task(heartbeat())
        res = await bf.run(M, SYM, END, [])
        running = False
        await beat
        assert await writer.join(2.0)
        store.close()
        return ticks, res

    ticks, res = asyncio.run(scenario())
    assert res.status == "complete"
    assert res.chunks_done == 10
    # inline writes would block the loop for the whole run (a handful of ticks at most)
    assert ticks > 100
