"""P-1: the paper service's observation boundary (L-02 as the owner clarified it for paper).

A bar counts as observed only if its source qualifies (live WebSocket) and it opens at or after the
observation anchor, the first minute open after the runtime's first poll in exchange time, bounded
with the measured server-clock offset and its uncertainty. Gated without an anchor, nothing is
observed; ungated (direct callers, the instant-restart oracle) observation stays continuous.
"""

from __future__ import annotations

import math
import random

import pytest
from paper_helpers import drive, new_service
from paper_scenarios import random_bars, random_script, redeliver, restore_after_crash

from tsclaude.contracts.market import Bar, BarSource
from tsclaude.contracts.timeutil import MS_PER_MINUTE, ManualClock
from tsclaude.quality.clock import ClockMonitor, ClockSample
from tsclaude.sim.paper import (
    PaperTradingService,
    clock_uncertainty_ms,
    observation_anchor_ms,
)
from tsclaude.sim.testing import bar, minute

M = MS_PER_MINUTE


# ---------------------------------------------------------------- the anchor
def test_the_uncertainty_is_half_the_largest_round_trip_plus_the_reading_resolution() -> None:
    assert [clock_uncertainty_ms(r) for r in (0, 1, 2, 3, 40, 41)] == [3, 3, 4, 4, 23, 23]
    with pytest.raises(ValueError, match="max_rtt_ms"):
        clock_uncertainty_ms(-1)


def test_the_anchor_is_the_first_minute_open_at_or_after_the_upper_bound() -> None:
    t = minute(10)
    # an exact clock (offset 0, no round trip): the bound is the reading plus 3 ms
    assert observation_anchor_ms(t - 3, 0, 0) == t  # the bound exactly at an open: that minute
    assert observation_anchor_ms(t - 4, 0, 0) == t  # 1 ms before it
    assert observation_anchor_ms(t - 2, 0, 0) == t + M  # 1 ms after it: partly observed
    assert observation_anchor_ms(t + 30_000, 0, 0) == t + M  # mid-candle: the next open
    # the offset converts local time to exchange time, in both directions
    assert observation_anchor_ms(t - 500, 900, 0) == t + M  # local behind exchange
    assert observation_anchor_ms(t - 500, 0, 0) == t  # what ignoring that offset would give
    assert observation_anchor_ms(t + 500, -900, 0) == t  # local ahead of exchange
    # the round trip widens the bound: 10 ms before an open with a 40 ms round trip
    assert observation_anchor_ms(t - 10, 0, 40) == t + M
    assert observation_anchor_ms(t - 10, 0, 0) == t


@pytest.mark.parametrize("samples", [1, 2, 3, 4, 5])
def test_the_anchor_never_counts_a_minute_the_observation_started_inside(samples: int) -> None:
    """Soundness of the bound under the measurement contract of `ClockMonitor`: each server
    reading is taken between the local send and receive instants, every reading is a floored
    millisecond, and the true offset is constant. For any such round (any round trips, the
    server stamp anywhere inside each, odd and even sample counts) and any poll instant, the
    exchange time of the poll lies before the anchor, so no minute opening before the poll
    counts."""
    rng = random.Random(20261103 + samples)
    worst = math.inf
    for _ in range(4_000):
        offset = rng.uniform(-2_000.0, 2_000.0)
        local = rng.uniform(1.7e12, 1.8e12)
        readings = []
        for _ in range(samples):
            send = local + rng.uniform(0.0, 50.0)
            recv = send + rng.choice([0.0, 0.4, 1.0, rng.uniform(0.0, 400.0)])
            stamp = rng.uniform(send, recv)  # the server reads its clock inside the round trip
            readings.append(
                ClockSample(math.floor(send), math.floor(recv), math.floor(stamp + offset))
            )
            local = recv
        mon = ClockMonitor(ManualClock(0), _never, lambda _e: None, samples_per_round=samples)
        rnd = mon.record(readings)
        poll = local + rng.uniform(0.0, 900_000.0)  # any poll while the round is fresh
        exchange_at_poll = poll + offset
        upper = math.floor(poll) + rnd.offset_ms + clock_uncertainty_ms(rnd.max_rtt_ms)
        anchor = observation_anchor_ms(math.floor(poll), rnd.offset_ms, rnd.max_rtt_ms)
        assert exchange_at_poll < upper <= anchor
        assert anchor % M == 0
        assert anchor - M < upper
        worst = min(worst, upper - exchange_at_poll)
    assert worst > 0


async def _never() -> int:
    raise AssertionError("not called")


def test_a_round_keeps_its_largest_round_trip() -> None:
    mon = ClockMonitor(ManualClock(0), _never, lambda _e: None, samples_per_round=3)
    rnd = mon.record([ClockSample(0, 2, 1), ClockSample(10, 110, 60), ClockSample(200, 204, 202)])
    assert (rnd.median_rtt_ms, rnd.max_rtt_ms, rnd.offset_ms) == (4, 100, 0)


# ---------------------------------------------------------------- the gate
def _bar(i: int, source: BarSource = BarSource.WS_LIVE) -> Bar:
    return bar(i, 100, 101, 99, 100).model_copy(update={"source": source})


