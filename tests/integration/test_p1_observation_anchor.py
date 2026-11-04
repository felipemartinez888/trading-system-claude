"""P-1: the paper runtime's observation anchor and the deferred reference warm-up (fake market
data, synthetic bars, no network).

The runtime fixes the anchor at its first poll from the market-data clock round (offset and largest
round trip) and fails closed while no trustworthy round exists; bars opening before the anchor are
processed unobserved. `bracket_reference` is built at the first observed bar and warmed up through
the store, so the unobserved minutes of a start do not break its history.
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
from tsclaude.marketdata.models import MarketSnapshot
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
            "attempted_at_ms": T0 + 20_000,
            "observing_from_ms": T0 + M,
            "problem": None,
            "offset_ms": 3,  # the fake's clock round
            "max_rtt_ms": 40,
            "uncertainty_ms": 23,
        }
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
        # every bar before the anchor is unobserved. (Not P-1: the runtime polls once per update,
        # so the backlog's first minute is released before its second bar is offered, which then
        # is late; the same happens without the gate. Recorded as a separate finding.)
        assert sorted(m for m in flags if m < T0 + 4 * M) == [T0 + i * M for i in range(4)]
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


NOW = T0 + 20_000


@pytest.mark.parametrize(
    ("update", "problem"),
    [
        (None, "no market-data snapshot"),
        ({"clock_offset_ms": Sourced[int].unavailable("not sampled")}, "offset unknown"),
        ({"clock_max_rtt_ms": Sourced[int].unavailable("not reported")}, "round trip unknown"),
        ({"clock_max_rtt_ms": _live(40, NOW - 1)}, "round trip unknown"),  # another round
        (
            {
                "clock_offset_ms": _live(3, NOW - 900_001),
                "clock_max_rtt_ms": _live(40, NOW - 900_001),
            },
            "900001 ms old",
        ),
        (
            {"clock_offset_ms": _live(3, NOW + 1), "clock_max_rtt_ms": _live(40, NOW + 1)},
            "-1 ms old",
        ),  # a round from the local future: the local clock stepped back
        ({"clock_offset_ms": _live(1_001, NOW)}, "beyond 1000 ms"),
        ({"clock_offset_ms": _live(-1_001, NOW)}, "beyond 1000 ms"),
    ],
)
def test_no_anchor_without_a_trustworthy_clock_round(
    tmp_path: Path, update: dict[str, Any] | None, problem: str
) -> None:
    rig = Rig(tmp_path)
    service = PaperTradingService(paper_config(), MemoryJournal(), observation_gated=True)
    snap = None if update is None else _snapshot(tmp_path, NOW, **update)
    rig.runtime._establish_observation(service, snap, NOW)
    assert service.observing_from_ms is None
    observation = rig.runtime.observation
    assert observation is not None
    assert observation["observing_from_ms"] is None
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
    ],
)
def test_the_anchor_converts_the_poll_to_exchange_time_with_its_uncertainty(
    tmp_path: Path, offset: int, age: int, local: int, expected: int
) -> None:
    rig = Rig(tmp_path)
    service = PaperTradingService(paper_config(), MemoryJournal(), observation_gated=True)
    as_of = local - age
    snap = _snapshot(
        tmp_path, local, clock_offset_ms=_live(offset, as_of), clock_max_rtt_ms=_live(40, as_of)
    )
    rig.runtime._establish_observation(service, snap, local)
    assert service.observing_from_ms == expected
    assert expected == observation_anchor_ms(local, offset, 40)


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
            return snap.model_copy(
                update={"clock_offset_ms": _live(3, old), "clock_max_rtt_ms": _live(40, old)}
            )

        md.snapshot = snapshot  # type: ignore[method-assign]
        await rig.idle(T0 + 20_000)
        for i in range(2):
            await rig.minute(i)
        observation = rig.runtime.observation
        assert observation is not None
        assert observation["observing_from_ms"] is None
        assert "old" in observation["problem"]
        assert observation["attempted_at_ms"] == T0 + 2 * M + 1_500  # retried at every poll
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
