"""P-1: the paper runtime's observation anchor and the deferred reference warm-up (fake market
data, synthetic bars, no network).

The runtime fixes the anchor at its first poll from the market-data clock round (offset, largest
round trip, and the round's monotonic evidence: drift since the round, wall-clock movement against
the monotonic clock) and fails closed while no trustworthy round exists; bars opening before the
anchor are processed unobserved. `bracket_reference` is built at the first observed bar and warmed
up through the store, so the unobserved minutes of a start do not break its history.
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from m1t2_fakes import FakeMarketData
from test_mvp2_paper import POLL, T0, GatedFactory, Rig, paper_config, run

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "strategies"))
from test_live_adapter import COSTS, PARAMS, TRADE_FROM, series

from tsclaude.contracts.market import Bar
from tsclaude.contracts.provenance import Provenance, Sourced
from tsclaude.contracts.signals import SignalStatus
from tsclaude.contracts.timeutil import MS_PER_MINUTE, ManualClock
from tsclaude.marketdata.bars import BarClosed
from tsclaude.marketdata.models import ClockContinuity, MarketSnapshot
from tsclaude.quality.clock import drift_allowance_ms
from tsclaude.research.source import StrategySource
from tsclaude.runtime.paper_runtime import DeferredReference
from tsclaude.sim.engine import SimView
from tsclaude.sim.journal import MemoryJournal
from tsclaude.sim.paper import Intent, PaperConfig, PaperTradingService, observation_anchor_ms
from tsclaude.sim.testing import spot_spec, usdm_spec
from tsclaude.store.eventstore import EventStore
from tsclaude.strategies import BracketReference
from tsclaude.strategies.live_adapter import ReferenceLiveStrategy

M = MS_PER_MINUTE


class Recorder:
    """A live strategy that records every bar it is called for and never trades."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def on_bar(self, bar: Bar, view: SimView) -> list[Intent]:
        self.calls.append(bar.open_time_ms)
        return []


def _flags(rig: Rig) -> dict[int, list[bool]]:
    out: dict[int, list[bool]] = {}
    for b in rig.journal():
        head = b[0].payload
        out.setdefault(head["bar"]["open_time_ms"], []).append(head["observed"])
    return out


async def _settled(rig: Rig) -> dict[str, Any]:
    """The runtime's observation once it is not the transient wait for a fresh round (D-21)."""
    for _ in range(500):
        observation = rig.runtime.observation
        if observation is not None and observation["code"] != "clock_resync_pending":
            return observation
        await asyncio.sleep(POLL)
    raise AssertionError(f"observation stayed pending: {rig.runtime.observation}")


# ---------------------------------------------------------------- the anchor at the first poll
def test_the_runtime_observes_from_the_first_whole_minute_after_its_first_poll(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path)
        recorder = Recorder()
        rig.runtime._strategy_factory = lambda: recorder
        rig.clock.set(T0 + 20_000)
        await rig.supervisor.start("S1")
        await rig.idle(T0 + 20_000)  # the first poll, 20 s into minute 0 (mid-candle)
        observation = rig.runtime.observation
        assert observation == {
            "state": "active",
            "attempted_at_ms": T0 + 20_000,
            "observing_from_ms": T0 + M,
            "problem": None,
            "code": None,
            "offset_ms": 3,  # the fake's clock round
            "max_rtt_ms": 40,
            "uncertainty_ms": 23,
            "drift_ms": 1,  # 500 ppm of the round's 120 ms span (+2 ms), rounded up
            "step_ms": 0,
            "allowance_ms": 2,  # + 1 ms for the elapsed reading
            "round_age_ms": 0,
            # D-21: one fresh round requested by this run, and it gave the anchor
            "resync": {
                "available": True,
                "pending": False,
                "attempts": 1,
                "consecutive_failures": 0,
                "last_error": None,
                "retry_in_s": None,
            },
        }
        assert rig.md.resync_calls == 1
        service = rig.runtime.service
        assert service is not None
        assert service.observing_from_ms == T0 + M
        snap, _ = rig.runtime.published()
        assert snap["observation"]["observing_from_ms"] == T0 + M
        assert snap["service"]["observation"] == {"gated": True, "observing_from_ms": T0 + M}
        for i in range(3):
            await rig.minute(i)
        assert _flags(rig) == {T0: [False, False], T0 + M: [True, True], T0 + 2 * M: [True, True]}
        assert recorder.calls
        assert min(recorder.calls) == T0 + M
        await rig.supervisor.close()

    run(scenario())


