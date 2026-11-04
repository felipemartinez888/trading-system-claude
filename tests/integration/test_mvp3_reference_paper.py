"""MVP-3: the REFERENCE / NOT VALIDATED `bracket_reference` adapter inside the paper engine and the
paper runtime. Synthetic bars and fake market data only; results are provisional (WHEA open)."""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from m1t2_fakes import make_bar
from test_mvp2_paper import KEYS, POLL, T0, Rig, run

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "strategies"))
from test_live_adapter import COSTS, PARAMS, TRADE_FROM, series

from tsclaude.api.paper import paper_router
from tsclaude.contracts.events import EventType
from tsclaude.contracts.market import Bar, BarSource, Market
from tsclaude.contracts.signals import SignalStatus
from tsclaude.contracts.timeutil import MS_PER_DAY, MS_PER_MINUTE, ManualClock
from tsclaude.marketdata.backfill import Backfiller, BackfillSettings
from tsclaude.marketdata.bars import BarClosed
from tsclaude.marketdata.config import load_config
from tsclaude.marketdata.types import RestKline
from tsclaude.quality.gaps import GapRegistry
from tsclaude.research.source import StrategySource
from tsclaude.runtime.paper_runtime import (
    DeferredReference,
    PaperSettings,
    PaperState,
    load_paper_runtime,
)
from tsclaude.signals.demo import DemoDetectorConfig
from tsclaude.sim.journal import MemoryJournal
from tsclaude.sim.paper import PaperConfig, PaperTradingService
from tsclaude.sim.testing import spot_spec, usdm_spec
from tsclaude.store.eventstore import EventStore
from tsclaude.store.livebars import LiveBarStore
from tsclaude.strategies import BracketReference
from tsclaude.strategies.live_adapter import ReferenceLiveStrategy


def _drive(svc: PaperTradingService, bars: list) -> None:  # type: ignore[type-arg]
    """Deliver each minute like the subscription, then poll shortly after its close."""
    by_minute: dict[int, list] = {}  # type: ignore[type-arg]
    for b in bars:
        by_minute.setdefault(b.open_time_ms, []).append(b)
    for m in sorted(by_minute):
        for b in by_minute[m]:
            svc.on_update(BarClosed(b))
        svc.poll(m + MS_PER_MINUTE + 1_500)


# ---------------------------------------------------------------------- inside the paper engine
def test_reference_intents_in_the_paper_engine_match_the_backtest_and_are_risk_gated() -> None:
    bars = series()
    adapter = ReferenceLiveStrategy(BracketReference(PARAMS), COSTS, trade_from_ms=TRADE_FROM)
    adapter.warm_up([b for b in bars if b.close_time_ms < TRADE_FROM])
    cfg = PaperConfig(instruments=(spot_spec(), usdm_spec()), starting_equity=Decimal(10_000))
    svc = PaperTradingService(cfg, MemoryJournal(), strategy=adapter)  # no snapshots: no view
    _drive(svc, [b for b in bars if b.close_time_ms >= TRADE_FROM - 2 * MS_PER_MINUTE])
    assert svc.fault is None

    # parity: the paper session decided exactly what the backtest adapter decided
    src = StrategySource(
        BracketReference(PARAMS), COSTS, trade_from_ms=TRADE_FROM, flat_from_ms=2**62
    )
    for b in bars:
        src.on_bar(b, None)  # type: ignore[arg-type]
    expected = [r.decision_ms for r in src.records if r.proposal.stage is SignalStatus.CONFIRMED]
    signals = svc.session.lifecycle.signals()
    assert expected
    assert sorted(s.record["decision_time_ms"] for s in signals) == expected

    # only CONFIRMED, all REFERENCE / NOT VALIDATED; WATCH proposals never reached the books
    assert adapter.stats.watch_proposals > 0
    assert all(s.record["stage"] == "CONFIRMED" for s in signals)
    assert all(s.record["market_conditions"]["label"] == "REFERENCE" for s in signals)

    # the risk engine stays active: without a market view every baseline entry is blocked,
    # while the ungated shadow book records the hypothetical outcome
    events = svc.session.sim.events
    rejected = [e for e in events if e["type"] == "sim_request_rejected"]
    assert len(rejected) == len(expected)
    assert all(e["aggregate_id"] == "baseline" for e in rejected)
    assert all("no_market_view" in e["payload"]["reason"] for e in rejected)
    submitted = [e for e in events if e["type"] == "sim_order_submitted"]
    assert sum(1 for e in submitted if e["aggregate_id"] == "shadow") == len(expected)
    assert not any(e["aggregate_id"] == "baseline" for e in submitted)
    # fills never before the decision: the earliest execution is the next 1m open
    decisions = {s.signal_id: s.record["decision_time_ms"] for s in signals}
    for e in events:
        if e["type"] == "sim_order_filled" and e["payload"].get("signal_id") in decisions:
            assert e["ts_ms"] > decisions[e["payload"]["signal_id"]]


