"""Ingestion coordinator: bounded catch-up, on-demand history, deterministic Stop (synthetic)."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from md_helpers import MIN, T0, FakeFetcher, Sink, rest_kline

from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.marketdata.config import BackfillConfig, BackfillRunConfig, BackfillStartupConfig
from tsclaude.marketdata.ingest import (
    CATCHUP,
    HISTORY,
    RUNNING,
    STOPPED,
    STUCK,
    IngestionCoordinator,
)
from tsclaude.marketdata.types import RestKline
from tsclaude.quality.gaps import GapRegistry
from tsclaude.store.livebars import LiveBarStore

M, SYM = Market.SPOT, "BTCUSDT"
DAY = 1440
END = T0 + DAY * MIN


def _cfg(*, catchup: int | None = 60, history: str = "on_demand", **run: Any) -> BackfillConfig:
    base: dict[str, Any] = {
        "chunk_minutes": 500,
        "inter_chunk_pause_ms": 0,
        "chunk_timeout_s": 5,
        "write_timeout_s": 5,
        "write_join_timeout_s": 10,
        "pair_budget_s": 30,
        "run_budget_s": 60,
        "stop_grace_s": 5,
    }
    return BackfillConfig(
        lookback_days=1.0,
        startup=BackfillStartupConfig(catchup_max_minutes=catchup, history=history),  # type: ignore[arg-type]
        run=BackfillRunConfig(**(base | run)),
    )


def _fetcher() -> FakeFetcher:
    f = FakeFetcher()
    for i in range(DAY + 10):
        f.put(M, SYM, rest_kline(T0 + i * MIN))
    return f


def _coordinator(
    store: LiveBarStore,
    cfg: BackfillConfig,
    fetch: Callable[[Market, str, int, int], Awaitable[list[RestKline]]],
) -> IngestionCoordinator:
    clock, sink = ManualClock(T0), Sink()
    ids = iter(range(999))
    gaps = GapRegistry(sink, clock.now_ms, new_id=lambda _t: f"g{next(ids)}")
    return IngestionCoordinator(
        bars=store,
        gaps=gaps,
        fetch=fetch,
        emit=sink,
        now_ms=clock.now_ms,
        config=cfg,
        pairs=lambda: [(M, SYM)],
        intervals=lambda: [],
    )


def test_start_loads_only_the_bounded_catchup_and_history_waits_for_a_request(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fetch = _fetcher()
        with LiveBarStore(tmp_path / "s.db") as store:
            co = _coordinator(store, _cfg(), fetch)
            co.start(END)
            await co.wait_catchup(10)
            assert co.catchup_complete()
            assert not co.history_complete()  # 60 of 1440 minutes: not a full warm-up window
            assert store.count() == 60
            assert min(lo for _m, _s, lo, _hi in fetch.calls) == END - 60 * MIN
            assert co.status()["history"]["state"] == "not_requested"

            assert co.request_history() == "started"
            assert co.request_history() == "already_running"
            await co.wait_history(10)
            assert co.history_complete()
            assert store.count() == DAY
            assert co.request_history() == "complete"
            assert await co.stop() is True
            assert (co.phase, co.safe_to_close()) == (STOPPED, True)
            assert {k[0] for k in co.progress} == {CATCHUP, HISTORY}

    asyncio.run(scenario())


def test_background_policy_runs_history_right_after_the_catchup(tmp_path: Path) -> None:
    async def scenario() -> None:
        with LiveBarStore(tmp_path / "s.db") as store:
            co = _coordinator(store, _cfg(history="background"), _fetcher())
            co.start(END)
            await co.wait_history(10)
            assert co.history_complete()
            assert store.count() == DAY
            await co.stop()

    asyncio.run(scenario())


def test_history_off_refuses_and_legacy_catchup_is_the_whole_window(tmp_path: Path) -> None:
    async def scenario() -> None:
        with LiveBarStore(tmp_path / "off.db") as store:
            co = _coordinator(store, _cfg(history="off"), _fetcher())
            co.start(END)
            await co.wait_catchup(10)
            assert co.request_history() == "refused"
            assert not co.history_complete()
            await co.stop()
        with LiveBarStore(tmp_path / "legacy.db") as store:
            co = _coordinator(store, _cfg(catchup=None, history="off"), _fetcher())
            co.start(END)
            await co.wait_catchup(10)
            assert co.request_history() == "not_needed"
            assert co.history_complete()  # the catch-up already covered the whole lookback
            assert store.count() == DAY
            await co.stop()

    asyncio.run(scenario())


def test_incomplete_history_never_reports_complete(tmp_path: Path) -> None:
    async def scenario() -> None:
        fetch = _fetcher()
        inner = fetch.__call__

        async def flaky(m: Market, s: str, lo: int, hi: int) -> list[RestKline]:
            if lo < END - 600 * MIN <= hi:
                raise ConnectionError("reset")
            return await inner(m, s, lo, hi)

        with LiveBarStore(tmp_path / "s.db") as store:
            co = _coordinator(store, _cfg(), flaky)
            co.start(END)
            await co.wait_catchup(10)
            co.request_history()
            await co.wait_history(10)
            assert not co.history_complete()
            assert co.status()["history"]["state"] in {"incomplete", "failed"}
            await co.stop()

    asyncio.run(scenario())


def test_stop_cancels_a_hanging_fetch_after_the_grace_and_the_writer_is_idle(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()

        async def hang(m: Market, s: str, lo: int, hi: int) -> list[RestKline]:
            entered.set()
            await asyncio.sleep(60)
            return []

        with LiveBarStore(tmp_path / "s.db") as store:
            co = _coordinator(store, _cfg(stop_grace_s=0.05), hang)
            co.start(END)
            await asyncio.wait_for(entered.wait(), 5)
            started = time.monotonic()
            assert await co.stop() is True
            assert time.monotonic() - started < 3
            assert co.phase == STOPPED
            assert co.catchup_done.is_set()  # nobody keeps waiting on a stopped run
            assert not co._writer.alive  # type: ignore[union-attr]

    asyncio.run(scenario())


class _SlowStore(LiveBarStore):
    """write_many takes `delay` s; records whether a write ever ran after close()."""

    delay = 0.3
    gate: threading.Event | None = None

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.write_after_close = False
        self.in_write = threading.Event()

    def write_many(self, bars: Any, observed_session_id: Any = None, **kw: Any) -> Any:
        if self.closed:
            self.write_after_close = True
        self.in_write.set()
        if self.gate is not None:
            self.gate.wait(30)
        else:
            time.sleep(self.delay)
        return super().write_many(bars, observed_session_id, **kw)


def test_stop_during_a_slow_write_waits_for_the_transaction_before_the_store_closes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = _SlowStore(tmp_path / "s.db")
        co = _coordinator(store, _cfg(catchup=None, history="off"), _fetcher())
        co.start(END)
        await asyncio.wait_for(asyncio.to_thread(store.in_write.wait, 5), 6)
        assert await co.stop() is True  # joins the in-flight transaction; nothing more is submitted
        assert not co._writer.alive  # type: ignore[union-attr]
        store.close()
        assert not store.write_after_close
        with LiveBarStore(tmp_path / "s.db") as reopened:  # committed chunks are whole chunks
            assert reopened.count() % 500 in (0, DAY % 500)
            cp = reopened.get_checkpoint(CATCHUP, M, SYM)
            assert cp is not None
            assert cp.status in {"cancelled", "running"}

    asyncio.run(scenario())


def test_a_stuck_writer_is_reported_and_the_store_is_not_declared_safe_to_close(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        gate = threading.Event()
        store = _SlowStore(tmp_path / "s.db")
        store.gate = gate
        co = _coordinator(
            store,
            _cfg(catchup=None, history="off", write_timeout_s=0.05, write_join_timeout_s=0.2),
            _fetcher(),
        )
        try:
            co.start(END)
            await asyncio.wait_for(asyncio.to_thread(store.in_write.wait, 5), 6)
            assert await co.stop() is False
            assert co.phase == STUCK
            assert co.safe_to_close() is False
            status = co.status()
            assert status["safe_to_close_store"] is False
            assert status["writer"]["busy"] is True
            assert any("writer_stuck" in n for n in status["notes"])
        finally:
            gate.set()  # let the blocked "disk write" finish
            assert await co._writer.join(5)  # type: ignore[union-attr]
        assert co.safe_to_close()
        store.close()

    asyncio.run(scenario())


def test_status_is_json_ready_and_shows_progress(tmp_path: Path) -> None:
    import json

    async def scenario() -> None:
        with LiveBarStore(tmp_path / "s.db") as store:
            co = _coordinator(store, _cfg(), _fetcher())
            assert co.status()["phase"] == "idle"
            co.start(END)
            assert co.phase == RUNNING
            await co.wait_catchup(10)
            st = co.status()
            json.dumps(st)
            (pair,) = st["pairs"]
            assert (pair["job"], pair["state"], pair["market"]) == (CATCHUP, "complete", "spot")
            assert pair["chunks_done"] == pair["chunks_total"] == 1
            await co.stop()

    asyncio.run(scenario())
