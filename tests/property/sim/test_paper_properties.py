"""Hypothesis properties for live paper trading (CI; hypothesis is not available offline).

- replay parity: any random live session replays through the historical clock to identical
  ledger hashes and fingerprint, with balanced ledgers and non-negative cash in every book;
- restart safety: a journal failure at any batch, then restore + re-delivery, reproduces the
  uninterrupted session's ledgers;
- live clock: any arrival order within the settle window releases the historical clock order;
- risk engine: deterministic, never passes with a missing market input or a view after cutoff.

The checks live in `tests/unit/sim/paper_scenarios.py` (seeded offline runs use them too).
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "unit" / "sim"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "unit" / "risk"))
from paper_scenarios import (
    check_live_clock_order,
    check_replay_parity,
    check_restore_equivalence,
    check_risk_engine_fail_closed,
)

SEEDS = st.integers(min_value=0, max_value=10**9)
SETTINGS = settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@SETTINGS
@given(seed=SEEDS)
def test_replay_parity_for_any_session(seed: int) -> None:
    assert check_replay_parity(seed) == []


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed=SEEDS, fail_batch=st.integers(min_value=0, max_value=200))
def test_restore_after_any_journal_failure(seed: int, fail_batch: int) -> None:
    assert check_restore_equivalence(seed, fail_batch) == []


@settings(max_examples=100, deadline=None)
@given(seed=SEEDS, settle_ms=st.integers(min_value=1, max_value=120_000))
def test_live_clock_releases_historical_order(seed: int, settle_ms: int) -> None:
    assert check_live_clock_order(seed, settle_ms) == []


@settings(max_examples=200, deadline=None)
@given(seed=SEEDS)
def test_risk_engine_fail_closed(seed: int) -> None:
    assert check_risk_engine_fail_closed(seed) == []