def test_an_ungated_service_keeps_continuous_observation() -> None:
    svc, _ = new_service()
    assert svc.observation_gated is False
    assert svc._observed(_bar(3)) is True
    assert svc._observed(_bar(3, BarSource.REST_RECOVERED)) is False
    assert svc.status()["observation"] == {"gated": False, "observing_from_ms": None}


def test_a_gated_service_observes_nothing_before_its_anchor_is_set() -> None:
    svc, _ = new_service(observation_gated=True)
    assert svc._observed(_bar(3)) is False
    svc.observe_from(minute(4))
    assert svc._observed(_bar(3)) is False  # opens before the anchor
    assert svc._observed(_bar(4)) is True  # opens at it
    assert svc._observed(_bar(5)) is True
    assert svc._observed(_bar(4, BarSource.REST_RECOVERED)) is False  # the source rule stays
    assert svc.status()["observation"] == {"gated": True, "observing_from_ms": minute(4)}


def test_the_anchor_is_a_minute_open_set_once() -> None:
    svc, _ = new_service()
    with pytest.raises(ValueError, match="minute open"):
        svc.observe_from(minute(4) + 1)
    svc.observe_from(minute(4))
    assert svc.observation_gated is True  # setting an anchor gates an ungated service
    svc.observe_from(minute(4))  # the same anchor again is harmless
    with pytest.raises(ValueError, match="already set"):
        svc.observe_from(minute(5))
    assert svc.observing_from_ms == minute(4)


def _gated_session(seed: int, anchor_minute: int | None) -> tuple[PaperTradingService, list]:
    rng = random.Random(seed)
    bars = random_bars(rng, 40)
    strat = random_script(rng, bars, 40)
    svc, journal = new_service(strat, observation_gated=True)
    if anchor_minute is not None:
        svc.observe_from(minute(anchor_minute))
    drive(svc, bars, list(range(40)))
    return svc, journal.batches()


@pytest.mark.parametrize("seed", [0, 7, 13073, 465923499, 696831125])
def test_no_book_fills_or_decides_before_the_anchor(seed: int) -> None:
    """Every book (benchmark, baseline, shadow) follows the same boundary: bars opening before the
    anchor are journaled unobserved, no entry fills and no decision is made on them, and the
    strategy is never called for one; from the anchor on the session observes and trades."""
    svc, batches = _gated_session(seed, 12)
    strat = svc.session.source.strategy
    assert strat is not None
    flags = {}
    for b in batches:
        head = b[0].payload
        if head.get("kind") != "bar":
            continue
        at = head["bar"]["open_time_ms"]
        flags[at] = head["observed"]
        for e in b[1:]:
            if at < minute(12):
                assert e.type not in ("signal_created", "sim_order_submitted")
                assert not (e.type == "sim_order_filled" and e.payload["role"] == "entry")
    assert all(flags[m] is (m >= minute(12)) for m in flags)
    assert all(at >= minute(12) for at, _ in strat.calls)  # type: ignore[attr-defined]
    assert any(
        e.type == "sim_order_filled" and e.payload["role"] == "entry"
        for b in batches
        for e in b[1:]
    )  # the scenario trades after the anchor


def test_without_an_anchor_nothing_is_observed_but_nothing_is_lost() -> None:
    """Fail closed: a gated service whose anchor was never set processes every bar unobserved
    (exits still resolve; no decision, no entry fill) and stays RUNNING."""
    svc, batches = _gated_session(7, None)
    bars_ = [b for b in batches if b[0].payload.get("kind") == "bar"]
    assert len(bars_) == 80
    assert all(b[0].payload["observed"] is False for b in bars_)
    assert not [
        e
        for b in batches
        for e in b[1:]
        if e.type in ("signal_created", "sim_order_submitted")
        or (e.type == "sim_order_filled" and e.payload["role"] == "entry")
    ]
    assert svc.session.source.strategy.calls == []  # type: ignore[union-attr]
    assert str(svc.state) == "RUNNING"


@pytest.mark.parametrize("gated", [True, False])
def test_a_journal_restores_identically_with_or_without_the_gate(gated: bool) -> None:
    """The observed flag is journaled per bar and replay uses the recorded value: a journal written
    with the gate restores through the verified replay to the same ledgers, and so does one
    written without it (legacy journals). The gate belongs to a run and is not restored."""
    rng = random.Random(13073)
    bars = random_bars(rng, 40)
    svc, journal = new_service(random_script(rng, bars, 40), observation_gated=gated)
    if gated:
        svc.observe_from(minute(12))
    drive(svc, bars, list(range(40)))
    restored = PaperTradingService.restore(svc.config, journal)
    assert restored.ledger_hashes() == svc.ledger_hashes()
    assert restored.observation_gated is False
    observed = [
        b[0].payload["observed"] for b in journal.batches() if b[0].payload.get("kind") == "bar"
    ]
    assert (False in observed) is gated


def test_the_instant_restart_oracle_stays_ungated() -> None:
    """Delay 0 models continuous observation, an idealized determinism check: the re-delivery never
    sets an anchor, so it is not judged against the production boundary."""
    r = restore_after_crash(13073, 124)
    assert not isinstance(r, str)
    redeliver(r)
    assert r.restored.observation_gated is False
    assert r.restored.observing_from_ms is None
