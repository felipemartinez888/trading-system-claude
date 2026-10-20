"""Session and monitoring lifecycle, crash detection and observation coverage.

Two different things are tracked (DATA_ARCHITECTURE §4):
- an *app session*: the process is running (server up);
- a *monitoring interval*: inside a session, the app is actively observing the market.

Only monitoring intervals count as observation coverage. A crash is detected on the next start:
the previous session's open intervals are closed at its last heartbeat (conservative: up to one
heartbeat interval of real observation may be discarded, never invented).
"""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import dataclass
from enum import StrEnum

from tsclaude.contracts.events import AggregateType, Event, EventType, NewEvent
from tsclaude.contracts.ids import new_ulid
from tsclaude.contracts.timeutil import WallClock
from tsclaude.store.eventstore import EventStore

HEARTBEAT_KEY = "heartbeat"


class SessionError(RuntimeError):
    pass


class MonitoringState(StrEnum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"


@dataclass(frozen=True)
class CrashReport:
    session_id: str
    last_alive_ms: int
    monitoring_was_active: bool


@dataclass(frozen=True)
class ObservationInterval:
    start_ms: int
    end_ms: int | None  # None = still observing
    monitoring_id: str
    end_reason: str | None


class SessionManager:
    def __init__(
        self,
        store: EventStore,
        clock: WallClock,
        *,
        app_version: str,
        git_commit: str | None = None,
        config_hash: str | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._app_version = app_version
        self._git_commit = git_commit
        self._config_hash = config_hash
        self._session_id: str | None = None
        self._monitoring_id: str | None = None
        self._crash: CrashReport | None = None

    # ------------------------------------------------------------------ properties
    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def monitoring_state(self) -> MonitoringState:
        return MonitoringState.RUNNING if self._monitoring_id else MonitoringState.STOPPED

    @property
    def monitoring_id(self) -> str | None:
        return self._monitoring_id

    @property
    def detected_crash(self) -> CrashReport | None:
        """Crash of the previous session found while opening this one, if any."""
        return self._crash

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> str:
        if self._session_id is not None:
            raise SessionError("session already open")
        self._crash = self._detect_unclosed_session()
        now = self._clock.now_ms()
        sid = new_ulid(now)
        self._store.append(
            NewEvent(
                type=EventType.SESSION_STARTED,
                aggregate_type=AggregateType.SESSION,
                aggregate_id=sid,
                ts_ms=now,
                payload={
                    "session_id": sid,
                    "app_version": self._app_version,
                    "git_commit": self._git_commit,
                    "config_hash": self._config_hash,
                    "platform": platform.platform(),
                    "python": sys.version.split()[0],
                },
            )
        )
        self._session_id = sid
        self.heartbeat()
        return sid

    def heartbeat(self) -> None:
        sid = self._require_session()
        self._store.set_state(
            HEARTBEAT_KEY,
            json.dumps({"session_id": sid, "ts_ms": self._clock.now_ms()}),
        )

    def start_monitoring(self) -> str:
        sid = self._require_session()
        if self._monitoring_id is not None:
            raise SessionError("monitoring already running")
        now = self._clock.now_ms()
        mid = new_ulid(now)
        self._store.append(
            NewEvent(
                type=EventType.MONITORING_STARTED,
                aggregate_type=AggregateType.SESSION,
                aggregate_id=sid,
                ts_ms=now,
                payload={"session_id": sid, "monitoring_id": mid},
            )
        )
        self._monitoring_id = mid
        return mid

    def stop_monitoring(self, reason: str = "user") -> None:
        sid = self._require_session()
        if self._monitoring_id is None:
            raise SessionError("monitoring is not running")
        self._store.append(
            NewEvent(
                type=EventType.MONITORING_STOPPED,
                aggregate_type=AggregateType.SESSION,
                aggregate_id=sid,
                ts_ms=self._clock.now_ms(),
                payload={"session_id": sid, "monitoring_id": self._monitoring_id, "reason": reason},
            )
        )
        self._monitoring_id = None

    def close(self, reason: str = "user") -> None:
        sid = self._require_session()
        if self._monitoring_id is not None:
            self.stop_monitoring(reason="session_closing")
        self._store.append(
            NewEvent(
                type=EventType.SESSION_STOPPED,
                aggregate_type=AggregateType.SESSION,
                aggregate_id=sid,
                ts_ms=self._clock.now_ms(),
                payload={"session_id": sid, "reason": reason},
            )
        )
        self._session_id = None

    # ------------------------------------------------------------------ internals
    def _require_session(self) -> str:
        if self._session_id is None:
            raise SessionError("no open session")
        return self._session_id

    def _detect_unclosed_session(self) -> CrashReport | None:
        last_start = self._store.read(types=[EventType.SESSION_STARTED], limit=1, descending=True)
        if not last_start:
            return None
        start = last_start[0]
        sid = str(start.payload["session_id"])
        session_events = self._store.read(
            after_seq=start.seq - 1, aggregate=(AggregateType.SESSION, sid)
        )
        if any(
            e.type in (EventType.SESSION_STOPPED, EventType.SESSION_CRASH_DETECTED)
            for e in session_events
        ):
            return None
        monitoring_active = _monitoring_open(session_events)
        last_alive = max(e.ts_ms for e in session_events)
        hb_raw = self._store.get_state(HEARTBEAT_KEY)
        if hb_raw:
            hb = json.loads(hb_raw)
            if hb.get("session_id") == sid:
                last_alive = max(last_alive, int(hb["ts_ms"]))
        now = self._clock.now_ms()
        self._store.append(
            NewEvent(
                type=EventType.SESSION_CRASH_DETECTED,
                aggregate_type=AggregateType.SESSION,
                aggregate_id=sid,
                ts_ms=now,
                payload={
                    "session_id": sid,
                    "last_alive_ms": last_alive,
                    "monitoring_was_active": monitoring_active is not None,
                    "monitoring_id": monitoring_active,
                    "detected_at_ms": now,
                },
            )
        )
        return CrashReport(sid, last_alive, monitoring_active is not None)


def _monitoring_open(session_events: list[Event]) -> str | None:
    current: str | None = None
    for e in session_events:
        if e.type is EventType.MONITORING_STARTED:
            current = str(e.payload["monitoring_id"])
        elif e.type is EventType.MONITORING_STOPPED:
            current = None
    return current


def observation_intervals(store: EventStore) -> list[ObservationInterval]:
    """Fold session events into monitoring intervals (the only valid live-observation coverage)."""
    events = store.read(
        types=[
            EventType.MONITORING_STARTED,
            EventType.MONITORING_STOPPED,
            EventType.SESSION_CRASH_DETECTED,
        ]
    )
    open_by_id: dict[str, int] = {}
    result: list[ObservationInterval] = []
    for e in events:
        if e.type is EventType.MONITORING_STARTED:
            open_by_id[str(e.payload["monitoring_id"])] = e.ts_ms
        elif e.type is EventType.MONITORING_STOPPED:
            mid = str(e.payload["monitoring_id"])
            start = open_by_id.pop(mid, None)
            if start is not None:
                result.append(ObservationInterval(start, e.ts_ms, mid, str(e.payload["reason"])))
        else:  # crash: close the crashed session's open interval at its last heartbeat
            mid_raw = e.payload.get("monitoring_id")
            if mid_raw is not None:
                mid = str(mid_raw)
                start = open_by_id.pop(mid, None)
                if start is not None:
                    end = max(start, int(e.payload["last_alive_ms"]))
                    result.append(ObservationInterval(start, end, mid, "crash"))
    for mid, start in open_by_id.items():
        result.append(ObservationInterval(start, None, mid, None))
    result.sort(key=lambda iv: iv.start_ms)
    return result
