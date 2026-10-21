"""Signal lifecycle: contract transitions only, events as the source of truth, projection rebuild,
simulator-event mapping and shadow outcomes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tsclaude.contracts.events import AggregateType, EventType
from tsclaude.contracts.signals import ALLOWED_TRANSITIONS, SignalStatus
from tsclaude.signals.lifecycle import LifecycleError, SignalLifecycle

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sim"))
from paper_helpers import record, sid

S = SignalStatus


def test_create_and_transition_emit_contract_events() -> None:
    lc = SignalLifecycle()
    created = lc.create(record(1, 1, stage=S.WATCH), 100)
    assert created.type is EventType.SIGNAL_CREATED
    assert created.aggregate_type is AggregateType.SIGNAL
    assert created.aggregate_id == sid(1)
    t = lc.transition(sid(1), S.CONFIRMED, 200, "entry_rule_met")
    assert t.type is EventType.SIGNAL_TRANSITIONED
    assert t.payload == {
        "signal_id": sid(1),
        "from": "WATCH",
        "to": "CONFIRMED",
        "reason": "entry_rule_met",
        "book": None,
        "outcome": None,
    }
    assert lc.status(sid(1)) is S.CONFIRMED


@pytest.mark.parametrize("source", list(SignalStatus))
@pytest.mark.parametrize("target", list(SignalStatus))
def test_only_contract_transitions_are_possible(source: SignalStatus, target: SignalStatus) -> None:
    """Drive a signal to `source` through allowed moves, then try `target`."""
    paths = {
        S.WATCH: [],
        S.CONFIRMED: [S.CONFIRMED],
        S.OPEN: [S.CONFIRMED, S.OPEN],
        S.CLOSED: [S.CONFIRMED, S.OPEN, S.CLOSED],
        S.VETOED: [S.CONFIRMED, S.VETOED],
        S.EXPIRED: [S.EXPIRED],
        S.INVALIDATED: [S.INVALIDATED],
    }
    lc = SignalLifecycle()
    lc.create(record(1, 1, stage=S.WATCH), 0)
    for step in paths[source]:
        lc.transition(sid(1), step, 1)
    assert lc.status(sid(1)) is source
    if target in ALLOWED_TRANSITIONS[source]:
        lc.transition(sid(1), target, 2)
        assert lc.status(sid(1)) is target
    else:
        with pytest.raises(LifecycleError, match="not allowed"):
            lc.transition(sid(1), target, 2)
        assert lc.try_transition(sid(1), target, 2) is None
        assert lc.status(sid(1)) is source


def test_duplicate_and_unknown_signals_are_rejected() -> None:
    lc = SignalLifecycle()
    lc.create(record(1, 1), 0)
    with pytest.raises(LifecycleError, match="already exists"):
        lc.create(record(1, 1), 0)
    with pytest.raises(LifecycleError, match="unknown"):
        lc.transition(sid(2), S.OPEN, 0)


def test_rebuild_from_events_equals_live_projection_and_rejects_bad_history() -> None:
    lc = SignalLifecycle()
    events = [
        lc.create(record(1, 1), 0),
        lc.transition(sid(1), S.OPEN, 60_000, "entry_filled", book="baseline"),
        lc.transition(sid(1), S.CLOSED, 120_000, "take_profit", outcome={"r_multiple": "2"}),
        lc.create(record(2, 2, stage=S.WATCH), 0),
    ]
    rebuilt = SignalLifecycle.rebuild(events)
    assert [(s.signal_id, s.status, s.outcome) for s in rebuilt.signals()] == [
        (s.signal_id, s.status, s.outcome) for s in lc.signals()
    ]
    bad = SignalLifecycle()
    bad.create(record(1, 1), 0)
    with pytest.raises(LifecycleError, match="invalid"):
        bad.apply(events[2])  # CLOSED from OPEN, but the projection is CONFIRMED


def test_expire_due_only_touches_watch_signals_of_that_symbol() -> None:
    lc = SignalLifecycle()
    lc.create(record(1, 1, stage=S.WATCH, valid_bars=2), 0)
    lc.create(record(2, 1, stage=S.WATCH, valid_bars=2, symbol="ETHUSDT"), 0)
    lc.create(record(3, 1, valid_bars=2), 0)  # CONFIRMED: expires through its order
    valid_until = lc.get(sid(1)).valid_until_ms  # type: ignore[union-attr]
    assert lc.expire_due(valid_until - 1, market="spot", symbol="BTCUSDT") == []
    (ev,) = lc.expire_due(valid_until, market="spot", symbol="BTCUSDT", downtime=True)
    assert ev.payload["reason"] == "downtime"
    assert lc.status(sid(1)) is S.EXPIRED
    assert lc.status(sid(2)) is S.WATCH
    assert lc.status(sid(3)) is S.CONFIRMED


def test_sim_event_mapping_and_shadow_outcome() -> None:
    lc = SignalLifecycle()
    for i in (1, 2, 3, 4):
        lc.create(record(i, 1), 0)
    e = lambda kind, **p: {"type": kind, "ts_ms": 5, "payload": p}  # noqa: E731
    closed = {
        "book": "baseline",
        "request_id": sid(1),
        "exit_reason": "stop_loss",
        "r_multiple": "-1.1",
        "resolved_from_backfill": True,
    }
    out = lc.transitions_from_sim(
        [
            e(
                "sim_order_filled",
                book="baseline",
                order_id=f"baseline:{sid(1)}:entry",
                role="entry",
            ),
            e(
                "sim_order_filled",
                book="baseline",
                order_id=f"baseline:{sid(1)}:sl",
                role="stop_loss",
            ),
            e("sim_position_closed", **closed),
            e("sim_request_rejected", book="baseline", request_id=sid(2), reason="risk_blocked:x"),
            e(
                "sim_order_expired",
                book="baseline",
                order_id=f"baseline:{sid(3)}:entry",
                reason="valid_until_passed:downtime",
            ),
            e(
                "sim_order_cancelled",
                book="baseline",
                order_id=f"baseline:{sid(4)}:entry",
                reason="book_halted:drawdown_kill_switch",
            ),
            e(
                "sim_position_closed",
                book="shadow",
                request_id=sid(2),
                exit_reason="take_profit",
                r_multiple="2",
            ),
            e("sim_order_filled", book="shadow", order_id=f"shadow:{sid(3)}:entry", role="entry"),
        ]
    )
    assert [(ev.payload["signal_id"], ev.payload["to"]) for ev in out] == [
        (sid(1), "OPEN"),
        (sid(1), "CLOSED"),
        (sid(2), "VETOED"),
        (sid(3), "EXPIRED"),
        (sid(4), "VETOED"),
    ]
    s1 = lc.get(sid(1))
    assert s1 is not None
    assert s1.outcome is not None
    assert s1.outcome["resolved_from_backfill"] is True
    assert lc.get(sid(3)).reason == "downtime"  # type: ignore[union-attr]
    shadow = lc.shadow_outcome(sid(2))
    assert shadow is not None
    assert shadow["exit_reason"] == "take_profit"