# ---------------------------------------------------------------------- runtime selector
def test_strategy_none_is_the_default_and_unknown_strategies_fail_closed(tmp_path: Path) -> None:
    assert PaperSettings().strategy == "none"
    assert PaperSettings.load(Path("configs/paper.yaml")).strategy == "none"
    with pytest.raises(ValueError, match="not available"):
        PaperSettings(strategy="h1_breakout")
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "paper.yaml").write_text("enabled: true\nstrategy: h1_breakout\n", encoding="utf-8")
    rt = load_paper_runtime(
        cfg_dir, tmp_path, EventStore(tmp_path / "s.db"), Rig(tmp_path / "r").clock, None
    )
    assert rt.state is PaperState.UNAVAILABLE
    assert rt.enabled is False


def test_bracket_reference_is_warmed_up_causally_and_labelled_in_status(tmp_path: Path) -> None:
    async def scenario() -> Rig:
        rig = Rig(tmp_path)
        rig.runtime.settings = PaperSettings(
            enabled=True, poll_s=POLL, strategy="bracket_reference"
        )
        for i in range(120):  # two hours of closed history before the start
            rig.bars.add(i)
        rig.clock.set(T0 + 120 * MS_PER_MINUTE + 5_000)
        await rig.supervisor.start("S1")
        await rig.idle(T0 + 120 * MS_PER_MINUTE + 5_000)  # the first poll, 5 s into minute 120
        assert rig.runtime.state is PaperState.RUNNING
        observation = rig.runtime.observation
        assert observation is not None
        # P-1: minute 120 is only partly observed, so the run observes from minute 121
        assert observation["observing_from_ms"] == T0 + 121 * MS_PER_MINUTE
        assert rig.runtime.reference is None  # built at the first observed minute
        for i in (120, 121):  # stored before it is published, as the market-data service does
            rig.bars.add(i)
            await rig.minute(i)
        ref = rig.runtime.reference
        assert isinstance(ref, ReferenceLiveStrategy)
        assert ref.trade_from_ms == T0 + 121 * MS_PER_MINUTE  # the first observed minute
        # both instruments, minutes 0-120 (the unobserved minute 120 included), only bars that
        # closed before the first observed minute (121 is already stored, and not used)
        assert ref.stats.warmup_bars == 242
        # the toy history starts inside a 15m bucket: one warm-up break per instrument, as a
        # warm-up at minute 120 gives too; none in the live phase (no minute was lost)
        assert ref.history_breaks == 2
        assert ref.describe()["history_breaks"] == 2
        assert {b["phase"] for b in ref._breaks.values()} == {"warm-up"}
        assert ref.stats.skipped_not_newer == 0
        assert ref.stats.bars_seen == 2  # minute 121 only: minute 120 was unobserved
        batches = rig.journal()
        assert [b[0].payload["observed"] for b in batches] == [False, False, True, True]
        await rig.supervisor.close()
        return rig

    rig = run(scenario())
    snap, _ = rig.runtime.published()
    reference = snap["reference"]
    assert reference["label"] == "REFERENCE"
    assert reference["validation"] == "NOT_VALIDATED"
    assert reference["evidence_grade"] == "X"
    assert reference["confirmed_intents"] == 0  # far too little history for a decision
    app = FastAPI()
    app.include_router(paper_router(lambda: rig.runtime, rig.clock, []))
    status = TestClient(app).get("/api/paper/status").json()["status"]["value"]
    assert status["reference"]["label"] == "REFERENCE"


def test_demo_watch_signals_never_reach_the_reference_paper_books(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path, demo=DemoDetectorConfig(threshold=0.001))
        rig.runtime.settings = PaperSettings(
            enabled=True, poll_s=POLL, strategy="bracket_reference"
        )
        await rig.supervisor.start("S1")
        for i in range(3):
            await rig.minute(i)
        assert rig.store.read(types=[EventType.SIGNAL_CREATED])  # demo WATCH signals exist
        service = rig.runtime.service
        assert service is not None
        assert isinstance(service.session.source.strategy, DeferredReference)
        assert service.session.lifecycle.signals() == []
        await rig.supervisor.close()

    run(scenario())


