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

    # simulator / paper trading (M3+)
    PAPER_STEP = "paper_step"
    SIGNAL_SHADOW_OUTCOME = "signal_shadow_outcome"
    SIM_RUN_STARTED = "sim_run_started"
    SIM_RUN_COMPLETED = "sim_run_completed"
    SIM_BAR_GAP = "sim_bar_gap"
    SIM_REQUEST_REJECTED = "sim_request_rejected"
    SIM_ORDER_SUBMITTED = "sim_order_submitted"
    SIM_ORDER_FILLED = "sim_order_filled"
    SIM_ORDER_CANCELLED = "sim_order_cancelled"
    SIM_ORDER_EXPIRED = "sim_order_expired"
    SIM_ORDER_REJECTED = "sim_order_rejected"
    SIM_POSITION_OPENED = "sim_position_opened"
    SIM_POSITION_CLOSED = "sim_position_closed"
    SIM_EXIT_REQUESTED = "sim_exit_requested"
    SIM_FUNDING_APPLIED = "sim_funding_applied"
    SIM_LEDGER_POSTED = "sim_ledger_posted"
    SIM_SOURCE_ERROR = "sim_source_error"
    SIM_BOOK_HALTED = "sim_book_halted"
    SIM_BOOK_RESET = "sim_book_reset"
    SIM_OBSERVATION_CHANGED = "sim_observation_changed"


class AggregateType(StrEnum):
    RUNTIME = "runtime"
    SESSION = "session"
    STREAM = "stream"
    MARKET_DATA = "market_data"
    SIGNAL = "signal"
    RESEARCH = "research"
    SIM = "sim"


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
