"""MVP-2 paper runtime wired into the monitoring supervisor (fake market data, no network).

P1-P6, P8, P9 of the MVP-2 brief, plus backfill validation and the new-session audit. Every test is
small (a few simulated minutes); results are provisional while the WHEA investigation is open.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from m1t2_fakes import FakeFactory, FakeMarketData, FakeSubscription, make_bar

from tsclaude.contracts.events import EventType
from tsclaude.contracts.market import Bar, Interval, Market
from tsclaude.contracts.timeutil import MS_PER_MINUTE, ManualClock
from tsclaude.runtime.paper_runtime import (
    AUDIT_FILE,
    PaperActionRefused,
    PaperRuntime,
    PaperSettings,
    PaperState,
    validate_backfill,
)
from tsclaude.runtime.supervisor import MonitoringSupervisor
from tsclaude.signals.demo import DemoDetectorConfig
from tsclaude.sim.journal import EventStoreJournal
from tsclaude.sim.paper import PaperConfig
from tsclaude.sim.testing import spot_spec, usdm_spec
from tsclaude.store.eventstore import EventStore

T0 = 1_760_000_040_000 - 1_760_000_040_000 % MS_PER_MINUTE
KEYS = [(Market.SPOT, "BTCUSDT"), (Market.USDM, "BTCUSDT")]
POLL = 0.01


def paper_config(equity: str = "10000") -> PaperConfig:
    return PaperConfig(instruments=(spot_spec(), usdm_spec()), starting_equity=Decimal(equity))


class Bars:
    """In-memory stand-in for the live bar store (`BarReader`)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[Market, str], list[Bar]] = {}

    def add(self, minute: int, keys: Sequence[tuple[Market, str]] = KEYS) -> None:
        for m, s in keys:
            self.rows.setdefault((m, s), []).append(make_bar(m, s, T0 + minute * MS_PER_MINUTE))

    def __call__(self, market: Market, symbol: str, start: int, end: int) -> list[Bar]:
        return [b for b in self.rows.get((market, symbol), []) if start <= b.open_time_ms < end]


class GatedFactory(FakeFactory):
    """Fake market data whose startup backfill finishes only when the test releases it, like the
    real service's background `md-backfill` task (`wait_backfill`)."""

    def __init__(self) -> None:
        super().__init__()
        self.done = asyncio.Event()

    def __call__(self, store: EventStore, clock: Any) -> Any:
        instance = super().__call__(store, clock)

        async def wait_backfill(timeout: float | None = None) -> None:
            await asyncio.wait_for(self.done.wait(), timeout)

        instance.wait_backfill = wait_backfill
        return instance


class Rig:
    def __init__(self, tmp_path: Path, *, enabled: bool = True, **kw: Any) -> None:
        self.tmp = tmp_path
        self.store = EventStore(tmp_path / "state.db")
        self.clock = ManualClock(T0)
        self.factory = kw.pop("factory", None) or FakeFactory()
        self.bars = Bars()
        self.problems: list[tuple[str, str]] = []
        self.config_factory = kw.pop("config_factory", paper_config)
        self.runtime = self._runtime(enabled)
        self.supervisor = MonitoringSupervisor(
            self.store,
            self.clock,
            service_factory=self.factory,
            demo=kw.pop("demo", DemoDetectorConfig(enabled=False)),
            on_problem=lambda t, d: self.problems.append((t, d)),
            paper=self.runtime,
        )

    def _runtime(self, enabled: bool) -> PaperRuntime:
        return PaperRuntime(
            PaperSettings(enabled=enabled, poll_s=POLL),
            self.store,
            self.clock,
            config_factory=lambda: self.config_factory(),
            audit_path=self.tmp / AUDIT_FILE,
            bars=self.bars,
        )

    @property
    def md(self) -> FakeMarketData:
        assert self.factory.instance is not None
        return self.factory.instance

    def paper_sub(self) -> FakeSubscription:
        sub = self.runtime._sub
        assert isinstance(sub, FakeSubscription)
        return sub

    def journal(self, session_id: str = "paper") -> list[Any]:
        return EventStoreJournal(self.store, session_id).batches()

    async def minute(self, i: int, keys: Sequence[tuple[Market, str]] = KEYS) -> None:
        """Deliver minute `i` like the live feed, then let the paper consumer poll."""
        self.clock.set(T0 + (i + 1) * MS_PER_MINUTE + 1_500)
        for m, s in keys:
            self.md.push_bar(make_bar(m, s, T0 + i * MS_PER_MINUTE))
        await asyncio.sleep(POLL * 6)

    async def idle(self, now_ms: int) -> None:
        self.clock.set(now_ms)
        await asyncio.sleep(POLL * 6)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------- P1, P2
