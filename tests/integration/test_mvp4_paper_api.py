"""MVP-4 backend additions (read-only): closed trades and request-level rejections (B-1, B-2) and
per-instrument history-break details of the reference warm-up (B-3). Synthetic bars only."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_mvp2_paper import Rig
from test_mvp3_reference_paper import _drive

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "strategies"))
from test_live_adapter import COSTS, PARAMS, TRADE_FROM, series

from tsclaude.api.paper import paper_router
from tsclaude.contracts.timeutil import MS_PER_MINUTE, ManualClock
from tsclaude.runtime.paper_runtime import PaperState
from tsclaude.sim.journal import MemoryJournal
from tsclaude.sim.paper import PaperConfig, PaperTradingService
from tsclaude.sim.testing import spot_spec, usdm_spec
from tsclaude.strategies import BracketReference
from tsclaude.strategies.live_adapter import ReferenceLiveStrategy


def _client(rig: Rig | None) -> TestClient:
    app = FastAPI()
    clock = rig.clock if rig is not None else ManualClock(0)
    app.include_router(paper_router(lambda: None if rig is None else rig.runtime, clock, []))
    return TestClient(app)


def _rig_with_reference_session(tmp_path: Path) -> tuple[Rig, ReferenceLiveStrategy, int]:
    """A runtime holding a paper session driven by the reference adapter over synthetic bars
    (no risk view: baseline entries are risk-rejected, the ungated shadow book trades)."""
    bars = series()
    adapter = ReferenceLiveStrategy(BracketReference(PARAMS), COSTS, trade_from_ms=TRADE_FROM)
    adapter.warm_up([b for b in bars if b.close_time_ms < TRADE_FROM])
    cfg = PaperConfig(instruments=(spot_spec(), usdm_spec()), starting_equity=Decimal(10_000))
    svc = PaperTradingService(cfg, MemoryJournal(), strategy=adapter)
    _drive(svc, [b for b in bars if b.close_time_ms >= TRADE_FROM - 2 * MS_PER_MINUTE])
    rig = Rig(tmp_path)
    runtime = rig.runtime
    runtime.service = svc
    runtime.reference = adapter
    runtime.state = PaperState.RUNNING
    runtime._released_at_open = 0
    as_of = max(b.close_time_ms for b in bars)
    rig.clock = ManualClock(as_of + 1_000)  # the synthetic series is older than the Rig's T0
    runtime._clock = rig.clock
    runtime._publish()
    return rig, adapter, as_of


def test_activity_is_unavailable_without_a_paper_runtime() -> None:
    body = _client(None).get("/api/paper/activity").json()
    assert body["activity"]["provenance"] == "UNAVAILABLE"
    assert body["activity"]["value"] is None


def test_activity_serves_recorded_trades_and_request_rejections(tmp_path: Path) -> None:
    rig, _adapter, as_of = _rig_with_reference_session(tmp_path)
    client = _client(rig)
    activity: dict[str, Any] = client.get("/api/paper/activity").json()["activity"]
    assert activity["provenance"] == "SIMULATED"
    trades = activity["value"]["trades"]
    books = rig.runtime.service.session.sim.books  # type: ignore[union-attr]
    assert len(trades) == sum(len(b.trades) for b in books.values()) > 0  # not vacuous
    assert {t["book"] for t in trades} == {"shadow"}  # baseline was risk-gated, never filled
    first = trades[0]
    for field in ("entry_price", "exit_price", "net_pnl", "r_multiple", "exit_reason", "qty"):
        assert field in first
    assert isinstance(first["entry_price"], str)  # Decimals stay exact strings
    assert [t["exit_bar_open_ms"] for t in trades] == sorted(t["exit_bar_open_ms"] for t in trades)
    rejections = activity["value"]["request_rejections"]
    assert rejections
    assert {r["aggregate_id"] for r in rejections} == {"baseline"}
    assert all("no_market_view" in r["reason"] for r in rejections)
    assert len(client.get("/api/paper/activity?limit=2").json()["activity"]["value"]["trades"]) == 2

    rig.clock.set(as_of + 10 * MS_PER_MINUTE)  # no newer bar processed: last known values
    stale = client.get("/api/paper/activity").json()["activity"]
    assert stale["provenance"] == "STALE"
    assert stale["as_of_ms"] == as_of
    assert "ago" in stale["reason"]


def test_status_reports_per_instrument_history_breaks(tmp_path: Path) -> None:
    rig, _adapter, _as_of = _rig_with_reference_session(tmp_path)
    status = _client(rig).get("/api/paper/status").json()["status"]
    assert status["provenance"] == "SYSTEM"
    progress = status["value"]["reference"]["warmup_progress"]
    spot, usdm = progress["spot:BTCUSDT"], progress["usdm:BTCUSDT"]
    assert spot["history_breaks"] == 1  # the synthetic gap in spot minutes 1500-1503
    assert spot["last_break_phase"] == "live"
    assert spot["last_break_detected_at_ms"] is not None
    assert "incomplete 15m bucket" in spot["last_break_reason"]
    assert usdm["history_breaks"] == 0
    assert usdm["last_break_reason"] is None
