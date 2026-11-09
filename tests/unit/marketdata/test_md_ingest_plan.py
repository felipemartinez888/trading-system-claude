"""`backfill-plan`: read-only, no network, mirrors what the engine would fetch."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from md_helpers import MIN, T0, FakeFetcher, Sink, rest_kline

from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.marketdata.backfill import Backfiller, BackfillSettings, RunLimits
from tsclaude.marketdata.config import BackfillConfig, BackfillRunConfig, BackfillStartupConfig
from tsclaude.marketdata.ingest_plan import plan
from tsclaude.quality.gaps import GapRegistry
from tsclaude.store.livebars import LiveBarStore

END = T0 + 1440 * MIN
CFG = BackfillConfig(
    lookback_days=1.0,
    startup=BackfillStartupConfig(catchup_max_minutes=60, history="on_demand"),
    run=BackfillRunConfig(chunk_minutes=100),
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_plan_of_a_missing_database_lists_everything_as_missing(tmp_path: Path) -> None:
    out = plan(tmp_path / "none.db", CFG, [("spot", "BTCUSDT")], END)
    assert out["database_exists"] is False
    assert not (tmp_path / "none.db").exists()  # planning never creates the database
    (pair,) = out["pairs"]
    assert pair["catchup"] == {
        "window_minutes": 60,
        "stored_minutes": 0,
        "missing_minutes": 60,
        "chunks": 1,
    }
    assert pair["history"]["missing_minutes"] == 1440
    assert pair["history"]["chunks"] == 15


def test_plan_counts_stored_bars_and_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    fetch = FakeFetcher()
    for i in range(1500):
        fetch.put(Market.SPOT, "BTCUSDT", rest_kline(T0 + i * MIN))
    sink, clock = Sink(), ManualClock(T0)
    with LiveBarStore(path) as store:
        gaps = GapRegistry(sink, clock.now_ms, new_id=lambda _t: "g")
        small = BackfillSettings(lookback_ms=30 * MIN, extend_to_last_stored=False)
        bf = Backfiller(
            store, gaps, fetch, sink, clock.now_ms, small, limits=RunLimits(chunk_minutes=100)
        )
        asyncio.run(bf.run(Market.SPOT, "BTCUSDT", END, []))
    before = _digest(path)
    out = plan(path, CFG, [("spot", "BTCUSDT")], END)
    (pair,) = out["pairs"]
    assert pair["catchup"]["stored_minutes"] == 30
    assert pair["catchup"]["missing_minutes"] == 30
    assert pair["history"]["stored_minutes"] == 30
    assert pair["history"]["missing_minutes"] == 1410
    assert out["history_policy"] == "on_demand"
    assert out["limits"]["chunk_minutes"] == 100
    assert _digest(path) == before  # byte-identical: strictly read-only
