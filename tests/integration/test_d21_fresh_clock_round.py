"""D-21 (NB-1 option B): the paper runtime fixes its observation anchor only right after a fresh
server-clock round it requested, and only if the round is fresh at the poll (fake market data,
synthetic bars, no network).

Fail closed throughout: while the round is pending, failed, too old or otherwise untrustworthy the
service processes every bar unobserved (no entry fills or is created; resting exits still
resolve), paper stays RUNNING, the reason is published, and the request is retried with a bounded,
growing delay. This narrows NB-1's exposure (the round-to-anchor interval); it does not remove it
(SIMULATION §7).
"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path
from typing import Any

import pytest
from m1t2_fakes import FakeMarketData
from test_mvp2_paper import POLL, T0, Rig, run
from test_p1_observation_anchor import Recorder, _flags, _round, _settled

from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.marketdata.models import MarketSnapshot
from tsclaude.runtime.paper_runtime import PaperSettings
from tsclaude.sim.journal import MemoryJournal
from tsclaude.sim.paper import PaperTradingService

M = MS_PER_MINUTE
PENDING = "clock_resync_pending"


def _settings(**kw: Any) -> PaperSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "poll_s": POLL,
        "resync_retry_s": POLL,
        "resync_retry_max_s": POLL,
    }
    base.update(kw)
    return PaperSettings(**base)


def _blocked_titles(rig: Rig) -> list[str]:
    return [d for t, d in rig.problems if t == "Paper observation blocked"]


# ---------------------------------------------------------------- success and pending
def test_no_anchor_until_the_requested_round_finishes(tmp_path: Path) -> None:
    """The first poll only requests the round; while it runs bars are unobserved and the status
    says why (pending: transient, no problem raised); the anchor is the first whole minute after
    the poll that found the finished round, from a snapshot taken after it."""

    async def scenario() -> None:
        rig = Rig(tmp_path)
        recorder = Recorder()
        rig.runtime._strategy_factory = lambda: recorder
        rig.clock.set(T0 + 20_000)
        await rig.supervisor.start("S1")
        finish = asyncio.Event()  # the round runs until the test lets it finish
        real = rig.md.resync_clock

        async def slow_round() -> bool:
            await finish.wait()
            return await real()

        rig.runtime._resync = slow_round  # the hook the runtime was started with
        await rig.idle(T0 + 20_000, anchor=False)  # polled; the round is still running
        observation = rig.runtime.observation
        assert observation is not None
        assert (observation["state"], observation["code"]) == ("blocked", PENDING)
        assert observation["resync"]["pending"] is True
        assert rig.runtime.service is not None
        assert rig.runtime.service.observing_from_ms is None
        await rig.minute(0, anchor=False)  # minute 0 closes while the round is still running
        assert rig.runtime.service.observing_from_ms is None
        finish.set()  # the round finishes; the next poll is at minute 1's open + 1.5 s
        observation = await _settled(rig)
        assert observation["state"] == "active"
        assert observation["observing_from_ms"] == T0 + 2 * M
        assert observation["round_age_ms"] == 0
        assert observation["resync"]["attempts"] == 1
        assert rig.md.resync_calls == 1
        for i in (1, 2):
            await rig.minute(i)
        flags = _flags(rig)
        assert flags[T0] == [False, False]
        assert flags[T0 + M] == [False, False]
        assert flags[T0 + 2 * M] == [True, True]
        assert recorder.calls == [T0 + 2 * M, T0 + 2 * M]
        assert _blocked_titles(rig) == []  # pending is not a problem
        await rig.supervisor.close()

    run(scenario())


# ---------------------------------------------------------------- failures and recovery
@pytest.mark.parametrize(
    ("outcome", "why"),
    [
        (ConnectionError("exchange unreachable"), "ConnectionError: exchange unreachable"),
        (False, "the server-clock round produced no sample"),
    ],
)
def test_a_failed_round_blocks_observation_until_a_later_round_succeeds(
    tmp_path: Path, outcome: Any, why: str
) -> None:
    """REST sync unavailable or empty: no anchor, paper RUNNING, bars unobserved, one problem;
    the retry (after the backoff) requests a new round and recovers."""

    async def scenario() -> None:
        rig = Rig(tmp_path)
        recorder = Recorder()
        rig.runtime._strategy_factory = lambda: recorder
        rig.clock.set(T0 + 20_000)
        await rig.supervisor.start("S1")
        rig.md.resync_results = [outcome] * 50
        await rig.idle(T0 + 20_000)
        await rig.minute(0)
        observation = await _settled(rig)
        assert observation["code"] == "clock_resync_failed"
        assert why in observation["problem"]
        assert observation["observing_from_ms"] is None
        assert observation["resync"]["consecutive_failures"] >= 1
        assert observation["resync"]["last_error"] == why
        assert rig.runtime.state == "RUNNING"
        assert len(_blocked_titles(rig)) == 1  # once per reason, not per retry
        calls = rig.md.resync_calls
        assert calls >= 2  # retried
        rig.md.resync_results = []  # the exchange answers again
        await rig.minute(1)  # recovery at minute 1's close + 1.5 s: anchor at minute 3's open
        await rig.minute(2)
        observation = await _settled(rig)
        assert observation["state"] == "active"
        assert observation["observing_from_ms"] == T0 + 3 * M
        assert observation["resync"]["consecutive_failures"] == 0
        await rig.minute(3)
        flags = _flags(rig)
        assert all(flags[T0 + i * M] == [False, False] for i in range(3))
        assert flags[T0 + 3 * M] == [True, True]
        assert recorder.calls == [T0 + 3 * M, T0 + 3 * M]
        await rig.supervisor.close()

    run(scenario())


def test_a_round_that_does_not_finish_in_time_counts_as_failed(tmp_path: Path) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path)
        rig.runtime.settings = _settings(
            resync_timeout_s=0.05, resync_retry_s=5.0, resync_retry_max_s=5.0
        )
        rig.clock.set(T0 + 20_000)
        await rig.supervisor.start("S1")
        rig.md.resync_results = [5.0]  # far longer than the timeout
        await rig.idle(T0 + 20_000)  # returns once the round timed out (the gate's verdict)
        observation = await _settled(rig)
        assert observation["code"] == "clock_resync_failed"
        assert "no server-clock round within 0.05 s" in observation["problem"]
        assert rig.md.resync_calls == 1  # the next request waits for the 5 s backoff
        assert observation["resync"]["retry_in_s"] is not None
        assert rig.runtime.service is not None
        assert rig.runtime.service.observing_from_ms is None
        await rig.supervisor.close()

    run(scenario())


def test_without_a_resync_hook_paper_never_observes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A market-data service with no `resync_clock`: fail closed for the whole run."""
    monkeypatch.setattr(FakeMarketData, "resync_clock", None)

    async def scenario() -> None:
        rig = Rig(tmp_path)
        rig.clock.set(T0 + 20_000)
        await rig.supervisor.start("S1")
        await rig.idle(T0 + 20_000)
        for i in range(3):
            await rig.minute(i)
        observation = rig.runtime.observation
        assert observation is not None
        assert observation["code"] == "clock_resync_unavailable"
        assert observation["resync"]["available"] is False
        flags = _flags(rig)
        assert all(flags[T0 + i * M] == [False, False] for i in range(3))
        assert rig.runtime.state == "RUNNING"
        await rig.supervisor.close()

    run(scenario())