def test_minutes_queued_during_the_startup_wait_are_processed_unobserved(tmp_path: Path) -> None:
    """Audit N-1: the subscription exists before paper opens; minutes delivered while paper waits
    for the market-data startup backfill are processed only after it, so they are not observed."""

    async def scenario() -> None:
        gate = GatedFactory()
        rig = Rig(tmp_path, factory=gate)
        recorder = Recorder()
        rig.runtime._strategy_factory = lambda: recorder
        await rig.supervisor.start("S1")
        for i in range(3):  # queued, not processed: paper is still waiting
            await rig.minute(i)
        assert rig.runtime.service is None
        gate.done.set()  # the first poll happens now, at minute 2's close + 1.5 s
        await asyncio.sleep(POLL * 10)
        observation = rig.runtime.observation
        assert observation is not None
        assert observation["observing_from_ms"] == T0 + 4 * M
        for i in (3, 4):
            await rig.minute(i)
        flags = _flags(rig)
        # every bar before the anchor is unobserved; the backlog is drained before the first poll,
        # so its first minute keeps both bars (M3-PB.3, test_pb3_startup_backlog.py)
        assert sorted(m for m in flags if m < T0 + 4 * M) == [T0 + i * M for i in range(4)]
        assert flags[T0] == [False, False]
        assert not any(any(v) for m, v in flags.items() if m < T0 + 4 * M)
        assert flags[T0 + 4 * M] == [True, True]
        assert recorder.calls == [T0 + 4 * M, T0 + 4 * M]
        await rig.supervisor.close()

    run(scenario())


# ---------------------------------------------------------------------- fail closed on the clock
def _snapshot(tmp_path: Path, now: int, **update: Any) -> MarketSnapshot:
    fake = FakeMarketData(EventStore(tmp_path / "snap.db"), ManualClock(now))
    return fake.snapshot().model_copy(update=update)


def _live(value: int, as_of: int) -> Sourced[int]:
    return Sourced[int](value=value, provenance=Provenance.LIVE, as_of_ms=as_of)


def _continuity(
    as_of: int,
    *,
    elapsed: int = 0,
    wall_elapsed: int | None = None,
    min_rtt: int = 10,
    span: int = 120,
    step: int = 0,
    provenance: Provenance = Provenance.LIVE,
) -> Sourced[ClockContinuity]:
    value = ClockContinuity(
        samples=5,
        min_rtt_ms=min_rtt,
        round_span_ms=span,
        round_step_ms=step,
        elapsed_ms=elapsed,
        wall_elapsed_ms=elapsed if wall_elapsed is None else wall_elapsed,
    )
    return Sourced[ClockContinuity](value=value, provenance=provenance, as_of_ms=as_of)


def _round(offset: int, rtt: int, as_of: int, **continuity: Any) -> dict[str, Any]:
    """The three clock fields of one round, as the market-data snapshot carries them."""
    return {
        "clock_offset_ms": _live(offset, as_of),
        "clock_max_rtt_ms": _live(rtt, as_of),
        "clock_continuity": _continuity(as_of, **continuity),
    }


NOW = T0 + 20_000
DEMO_RTT = Sourced[int](value=0, provenance=Provenance.DEMO, as_of_ms=NOW)


