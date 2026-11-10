"""MVP-2 paper runtime wired into the monitoring supervisor (fake market data, no network).

P1-P6, P8, P9 of the MVP-2 brief, plus backfill validation and the new-session audit. Every test is
small (a few simulated minutes); results are provisional while the WHEA investigation is open.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
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
from tsclaude.sim.paper import PaperConfig, PaperTradingService
from tsclaude.sim.testing import spot_spec, usdm_spec
from tsclaude.store.eventstore import EventStore

T0 = 1_760_000_040_000 - 1_760_000_040_000 % MS_PER_MINUTE
KEYS = [(Market.SPOT, "BTCUSDT"), (Market.USDM, "BTCUSDT")]
POLL = 0.01
#: upper bound of every condition wait below: only a hung runtime reaches it (a slow runner does
#: not: the waits end on what the runtime did, not on elapsed time)
WAIT_S = 60.0


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


async def until(
    cond: Callable[[], bool], what: str, detail: Callable[[], str] = lambda: ""
) -> None:
    """Wait (bounded by `WAIT_S`) until `cond()` holds; the event loop runs in between."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_S
    while not cond():
        if loop.time() > deadline:
            raise AssertionError(f"timed out after {WAIT_S:g} s waiting for {what}: {detail()}")
        await asyncio.sleep(POLL / 2)


