"""Operator hints for paper blockers (MVP): actionable, presentation only, never a pass."""

from __future__ import annotations

import pytest

from tsclaude.runtime.operator_hints import (
    CLOCK_SYNC_STEPS,
    UNOBSERVED,
    observation_hint,
    paper_hint,
)


def _blocked(code: str | None, problem: str = "why") -> dict[str, object]:
    return {"state": "blocked", "code": code, "problem": problem, "observing_from_ms": None}


def test_the_owner_clock_case_says_how_to_sync_and_that_it_fails_closed() -> None:
    """2026-11-09 Paper O-1: offset -3409 ms beyond 1000 ms -> every bar processed unobserved."""
    snap = {
        "state": "RUNNING",
        "observation": _blocked("clock_offset_skew", "server clock offset -3409 ms beyond 1000 ms"),
    }
    hint = paper_hint(snap)
    assert hint is not None
    assert CLOCK_SYNC_STEPS in hint
    assert "Sync now" in hint
    assert "no administrator rights needed" in hint
    assert hint.endswith(UNOBSERVED)


@pytest.mark.parametrize(
    "code",
    [
        "clock_resync_failed",
        "clock_resync_unavailable",
        "clock_round_stale",
        "clock_round_not_fresh",
        "clock_offset_unknown",
        "clock_rtt_unknown",
        "clock_discontinuity",
        "clock_step_in_round",
        "clock_rtt_negative",
        "clock_continuity_unknown",
        "clock_continuity_other_round",
        "clock_rtt_other_round",
        "clock_evidence_inconsistent",
        "clock_round_future",
        "no_snapshot",
        "a_code_added_later",
        None,
    ],
)
def test_every_blocked_code_gets_a_hint_that_keeps_the_block(code: str | None) -> None:
    hint = observation_hint(_blocked(code))
    assert hint is not None
    assert hint.endswith(UNOBSERVED)
    if code == "a_code_added_later":
        assert "(a_code_added_later)" in hint


@pytest.mark.parametrize(
    "observation",
    [
        None,
        {},
        {"state": "active", "code": None, "observing_from_ms": 1},
        _blocked("clock_resync_pending"),  # transient by design: nothing to do yet
    ],
)
def test_no_hint_when_nothing_is_blocked_or_the_block_is_transient(
    observation: dict[str, object] | None,
) -> None:
    assert observation_hint(observation) is None
    assert paper_hint({"state": "RUNNING", "observation": observation}) is None


def test_a_session_recorded_with_another_configuration_gets_a_recovery_hint() -> None:
    """Operational-readiness review R-04 (task T3): the reason names the changed fields; the hint
    says how to recover, without suggesting any change to the recorded session."""
    reason = (
        "paper session could not be opened: ConfigurationMismatch: the session was recorded "
        "with a different paper configuration (decision_latency_ms: 0 -> 3000)"
    )
    hint = paper_hint({"state": "UNAVAILABLE", "reason": reason})
    assert hint is not None
    assert "TSC_CONFIG_DIR" in hint
    # the in-app recovery documented in MVP_USER_GUIDE §3 (works in the same data folder)
    assert "Record new paper session" in hint
    assert "press Stop" in hint
    # a command the owner can paste as written (every doc runs the CLI through uv)
    assert "uv run tsclaude verify-replay" in hint
    assert "--evidence" in hint
    assert "nothing was opened, changed or traded" in hint
    # other UNAVAILABLE reasons do not get it
    other = paper_hint({"state": "UNAVAILABLE", "reason": "paper session could not be opened: x"})
    assert other is None or "TSC_CONFIG_DIR" not in other


def test_state_hints() -> None:
    incomplete = paper_hint(
        {
            "state": "UNAVAILABLE",
            "reason": "market-data startup backfill: the required history window is not complete "
            "(state: paused)",
        }
    )
    assert incomplete is not None
    assert "Stop, then Start again in the same data directory" in incomplete
    assert "never opens on a partial history" in incomplete
    awaiting = paper_hint({"state": "AWAITING_BACKFILL"})
    assert awaiting is not None
    assert "nothing is traded" in awaiting
    for state in ("DISABLED", "IDLE", "STARTING", "FAULTED", "UNAVAILABLE"):
        assert paper_hint({"state": state, "reason": "something else"}) is None
    # a blocked observation of a run that is not RUNNING is not the thing to fix first
    assert paper_hint({"state": "FAULTED", "observation": _blocked("clock_offset_skew")}) is None
