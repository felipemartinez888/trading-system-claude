"""MarketDataService <-> IngestionCoordinator glue that needs no network: the history gate fails
closed, Stop joins the writer first, and a stuck writer keeps the SQLite connection open."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from md_helpers import MIN, T0, FakeFetcher, rest_kline

from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.marketdata.config import MarketDataConfig
from tsclaude.marketdata.ingest import HistoryIncomplete, IngestionCoordinator
from tsclaude.marketdata.service import MarketDataService
from tsclaude.store.eventstore import EventStore

M, SYM = Market.SPOT, "BTCUSDT"
END = T0 + 1440 * MIN


def _config(history: str = "on_demand", catchup: int | None = 60, **run: Any) -> MarketDataConfig:
    return MarketDataConfig.from_mapping(
        {"version": 0, "snapshot_date": "2026-10-19", "rule": "t", "spot": [SYM], "usdm": []},
        {
            "backfill": {
                "lookback_days": 1.0,
                "max_backfill_days": 2,
                "startup": {"catchup_max_minutes": catchup, "history": history},
                "run": {"chunk_minutes": 500, "inter_chunk_pause_ms": 0, **run},
            }
        },
    )


def _service(tmp_path: Path, cfg: MarketDataConfig) -> tuple[MarketDataService, EventStore]:
    store = EventStore(tmp_path / "state.db")
    svc = MarketDataService(cfg, store, ManualClock(T0))
    rows = FakeFetcher()
    for i in range(1450):
        rows.put(M, SYM, rest_kline(T0 + i * MIN))
    svc._fetch_klines = rows  # type: ignore[method-assign]  # no network: an in-memory source
    svc._ingest = IngestionCoordinator(
        bars=svc._bars,
        gaps=svc.gaps,
        fetch=rows,
        emit=svc._emit,
        now_ms=svc._clock.now_ms,
        config=cfg.quality.backfill,
        pairs=lambda: list(cfg.universe.pairs()),
        intervals=lambda: [],
    )
    svc._running = True
    return svc, store


def test_a_consumer_that_requires_history_waits_for_the_whole_window(tmp_path: Path) -> None:
    async def scenario() -> None:
        svc, store = _service(tmp_path, _config())
        assert svc._ingest is not None
        svc.require_history()
        svc._ingest.start(END)
        await svc.wait_backfill(10)
        assert svc._ingest.history_complete()
        assert svc._bars.count() == 1440
        assert len(svc.backfill_results) == 2  # one catch-up and one history result
        assert svc.ingestion_status()["history_required"] is True
        await svc.stop()
        svc.close()
        store.close()

    asyncio.run(scenario())


def test_without_a_requirement_wait_backfill_returns_after_the_catchup_only(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        svc, store = _service(tmp_path, _config())
        assert svc._ingest is not None
        svc._ingest.start(END)
        await svc.wait_backfill(10)
        assert svc._bars.count() == 60  # bounded: history was not loaded
        assert svc.ingestion_status()["history"]["state"] == "not_requested"
        await svc.stop()
        svc.close()
        store.close()

    asyncio.run(scenario())


def test_required_history_that_cannot_be_loaded_fails_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        svc, store = _service(tmp_path, _config(history="off"))
        assert svc._ingest is not None
        svc.require_history()
        svc._ingest.start(END)
        with pytest.raises(HistoryIncomplete):
            await svc.wait_backfill(10)  # policy off: the warm-up window can never be complete
        await svc.stop()
        svc.close()
        store.close()

    asyncio.run(scenario())


def test_a_history_run_cut_short_by_stop_does_not_count_as_complete(tmp_path: Path) -> None:
    async def scenario() -> None:
        svc, store = _service(tmp_path, _config())
        assert svc._ingest is not None
        svc.require_history()
        svc._ingest.start(END)
        waiter = asyncio.create_task(svc.wait_backfill(30))
        await svc._ingest.wait_catchup(10)
        await svc.stop()  # Stop while the history is (possibly) still loading
        if svc._ingest.history_complete():
            await waiter  # it finished before Stop: that is a legitimate complete window
        else:
            with pytest.raises(HistoryIncomplete):
                await waiter
        svc.close()
        store.close()

    asyncio.run(scenario())


def test_stop_leaves_a_stuck_writers_connection_open_and_close_does_not_close_it(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        cfg = _config(catchup=None, history="off", write_timeout_s=0.05, write_join_timeout_s=0.2)
        svc, store = _service(tmp_path, cfg)
        assert svc._ingest is not None
        gate, entered = threading.Event(), threading.Event()
        real = svc._bars.write_many

        def blocked(*a: Any, **kw: Any) -> Any:
            entered.set()
            gate.wait(30)
            return real(*a, **kw)

        svc._bars.write_many = blocked  # type: ignore[method-assign]
        try:
            svc._ingest.start(END)
            await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), 6)
            await svc.stop()
            assert svc._store_left_open
            svc.close()  # must not close the connection under the running transaction
            assert not svc._bars.closed
        finally:
            gate.set()
            assert await svc._ingest._writer.join(5)  # type: ignore[union-attr]
        svc._bars.close()
        store.close()

    asyncio.run(scenario())
