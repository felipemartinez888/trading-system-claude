"""Hypothesis properties for live paper trading (CI; hypothesis is not available offline).

- replay parity: any random live session replays through the historical clock to identical
  ledger hashes and fingerprint, with balanced ledgers and non-negative cash in every book;
- restart safety: a journal failure at any batch, then restore + re-delivery never polled before
  the crash, reproduces the uninterrupted session's journal and ledgers; a restart after downtime
  recovers as the paper runtime does (new subscription, its backfill, unobserved downtime bars)
  and stays causal (`restart_causality`, `recovery_outcome`): no entry fills in a minute the
  downtime overlaps, since the service observes only from the anchor fixed at the restart (P-1,
  docs/tasks/M3-PB.md);
- recovery outcomes (OD-2, `restart_outcome`): every restart is RECOVERED (the requirements above,
  unchanged), SAFE_BLOCK (blocked, with the oracle's proof from the ground truth that no store
  holds the evidence a resume needs, and the safety invariants S1-S7) or FAIL; the properties fail
  on any FAIL, and an instant restart may block only on a genuinely empty minute. Each case
  reports its outcome as a hypothesis event (`--hypothesis-show-statistics`) and in the end-of-run
  summary (`tests/conftest.py`, explicit examples included). A SAFE_BLOCK is not
  recovery, equivalence or availability;
- live clock: any arrival order within the settle window releases the historical clock order;
- risk engine: deterministic, never passes with a missing market input or a view after cutoff.

The checks live in `tests/unit/sim/paper_scenarios.py` (seeded offline runs use them too).
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "unit" / "sim"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "unit" / "risk"))
from paper_scenarios import (
    EMPTY_MINUTE,
    FAIL,
    RECOVERED,
    SAFE_BLOCK,
    check_live_clock_order,
    check_replay_parity,
    check_restore_equivalence,
    check_risk_engine_fail_closed,
    report_outcome,
    restart_outcome,
)

SEEDS = st.integers(min_value=0, max_value=10**9)
SETTINGS = settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@SETTINGS
@given(seed=SEEDS)
def test_replay_parity_for_any_session(seed: int) -> None:
    assert check_replay_parity(seed) == []


FAIL_BATCHES = st.integers(min_value=0, max_value=200)


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed=SEEDS, fail_batch=FAIL_BATCHES)
@example(seed=13073, fail_batch=124)  # diverged under the pre-crash timeline (audit F1)
@example(seed=465923499, fail_batch=162)  # likewise
@example(seed=130741648, fail_batch=8)  # empty minute (OD-1): SAFE_BLOCK, not recovery
@example(seed=50553820, fail_batch=135)  # likewise
def test_restore_after_any_journal_failure(seed: int, fail_batch: int) -> None:
    out = restart_outcome(seed, fail_batch)
    report_outcome("instant restart property", out)
    event(f"instant restart: {out.kind}{'' if out.reason is None else ' ' + out.reason}")
    assert out.kind != FAIL, out.problems
    if out.kind == RECOVERED:
        assert check_restore_equivalence(seed, fail_batch) == []
    else:
        assert (out.kind, out.reason) == (SAFE_BLOCK, EMPTY_MINUTE), out


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed=SEEDS, fail_batch=FAIL_BATCHES, delay_ms=st.integers(min_value=1, max_value=600_000))
@example(seed=0, fail_batch=1, delay_ms=58_502)  # P-1 at an open during the downtime (shrunk)
@example(seed=0, fail_batch=2, delay_ms=1)  # P-1 in the minute the crash interrupted (shrunk)
@example(seed=696831125, fail_batch=13, delay_ms=180_000)  # P-1, the partly observed minute
@example(seed=32438366, fail_batch=129, delay_ms=24_837)  # P-1 intrabar (RR-B1)
@example(seed=130741648, fail_batch=6, delay_ms=60_000)  # empty minute in the downtime
def test_delayed_restart_stays_causal(seed: int, fail_batch: int, delay_ms: int) -> None:
    assert check_restore_equivalence(seed, fail_batch, delay_ms) == []
    out = restart_outcome(seed, fail_batch, delay_ms)
    report_outcome("delayed restart property", out)
    event(f"delayed restart: {out.kind}{'' if out.reason is None else ' ' + out.reason}")
    assert out.kind in (RECOVERED, SAFE_BLOCK), out.problems


@settings(max_examples=100, deadline=None)
@given(seed=SEEDS, settle_ms=st.integers(min_value=1, max_value=120_000))
def test_live_clock_releases_historical_order(seed: int, settle_ms: int) -> None:
    assert check_live_clock_order(seed, settle_ms) == []


@settings(max_examples=200, deadline=None)
@given(seed=SEEDS)
def test_risk_engine_fail_closed(seed: int) -> None:
    assert check_risk_engine_fail_closed(seed) == []
