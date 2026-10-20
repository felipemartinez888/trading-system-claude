"""`AppContext`: the single owner of the runtime state exposed by the API.

It wraps the event store and the `SessionManager`, remembers the chain-verification result taken at
startup, serializes all state transitions behind one lock (FastAPI runs sync endpoints in a thread
pool), and publishes a status snapshot to WebSocket clients on every change and every heartbeat.

Fail-closed rules implemented here:
- if the chain failed verification at startup, monitoring cannot be started (HTTP 423);
- if the session could not be opened, monitoring cannot be started and the status says why;
- a heartbeat older than `STALE_HEARTBEATS` intervals is reported `STALE`, never `SYSTEM`.

This module deliberately does not import FastAPI, so it is unit-testable everywhere.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from tsclaude.api.live import LiveHub, LiveMessage
from tsclaude.api.schemas import (
    ChainInfo,
    CoverageGap,
    CoverageInterval,
    CoverageResponse,
    EventOut,
    EventPage,
    EventsResponse,
    MonitoringInfo,
    PreviousSessionInfo,
    QuitResponse,
    SessionInfo,
    ShutdownInfo,
    StatusResponse,
)
from tsclaude.contracts.events import AggregateType, Event, EventType
from tsclaude.contracts.ids import ulid_timestamp_ms
from tsclaude.contracts.provenance import Provenance, Sourced
from tsclaude.contracts.timeutil import WallClock, to_iso
from tsclaude.runtime.sessions import (
    MonitoringState,
    ObservationInterval,
    SessionError,
    SessionManager,
    observation_intervals,
)
from tsclaude.store.eventstore import ChainReport, EventStore

log = logging.getLogger(__name__)

STALE_HEARTBEATS = 3  # heartbeat older than this many intervals -> STALE
STREAMS_NOT_IMPLEMENTED = "not implemented until M1"
STREAM_GROUPS = ("spot", "futures")
MAX_EVENTS_LIMIT = 500


class TransitionRefused(StrEnum):
    """Why a monitoring transition was refused; mapped to HTTP status codes by the app."""

    INVALID = "invalid"  # 409: not allowed from the current state
    LOCKED = "locked"  # 423: blocked by a failed integrity check or a missing session


class TransitionError(RuntimeError):
    def __init__(self, kind: TransitionRefused, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class ChainCheck:
    report: ChainReport
    verified_at_ms: int


def new_token() -> str:
    """Per-launch random token required on mutating requests and on the WebSocket."""
    return secrets.token_urlsafe(32)


class AppContext:
    def __init__(
        self,
        *,
        store: EventStore,
        clock: WallClock,
        app_version: str,
        port: int,
        heartbeat_interval_s: float,
        token: str | None = None,
        git_commit: str | None = None,
        config_hash: str | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.app_version = app_version
        self.port = port
        self.heartbeat_interval_s = heartbeat_interval_s
        self.token = token if token is not None else new_token()
        self.hub = LiveHub()
        self._lock = threading.RLock()
        self._sessions = SessionManager(
            store, clock, app_version=app_version, git_commit=git_commit, config_hash=config_hash
        )
        self._chain: ChainCheck | None = None
        self._session_id: str | None = None
        self._session_error: str | None = None
        self._previous: PreviousSessionInfo | None = None
        self._last_heartbeat_ms: int | None = None
        self._closed = False
        self._close_result: ShutdownInfo | None = None
        self._shutdown_callback: Callable[[], None] | None = None
        self.shutdown_reason: str | None = None

    # ------------------------------------------------------------------ startup / shutdown
    def start(self) -> None:
        """Verify the event chain, then open the app session (idempotent)."""
        with self._lock:
            if self._chain is not None:
                return
            try:
                report = self.store.verify()
            except Exception as exc:  # an unreadable chain is a failed chain (fail closed)
                log.exception("event chain verification raised")
                report = ChainReport(
                    ok=False,
                    events_checked=0,
                    head_seq=0,
                    head_hash="",
                    first_bad_seq=None,
                    reason=f"verification error: {type(exc).__name__}: {exc}",
                )
            self._chain = ChainCheck(report, self.clock.now_ms())
            if report.ok:
                log.info("event chain verified", extra={"events_checked": report.events_checked})
            else:
                log.error(
                    "event chain verification FAILED; monitoring is locked",
                    extra={"first_bad_seq": report.first_bad_seq, "reason": report.reason},
                )
            try:
                self._session_id = self._sessions.open()
                self._last_heartbeat_ms = self.clock.now_ms()
                self._previous = _previous_session(self.store, self._session_id)
            except Exception as exc:  # the UI must still come up and show why
                log.exception("could not open the app session")
                self._session_error = f"{type(exc).__name__}: {exc}"
        self._publish_status()

    def close(self, reason: str = "user") -> ShutdownInfo:
        """Close the session (stops monitoring first) and publish the *actual* outcome.

        Idempotent: later calls return the first outcome. The `shutdown` push message is sent only
        after `session_stopped` was written, or after writing it failed (`clean=False`).
        """
        with self._lock:
            if self._close_result is not None:
                return self._close_result
            self._closed = True
            if self._session_id is None:
                result = ShutdownInfo(
                    clean=False,
                    reason=reason,
                    detail=f"no app session was open: {self._session_error or 'not opened'}",
                )
            else:
                try:
                    self._sessions.close(reason)
                except Exception as exc:
                    log.exception("failed to close the app session cleanly")
                    result = ShutdownInfo(
                        clean=False,
                        reason=reason,
                        detail=(
                            f"session_stopped could not be recorded ({type(exc).__name__}); "
                            "the next start will report this session as not closed cleanly"
                        ),
                    )
                else:
                    log.info("app session closed", extra={"reason": reason})
                    result = ShutdownInfo(clean=True, reason=reason, detail=None)
            self._close_result = result
            self.hub.publish({"type": "shutdown", "data": result.model_dump(mode="json")})
        return result

    @property
    def closed(self) -> bool:
        return self._closed

    def set_shutdown_callback(self, callback: Callable[[], None]) -> None:
        self._shutdown_callback = callback

    def request_shutdown(self, reason: str) -> QuitResponse:
        """Close the session, then ask the hosting server to stop.

        The session is closed *before* the server stops, so the HTTP response (and the push
        message) can report truthfully whether `session_stopped` was recorded. Without an
        attached server nothing is closed (the app would keep serving with no session).
        """
        callback = self._shutdown_callback
        if callback is None:
            return QuitResponse(
                accepted=False,
                session=Sourced[ShutdownInfo].unavailable("no server attached; nothing was closed"),
            )
        if self.shutdown_reason is None:
            self.shutdown_reason = reason
        self.hub.publish({"type": "shutting_down", "data": {"reason": reason}})
        result = self.close(reason)
        callback()
        return QuitResponse(
            accepted=True,
            session=Sourced[ShutdownInfo](
                value=result, provenance=Provenance.SYSTEM, as_of_ms=self.clock.now_ms()
            ),
        )

    # ------------------------------------------------------------------ periodic
    def heartbeat(self) -> None:
        with self._lock:
            if self._closed or self._session_id is None:
                return
            try:
                self._sessions.heartbeat()
                self._last_heartbeat_ms = self.clock.now_ms()
            except Exception:
                log.exception("heartbeat write failed")
        self._publish_status()

    # ------------------------------------------------------------------ commands
    def start_monitoring(self) -> StatusResponse:
        with self._lock:
            self._require_running()
            chain = self._chain
            if chain is None or not chain.report.ok:
                raise TransitionError(
                    TransitionRefused.LOCKED, self._chain_block_reason() or "chain not verified"
                )
            try:
                mid = self._sessions.start_monitoring()
            except SessionError as exc:
                raise TransitionError(TransitionRefused.INVALID, str(exc)) from exc
            log.info("monitoring started", extra={"monitoring_id": mid})
        return self._publish_status()

    def stop_monitoring(self) -> StatusResponse:
        with self._lock:
            self._require_running()
            try:
                self._sessions.stop_monitoring(reason="user")
            except SessionError as exc:
                raise TransitionError(TransitionRefused.INVALID, str(exc)) from exc
            log.info("monitoring stopped")
        return self._publish_status()

    def _require_running(self) -> None:
        if self._closed:
            raise TransitionError(TransitionRefused.INVALID, "the app is shutting down")
        if self._session_id is None:
            raise TransitionError(
                TransitionRefused.LOCKED,
                f"no app session: {self._session_error or 'session not opened'}",
            )

    # ------------------------------------------------------------------ read models
    def status(self) -> StatusResponse:
        with self._lock:
            now = self.clock.now_ms()
            return StatusResponse(
                app_version=self.app_version,
                server_time=to_iso(now),
                heartbeat_interval_s=self.heartbeat_interval_s,
                session=self._session_sourced(now),
                monitoring=self._monitoring_sourced(now),
                chain=self._chain_sourced(),
                previous_session=self._previous_sourced(now),
                heartbeat=self._heartbeat_sourced(now),
                streams={
                    name: Sourced[str].unavailable(STREAMS_NOT_IMPLEMENTED)
                    for name in STREAM_GROUPS
                },
            )

    def coverage(self) -> CoverageResponse:
        now = self.clock.now_ms()
        untrusted = self._untrusted_projection_reason()
        if untrusted is not None:
            return CoverageResponse(
                generated_at=to_iso(now),
                intervals=Sourced[list[CoverageInterval]].unavailable(untrusted),
                gaps=Sourced[list[CoverageGap]].unavailable(untrusted),
            )
        try:
            intervals = observation_intervals(self.store)
        except Exception as exc:
            log.exception("coverage projection failed")
            reason = f"coverage could not be computed: {type(exc).__name__}"
            return CoverageResponse(
                generated_at=to_iso(now),
                intervals=Sourced[list[CoverageInterval]].unavailable(reason),
                gaps=Sourced[list[CoverageGap]].unavailable(reason),
            )
        return CoverageResponse(
            generated_at=to_iso(now),
            intervals=Sourced[list[CoverageInterval]](
                value=[_interval_out(iv) for iv in intervals],
                provenance=Provenance.SYSTEM,
                as_of_ms=now,
            ),
            gaps=Sourced[list[CoverageGap]](
                value=coverage_gaps(intervals), provenance=Provenance.SYSTEM, as_of_ms=now
            ),
        )

    def events(self, *, after_seq: int | None, limit: int) -> EventsResponse:
        now = self.clock.now_ms()
        limit = max(1, min(limit, MAX_EVENTS_LIMIT))
        head_seq, _ = self.store.head
        try:
            if after_seq is None:  # newest page
                rows = list(reversed(self.store.read(limit=limit, descending=True)))
                has_more = bool(rows) and rows[0].seq > 1
            else:
                rows = self.store.read(after_seq=max(0, after_seq), limit=limit + 1)
                has_more = len(rows) > limit
                rows = rows[:limit]
            page = Sourced[EventPage](
                value=EventPage(
                    items=[_event_out(e) for e in rows], head_seq=head_seq, has_more=has_more
                ),
                provenance=Provenance.SYSTEM,
                as_of_ms=now,
            )
        except Exception as exc:
            log.exception("event read failed")
            page = Sourced[EventPage].unavailable(f"events could not be read: {type(exc).__name__}")
        return EventsResponse(generated_at=to_iso(now), events=page, chain=self._chain_sourced())

    # ------------------------------------------------------------------ push
    def status_message(self) -> LiveMessage:
        return {"type": "status", "data": self.status().model_dump(mode="json")}

    def _publish_status(self) -> StatusResponse:
        # Under the lock so that snapshots are published in the order the state changed.
        with self._lock:
            status = self.status()
            self.hub.publish({"type": "status", "data": status.model_dump(mode="json")})
        return status

    # ------------------------------------------------------------------ helpers
    def _chain_block_reason(self) -> str | None:
        chain = self._chain
        if chain is None:
            return "event chain not verified yet"
        if chain.report.ok:
            return None
        where = (
            f" at seq {chain.report.first_bad_seq}"
            if chain.report.first_bad_seq is not None
            else ""
        )
        return f"event chain verification failed{where}: {chain.report.reason}"

    def _session_sourced(self, now: int) -> Sourced[SessionInfo]:
        if self._session_id is None:
            return Sourced[SessionInfo].unavailable(self._session_error or "session not opened yet")
        if self._closed:
            return Sourced[SessionInfo].unavailable("session closed (app shutting down)")
        started = ulid_timestamp_ms(self._session_id)
        return Sourced[SessionInfo](
            value=SessionInfo(
                session_id=self._session_id,
                started_at=to_iso(started),
                uptime_s=max(0, now - started) / 1000,
            ),
            provenance=Provenance.SYSTEM,
            as_of_ms=now,
        )

    def _monitoring_sourced(self, now: int) -> Sourced[MonitoringInfo]:
        if self._session_id is None or self._closed:
            return Sourced[MonitoringInfo].unavailable(
                "app shutting down" if self._closed else (self._session_error or "no session")
            )
        mid = self._sessions.monitoring_id
        state = self._sessions.monitoring_state
        block = self._chain_block_reason()
        return Sourced[MonitoringInfo](
            value=MonitoringInfo(
                state=state,
                monitoring_id=mid,
                since=None if mid is None else to_iso(ulid_timestamp_ms(mid)),
                start_allowed=state is MonitoringState.STOPPED and block is None,
                blocked_reason=block,
            ),
            provenance=Provenance.SYSTEM,
            as_of_ms=now,
        )

    def _chain_sourced(self) -> Sourced[ChainInfo]:
        chain = self._chain
        if chain is None:
            return Sourced[ChainInfo].unavailable("event chain not verified yet")
        r = chain.report
        return Sourced[ChainInfo](
            value=ChainInfo(
                ok=r.ok,
                events_checked=r.events_checked,
                head_seq=r.head_seq,
                head_hash=r.head_hash,
                first_bad_seq=r.first_bad_seq,
                reason=r.reason,
                verified_at=to_iso(chain.verified_at_ms),
            ),
            provenance=Provenance.SYSTEM,
            as_of_ms=chain.verified_at_ms,
        )

    def _untrusted_projection_reason(self) -> str | None:
        """Projections folded from a chain that failed verification are not shown (fail closed)."""
        block = self._chain_block_reason()
        if block is None:
            return None
        return f"not derived: {block}; the raw events remain listed in Logs & audit"

    def _previous_sourced(self, now: int) -> Sourced[PreviousSessionInfo]:
        untrusted = self._untrusted_projection_reason()
        if untrusted is not None:
            return Sourced[PreviousSessionInfo].unavailable(untrusted)
        if self._previous is None:
            return Sourced[PreviousSessionInfo].unavailable(
                self._session_error or "session not opened yet"
            )
        return Sourced[PreviousSessionInfo](
            value=self._previous, provenance=Provenance.SYSTEM, as_of_ms=now
        )

    def _heartbeat_sourced(self, now: int) -> Sourced[str]:
        hb = self._last_heartbeat_ms
        if hb is None or self._session_id is None:
            return Sourced[str].unavailable("no heartbeat recorded in this session")
        budget_ms = int(self.heartbeat_interval_s * 1000 * STALE_HEARTBEATS)
        if now - hb > budget_ms:
            return Sourced[str](value=to_iso(hb), provenance=Provenance.STALE, as_of_ms=hb)
        return Sourced[str](value=to_iso(hb), provenance=Provenance.SYSTEM, as_of_ms=hb)


# ---------------------------------------------------------------------- pure helpers
def coverage_gaps(intervals: list[ObservationInterval]) -> list[CoverageGap]:
    """Unobserved time between monitoring intervals, plus the open tail if not observing now.

    Time before the first interval is not reported: there is nothing to compare it with.
    """
    gaps: list[CoverageGap] = []
    covered_until: int | None = None
    last_reason: str | None = None
    open_now = False
    for iv in sorted(intervals, key=lambda i: i.start_ms):
        if covered_until is not None and iv.start_ms > covered_until:
            gaps.append(
                CoverageGap(
                    start=to_iso(covered_until), end=to_iso(iv.start_ms), after_reason=last_reason
                )
            )
        if iv.end_ms is None:
            open_now = True
            continue
        if covered_until is None or iv.end_ms >= covered_until:
            covered_until = iv.end_ms
            last_reason = iv.end_reason
    if covered_until is not None and not open_now:
        gaps.append(CoverageGap(start=to_iso(covered_until), end=None, after_reason=last_reason))
    return gaps


def _interval_out(iv: ObservationInterval) -> CoverageInterval:
    return CoverageInterval(
        monitoring_id=iv.monitoring_id,
        start=to_iso(iv.start_ms),
        end=None if iv.end_ms is None else to_iso(iv.end_ms),
        end_reason=iv.end_reason,
    )


def _event_out(e: Event) -> EventOut:
    return EventOut(
        seq=e.seq,
        ts=to_iso(e.ts_ms),
        type=str(e.type),
        aggregate_type=str(e.aggregate_type),
        aggregate_id=e.aggregate_id,
        payload=e.payload,
        prev_hash=e.prev_hash,
        hash=e.hash,
    )


def _previous_session(store: EventStore, current_session_id: str) -> PreviousSessionInfo:
    """Describe how the session before `current_session_id` ended, from the event log."""
    none = PreviousSessionInfo(
        outcome="NONE",
        session_id=None,
        started_at=None,
        ended_at=None,
        end_reason=None,
        monitoring_was_active=None,
        unobserved_from=None,
        unobserved_to=None,
    )
    try:
        starts = store.read(types=[EventType.SESSION_STARTED], limit=2, descending=True)
        current = next((s for s in starts if s.aggregate_id == current_session_id), None)
        previous = [s for s in starts if s.aggregate_id != current_session_id]
        if current is None or not previous:
            return none
        prev = previous[0]
        sid = prev.aggregate_id
        events = store.read(after_seq=prev.seq - 1, aggregate=(AggregateType.SESSION, sid))
        return _describe(prev, events, current.ts_ms)
    except Exception:
        log.exception("could not determine how the previous session ended")
        return none.model_copy(update={"outcome": "UNKNOWN"})


def _describe(start: Event, events: list[Event], current_start_ms: int) -> PreviousSessionInfo:
    sid = start.aggregate_id
    stop = next((e for e in events if e.type is EventType.SESSION_STOPPED), None)
    crash = next((e for e in events if e.type is EventType.SESSION_CRASH_DETECTED), None)
    base: dict[str, Any] = {"session_id": sid, "started_at": to_iso(start.ts_ms)}
    if stop is not None:
        was_monitoring = any(
            e.type is EventType.MONITORING_STOPPED and e.payload.get("reason") == "session_closing"
            for e in events
        )
        return PreviousSessionInfo(
            outcome="CLEAN",
            ended_at=to_iso(stop.ts_ms),
            end_reason=str(stop.payload.get("reason")),
            monitoring_was_active=was_monitoring,
            unobserved_from=to_iso(stop.ts_ms),
            unobserved_to=to_iso(current_start_ms),
            **base,
        )
    if crash is not None:
        last_alive = int(crash.payload["last_alive_ms"])
        return PreviousSessionInfo(
            outcome="CRASHED",
            ended_at=to_iso(last_alive),
            end_reason="crash (no clean shutdown recorded; ended at last heartbeat)",
            monitoring_was_active=bool(crash.payload.get("monitoring_was_active")),
            unobserved_from=to_iso(last_alive),
            unobserved_to=to_iso(current_start_ms),
            **base,
        )
    return PreviousSessionInfo(
        outcome="UNKNOWN",
        ended_at=None,
        end_reason="no stop or crash record found",
        monitoring_was_active=None,
        unobserved_from=None,
        unobserved_to=None,
        **base,
    )
