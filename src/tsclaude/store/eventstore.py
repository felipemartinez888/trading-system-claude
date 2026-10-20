"""Append-only, hash-chained event store on SQLite (ADR-0003, ADR-0011).

Guarantees:
- events are never updated or deleted (enforced by SQLite triggers, not only by convention);
- every event carries prev_hash and hash; `verify()` recomputes the whole chain;
- a single writer serializes appends (thread lock), so seq is gap-free and monotonic.

The `runtime_state` table is deliberately *outside* the chain: it holds mutable operational
markers such as the heartbeat, whose meaning is captured in chained events when it matters
(e.g. a crash-detection event records the last heartbeat).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tsclaude.contracts.events import (
    EVENT_SCHEMA_VERSION,
    GENESIS_HASH,
    AggregateType,
    Event,
    EventType,
    NewEvent,
    canonical_json,
    chain_hash,
    event_body,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY,
    ts_ms          INTEGER NOT NULL,
    type           TEXT    NOT NULL,
    aggregate_type TEXT    NOT NULL,
    aggregate_id   TEXT    NOT NULL,
    payload        TEXT    NOT NULL,
    schema_version INTEGER NOT NULL,
    prev_hash      TEXT    NOT NULL,
    hash           TEXT    NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_events_type ON events(type, seq);
CREATE INDEX IF NOT EXISTS ix_events_aggregate ON events(aggregate_type, aggregate_id, seq);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TABLE IF NOT EXISTS runtime_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

STORE_FORMAT_VERSION = "1"


class EventStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    events_checked: int
    head_seq: int
    head_hash: str
    first_bad_seq: int | None = None
    reason: str | None = None


class EventStore:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self._path, check_same_thread=False, isolation_level=None, timeout=30.0
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._init_meta()
        self._head_seq, self._head_hash = self._load_head()

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> EventStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def path(self) -> Path:
        return self._path

    def _init_meta(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM store_meta WHERE key='format_version'"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO store_meta(key, value) VALUES('format_version', ?)",
                (STORE_FORMAT_VERSION,),
            )
        elif row[0] != STORE_FORMAT_VERSION:
            raise EventStoreError(
                f"unsupported store format {row[0]!r} (expected {STORE_FORMAT_VERSION})"
            )

    def _load_head(self) -> tuple[int, str]:
        row = self._conn.execute(
            "SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return 0, GENESIS_HASH
        return int(row[0]), str(row[1])

    # ------------------------------------------------------------------ writes
    def append(self, event: NewEvent) -> Event:
        return self.append_many([event])[0]

    def append_many(self, events: Sequence[NewEvent]) -> list[Event]:
        """Append events atomically (all or none), extending the chain in order."""
        if not events:
            return []
        with self._lock:
            seq, prev = self._head_seq, self._head_hash
            stored: list[Event] = []
            rows: list[tuple[Any, ...]] = []
            for ev in events:
                seq += 1
                payload_text = canonical_json(ev.payload)
                body = event_body(
                    seq=seq,
                    ts_ms=ev.ts_ms,
                    type=ev.type,
                    aggregate_type=ev.aggregate_type,
                    aggregate_id=ev.aggregate_id,
                    payload=json.loads(payload_text),
                    schema_version=EVENT_SCHEMA_VERSION,
                )
                digest = chain_hash(prev, body)
                rows.append(
                    (
                        seq,
                        ev.ts_ms,
                        str(ev.type),
                        str(ev.aggregate_type),
                        ev.aggregate_id,
                        payload_text,
                        EVENT_SCHEMA_VERSION,
                        prev,
                        digest,
                    )
                )
                stored.append(
                    Event(
                        seq=seq,
                        ts_ms=ev.ts_ms,
                        type=ev.type,
                        aggregate_type=ev.aggregate_type,
                        aggregate_id=ev.aggregate_id,
                        payload=json.loads(payload_text),
                        schema_version=EVENT_SCHEMA_VERSION,
                        prev_hash=prev,
                        hash=digest,
                    )
                )
                prev = digest
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.executemany(
                    "INSERT INTO events(seq, ts_ms, type, aggregate_type, aggregate_id, payload,"
                    " schema_version, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)",
                    rows,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self._head_seq, self._head_hash = seq, prev
            return stored

    # ------------------------------------------------------------------ mutable runtime state
    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runtime_state(key, value) VALUES(?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_state(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM runtime_state WHERE key=?", (key,)
            ).fetchone()
        return None if row is None else str(row[0])

    # ------------------------------------------------------------------ reads
    @property
    def head(self) -> tuple[int, str]:
        with self._lock:
            return self._head_seq, self._head_hash

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def read(
        self,
        *,
        after_seq: int = 0,
        types: Sequence[EventType] | None = None,
        aggregate: tuple[AggregateType, str] | None = None,
        limit: int | None = None,
        descending: bool = False,
    ) -> list[Event]:
        clauses = ["seq > ?"]
        params: list[Any] = [after_seq]
        if types:
            clauses.append(f"type IN ({','.join('?' for _ in types)})")
            params.extend(str(t) for t in types)
        if aggregate is not None:
            clauses.append("aggregate_type = ? AND aggregate_id = ?")
            params.extend([str(aggregate[0]), aggregate[1]])
        # Only fixed column names and "?" placeholders are interpolated; values are bound.
        sql = (
            "SELECT seq, ts_ms, type, aggregate_type, aggregate_id, payload, schema_version,"  # noqa: S608
            f" prev_hash, hash FROM events WHERE {' AND '.join(clauses)}"
            f" ORDER BY seq {'DESC' if descending else 'ASC'}"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_event(r) for r in rows]

    def iter_all(self, batch: int = 5000) -> Iterator[tuple[Any, ...]]:
        """Raw rows in seq order, batched, for integrity checks over large stores."""
        last = 0
        while True:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT seq, ts_ms, type, aggregate_type, aggregate_id, payload,"
                    " schema_version, prev_hash, hash FROM events WHERE seq > ?"
                    " ORDER BY seq LIMIT ?",
                    (last, batch),
                ).fetchall()
            if not rows:
                return
            yield from rows
            last = int(rows[-1][0])

    # ------------------------------------------------------------------ integrity
    def verify(self) -> ChainReport:
        """Recompute every hash from stored content; report the first inconsistency."""
        prev = GENESIS_HASH
        expected_seq = 1
        checked = 0
        for row in self.iter_all():
            seq, ts_ms, type_, agg_type, agg_id, payload_text, schema_version, prev_hash, digest = (
                row
            )
            if seq != expected_seq:
                return ChainReport(
                    False, checked, seq, digest, seq, f"seq gap: expected {expected_seq}"
                )
            if prev_hash != prev:
                return ChainReport(False, checked, seq, digest, seq, "prev_hash does not link")
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                return ChainReport(False, checked, seq, digest, seq, "payload is not valid JSON")
            if canonical_json(payload) != payload_text:
                return ChainReport(False, checked, seq, digest, seq, "payload is not canonical")
            body = event_body(
                seq=seq,
                ts_ms=ts_ms,
                type=type_,
                aggregate_type=agg_type,
                aggregate_id=agg_id,
                payload=payload,
                schema_version=schema_version,
            )
            if chain_hash(prev, body) != digest:
                return ChainReport(False, checked, seq, digest, seq, "hash mismatch")
            prev = digest
            expected_seq += 1
            checked += 1
        return ChainReport(True, checked, checked, prev)


def _row_to_event(row: tuple[Any, ...]) -> Event:
    seq, ts_ms, type_, agg_type, agg_id, payload_text, schema_version, prev_hash, digest = row
    return Event(
        seq=seq,
        ts_ms=ts_ms,
        type=EventType(type_),
        aggregate_type=AggregateType(agg_type),
        aggregate_id=agg_id,
        payload=json.loads(payload_text),
        schema_version=schema_version,
        prev_hash=prev_hash,
        hash=digest,
    )