def test_an_unobserved_minute_restarts_the_reference_warm_up() -> None:
    """Review B2 (disclosed limitation): the paper engine never shows unobserved bars (here a
    REST-recovered one) to a strategy, so the reference history breaks and the warm-up restarts."""
    from tsclaude.contracts.market import BarSource

    bars = series()
    adapter = ReferenceLiveStrategy(BracketReference(PARAMS), COSTS, trade_from_ms=TRADE_FROM)
    adapter.warm_up([b for b in bars if b.close_time_ms < TRADE_FROM])
    cfg = PaperConfig(instruments=(spot_spec(), usdm_spec()), starting_equity=Decimal(10_000))
    svc = PaperTradingService(cfg, MemoryJournal(), strategy=adapter)
    recovered_at = TRADE_FROM + 200 * MS_PER_MINUTE
    feed = [
        b.model_copy(update={"source": BarSource.REST_RECOVERED})
        if b.open_time_ms == recovered_at and b.symbol == "BTCUSDT" and str(b.market) == "usdm"
        else b
        for b in bars
        if b.close_time_ms >= TRADE_FROM - 2 * MS_PER_MINUTE
    ]
    before = adapter.history_breaks
    _drive(svc, [b for b in feed if b.open_time_ms <= recovered_at + 20 * MS_PER_MINUTE])
    assert adapter.history_breaks > before
    held = adapter.warmup_progress()["usdm:BTCUSDT"]
    assert held["ready"] is False
    assert held["strategy_bars"] <= 2  # restarted right after the unobserved minute


def test_reference_signal_prices_are_not_served_as_system_values(tmp_path: Path) -> None:
    """Audit N1: the reference block in status is SYSTEM metadata only; the signals (market prices)
    are served separately with simulated-value provenance."""

    async def scenario() -> Rig:
        rig = Rig(tmp_path)
        rig.runtime.settings = PaperSettings(
            enabled=True, poll_s=POLL, strategy="bracket_reference"
        )
        await rig.supervisor.start("S1")
        await rig.idle(T0)  # the first poll at minute 0's open: observing from minute 1
        await rig.minute(0)
        await rig.minute(1)  # the first observed minute builds the reference strategy
        assert rig.runtime.reference is not None
        await rig.supervisor.close()
        return rig

    rig = run(scenario())
    app = FastAPI()
    app.include_router(paper_router(lambda: rig.runtime, rig.clock, []))
    body = TestClient(app).get("/api/paper/status").json()
    assert body["status"]["provenance"] == "SYSTEM"
    assert "recent_signals" not in body["status"]["value"]["reference"]
    assert "warmup_progress" in body["status"]["value"]["reference"]
    assert body["reference_signals"]["provenance"] in {"SIMULATED", "STALE", "UNAVAILABLE"}


# ---------------------------------------------------------------------- option 1: 9-day history
def test_configured_startup_backfill_covers_the_reference_warm_up_and_paper_stays_off() -> None:
    cfg = load_config(Path("configs/universe.yaml"), Path("configs/data_quality.yaml"))
    backfill = cfg.quality.backfill
    strategy = BracketReference()
    # the runtime reads (window_bars + 2) bars plus the 15m bucket in progress at the start
    needed_days = (strategy.window_bars + 3) * strategy.interval.ms / MS_PER_DAY
    assert backfill.lookback_days == 9
    assert needed_days < backfill.lookback_days <= backfill.max_backfill_days
    # first-start budget stated in data_quality.yaml: <= 13 pages of 1000 per pair, ~78k rows
    assert backfill.lookback_days * 1440 <= 13 * 1000
    assert len(cfg.universe.pairs()) * backfill.lookback_days * 1440 <= 80_000
    settings = PaperSettings.load(Path("configs/paper.yaml"))
    assert settings.enabled is False
    assert settings.strategy == "none"


def _rest_kline(open_ms: int) -> RestKline:
    return RestKline(
        open_time_ms=open_ms, open=100.0, high=101.0, low=99.0, close=100.5, volume=10.0,
        close_time_ms=open_ms + MS_PER_MINUTE - 1, quote_volume=1005.0, trades=1,
        taker_buy_volume=5.0, taker_buy_quote_volume=502.5,
    )  # fmt: skip


