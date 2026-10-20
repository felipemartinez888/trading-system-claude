"""Event envelope, canonical serialization and hash chaining (ADR-0003, ADR-0011)."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

GENESIS_HASH = "0" * 64
EVENT_SCHEMA_VERSION = 1


class EventType(StrEnum):
    # runtime
    SESSION_STARTED = "session_started"
    SESSION_STOPPED = "session_stopped"
    SESSION_CRASH_DETECTED = "session_crash_detected"
    MONITORING_STARTED = "monitoring_started"
    MONITORING_STOPPED = "monitoring_stopped"
    # integrity
    CHAIN_VERIFIED = "chain_verified"
    # market data (M1+)
    BAR_LATE = "bar_late"
    BAR_CONFLICT = "bar_conflict"
    GAP_DETECTED = "gap_detected"
    GAP_RESOLVED = "gap_resolved"
    STREAM_STATE_CHANGED = "stream_state_changed"
    CLOCK_SKEW_CHANGED = "clock_skew_changed"
    BACKFILL_COMPLETED = "backfill_completed"
    # signals (M1+)
    SIGNAL_CREATED = "signal_created"
    SIGNAL_TRANSITIONED = "signal_transitioned"
    # research (M2+)
    HOLDOUT_UNLOCKED = "holdout_unlocked"


class AggregateType(StrEnum):
    RUNTIME = "runtime"
    SESSION = "session"
    STREAM = "stream"
    MARKET_DATA = "market_data"
    SIGNAL = "signal"
    RESEARCH = "research"


class NewEvent(BaseModel):
    """What a producer submits; seq/hashes are assigned by the store."""

    model_config = ConfigDict(frozen=True)

    type: EventType
    aggregate_type: AggregateType
    aggregate_id: str
    payload: dict[str, Any]
    ts_ms: int


class Event(BaseModel):
    """A persisted, chained event."""

    model_config = ConfigDict(frozen=True)

    seq: int
    ts_ms: int
    type: EventType
    aggregate_type: AggregateType
    aggregate_id: str
    payload: dict[str, Any]
    schema_version: int
    prev_hash: str
    hash: str


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8, NaN/Infinity forbidden."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def event_body(
    *,
    seq: int,
    ts_ms: int,
    type: EventType | str,
    aggregate_type: AggregateType | str,
    aggregate_id: str,
    payload: dict[str, Any],
    schema_version: int,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "ts_ms": ts_ms,
        "type": str(type),
        "aggregate_type": str(aggregate_type),
        "aggregate_id": aggregate_id,
        "payload": payload,
        "schema_version": schema_version,
    }


def chain_hash(prev_hash: str, body: dict[str, Any]) -> str:
    """hash_n = SHA-256(prev_hash_n || "\\n" || canonical_json(body_n))."""
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("ascii"))
    digest.update(b"\n")
    digest.update(canonical_json(body).encode("utf-8"))
    return digest.hexdigest()
