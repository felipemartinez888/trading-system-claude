"""`/api/ingestion` view model (no FastAPI needed): idle default, real coordinator status."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from tsclaude.api.market_views import ingestion_response
from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.marketdata.config import BackfillConfig, BackfillRunConfig, BackfillStartupConfig
from tsclaude.marketdata.ingest import IngestionCoordinator
from tsclaude.marketdata.types import RestKline
from tsclaude.quality.gaps import GapRegistry
from tsclaude.store.livebars import LiveBarStore

T0 = 1_792_411_200_000
MIN = 60_000


class _Supervisor:
    def __init__(self, status: dict) -> None:  # type: ignore[type-arg]
        self._status, self.running = status, True

    def ingestion_status(self) -> dict:  # type: ignore[type-arg]
        return self._status


def test_without_a_supervisor_or_service_the_view_is_idle_and_safe() -> None:
    out = ingestion_response(None, T0)
    assert (out.phase, out.pairs, out.safe_to_close_store) == ("idle", [], True)
    assert out.monitoring_running is False


def test_view_of_a_real_coordinator_status(tmp_path: Path) -> None:
    async def scenario() -> dict:  # type: ignore[type-arg]
        clock = ManualClock(T0)
        events: list = []  # type: ignore[type-arg]
        gaps = GapRegistry(events.append, clock.now_ms, new_id=lambda _t: "g")

        async def fetch(m: Market, s: str, lo: int, hi: int) -> list[RestKline]:
            from md_helpers import rest_kline  # type: ignore[import-not-found]

            return [rest_kline(t) for t in range(lo, hi, MIN)]

        cfg = BackfillConfig(
            lookback_days=1.0,
            startup=BackfillStartupConfig(catchup_max_minutes=30, history="on_demand"),
            run=BackfillRunConfig(chunk_minutes=10, inter_chunk_pause_ms=0),
        )
        with LiveBarStore(tmp_path / "s.db") as store:
            co = IngestionCoordinator(
                bars=store,
                gaps=gaps,
                fetch=fetch,
                emit=events.append,
                now_ms=clock.now_ms,
                config=cfg,
                pairs=lambda: [(Market.SPOT, "BTCUSDT")],
                intervals=lambda: [],
            )
            co.start(T0 + 1440 * MIN)
            await co.wait_catchup(10)
            status = co.status()
            await co.stop()
        return status

    status = asyncio.run(scenario())
    out = ingestion_response(_Supervisor(status), T0)  # type: ignore[arg-type]
    json.loads(out.model_dump_json())
    (pair,) = out.pairs
    assert (pair.job, pair.state, pair.chunks_done, pair.chunks_total) == (
        "catchup",
        "complete",
        3,
        3,
    )
    assert out.history.policy == "on_demand"
    assert out.history.complete is False
    assert out.limits.chunk_minutes == 10
