"""P-1 hardening (F-1, F-3): the observation anchor under clock drift, wall-clock discontinuities
and invalid clock evidence.

`observation_bound` bounds exchange time at a snapshot from a `ClockMonitor` round and the
monotonic clock: the round's offset and largest round trip, the drift the monotonic clock may have
accumulated since the round (`CLOCK_DRIFT_PPM`), and how far the wall clock moved against the
monotonic clock during the round. It gives no anchor when the evidence is inconsistent, a round
trip is negative, or the wall clock moved against the monotonic one (a step, a suspend, a fast
slew) beyond `continuity_tolerance_ms`. The soundness check here simulates the true timeline
(exchange time), a monotonic clock within the drift allowance and a wall clock with steps, and
feeds the floored readings through `ClockMonitor.record` exactly as production does.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

import pytest

from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.quality.clock import (
    CLOCK_DRIFT_PPM,
    ClockMonitor,
    ClockRound,
    ClockSample,
    MonotonicClock,
    continuity_tolerance_ms,
    drift_allowance_ms,
)
from tsclaude.sim.paper import (
    ClockEvidence,
    clock_uncertainty_ms,
    observation_anchor_ms,
    observation_bound,
)

M = MS_PER_MINUTE
MAX_AGE = 900_000
SKEW = 1_000


class Reading:
    """A clock whose next reading the test sets."""

    def __init__(self) -> None:
        self.value = 0

    def now_ms(self) -> int:
        return self.value


async def _never() -> int:
    raise AssertionError("not called")


def _evidence(**kw: int) -> ClockEvidence:
    base = {
        "round_at_ms": 10 * M,
        "offset_ms": 0,
        "max_rtt_ms": 0,
        "min_rtt_ms": 0,
        "samples": 5,
        "round_span_ms": 0,
        "round_step_ms": 0,
        "elapsed_ms": 0,
        "wall_elapsed_ms": 0,
    }
    base.update(kw)
    return ClockEvidence(**base)


def _verdict(**kw: int) -> tuple[int | None, str | None]:
    v = observation_bound(_evidence(**kw), max_age_ms=MAX_AGE, skew_max_ms=SKEW)
    return v.anchor_ms, v.code


# ---------------------------------------------------------------- the allowances
def test_the_drift_allowance_is_500_ppm_of_the_elapsed_monotonic_time_rounded_up() -> None:
    assert CLOCK_DRIFT_PPM == 500
    got = [drift_allowance_ms(n) for n in (0, 1, 1_999, 2_000, 60_000, 300_000, 900_000)]
    assert got == [0, 1, 1, 2, 31, 151, 451]
    for n in (1, 7, 59_999, 900_123):  # never below n * r / (1 - r)
        assert drift_allowance_ms(n) >= n * 500 / 999_500
    with pytest.raises(ValueError, match="elapsed_ms"):
        drift_allowance_ms(-1)


def test_the_continuity_tolerance_is_the_reading_resolution_plus_both_rate_allowances() -> None:
    assert [continuity_tolerance_ms(n) for n in (0, 1, 300_000)] == [2, 4, 304]


def test_the_anchor_allowance_must_not_be_negative() -> None:
    assert observation_anchor_ms(10 * M - 4, 0, 0, allowance_ms=1) == 10 * M
    assert observation_anchor_ms(10 * M - 3, 0, 0, allowance_ms=1) == 11 * M
    with pytest.raises(ValueError, match="allowance_ms"):
        observation_anchor_ms(10 * M, 0, 0, allowance_ms=-1)


# ---------------------------------------------------------------- the boundary, +-1 ms
@pytest.mark.parametrize(
    ("round_at", "expected"),
    [
        # exact clocks, no round trip, no span: the bound is the instant + 3 ms (offset error) +
        # 1 ms (the elapsed reading) + 1 ms (500 ppm of the 2 ms the readings may hide, rounded up)
        (10 * M - 5, 10 * M),  # the bound exactly at the open: that minute counts
        (10 * M - 6, 10 * M),  # 1 ms before it
        (10 * M - 4, 11 * M),  # 1 ms after it: the minute may have opened already
    ],
)
def test_the_bound_at_a_minute_open_plus_minus_one_ms(round_at: int, expected: int) -> None:
    assert _verdict(round_at_ms=round_at) == (expected, None)


@pytest.mark.parametrize(
    ("step", "round_at", "expected"),
    [
        # a wall/monotonic spread S within the tolerance (2 ms at span 0) widens the bound by
        # ceil(1.5 S): the wall offset and round trips may each be off by S in the monotonic frame
        (1, 10 * M - 7, 10 * M),  # 5 + 2: exactly at the open
        (1, 10 * M - 6, 11 * M),
        (2, 10 * M - 8, 10 * M),  # 5 + 3
        (2, 10 * M - 7, 11 * M),
    ],
)
def test_a_step_within_the_round_tolerance_widens_the_bound(
    step: int, round_at: int, expected: int
) -> None:
    v = observation_bound(
        _evidence(round_at_ms=round_at, round_step_ms=step), max_age_ms=MAX_AGE, skew_max_ms=SKEW
    )
    assert (v.anchor_ms, v.code) == (expected, None)
    assert v.details["step_ms"] == (3 * step + 1) // 2


def test_the_bound_follows_the_monotonic_clock_and_grows_with_the_drift_since_the_round() -> None:
    """The snapshot instant is the round end plus the elapsed monotonic time; the drift allowance
    grows with it. 175 ms before an open, a round 300 s old still counts that minute (3 + 1 + 151
    ms); 150 ms before it, it no longer does."""
    at = 20 * M
    assert drift_allowance_ms(300_002) == 151
    for elapsed, before, expected in [
        (0, 175, at),
        (60_000, 175, at),
        (300_000, 175, at),
        (300_000, 150, at + M),
        (900_000, 175, at + M),  # 451 ms of drift at the age limit
        (900_000, 460, at),
    ]:
        round_at = at - before - elapsed
        got = _verdict(round_at_ms=round_at, elapsed_ms=elapsed, wall_elapsed_ms=elapsed)
        assert got == (expected, None), (elapsed, before)


# ---------------------------------------------------------------- invalid evidence
@pytest.mark.parametrize(
    ("evidence", "code"),
    [
        ({"samples": 0}, "clock_evidence_inconsistent"),
        ({"min_rtt_ms": 5, "max_rtt_ms": 4}, "clock_evidence_inconsistent"),
        ({"round_span_ms": -1}, "clock_evidence_inconsistent"),
        ({"round_step_ms": -1}, "clock_evidence_inconsistent"),
        ({"elapsed_ms": -1}, "clock_evidence_inconsistent"),
        ({"min_rtt_ms": -3, "max_rtt_ms": -1}, "clock_rtt_negative"),  # every sample
        ({"min_rtt_ms": -1, "max_rtt_ms": 40}, "clock_rtt_negative"),  # only some
        ({"round_step_ms": 3}, "clock_step_in_round"),  # tolerance 2 ms for a 0 ms round
        ({"wall_elapsed_ms": -1}, "clock_round_future"),
        ({"elapsed_ms": 1_000, "wall_elapsed_ms": 995}, "clock_discontinuity"),  # tol 2 + 2
        ({"elapsed_ms": 1_000, "wall_elapsed_ms": 1_005}, "clock_discontinuity"),
        ({"elapsed_ms": MAX_AGE + 1, "wall_elapsed_ms": MAX_AGE + 1}, "clock_round_stale"),
        ({"offset_ms": SKEW + 1}, "clock_offset_skew"),
        ({"offset_ms": -SKEW - 1}, "clock_offset_skew"),
    ],
)
def test_invalid_clock_evidence_gives_no_anchor_and_never_raises(
    evidence: dict[str, int], code: str
) -> None:
    v = observation_bound(_evidence(**evidence), max_age_ms=MAX_AGE, skew_max_ms=SKEW)
    assert v.anchor_ms is None
    assert v.code == code
    assert v.problem


def test_the_limits_themselves_are_accepted() -> None:
    assert _verdict(round_step_ms=2)[1] is None
    assert _verdict(elapsed_ms=1_000, wall_elapsed_ms=1_004)[1] is None
    assert _verdict(elapsed_ms=1_000, wall_elapsed_ms=996)[1] is None
    assert _verdict(elapsed_ms=MAX_AGE, wall_elapsed_ms=MAX_AGE)[1] is None
    assert _verdict(offset_ms=SKEW)[1] is None
    assert _verdict(min_rtt_ms=0, max_rtt_ms=0)[1] is None


def test_the_auditor_counterexamples_no_longer_count_a_started_minute() -> None:
    """Review F-1. Drift: a round 839 s old, the local clocks 50 ppm slow (exchange time 41 ms
    ahead of the round's offset), U = 23 ms, a snapshot at 08:00:59.977 local: before the fix the
    anchor was 08:01:00.000 though exchange time was 08:01:00.018. Step: the wall clock stepped
    300 ms back after the round."""
    open_ = 1_762_156_860_000  # 2025-11-03T08:01:00Z
    snapshot = open_ - 23
    v = observation_bound(
        _evidence(
            round_at_ms=snapshot - 839_000,
            max_rtt_ms=40,
            min_rtt_ms=40,
            round_span_ms=200,
            elapsed_ms=839_000,
            wall_elapsed_ms=839_000,
        ),
        max_age_ms=MAX_AGE,
        skew_max_ms=SKEW,
    )
    assert observation_anchor_ms(snapshot, 0, 40) == open_  # the old anchor
    assert snapshot + 41 > open_  # exchange time at the snapshot: minute A had started
    assert v.anchor_ms == open_ + M
    assert v.details["drift_ms"] == drift_allowance_ms(200 + 839_000 + 2) == 420
    # the step: the wall clock was 300 ms ahead of exchange at the round (offset -300) and the OS
    # stepped it back 300 ms; the snapshot reads 08:08:00.200, which is exchange time now. The old
    # anchor was 08:08:00.000; the monotonic clock elapsed 300 ms more than the wall clock, so the
    # bound follows it.
    snap = 1_762_157_280_200  # 2025-11-03T08:08:00.200Z
    elapsed = 480_000  # within the tolerance (484 ms): a 300 ms step is not "detected" here
    step = observation_bound(
        _evidence(
            round_at_ms=snap + 300 - elapsed,
            offset_ms=-300,
            max_rtt_ms=20,
            min_rtt_ms=20,
            elapsed_ms=elapsed,
            wall_elapsed_ms=elapsed - 300,
        ),
        max_age_ms=MAX_AGE,
        skew_max_ms=SKEW,
    )
    assert observation_anchor_ms(snap, -300, 20) == snap - 200  # the old anchor: started
    assert step.anchor_ms == snap - 200 + M
    big = observation_bound(  # a 2 s step is beyond the tolerance: no anchor
        _evidence(
            offset_ms=-300, max_rtt_ms=20, elapsed_ms=elapsed, wall_elapsed_ms=elapsed - 2_000
        ),
        max_age_ms=MAX_AGE,
        skew_max_ms=SKEW,
    )
    assert (big.anchor_ms, big.code) == (None, "clock_discontinuity")


# ---------------------------------------------------------------- the monitor's evidence
def test_a_round_records_its_monotonic_evidence() -> None:
    wall, mono = Reading(), Reading()
    mon = ClockMonitor(wall, _never, lambda _e: None, samples_per_round=3, monotonic=mono)
    samples = [
        ClockSample(1_000, 1_010, 1_005, 501, 511),
        ClockSample(1_020, 1_060, 1_041, 521, 561),
        ClockSample(1_070, 1_072, 1_071, 571, 573),
    ]
    wall.value, mono.value = 1_080, 581
    rnd = mon.record(samples)
    assert (rnd.min_rtt_ms, rnd.max_rtt_ms) == (2, 40)
    assert (rnd.mono_at_ms, rnd.mono_span_ms, rnd.step_ms) == (581, 80, 0)
    wall.value, mono.value = 1_380, 881
    assert mon.monotonic_ms() == 881


def test_a_wall_step_during_the_round_shows_as_its_step() -> None:
    wall, mono = Reading(), Reading()
    mon = ClockMonitor(wall, _never, lambda _e: None, samples_per_round=2, monotonic=mono)
    # the wall clock steps 250 ms back between the two samples
    samples = [ClockSample(1_000, 1_010, 1_005, 0, 10), ClockSample(770, 780, 1_025, 20, 30)]
    wall.value, mono.value = 790, 40
    rnd = mon.record(samples)
    assert rnd.step_ms == 250
    assert rnd.mono_span_ms == 40


def test_without_complete_monotonic_readings_a_round_has_no_monotonic_evidence() -> None:
    mon = ClockMonitor(Reading(), _never, lambda _e: None, samples_per_round=1)
    rnd = mon.record([ClockSample(0, 4, 2)])
    assert (rnd.mono_at_ms, rnd.mono_span_ms, rnd.step_ms) == (None, None, None)
    assert mon.monotonic_ms() is None
    mono = Reading()
    mon2 = ClockMonitor(Reading(), _never, lambda _e: None, samples_per_round=2, monotonic=mono)
    rnd2 = mon2.record([ClockSample(0, 4, 2, 0, 4), ClockSample(5, 9, 7)])  # one sample without
    assert (rnd2.mono_at_ms, rnd2.mono_span_ms, rnd2.step_ms) == (None, None, None)


def test_the_monotonic_clock_never_goes_back() -> None:
    clock = MonotonicClock()
    readings = [clock.now_ms() for _ in range(1_000)]
    assert readings == sorted(readings)


# ---------------------------------------------------------------- soundness on a true timeline
@dataclass
class Clocks:
    """True time is exchange time. Monotonic: rate 1 + mono_rate, optionally paused (a suspend).
    Wall: rate 1 + wall_rate, plus steps at given true instants."""

    mono_origin: float
    mono_rate: float
    wall_origin: float
    wall_rate: float
    steps: list[tuple[float, float]]  # (true instant, step in ms)
    pause: tuple[float, float] | None = None  # true instants the monotonic clock stops between

    def mono(self, t: float) -> float:
        run = t
        if self.pause is not None:
            a, b = self.pause
            run = t - max(0.0, min(t, b) - a)
        return self.mono_origin + (1 + self.mono_rate) * run

    def wall(self, t: float) -> float:
        return (
            self.wall_origin
            + (1 + self.wall_rate) * t
            + sum(size for at, size in self.steps if t >= at)
        )


def _round_on(clocks: Clocks, rng: random.Random, samples: int) -> tuple[ClockRound, float]:
    wall, mono = Reading(), Reading()
    mon = ClockMonitor(wall, _never, lambda _e: None, samples_per_round=samples, monotonic=mono)
    t = rng.uniform(0.0, 5.0)
    readings = []
    for _ in range(samples):
        send = t + rng.uniform(0.0, 20.0)
        recv = send + rng.choice([0.0, 0.3, 1.0, rng.uniform(0.0, 300.0)])
        stamp = rng.uniform(send, recv)  # the server reads its clock while the request is in flight
        readings.append(
            ClockSample(
                math.floor(clocks.wall(send)),
                math.floor(clocks.wall(recv)),
                math.floor(stamp),  # exchange time
                math.floor(clocks.mono(send)),
                math.floor(clocks.mono(recv)),
            )
        )
        t = recv
    end = t + rng.uniform(0.0, 2.0)
    wall.value, mono.value = math.floor(clocks.wall(end)), math.floor(clocks.mono(end))
    return mon.record(readings), end


def _snapshot_evidence(clocks: Clocks, rnd: ClockRound, at: float) -> ClockEvidence:
    assert rnd.mono_at_ms is not None
    assert rnd.mono_span_ms is not None
    assert rnd.step_ms is not None
    return ClockEvidence(
        round_at_ms=rnd.at_ms,
        offset_ms=rnd.offset_ms,
        max_rtt_ms=rnd.max_rtt_ms,
        min_rtt_ms=rnd.min_rtt_ms,
        samples=rnd.samples,
        round_span_ms=rnd.mono_span_ms,
        round_step_ms=rnd.step_ms,
        elapsed_ms=math.floor(clocks.mono(at)) - rnd.mono_at_ms,
        wall_elapsed_ms=math.floor(clocks.wall(at)) - rnd.at_ms,
    )


def _upper(e: ClockEvidence, details: dict[str, int]) -> int:
    elapsed = max(e.elapsed_ms, e.wall_elapsed_ms)
    assert details["allowance_ms"] == 1 + details["drift_ms"] + details["step_ms"]
    return (
        e.round_at_ms + elapsed + e.offset_ms + details["uncertainty_ms"] + details["allowance_ms"]
    )


R = CLOCK_DRIFT_PPM / 1_000_000
SCENARIOS = ["drift", "independent rates", "step back", "step forward", "step in round", "suspend"]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_an_anchor_is_never_set_inside_a_minute_that_had_started(scenario: str) -> None:
    """For every simulated round and snapshot (1 to 5 samples, round trips up to 300 ms, the server
    stamp anywhere in flight, snapshots up to 900 s later, every reading floored): either the
    evidence gives no anchor, or exchange time at the snapshot lies strictly before the bound, so
    no minute that started before the snapshot counts as observed. Wall-clock discontinuities
    beyond the tolerance always give no anchor. The pre-hardening bound (wall clock at the
    snapshot, no drift or step allowance) is shown to miss some of these cases."""
    rng = random.Random(f"p1-hardening-{scenario}")
    accepted = blocked = old_misses = 0
    for _ in range(3_000):
        samples = rng.randint(1, 5)
        mono_rate = rng.uniform(-R, R)
        wall_rate = mono_rate if scenario == "drift" else rng.uniform(-R, R)
        offset = rng.uniform(-900.0, 900.0)  # wall - exchange at true time 0
        clocks = Clocks(rng.uniform(0.0, 1e6), mono_rate, offset, wall_rate, [])
        at = rng.uniform(0.0, 900_000.0)  # the snapshot, relative to the round
        size = rng.choice([1.0, 3.0, 10.0, 300.0, 2_000.0, rng.uniform(1.0, 5_000.0)])
        if scenario == "step back":
            clocks.steps = [(rng.uniform(400.0, 400.0 + at), -size)]
        elif scenario == "step forward":
            clocks.steps = [(rng.uniform(400.0, 400.0 + at), size)]
        elif scenario == "step in round":
            clocks.steps = [(rng.uniform(0.0, 300.0), rng.choice([-size, size]))]
        elif scenario == "suspend":
            begin = rng.uniform(400.0, 400.0 + at)
            clocks.pause = (begin, begin + size)
        rnd, end = _round_on(clocks, rng, samples)
        snap_at = end + at
        e = _snapshot_evidence(clocks, rnd, snap_at)
        v = observation_bound(e, max_age_ms=MAX_AGE, skew_max_ms=SKEW)
        moved = abs(e.wall_elapsed_ms - e.elapsed_ms)
        if v.anchor_ms is None:
            blocked += 1
            continue
        accepted += 1
        assert snap_at < _upper(e, v.details) <= v.anchor_ms, (scenario, e)
        assert v.anchor_ms % M == 0
        assert v.anchor_ms - M < _upper(e, v.details)
        assert moved <= continuity_tolerance_ms(e.elapsed_ms)
        assert e.round_step_ms <= continuity_tolerance_ms(e.round_span_ms)
        wall_at = math.floor(clocks.wall(snap_at))
        old_upper = wall_at + rnd.offset_ms + clock_uncertainty_ms(max(rnd.max_rtt_ms, 0))
        old_misses += snap_at >= old_upper
    assert accepted > 0
    if scenario != "drift":
        assert blocked > 0  # the discontinuities (and fast relative rates) are detected
    if scenario in ("drift", "independent rates", "step back"):
        assert old_misses > 0  # the old bound would have counted a started minute