# ---------------------------------------------------------------- the round's own evidence
@pytest.mark.parametrize(
    ("update", "code", "text"),
    [
        # the requested round "succeeded" but the snapshot still carries a 6 s old round
        (lambda at: _round(3, 40, at - 6_000, elapsed=6_000), "clock_round_not_fresh", "6000 ms"),
        # a wall step back after the fresh round (detected against the monotonic clock)
        (
            lambda at: _round(3, 40, at - 1_000, elapsed=1_300, wall_elapsed=1_000),
            "clock_discontinuity",
            "moved -300 ms",
        ),
        # a monotonic suspend after the fresh round
        (
            lambda at: _round(3, 40, at - 4_000, elapsed=500, wall_elapsed=4_000),
            "clock_discontinuity",
            "moved 3500 ms",
        ),
        (lambda at: _round(3, 40, at, step=5), "clock_step_in_round", "during the server clock"),
    ],
)
def test_a_fresh_round_must_still_be_fresh_and_continuous_at_the_poll(
    tmp_path: Path, update: Any, code: str, text: str
) -> None:
    async def scenario() -> None:
        rig = Rig(tmp_path)
        rig.clock.set(T0 + 20_000)
        await rig.supervisor.start("S1")
        real = rig.md.snapshot
        bad = {"on": True}

        def snapshot() -> MarketSnapshot:
            snap = real()
            return snap.model_copy(update=update(snap.as_of_ms)) if bad["on"] else snap

        rig.md.snapshot = snapshot  # type: ignore[method-assign]
        await rig.idle(T0 + 20_000)
        await rig.minute(0)
        observation = await _settled(rig)
        assert observation["code"] == code
        assert text in observation["problem"]
        assert observation["observing_from_ms"] is None
        assert rig.md.resync_calls >= 2  # each retry requests a new round
        bad["on"] = False
        await rig.minute(1)
        await rig.minute(2)
        observation = await _settled(rig)
        assert observation["observing_from_ms"] == T0 + 3 * M
        await rig.supervisor.close()

    run(scenario())


