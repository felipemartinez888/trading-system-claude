"""Seeded (offline) runs of the paper-trading properties; hypothesis explores them in CI."""

from __future__ import annotations

import pytest
from paper_scenarios import (
    check_live_clock_order,
    check_replay_parity,
    check_restore_equivalence,
    check_risk_engine_fail_closed,
    run_random_session,
)


@pytest.mark.parametrize("seed", range(12))
def test_random_sessions_replay_identically(seed: int) -> None:
    assert check_replay_parity(seed) == []


def test_random_sessions_exercise_the_lifecycle() -> None:
    """The generator is not vacuous: across seeds every terminal path occurs."""
    seen: set[str] = set()
    trades = entries = 0
    for seed in range(12):
        svc, _, _ = run_random_session(seed)
        seen |= {str(s.status) for s in svc.session.lifecycle.signals()}
        trades += len(svc.session.sim.books["baseline"].trades)
        entries += sum(
            1
            for e in svc.session.sim.events
            if e["type"] == "sim_order_filled"
            and e["payload"]["book"] == "baseline"
            and e["payload"]["role"] == "entry"
        )
    assert {"CLOSED", "VETOED", "EXPIRED", "INVALIDATED", "OPEN"} <= seen
    assert entries >= 15
    assert trades >= 5


@pytest.mark.parametrize(("seed", "fail_batch"), [(0, 5), (1, 17), (2, 40), (3, 61), (4, 77)])
def test_random_sessions_restore_after_journal_failure(seed: int, fail_batch: int) -> None:
    assert check_restore_equivalence(seed, fail_batch) == []


@pytest.mark.parametrize("seed", range(20))
def test_live_clock_any_timely_arrival_order(seed: int) -> None:
    assert check_live_clock_order(seed) == []


@pytest.mark.parametrize("seed", range(30))
def test_risk_engine_deterministic_and_fail_closed(seed: int) -> None:
    assert check_risk_engine_fail_closed(seed) == []