async def consumer_stopped(runtime: PaperRuntime) -> None:
    """Wait until the paper consumer task has ended by itself (a halt or a failed open)."""
    task = runtime._task
    assert task is not None
    await until(task.done, "the paper consumer to stop", lambda: f"paper {runtime.state}")


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
        self._count_polls(self.runtime)
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
            # D-21: a failed fresh clock round is retried after one poll (no growing backoff)
            PaperSettings(
                enabled=enabled, poll_s=POLL, resync_retry_s=POLL, resync_retry_max_s=POLL
            ),
            self.store,
            self.clock,
            config_factory=lambda: self.config_factory(),
            audit_path=self.tmp / AUDIT_FILE,
            bars=self.bars,
        )

    # -------------------------------------------------------------- synchronization
    # The tests drive a manual clock, the consumer polls in real time. Asserting, or moving the
    # manual clock on, after a fixed real-time sleep is a race: on a slow runner (a blocking
    # commit, a loaded scheduler) the sleep ends before the poll it was meant to cover, and the
    # anchor is then fixed at a later manual time or not yet at all. These helpers wait for what
    # the consumer did instead. They only read the runtime (`_count_polls` wraps a method of this
    # test's instance; production code is unchanged).
    def _count_polls(self, runtime: PaperRuntime) -> None:
        """Record every completed poll of the consumer: `PaperRuntime._run` calls `_sync_state`
        last in each iteration that polled (and in no other), with no await in between."""
        self.polls = 0  # completed polls
        self.quiet_poll = 0  # the last poll that changed nothing since the poll before it
        self.settled_poll = 0  # ... and at which the anchor gate had decided at the clock's time
        self._mark: tuple[Any, ...] | None = None
        assert callable(getattr(runtime, "_sync_state", None))  # the hook point still exists
        real = runtime._sync_state

        def counted(service: PaperTradingService) -> None:
            real(service)
            stats = service.clock.stats
            mark = (
                self.store.head,  # everything journaled (paper steps, market data, signals)
                runtime.state,
                runtime.reason,
                service.state,
                service.observing_from_ms,
                (stats.offered, stats.released, stats.late),
                None if runtime.backfill is None else runtime.backfill["ok"],
            )
            self.polls += 1
            if mark == self._mark:
                self.quiet_poll = self.polls
                if self.anchor_decided():
                    self.settled_poll = self.polls
            self._mark = mark

        runtime._sync_state = counted  # type: ignore[method-assign]

    def anchor_decided(self) -> bool:
        """The anchor is fixed, or the gate gave its verdict (not the transient wait for a fresh
        round) at the manual clock's current time."""
        service = self.runtime.service
        if service is not None and service.observing_from_ms is not None:
            return True
        observation = self.runtime.observation
        return (
            observation is not None
            and observation["code"] != "clock_resync_pending"
            and observation["attempted_at_ms"] == self.clock.now_ms()
        )

    def consuming(self) -> bool:
        task = self.runtime._task
        return task is not None and not task.done()

    async def until(self, cond: Callable[[], bool], what: str) -> None:
        await until(
            cond,
            what,
            lambda: (
                f"paper {self.runtime.state} ({self.runtime.reason}), polls {self.polls}, "
                f"observation {self.runtime.observation}"
            ),
        )

    async def settle(self, *, anchor: bool = True) -> None:
        """Wait until the consumer has finished with everything given to it so far: it completed
        a poll, after this call, that changed nothing since the poll before it (also after this
        call), so with this manual time and these updates no further poll does anything; and,
        with `anchor`, the anchor gate had decided at that poll (pass False while a slow clock
        round is deliberately left pending). Ends at once when the consumer has stopped.

        While paper has no consumer polling (disabled, halted, or still waiting behind a closed
        startup-backfill gate) there is no poll to wait for: the loop gets the real time it always
        got, and whatever the test then asserts is that nothing happened."""
        gate = getattr(self.factory, "done", None)
        waiting = self.runtime.service is None and gate is not None and not gate.is_set()
        if not self.consuming() or waiting:
            await asyncio.sleep(POLL * 6)
            return
        start = self.polls
        reached = (lambda: self.settled_poll) if anchor else (lambda: self.quiet_poll)
        await self.until(
            lambda: not self.consuming() or reached() >= start + 2,
            "a poll that changed nothing" + (" with the anchor gate decided" if anchor else ""),
        )

    async def halted(self) -> None:
        await consumer_stopped(self.runtime)

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

    async def minute(
        self, i: int, keys: Sequence[tuple[Market, str]] = KEYS, *, anchor: bool = True
    ) -> None:
        """Deliver minute `i` like the live feed, then wait until the paper consumer has finished
        with it (`settle`), so the next step never moves the clock under an unfinished poll."""
        self.clock.set(T0 + (i + 1) * MS_PER_MINUTE + 1_500)
        for m, s in keys:
            self.md.push_bar(make_bar(m, s, T0 + i * MS_PER_MINUTE))
        await self.settle(anchor=anchor)

    async def idle(self, now_ms: int, *, anchor: bool = True) -> None:
        self.clock.set(now_ms)
        await self.settle(anchor=anchor)


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
        # the reason names the change (operational-readiness review R-04, task T3) ...
        reason = str(rig.runtime.reason)
        assert "ConfigurationMismatch" in reason
        assert "recorded with a different paper configuration" in reason
        assert "starting_equity: 10000 -> 5000" in reason
        # ... and the published snapshot (what api/paper.py turns into the hint) gets it
        from tsclaude.runtime.operator_hints import paper_hint

        published, _ = rig.runtime.published()
        assert published["state"] == "UNAVAILABLE"
        hint = paper_hint(published)
        assert hint is not None
        assert "TSC_CONFIG_DIR" in hint
        assert "Record new paper session" in hint
        assert len(rig.journal()) == 2  # the old journal is untouched
        # the recovery the hint gives: Stop, record a new session, Start (same data folder)
        await rig.supervisor.stop()
        record = rig.runtime.request_new_session("settings changed, start clean", "req-0007")
        assert record["created"] is True
        await rig.supervisor.start("S3")
        assert rig.runtime.session_id == record["new_session_id"]
        assert rig.runtime.state is PaperState.RUNNING
        await rig.minute(1)
        assert len(rig.journal(record["new_session_id"])) >= 1
        assert len(rig.journal()) == 2  # the old session stays as recorded
        assert rig.store.verify().ok
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
        assert len(batches) == 6 + 6 + 2
        # P-1: each run observes from the first whole minute after its first poll. Minute 0 is
        # never observed (S1 polled first at T0 or later); minute 1 only if S1 polled before it
        # opened; minute 2 always. Minutes 3-5 are the downtime backfill (no entries). S2's live
        # minute 6 is observed only if it opens at or after S2's anchor.
        assert observed[0:2] == [False, False]
        assert observed[2:4] in ([False, False], [True, True])
        assert observed[4:6] == [True, True]
        assert observed[6:12] == [False] * 6
        observation = rig.runtime.observation
        assert observation is not None
        assert observation["problem"] is None
        live_6 = observation["observing_from_ms"] <= T0 + 6 * MS_PER_MINUTE
        assert observed[12:] == [live_6, live_6]
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
    """Real downtime with a bar that does not exist: spot BTC minute 3 comes after the journal's
    last minute 2, a minute the live clock never released, so no recorded close covers it (unlike
    the class 1 case, a bar missing from a minute already closed before the restart). The service
    waits for a backfill that cannot come, shows it, never resumes by itself, and the only way out
    is an explicit new session."""

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
        # MVP-2.1: no switch while the old session's consumer is alive (it could still resume)
        with pytest.raises(PaperActionRefused):
            rig.runtime.request_new_session("restart ambiguity, start clean", "req-0001")
        await rig.supervisor.stop()
        record = rig.runtime.request_new_session("restart ambiguity, start clean", "req-0001")
        assert record["created"] is True
        assert record["previous_session_id"] == "paper"
        assert len(rig.journal()) == 6  # the old session is untouched
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
    """Market data stops underneath paper (like `MarketDataService.stop()`: `running` cleared, then
    every subscription closed). The real-`Subscription` version of this check is H5 in
    `test_mvp21_paper_hardening.py`."""

    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        await rig.minute(0)
        await rig.md.stop()  # not through the supervisor: paper is still consuming
        await rig.halted()
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
        await rig.halted()  # no update at all
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
        await rig.halted()
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
        await rig.settle()
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
        await rig.settle()
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
        await rig.halted()
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


# ---------------------------------------------------------------------- replay gate evidence
def test_a_clean_stop_writes_session_end_evidence_that_replays(tmp_path: Path) -> None:
    """The runtime's stop captures the live ledger hashes; the verifier replays the journal and
    reaches PASS on them (synthetic session: not the real recorded-session acceptance gate)."""
    from tsclaude.runtime.session_evidence import PASS, verify_recorded_session

    async def scenario() -> None:
        rig = Rig(tmp_path)
        await rig.supervisor.start("S1")
        for i in range(3):
            await rig.minute(i)
        live = rig.runtime.service.ledger_hashes()  # type: ignore[union-attr]
        await rig.supervisor.stop()
        assert rig.runtime.session_end_evidence is not None
        assert (tmp_path / "session_end").is_dir()
        report = verify_recorded_session(tmp_path, paper_config())
        assert report.verdict == PASS, report.reasons
        assert report.details["replayed_ledger_hashes"] == live
        await rig.supervisor.close()

    run(scenario())