def test_nine_day_startup_backfill_reaches_the_reference_warm_up(tmp_path: Path) -> None:
    """Option 1 end to end on synthetic data: the configured startup backfill (the real
    `Backfiller` into an in-memory live bar store) -> the paper runtime's warm-up read (the
    server's reader) -> a ready strategy at the first observed minute (P-1). Also: no historical
    paper trade, one ordered read per instrument without duplicates, the minute still owned by the
    live builder at open and the partly observed start minute arrive live, unobserved, and still
    reach the warm-up through the store without a break, and an unobserved live minute afterwards
    resets that instrument's readiness (disclosed limitation)."""
    q = load_config(Path("configs/universe.yaml"), Path("configs/data_quality.yaml")).quality
    settings = BackfillSettings(
        lookback_ms=int(q.backfill.lookback_days * MS_PER_DAY),
        max_backfill_ms=int(q.backfill.max_backfill_days * MS_PER_DAY),
    )
    start_minute = 9 * 1440  # Rig minute index of the paper start
    start = T0 + start_minute * MS_PER_MINUTE
    end = start - MS_PER_MINUTE  # market data started a minute earlier: start-1 is the builder's
    store = LiveBarStore(":memory:")
    events: list[Any] = []
    md_clock = ManualClock(end + 5_000)
    gaps = GapRegistry(events.append, md_clock.now_ms, new_id=lambda _t: "gap")

    async def fetch(market: Market, symbol: str, lo: int, hi: int) -> list[RestKline]:
        return [_rest_kline(t) for t in range(lo, hi, MS_PER_MINUTE)]

    backfiller = Backfiller(store, gaps, fetch, events.append, md_clock.now_ms, settings)
    for market, symbol in KEYS:
        result = run(backfiller.run(market, symbol, end, []))
        assert result.inserted_backfill == settings.lookback_ms // MS_PER_MINUTE
    calls: list[tuple[Market, str, int, int]] = []

    def reader(market: Market, symbol: str, lo: int, hi: int) -> list[Bar]:
        calls.append((market, symbol, lo, hi))
        return [sb.bar for sb in store.range(market, symbol, lo, hi)]  # as in api/server.py

    async def scenario() -> Rig:
        rig = Rig(tmp_path)
        rig.runtime._bars = reader
        rig.runtime.settings = PaperSettings(
            enabled=True, poll_s=POLL, strategy="bracket_reference"
        )

        async def live(i: int, usdm: BarSource = BarSource.WS_LIVE) -> None:
            """Minute `i` closes: the market-data service stores each bar, then publishes it."""
            rig.clock.set(T0 + (i + 1) * MS_PER_MINUTE + 1_500)
            for (m, s), source in zip(KEYS, (BarSource.WS_LIVE, usdm), strict=True):
                b = make_bar(m, s, T0 + i * MS_PER_MINUTE, source=source)
                store.write(b, "md" if source is BarSource.WS_LIVE else None)
                rig.md.push_bar(b)
            await asyncio.sleep(POLL * 6)

        rig.clock.set(start + 1_000)
        await rig.supervisor.start("S1")
        await rig.idle(start + 1_000)  # the first poll, 1 s into the start minute
        assert rig.runtime.state is PaperState.RUNNING, rig.runtime.reason
        first = start + MS_PER_MINUTE  # the start minute is only partly observed (P-1)
        observation = rig.runtime.observation
        assert observation is not None
        assert observation["observing_from_ms"] == first
        assert rig.runtime.reference is None  # built at the first observed minute
        service = rig.runtime.service
        assert service is not None

        await live(start_minute - 1)  # the builder's minute arrives live (ws_live), unobserved
        await live(start_minute)  # the partly observed start minute: unobserved
        assert rig.runtime.reference is None
        await live(start_minute + 1, usdm=BarSource.REST_RECOVERED)  # the first observed minute
        ref = rig.runtime.reference
        assert isinstance(ref, ReferenceLiveStrategy)
        assert ref.trade_from_ms == first
        interval = ref.strategy.interval.ms
        begin = first - first % interval - (ref.strategy.window_bars + 2) * interval
        assert begin >= end - settings.lookback_ms  # the read lies inside the backfilled window
        assert [c for c in calls if c[2] == begin] == [(m, s, begin, first) for m, s in KEYS]
        stored = (first - begin) // MS_PER_MINUTE  # [begin, first): the two live minutes too
        assert ref.stats.warmup_bars == 2 * stored
        assert ref.stats.skipped_not_newer == 0
        assert ref.history_breaks == 0
        assert all(p["ready"] for p in ref.warmup_progress().values()), ref.warmup_progress()
        assert service.session.lifecycle.signals() == []  # warm-up never trades
        assert not any(  # the benchmark buys at the first observed bar by design
            e["type"] == "sim_order_submitted" and e["aggregate_id"] != "benchmark"
            for e in service.session.sim.events
        )

        for i in range(start_minute + 2, start_minute + 20):
            await live(i)
        assert ref.history_breaks == 1  # usdm missed its REST-recovered first observed minute
        progress = ref.warmup_progress()
        assert progress["spot:BTCUSDT"]["ready"] is True
        assert progress["usdm:BTCUSDT"]["ready"] is False
        decided = [s.record["decision_time_ms"] for s in service.session.lifecycle.signals()]
        assert all(d >= first for d in decided)  # nothing decided before the first observed one
        await rig.supervisor.close()
        return rig

    run(scenario())