def test_the_freshness_limit_is_inclusive_and_rejects_one_ms_more(tmp_path: Path) -> None:
    """The bound alone accepts a round up to the risk age limit; the D-21 rule then requires at
    most `anchor_round_max_age_ms` on the larger of the two elapsed times."""
    from test_p1_observation_anchor import _snapshot

    rig = Rig(tmp_path)
    now = T0 + 20_000
    for age, wall_age, expected in [
        (5_000, 5_000, T0 + M),
        (5_001, 5_001, None),
        (5_000, 5_001, None),  # either clock
        (5_001, 5_000, None),
    ]:
        service = PaperTradingService(rig.config_factory(), MemoryJournal(), observation_gated=True)
        snap = _snapshot(
            tmp_path, now, **_round(3, 40, now - wall_age, elapsed=age, wall_elapsed=wall_age)
        )
        rig.runtime._establish_observation(service, snap, now)
        assert service.observing_from_ms == expected, (age, wall_age)
        if expected is None:
            assert rig.runtime.observation is not None
            assert rig.runtime.observation["code"] == "clock_round_not_fresh"
        bound_only = PaperTradingService(
            rig.config_factory(), MemoryJournal(), observation_gated=True
        )
        rig.runtime._establish_observation(bound_only, snap, now, bound_only=True)
        assert bound_only.observing_from_ms == T0 + M  # the bound itself still accepts it


# ---------------------------------------------------------------- bounded retries
def test_retries_back_off_and_are_capped(tmp_path: Path) -> None:
    """Consecutive failures double the wait from `resync_retry_s` up to `resync_retry_max_s`;
    one request at a time."""

    async def scenario() -> None:
        rig = Rig(tmp_path)
        rig.runtime.settings = _settings(resync_retry_s=0.04, resync_retry_max_s=0.16)
        rig.clock.set(T0 + 20_000)
        await rig.supervisor.start("S1")
        loop = asyncio.get_running_loop()
        times: list[float] = []
        real = rig.md.resync_clock

        async def failing() -> bool:
            times.append(loop.time())
            await real()
            return False

        rig.md.resync_clock = failing  # type: ignore[method-assign]
        rig.runtime._resync = failing  # the hook the runtime was started with
        await asyncio.sleep(1.2)
        gaps = [b - a for a, b in itertools.pairwise(times)]
        assert len(times) >= 5
        assert gaps[0] >= 0.04 - 0.005
        assert gaps[1] >= 0.08 - 0.005
        assert gaps[2] >= 0.16 - 0.005
        assert all(g <= 0.16 + 0.15 for g in gaps)  # capped (plus poll and scheduling slack)
        assert len(times) <= 1 + 1.2 / 0.04  # never faster than the base wait
        observation = rig.runtime.observation
        assert observation is not None
        assert observation["resync"]["consecutive_failures"] >= 4
        await rig.supervisor.close()

    run(scenario())


# ---------------------------------------------------------------- restart
def test_every_monitoring_start_requests_its_own_fresh_round(tmp_path: Path) -> None:
    """A stop cancels a pending round; the next start resets the gate and requests a new round
    (the previous run's anchor and retry state are never reused)."""

    async def scenario() -> None:
        rig = Rig(tmp_path)
        rig.clock.set(T0 + 20_000)
        await rig.supervisor.start("S1")
        rig.md.resync_results = [10.0]  # never finishes in this run
        await rig.idle(T0 + 20_000, anchor=False)
        assert rig.runtime._resync_task is not None
        pending = rig.runtime._resync_task
        await rig.supervisor.stop()
        assert pending.cancelled() or pending.done()
        assert rig.runtime._resync_task is None
        rig.clock.set(T0 + 5 * M + 20_000)
        await rig.supervisor.start("S2")
        await rig.idle(T0 + 5 * M + 20_000)
        observation = await _settled(rig)
        assert observation["state"] == "active"
        assert observation["observing_from_ms"] == T0 + 6 * M
        assert observation["resync"]["attempts"] == 1  # this run's own request
        assert rig.md.resync_calls == 2
        await rig.supervisor.close()

    run(scenario())


# ---------------------------------------------------------------- what the operator reads (N-4)
def test_the_served_limitations_state_the_clock_assumption_and_the_nb1_residual_risk() -> None:
    """N-4: `/api/paper/status` serves these limitations; they must carry the same-clock
    assumption and the undetected cases, and never claim a fixed error bound."""
    from tsclaude.runtime.paper_runtime import LIMITATIONS

    text = " ".join(LIMITATIONS)
    for phrase in [
        "requested by this run right before it",
        "one and the same local clock (wall or monotonic) runs within 500 ppm",
        "without switching clocks midway",
        "the runtime cannot verify",
        "roughly compensating monotonic suspend",
        "a VM pause or migration",
        "a clock outside 500 ppm",
        "grows with the anomaly (no fixed bound)",
    ]:
        assert phrase in text, phrase
    assert "eliminat" not in text