@pytest.mark.parametrize(
    ("update", "code", "problem"),
    [
        (None, "no_snapshot", "no market-data snapshot"),
        (
            {"clock_offset_ms": Sourced[int].unavailable("not sampled")},
            "clock_offset_unknown",
            "offset unknown",
        ),
        (
            {"clock_max_rtt_ms": Sourced[int].unavailable("not reported")},
            "clock_rtt_unknown",
            "round trip unknown",
        ),
        ({"clock_max_rtt_ms": DEMO_RTT}, "clock_rtt_unknown", "round trip unknown"),  # F-3
        (
            {"clock_max_rtt_ms": _live(40, NOW - 1)},
            "clock_rtt_other_round",
            "round trip comes from another round",
        ),
        (
            {"clock_continuity": Sourced[ClockContinuity].unavailable("no monotonic clock")},
            "clock_continuity_unknown",
            "no monotonic clock evidence (no monotonic clock)",
        ),
        (
            {"clock_continuity": _continuity(NOW, provenance=Provenance.DEMO)},
            "clock_continuity_unknown",
            "no monotonic clock evidence",
        ),
        (
            {"clock_continuity": _continuity(NOW - 1)},
            "clock_continuity_other_round",
            "monotonic clock evidence comes from another round",
        ),
        (_round(3, 40, NOW - 900_001, elapsed=900_001), "clock_round_stale", "900001 ms old"),
        (
            _round(3, 40, NOW + 1, elapsed=0, wall_elapsed=-1),
            "clock_round_future",
            "-1 ms old",
        ),  # a round from the local future: the local clock stepped back
        ({"clock_offset_ms": _live(1_001, NOW)}, "clock_offset_skew", "beyond 1000 ms"),
        ({"clock_offset_ms": _live(-1_001, NOW)}, "clock_offset_skew", "beyond 1000 ms"),
        # F-3: every sample's round trip negative, or only some of them
        (_round(3, -1, NOW, min_rtt=-5), "clock_rtt_negative", "negative round trip (-5 ms)"),
        (_round(3, 40, NOW, min_rtt=-1), "clock_rtt_negative", "negative round trip (-1 ms)"),
        (_round(3, 40, NOW, min_rtt=41), "clock_evidence_inconsistent", "inconsistent"),
        # F-1: the wall clock stepped during the round, or since it (back; forward or a suspend)
        (
            _round(3, 40, NOW, step=5),
            "clock_step_in_round",
            "moved 5 ms against the monotonic clock during the server clock round",
        ),
        (
            _round(3, 40, NOW - 1_000, elapsed=1_300, wall_elapsed=1_000),
            "clock_discontinuity",
            "moved -300 ms against the monotonic clock since",
        ),
        (
            _round(3, 40, NOW - 60_000, elapsed=1_000, wall_elapsed=60_000),
            "clock_discontinuity",
            "moved 59000 ms",
        ),
    ],
)
def test_no_anchor_without_a_trustworthy_clock_round(
    tmp_path: Path, update: dict[str, Any] | None, code: str, problem: str
) -> None:
    rig = Rig(tmp_path)
    service = PaperTradingService(paper_config(), MemoryJournal(), observation_gated=True)
    snap = None if update is None else _snapshot(tmp_path, NOW, **update)
    rig.runtime._establish_observation(service, snap, NOW)
    assert service.observing_from_ms is None
    observation = rig.runtime.observation
    assert observation is not None
    assert observation["state"] == "blocked"
    assert observation["observing_from_ms"] is None
    assert observation["code"] == code
    assert problem in observation["problem"]


@pytest.mark.parametrize(
    ("offset", "age", "local", "expected"),
    [
        (3, 0, NOW, T0 + M),
        (1_000, 900_000, NOW, T0 + M),  # the limits themselves are accepted
        (-1_000, 0, NOW, T0 + M),
        # local clock 900 ms behind exchange, polled 500 ms before an open by local time: in
        # exchange time the poll is 400 ms after it, so that minute is partly observed (ignoring
        # the offset would count it)
        (900, 0, T0 + M - 500, T0 + 2 * M),
        # 10 ms before an open in exchange time, but the round trip of 40 ms bounds the error at
        # 23 ms: the minute may have opened before the poll, so it does not count
        (0, 0, T0 + M - 10, T0 + 2 * M),
        (0, 0, T0 + M - 30, T0 + M),  # far enough before the open
        # F-1: the same poll 300 s after its round: up to 151 ms of drift since the round
        (0, 300_000, T0 + M - 30, T0 + 2 * M),
        (0, 300_000, T0 + M - 175, T0 + M),  # 23 + 1 + 151: the bound exactly at the open
        (0, 300_000, T0 + M - 174, T0 + 2 * M),
    ],
)
def test_the_anchor_converts_the_poll_to_exchange_time_with_its_uncertainty(
    tmp_path: Path, offset: int, age: int, local: int, expected: int
) -> None:
    rig = Rig(tmp_path)
    service = PaperTradingService(paper_config(), MemoryJournal(), observation_gated=True)
    as_of = local - age
    snap = _snapshot(tmp_path, local, **_round(offset, 40, as_of, elapsed=age))
    rig.runtime._establish_observation(service, snap, local, bound_only=True)  # the bound alone
    assert service.observing_from_ms == expected
    allowance = 1 + drift_allowance_ms(120 + age + 2)
    assert expected == observation_anchor_ms(local, offset, 40, allowance_ms=allowance)