def test_p1_paper_runs_with_monitoring_and_stops_before_market_data(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        assert rig.md.subscribed_total == 2  # demo detector + paper: independent subscriptions
        assert rig.runtime.state is PaperState.RUNNING
        for i in range(3):
            await rig.minute(i)
        assert len(rig.journal()) == 6  # one batch per released bar, in clock order
        snap, _ = rig.runtime.published()
        assert snap["state"] == "RUNNING"
        assert snap["observing_since_ms"] is not None
        assert set(snap["books"]) == {"baseline", "shadow", "benchmark"}
        assert all(b["ledger_reconciled"] for b in snap["books"].values())
        sub = rig.paper_sub()
        await rig.supervisor.stop()
        assert sub.closed
        assert rig.runtime._task is None
        assert rig.runtime.state is PaperState.IDLE
        assert rig.md.stop_calls == 1
        await rig.supervisor.close()

    run(scenario())


def test_p2_paper_is_off_by_default(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path, enabled=False)
        await rig.supervisor.start("S1")
        assert rig.md.subscribed_total == 1  # only the demo detector's
        await rig.minute(0)
        assert rig.runtime.state is PaperState.DISABLED
        assert rig.store.read(types=[EventType.PAPER_STEP]) == []
        await rig.supervisor.close()

    run(scenario())
    assert PaperSettings.load(Path("configs/paper.yaml")).enabled is False  # shipped default
    assert PaperSettings.load(tmp_path / "missing.yaml").enabled is False


# ---------------------------------------------------------------------- P3
def test_p3_open_failure_is_unavailable_and_monitoring_keeps_running(tmp_path: Path) -> None:
    def broken() -> PaperConfig:
        raise ValueError("no instrument spec for spot:BTCUSDT (fail closed)")

    async def scenario() -> None:
        rig = Rig(tmp_path, config_factory=broken)
        await rig.supervisor.start("S1")
        assert rig.supervisor.running
        assert rig.runtime.state is PaperState.UNAVAILABLE
        assert "no instrument spec" in str(rig.runtime.reason)
        assert any("unavailable" in t.lower() for t, _ in rig.problems)
        assert rig.md.subscribed_total == 2
        assert sum(1 for s in rig.md.subscriptions) == 1  # the paper subscription was closed
        await rig.supervisor.close()

    run(scenario())


def test_p3_restore_with_a_different_configuration_is_unavailable(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        await rig.minute(0)
        await rig.supervisor.stop()
        rig.config_factory = lambda: paper_config("5000")  # recorded decisions would differ
        await rig.supervisor.start("S2")
        assert rig.runtime.state is PaperState.UNAVAILABLE
        assert "RestoreError" in str(rig.runtime.reason)
        assert len(rig.journal()) == 2  # the old journal is untouched
        await rig.supervisor.close()

    run(scenario())


# ---------------------------------------------------------------------- P4, P5
async def _restart_after_gap(rig: Rig, last: int, resume: int) -> None:
    await rig.supervisor.start("S1")
    for i in range(last + 1):
        await rig.minute(i)
    await rig.supervisor.stop()
    await rig.supervisor.start("S2")
    await rig.minute(resume)
    await rig.idle(T0 + (resume + 1) * MS_PER_MINUTE + 3_000)


def test_p4_complete_backfill_from_live_bars_resumes(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path)
        for i in (3, 4, 5):
            rig.bars.add(i)
        await _restart_after_gap(rig, last=2, resume=6)
        backfill = rig.runtime.backfill
        assert backfill is not None
        assert backfill["ok"] is True, backfill
        assert backfill["processed"] == 6
        assert rig.runtime.state is PaperState.RUNNING
        batches = rig.journal()
        observed = [b[0].payload["observed"] for b in batches]
        assert observed.count(False) == 6  # minutes 3-5 resolved unobserved (no entries)
        assert len(batches) == 6 + 6 + 2
        await rig.supervisor.close()

    run(scenario())


def test_p4_incomplete_backfill_stays_awaiting_and_processes_nothing(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path)
        rig.bars.add(3)
        rig.bars.add(5)  # minute 4 is missing for every instrument
        await _restart_after_gap(rig, last=2, resume=6)
        assert rig.runtime.state is PaperState.AWAITING_BACKFILL
        backfill = rig.runtime.backfill
        assert backfill is not None
        assert backfill["ok"] is False
        assert backfill["processed"] == 0
        assert any("missing" in p for p in backfill["problems"])
        assert len(rig.journal()) == 6  # nothing processed after the restart
        snap, _ = rig.runtime.published()
        assert snap["service"]["awaiting_missing"]
        await rig.supervisor.close()

    run(scenario())


def test_p5_never_delivered_bar_stays_awaiting_and_is_never_auto_resolved(tmp_path: Path) -> None:
    """The operational form of the open restore class (seed 13073: a bar after the journal's last
    key that never arrives): the service waits for a backfill that cannot come, shows it, never
    resumes by itself, and the only way out is an explicit new session."""

    async def scenario() -> None:
        rig = Rig(tmp_path)
        rig.bars.add(3, keys=[(Market.USDM, "BTCUSDT")])  # spot BTC minute 3 never existed
        await _restart_after_gap(rig, last=2, resume=4)
        for k in range(5):  # time passes; still no silent resume
            await rig.idle(T0 + (6 + k) * MS_PER_MINUTE)
        assert rig.runtime.state is PaperState.AWAITING_BACKFILL
        assert len(rig.journal()) == 6
        awaiting = rig.runtime.published()[0]["service"]["awaiting_missing"]
        assert T0 + 3 * MS_PER_MINUTE in awaiting["spot:BTCUSDT"]
        record = rig.runtime.request_new_session("restart ambiguity, start clean", "req-0001")
        assert record["created"] is True
        assert record["previous_session_id"] == "paper"
        assert len(rig.journal()) == 6  # the old session is untouched
        await rig.supervisor.stop()
        await rig.supervisor.start("S3")  # the recorded session opens at the next start
        assert rig.runtime.session_id == record["new_session_id"]
        assert rig.runtime.state is PaperState.RUNNING
        await rig.minute(10)
        assert len(rig.journal(record["new_session_id"])) == 2
        assert len(rig.journal()) == 6
        assert rig.store.verify().ok
        await rig.supervisor.close()

    run(scenario())


# ---------------------------------------------------------------------- P6, P9
def test_p6_dropped_update_stops_paper_decisions(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        await rig.minute(0)
        rig.paper_sub().dropped = 1  # type: ignore[attr-defined]
        await rig.minute(1)
        await rig.minute(2)
        assert rig.runtime.state is PaperState.STOPPED_OVERFLOW
        assert rig.runtime.dropped_updates == 1
        assert len(rig.journal()) == 2  # nothing after the drop was decided
        assert rig.supervisor.running  # monitoring itself continues
        assert any("STOPPED_OVERFLOW" in t for t, _ in rig.problems)
        await rig.supervisor.close()

    run(scenario())


def test_p6_lost_subscription_stops_paper_decisions(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        await rig.minute(0)
        rig.paper_sub().close()  # closed while market data keeps running
        await rig.minute(1)
        assert rig.runtime.state is PaperState.SUBSCRIPTION_LOST
        assert len(rig.journal()) == 2
        await rig.supervisor.close()

    run(scenario())


def test_p6_silent_subscription_stops_paper_decisions(tmp_path: Path) -> None:
    """The real subscription returns None both on timeout and when closed: prolonged silence while
    market data runs is treated as a lost subscription."""

    async def scenario() -> None:
        rig = Rig(tmp_path)
        rig.runtime.settings = PaperSettings(enabled=True, poll_s=POLL, silence_s=POLL * 5)
        await rig.supervisor.start("S1")
        await asyncio.sleep(POLL * 30)  # no update at all
        assert rig.runtime.state is PaperState.SUBSCRIPTION_LOST
        assert "no market-data update" in str(rig.runtime.reason)
        await rig.supervisor.close()

    run(scenario())


def test_p9_consumer_exception_stops_paper_and_is_reported(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        await rig.minute(0)
        rig.paper_sub().queue.put_nowait(RuntimeError("subscription broken"))
        await asyncio.sleep(POLL * 6)
        assert rig.runtime.state is PaperState.CONSUMER_DIED
        assert "subscription broken" in str(rig.runtime.reason)
        assert any("CONSUMER_DIED" in t for t, _ in rig.problems)
        await rig.minute(1)
        assert len(rig.journal()) == 2
        await rig.supervisor.close()

    run(scenario())


# ---------------------------------------------------------------------- P8
def test_p8_demo_watch_signals_never_reach_the_paper_books(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path, demo=DemoDetectorConfig(threshold=0.001))
        await rig.supervisor.start("S1")
        for i in range(3):
            await rig.minute(i)  # each bar moves 0.5%: the demo detector emits WATCH
        assert rig.store.read(types=[EventType.SIGNAL_CREATED])  # demo WATCH signals exist
        service = rig.runtime.service
        assert service is not None
        assert service.session.source.strategy is None
        assert service.session.lifecycle.signals() == []
        submitted = [
            e
            for e in service.session.sim.events
            if e["type"] == "sim_order_submitted" and e["payload"].get("book") != "benchmark"
        ]
        assert submitted == []  # baseline and shadow never trade without a strategy
        await rig.supervisor.close()

    run(scenario())


# ---------------------------------------------------------------------- validation, audit
def _bar(m: Market, s: str, minute: int, **update: Any) -> Bar:
    b = make_bar(m, s, T0 + minute * MS_PER_MINUTE)
    return b.model_copy(update=update) if update else b


def test_backfill_validation_rejects_identity_timeframe_time_and_duplicates() -> None:
    spot = (Market.SPOT, "BTCUSDT")
    end = T0 + 10 * MS_PER_MINUTE
    good = validate_backfill(
        {spot: [_bar(*spot, 2), _bar(*spot, 1)]}, start_ms=T0, end_ms=end, now_ms=end
    )
    assert good.problems == []
    assert [b.open_time_ms for b in good.bars] == [T0 + MS_PER_MINUTE, T0 + 2 * MS_PER_MINUTE]
    cases = {
        "identity": [_bar(Market.USDM, "BTCUSDT", 1)],
        "interval": [_bar(*spot, 1, interval=Interval.M15)],
        "timestamps": [_bar(*spot, 1, close_time_ms=T0 + 2 * MS_PER_MINUTE)],
        "outside": [_bar(*spot, 12)],
        "not closed": [_bar(*spot, 9)],
        "duplicate": [_bar(*spot, 1), _bar(*spot, 1)],
    }
    for label, bars in cases.items():
        now = end - 1 if label == "not closed" else end
        check = validate_backfill({spot: bars}, start_ms=T0, end_ms=end, now_ms=now)
        assert check.problems, label


def test_new_session_is_authenticated_idempotent_and_never_reuses_a_session(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        await rig.minute(0)
        with pytest.raises(PaperActionRefused) as running:
            rig.runtime.request_new_session("while running", "req-0002")
        assert running.value.code == 409
        await rig.supervisor.stop()
        with pytest.raises(PaperActionRefused) as empty:
            rig.runtime.request_new_session("   ", "req-0003")
        assert empty.value.code == 422
        first = rig.runtime.request_new_session("clean start", "req-0004")
        again = rig.runtime.request_new_session("clean start", "req-0004")
        assert first["created"]
        assert not again["created"]
        assert again["new_session_id"] == first["new_session_id"]
        with pytest.raises(PaperActionRefused) as reused:
            rig.runtime.request_new_session("another note", "req-0004")
        assert reused.value.code == 409
        second = rig.runtime.request_new_session("again", "req-0005")
        assert second["new_session_id"] != first["new_session_id"]
        assert second["previous_session_id"] == first["new_session_id"]
        lines = (tmp_path / AUDIT_FILE).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2  # idempotent replay wrote nothing
        assert len(rig.journal()) == 2  # the original session is untouched
        await rig.supervisor.close()

    run(scenario())


# ---------------------------------------------------------------------- market-data backfill gate
def test_paper_waits_for_the_market_data_backfill_before_opening(tmp_path: Path) -> None:
    """Review B1: paper must not open, seed its risk tracker or attempt its own backfill while the
    market-data startup backfill is still filling the bar store."""

    async def scenario() -> None:
        gate = GatedFactory()
        rig = Rig(tmp_path, factory=gate)
        gate.done.set()  # first run: nothing to backfill
        await rig.supervisor.start("S1")
        await asyncio.sleep(POLL * 6)
        for i in range(3):
            await rig.minute(i)
        await rig.supervisor.stop()
        gate.done = asyncio.Event()  # the restart's backfill is still running
        await rig.supervisor.start("S2")
        assert rig.runtime.state is PaperState.STARTING
        assert "backfill" in str(rig.runtime.reason)
        await rig.minute(6)  # a live bar arrives while waiting: queued, not decided
        assert rig.runtime.service is None
        assert len(rig.journal()) == 6
        for i in (3, 4, 5):  # the market-data backfill fills the bar store, then completes
            rig.bars.add(i)
        gate.done.set()
        await asyncio.sleep(POLL * 10)
        assert rig.runtime.state is PaperState.RUNNING, rig.runtime.reason
        backfill = rig.runtime.backfill
        assert backfill is not None
        assert backfill["ok"] is True, backfill
        assert len(rig.journal()) == 6 + 6 + 2  # downtime resolved, then the queued live minute
        await rig.supervisor.close()

    run(scenario())


def test_market_data_backfill_that_never_finishes_leaves_paper_unavailable(tmp_path: Path) -> None:
    async def scenario() -> None:
        gate = GatedFactory()
        rig = Rig(tmp_path, factory=gate)
        rig.runtime.settings = PaperSettings(enabled=True, poll_s=POLL, backfill_wait_s=POLL * 5)
        await rig.supervisor.start("S1")
        await asyncio.sleep(POLL * 20)
        assert rig.runtime.state is PaperState.UNAVAILABLE
        assert "did not finish" in str(rig.runtime.reason)
        assert rig.runtime.service is None
        assert rig.supervisor.running  # monitoring itself continues
        await rig.supervisor.close()

    run(scenario())


def test_drop_while_waiting_for_the_backfill_stops_paper(tmp_path: Path) -> None:
    """Any drop on the (fresh) paper subscription counts, also one before the session opened."""

    async def scenario() -> None:
        gate = GatedFactory()
        rig = Rig(tmp_path, factory=gate)
        await rig.supervisor.start("S1")
        sub = rig.runtime._sub
        assert isinstance(sub, FakeSubscription)
        sub.dropped = 1  # type: ignore[attr-defined]
        gate.done.set()
        await rig.minute(0)
        assert rig.runtime.state is PaperState.STOPPED_OVERFLOW
        assert rig.store.read(types=[EventType.PAPER_STEP]) == []
        await rig.supervisor.close()

    run(scenario())
