"""API response models.

Rule (ADR-0010): every metric-like value is wrapped in `Sourced`, so the UI never has to guess
where a value came from. Only identity/metadata fields (app version, server time, configured
intervals) are plain. `tests/integration/test_api_core.py` enforces this for every response model.

Timestamps are ISO-8601 UTC strings with a `Z` suffix (CLAUDE.md). `Sourced.as_of_ms` is part of the
Lead-owned contract and stays an epoch-ms integer.

Provenance used for the app's *own* runtime state (session, monitoring, chain, coverage): `LIVE`
with `as_of_ms` = when the backend read that state. These are first-hand observations of the
running process, never exchange data; exchange-derived values start in M1.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from tsclaude.contracts.provenance import Sourced
from tsclaude.runtime.sessions import MonitoringState


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class SessionInfo(_Model):
    session_id: str
    started_at: str
    uptime_s: float


class MonitoringInfo(_Model):
    state: MonitoringState
    monitoring_id: str | None
    since: str | None
    # Whether a Start request would currently be accepted, and why not (fail closed).
    start_allowed: bool
    blocked_reason: str | None


class ChainInfo(_Model):
    ok: bool
    events_checked: int
    head_seq: int
    head_hash: str
    first_bad_seq: int | None
    reason: str | None
    verified_at: str


PreviousOutcome = Literal["NONE", "CLEAN", "CRASHED", "UNKNOWN"]


class PreviousSessionInfo(_Model):
    """How the session before this one ended.

    `unobserved_from`/`unobserved_to` bound the interval during which the app was not running
    (from the previous session's clean stop or last heartbeat, to the start of this session).
    """

    outcome: PreviousOutcome
    session_id: str | None
    started_at: str | None
    ended_at: str | None
    end_reason: str | None
    monitoring_was_active: bool | None
    unobserved_from: str | None
    unobserved_to: str | None


class StatusResponse(_Model):
    app_version: str
    server_time: str
    heartbeat_interval_s: float
    session: Sourced[SessionInfo]
    monitoring: Sourced[MonitoringInfo]
    chain: Sourced[ChainInfo]
    previous_session: Sourced[PreviousSessionInfo]
    heartbeat: Sourced[str]  # ISO time of the last persisted heartbeat
    streams: dict[str, Sourced[str]]  # stream group -> state (UNAVAILABLE until M1)


class CoverageInterval(_Model):
    """One monitoring interval: the only valid live-observation coverage."""

    monitoring_id: str
    start: str
    end: str | None  # None = still observing
    end_reason: str | None


class CoverageGap(_Model):
    """Unobserved time between two monitoring intervals (or since the last one ended)."""

    start: str
    end: str | None  # None = still unobserved now
    after_reason: str | None  # end_reason of the interval preceding the gap


class CoverageResponse(_Model):
    generated_at: str
    intervals: Sourced[list[CoverageInterval]]
    gaps: Sourced[list[CoverageGap]]


class EventOut(_Model):
    seq: int
    ts: str
    type: str
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str


class EventPage(_Model):
    items: list[EventOut]  # ascending by seq
    head_seq: int
    has_more: bool


class EventsResponse(_Model):
    generated_at: str
    events: Sourced[EventPage]
    chain: Sourced[ChainInfo]


class HealthResponse(_Model):
    """Liveness only (used by the launcher); carries no metrics."""

    ok: bool
    app: str
    version: str


class QuitResponse(_Model):
    accepted: bool


class ErrorResponse(_Model):
    detail: str