def test_a_stale_clock_blocks_observation_until_a_fresh_round_arrives(tmp_path: Path) -> None:
    """Fail closed, then recover: while the clock round is too old every bar is processed
    unobserved (no decision, no entry fill) and each poll tries again; the anchor is the first
    whole minute after the poll that found a fresh round, not after the first poll."""

    async def scenario() -> None:
        rig = Rig(tmp_path)
        recorder = Recorder()
        rig.runtime._strategy_factory = lambda: recorder
        fresh = {"ok": False}
        rig.clock.set(T0 + 20_000)
        await rig.supervisor.start("S1")
        md = rig.md
        real = md.snapshot

        def snapshot() -> MarketSnapshot:
            snap = real()
            if fresh["ok"]:
                return snap
            old = snap.as_of_ms - 900_001
            return snap.model_copy(update=_round(3, 40, old, elapsed=900_001))

        md.snapshot = snapshot  # type: ignore[method-assign]
        await rig.idle(T0 + 20_000)
        for i in range(2):
            await rig.minute(i)
        observation = await _settled(rig)
        assert observation["observing_from_ms"] is None
        assert "old" in observation["problem"]
        assert observation["attempted_at_ms"] == T0 + 2 * M + 1_500  # retried (D-21: a new round)
        assert rig.md.resync_calls >= 3  # one fresh round per retry, never the stale one reused
        fresh["ok"] = True
        for i in range(2, 5):
            await rig.minute(i)  # the first fresh poll is at minute 2's close + 1.5 s
        observation = rig.runtime.observation
        assert observation["observing_from_ms"] == T0 + 4 * M
        flags = _flags(rig)
        assert all(flags[T0 + i * M] == [False, False] for i in range(4))
        assert flags[T0 + 4 * M] == [True, True]
        assert recorder.calls == [T0 + 4 * M, T0 + 4 * M]
        service = rig.runtime.service
        assert service is not None
        assert not [
            e
            for e in service.session.sim.events
            if e["type"] in ("sim_order_submitted", "sim_order_filled")
            and e["payload"].get("bar_open_ms", e["ts_ms"]) < T0 + 4 * M
        ]  # the benchmark buys only at the first observed bar
        await rig.supervisor.close()

    run(scenario())


def test_invalid_clock_evidence_blocks_observation_without_stopping_paper(tmp_path: Path) -> None:
    """F-3 (and N-2): rounds whose round trips are all negative, partly negative, or not LIVE are
    invalid clock evidence. Paper keeps running (never CONSUMER_DIED), processes every bar
    unobserved, publishes why and raises one problem per change of reason; the first valid round
    then gives the anchor."""

    async def scenario() -> None:
        rig = Rig(tmp_path)
        recorder = Recorder()
        rig.runtime._strategy_factory = lambda: recorder
        phase = {"n": 0}
        rig.clock.set(T0 + 20_000)
        await rig.supervisor.start("S1")
        md = rig.md
        real = md.snapshot

        def snapshot() -> MarketSnapshot:
            snap = real()
            at = snap.as_of_ms
            bad: list[dict[str, Any]] = [
                _round(3, -1, at, min_rtt=-5),  # every sample's round trip negative
                _round(3, 40, at, min_rtt=-1),  # one sample's
                {
                    "clock_max_rtt_ms": Sourced[int](
                        value=0, provenance=Provenance.DEMO, as_of_ms=at
                    )
                },
            ]
            return snap if phase["n"] >= len(bad) else snap.model_copy(update=bad[phase["n"]])

        md.snapshot = snapshot  # type: ignore[method-assign]
        codes, problems = [], []
        for n, minutes in enumerate([(0, 1), (2,), (3,)]):
            phase["n"] = n
            for i in minutes:
                await rig.minute(i)
                observation = await _settled(rig)
                codes.append(observation["code"])
                problems.append(observation["problem"])
                assert observation["state"] == "blocked"
                assert rig.runtime.state == "RUNNING"
        assert codes == ["clock_rtt_negative"] * 3 + ["clock_rtt_unknown"]
        blocked = [d for title, d in rig.problems if title == "Paper observation blocked"]
        assert len(blocked) == 2  # one per change of reason (code), not one per poll
        assert "negative round trip (-5 ms)" in blocked[0]
        assert "round trip unknown" in blocked[1]
        assert "negative round trip (-1 ms)" in problems[2]  # the mixed round, same code
        snap, _ = rig.runtime.published()
        assert snap["observation"]["code"] == "clock_rtt_unknown"
        phase["n"] = 3  # a valid round: the next poll (minute 4's close + 1.5 s) anchors
        for i in (4, 5, 6):
            await rig.minute(i)
        observation = rig.runtime.observation
        assert observation["state"] == "active"
        assert observation["observing_from_ms"] == T0 + 6 * M
        flags = _flags(rig)
        assert all(flags[T0 + i * M] == [False, False] for i in range(6))
        assert flags[T0 + 6 * M] == [True, True]
        assert recorder.calls == [T0 + 6 * M, T0 + 6 * M]
        assert rig.runtime.state == "RUNNING"
        assert not [t for t, _ in rig.problems if t.startswith("Paper trading stopped")]
        await rig.supervisor.close()

    run(scenario())


