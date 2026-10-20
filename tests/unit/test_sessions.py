from __future__ import annotations

from pathlib import Path

import pytest

from tsclaude.contracts.events import EventType
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.runtime.sessions import (
    MonitoringState,
    SessionError,
    SessionManager,
    observation_intervals,
)
from tsclaude.store.eventstore import EventStore

T0 = 1_760_000_000_000


def _mgr(store: EventStore, clock: ManualClock) -> SessionManager:
    return SessionManager(store, clock, app_version="test")


def test_clean_session_lifecycle(tmp_path: Path) -> None:
    clock = ManualClock(T0)
    with EventStore(tmp_path / "s.db") as store:
        mgr = _mgr(store, clock)
        mgr.open()
        assert mgr.detected_crash is None
        mgr.start_monitoring()
        assert mgr.monitoring_state is MonitoringState.RUNNING
        clock.advance(60_000)
        mgr.close("user")
        types = [e.type for e in store.read()]
        assert types == [
            EventType.SESSION_STARTED,
            EventType.MONITORING_STARTED,
            EventType.MONITORING_STOPPED,
            EventType.SESSION_STOPPED,
        ]
        ivs = observation_intervals(store)
        assert len(ivs) == 1
        assert (ivs[0].start_ms, ivs[0].end_ms, ivs[0].end_reason) == (
            T0,
            T0 + 60_000,
            "session_closing",
        )
        assert store.verify().ok


def test_invalid_monitoring_transitions(tmp_path: Path) -> None:
    with EventStore(tmp_path / "s.db") as store:
        mgr = _mgr(store, ManualClock(T0))
        with pytest.raises(SessionError):
            mgr.start_monitoring()  # no session
        mgr.open()
        with pytest.raises(SessionError):
            mgr.stop_monitoring()
        mgr.start_monitoring()
        with pytest.raises(SessionError):
            mgr.start_monitoring()


def test_crash_is_detected_and_interval_closed_at_last_heartbeat(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    clock = ManualClock(T0)
    with EventStore(path) as store:
        crashed = _mgr(store, clock)
        sid = crashed.open()
        crashed.start_monitoring()
        clock.advance(30_000)
        crashed.heartbeat()  # last sign of life at T0+30s
        clock.advance(4_000)
        # process dies here: no stop events written

    clock.advance(3_600_000)  # restarted an hour later
    with EventStore(path) as store:
        mgr = _mgr(store, clock)
        mgr.open()
        crash = mgr.detected_crash
        assert crash is not None
        assert crash.session_id == sid
        assert crash.last_alive_ms == T0 + 30_000
        assert crash.monitoring_was_active
        ivs = observation_intervals(store)
        assert [(iv.start_ms, iv.end_ms, iv.end_reason) for iv in ivs] == [
            (T0, T0 + 30_000, "crash")
        ]
        assert mgr.monitoring_state is MonitoringState.STOPPED  # never resumes silently
        assert store.verify().ok


def test_crash_detected_only_once(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    clock = ManualClock(T0)
    with EventStore(path) as store:
        _mgr(store, clock).open()
    with EventStore(path) as store:
        m2 = _mgr(store, clock)
        m2.open()
        assert m2.detected_crash is not None
        m2.close()
    with EventStore(path) as store:
        m3 = _mgr(store, clock)
        m3.open()
        assert m3.detected_crash is None
        assert len(store.read(types=[EventType.SESSION_CRASH_DETECTED])) == 1


def test_open_interval_reported_while_monitoring(tmp_path: Path) -> None:
    with EventStore(tmp_path / "s.db") as store:
        mgr = _mgr(store, ManualClock(T0))
        mgr.open()
        mgr.start_monitoring()
        ivs = observation_intervals(store)
        assert ivs[0].end_ms is None