# ---------------------------------------------------------------------- warm-up continuity
def _paper_config() -> PaperConfig:
    return PaperConfig(instruments=(spot_spec(), usdm_spec()), starting_equity=Decimal(10_000))


def _drive(svc: PaperTradingService, bars: list[Bar]) -> None:
    by_minute: dict[int, list[Bar]] = {}
    for b in bars:
        by_minute.setdefault(b.open_time_ms, []).append(b)
    for m in sorted(by_minute):
        for b in by_minute[m]:
            svc.on_update(BarClosed(b))
        svc.poll(m + MS_PER_MINUTE + 1_500)


def _adapter(bars: list[Bar], start: int) -> ReferenceLiveStrategy:
    """The reference adapter trading from `start`, warmed up with the stored bars closed before
    it (what `PaperRuntime._reference` does with the live bar store)."""
    adapter = ReferenceLiveStrategy(BracketReference(PARAMS), COSTS, trade_from_ms=start)
    adapter.warm_up([b for b in bars if b.close_time_ms < start])
    return adapter


def test_a_deferred_reference_decides_as_an_uninterrupted_one_from_the_anchor() -> None:
    """The service starts 30 minutes before its anchor; those live minutes are unobserved and never
    reach the strategy. Built at the first observed bar and warmed up through the store, the
    reference decides exactly what an uninterrupted backtest source trading from the anchor decides,
    with the same history breaks (the series has a real gap for spot later on). A strategy built at
    the start instead (before P-1, `trade_from_ms` at the start minute) loses those minutes: its
    history breaks and its warm-up restarts."""
    bars = series()
    anchor = TRADE_FROM
    started = anchor - 30 * M
    live = [b for b in bars if b.open_time_ms >= started]

    deferred = DeferredReference(lambda start: _adapter(bars, start))
    svc = PaperTradingService(
        _paper_config(), MemoryJournal(), strategy=deferred, observation_gated=True
    )
    svc.observe_from(anchor)
    _drive(svc, live)
    assert svc.fault is None
    adapter = deferred.adapter
    assert adapter is not None
    assert adapter.trade_from_ms == anchor

    src = StrategySource(BracketReference(PARAMS), COSTS, trade_from_ms=anchor, flat_from_ms=2**62)
    for b in bars:
        src.on_bar(b, None)  # type: ignore[arg-type]
    expected = [r.decision_ms for r in src.records if r.proposal.stage is SignalStatus.CONFIRMED]
    assert expected
    decided = sorted(s.record["decision_time_ms"] for s in svc.session.lifecycle.signals())
    assert decided == expected
    assert adapter.history_breaks == src.breaks
    assert all(p["ready"] for p in adapter.warmup_progress().values())

    early = _adapter(bars, started)  # built at the start: never sees the 30 unobserved minutes
    svc2 = PaperTradingService(
        _paper_config(), MemoryJournal(), strategy=early, observation_gated=True
    )
    svc2.observe_from(anchor)
    _drive(svc2, live)
    assert early.history_breaks > src.breaks
    assert len(svc2.session.lifecycle.signals()) < len(expected)


def test_a_failed_deferred_build_is_a_strategy_error_and_is_retried() -> None:
    """Fail closed: a build that raises gives no intents for that bar (the service records the
    strategy error) and the next observed bar builds it, trading from that bar."""
    bars = series()
    attempts: list[int] = []

    def build(start: int) -> ReferenceLiveStrategy:
        attempts.append(start)
        if len(attempts) == 1:
            raise OSError("bar store unavailable")
        return _adapter(bars, start)

    deferred = DeferredReference(build)
    svc = PaperTradingService(
        _paper_config(), MemoryJournal(), strategy=deferred, observation_gated=True
    )
    svc.observe_from(TRADE_FROM)
    _drive(svc, [b for b in bars if TRADE_FROM <= b.open_time_ms < TRADE_FROM + 3 * M])
    assert svc.fault is None
    assert attempts == [TRADE_FROM, TRADE_FROM]  # the spot bar failed; the usdm bar built it
    assert deferred.adapter is not None
    assert deferred.adapter.trade_from_ms == TRADE_FROM
    errors = [
        b for b in svc.journal.batches() if any("strategy_error" in str(e.payload) for e in b)
    ]
    assert len(errors) == 1
