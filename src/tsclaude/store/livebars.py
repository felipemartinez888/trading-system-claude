"""Live closed 1m bars in `state.db` (DATA_ARCHITECTURE §3 and §5).

- Only *closed* bars are stored (the `Bar` contract has no partial form).
- Primary key (market, symbol, open_time_ms): one canonical row per minute.
- Writing an identical bar again is a no-op (`DUPLICATE`). "Identical" compares market data
  (OHLCV, trades, taker volumes), not `source` or `ingested_at_ms`: the same bar first seen live
  and later returned by REST is one bar, and the first-written row (with its provenance) is kept.
- A differing bar is never silently merged. The caller gets `CONFLICT` with both versions so it
  can record a `bar_conflict` event. The precedence rule from the M1-T1 brief: a WebSocket-closed
  bar (`ws_live`) replaces a non-WS row (`REPLACED`); in every other case the stored row is kept.

The table lives in the same SQLite file as the event store, on a separate connection (WAL mode
allows this). It is not part of the hash chain; conflicts are recorded in chained events.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from tsclaude.contracts.market import Bar, BarSource, Interval, Market

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars_1m (
    market                 TEXT    NOT NULL,
    symbol                 TEXT    NOT NULL,
    open_time_ms           INTEGER NOT NULL,
    close_time_ms          INTEGER NOT NULL,
    open                   REAL    NOT NULL,
    high                   REAL    NOT NULL,
    low                    REAL    NOT NULL,
    close                  REAL    NOT NULL,
    volume                 REAL    NOT NULL,
    quote_volume           REAL    NOT NULL,
    trades                 INTEGER NOT NULL,
    taker_buy_volume       REAL    NOT NULL,
    taker_buy_quote_volume REAL    NOT NULL,
    source                 TEXT    NOT NULL,
    ingested_at_ms         INTEGER NOT NULL,
    observed_session_id    TEXT,
    PRIMARY KEY (market, symbol, open_time_ms)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS ingest_checkpoints (
    job            TEXT    NOT NULL,
    market         TEXT    NOT NULL,
    symbol         TEXT    NOT NULL,
    schema_version INTEGER NOT NULL,
    window_from_ms INTEGER NOT NULL,
    window_to_ms   INTEGER NOT NULL,
    cursor_ms      INTEGER NOT NULL,
    rows_written   INTEGER NOT NULL,
    chunks_done    INTEGER NOT NULL,
    status         TEXT    NOT NULL,
    updated_at_ms  INTEGER NOT NULL,
    PRIMARY KEY (job, market, symbol)
) WITHOUT ROWID;
"""

#: Version of the `ingest_checkpoints` row layout; a checkpoint of another version is ignored.
CHECKPOINT_SCHEMA = 1

_COLUMNS = (
    "market, symbol, open_time_ms, close_time_ms, open, high, low, close, volume, quote_volume,"
    " trades, taker_buy_volume, taker_buy_quote_volume, source, ingested_at_ms, observed_session_id"
)

_MARKET_FIELDS = (
    "close_time_ms",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trades",
    "taker_buy_volume",
    "taker_buy_quote_volume",
)


class WriteOutcome(StrEnum):
    INSERTED = "inserted"
    DUPLICATE = "duplicate"  # identical market data already stored; nothing written
    CONFLICT = "conflict"  # differing data already stored; stored row kept
    REPLACED = "replaced"  # differing non-WS row replaced by a WS-closed bar


@dataclass(frozen=True, slots=True)
class StoredBar:
    bar: Bar
    observed_session_id: str | None


@dataclass(frozen=True, slots=True)
class WriteResult:
    outcome: WriteOutcome
    existing: StoredBar | None = None  # set for DUPLICATE, CONFLICT and REPLACED


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Progress of one ingestion job for one (market, symbol).

    `cursor_ms` is the open time below which the job has not processed anything yet: every wanted
    minute in `[cursor_ms, window_to_ms)` was fetched and stored (or registered as a gap when the
    source did not return it). Ingestion runs newest first, so the cursor only moves down. It is
    written in the same transaction as the rows it describes (`write_many`), so it can never be
    ahead of the stored data. It is advisory: the stored bars stay the source of truth.
    """

    job: str
    market: Market
    symbol: str
    window_from_ms: int
    window_to_ms: int
    cursor_ms: int
    rows_written: int
    chunks_done: int
    status: str
    updated_at_ms: int
    schema_version: int = CHECKPOINT_SCHEMA


def same_market_data(a: Bar, b: Bar) -> bool:
    return all(getattr(a, f) == getattr(b, f) for f in _MARKET_FIELDS)


class StoreClosed(RuntimeError):
    """The store was closed; no further reads or writes are possible."""


class LiveBarStore:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        self._conn = sqlite3.connect(
            self._path, check_same_thread=False, isolation_level=None, timeout=30.0
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        """Close the connection. Takes the store lock, so a write in flight on another thread
        finishes (commit or rollback) first; later calls raise `StoreClosed`. Idempotent."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> LiveBarStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ writes
    def write(self, bar: Bar, observed_session_id: str | None = None) -> WriteResult:
        return self.write_many([bar], observed_session_id)[0]

    def write_many(
        self,
        bars: Sequence[Bar],
        observed_session_id: str | None = None,
        *,
        checkpoint: Checkpoint | None = None,
    ) -> list[WriteResult]:
        """Write `bars` in order inside **one** transaction (one durable commit), with the same
        per-bar outcome rules as `write`, and upsert `checkpoint` in that same transaction.

        All-or-nothing: if anything raises, nothing of the batch (and no checkpoint) is stored.
        Validation happens before the transaction opens. `bars` may be empty (checkpoint only).
        """
        for bar in bars:
            if bar.interval is not Interval.M1:
                raise ValueError("bars_1m only stores 1m bars")
            if bar.source is BarSource.DERIVED:
                raise ValueError("derived bars are not canonical 1m bars")
        with self._lock:
            self._require_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                results = [self._write_in_txn(bar, observed_session_id) for bar in bars]
                if checkpoint is not None:
                    self._put_checkpoint(checkpoint)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return results

    def _write_in_txn(self, bar: Bar, observed_session_id: str | None) -> WriteResult:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM bars_1m"  # noqa: S608 - fixed column list
            " WHERE market=? AND symbol=? AND open_time_ms=?",
            (str(bar.market), bar.symbol, bar.open_time_ms),
        ).fetchone()
        if row is None:
            self._conn.execute(
                f"INSERT INTO bars_1m({_COLUMNS})"  # noqa: S608 - fixed column list
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _to_row(bar, observed_session_id),
            )
            return WriteResult(WriteOutcome.INSERTED)
        existing = _from_row(row)
        if same_market_data(existing.bar, bar):
            return WriteResult(WriteOutcome.DUPLICATE, existing)
        if bar.source is BarSource.WS_LIVE and existing.bar.source is not BarSource.WS_LIVE:
            self._conn.execute(
                "DELETE FROM bars_1m WHERE market=? AND symbol=? AND open_time_ms=?",
                (str(bar.market), bar.symbol, bar.open_time_ms),
            )
            self._conn.execute(
                f"INSERT INTO bars_1m({_COLUMNS})"  # noqa: S608 - fixed column list
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _to_row(bar, observed_session_id),
            )
            return WriteResult(WriteOutcome.REPLACED, existing)
        return WriteResult(WriteOutcome.CONFLICT, existing)

    def _require_open(self) -> None:
        if self._closed:
            raise StoreClosed("the live bar store is closed")

    # ------------------------------------------------------------------ checkpoints
    def _put_checkpoint(self, cp: Checkpoint) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO ingest_checkpoints(job, market, symbol, schema_version,"
            " window_from_ms, window_to_ms, cursor_ms, rows_written, chunks_done, status,"
            " updated_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                cp.job,
                str(cp.market),
                cp.symbol,
                cp.schema_version,
                cp.window_from_ms,
                cp.window_to_ms,
                cp.cursor_ms,
                cp.rows_written,
                cp.chunks_done,
                cp.status,
                cp.updated_at_ms,
            ),
        )

    def set_checkpoint(self, cp: Checkpoint) -> None:
        """Store `cp` alone (one transaction), e.g. a terminal status after the last chunk."""
        self.write_many([], checkpoint=cp)

    def get_checkpoint(self, job: str, market: Market, symbol: str) -> Checkpoint | None:
        with self._lock:
            self._require_open()
            row = self._conn.execute(
                "SELECT schema_version, window_from_ms, window_to_ms, cursor_ms, rows_written,"
                " chunks_done, status, updated_at_ms FROM ingest_checkpoints"
                " WHERE job=? AND market=? AND symbol=?",
                (job, str(market), symbol),
            ).fetchone()
        if row is None:
            return None
        return Checkpoint(
            job=job,
            market=market,
            symbol=symbol,
            schema_version=int(row[0]),
            window_from_ms=int(row[1]),
            window_to_ms=int(row[2]),
            cursor_ms=int(row[3]),
            rows_written=int(row[4]),
            chunks_done=int(row[5]),
            status=str(row[6]),
            updated_at_ms=int(row[7]),
        )

    # ------------------------------------------------------------------ reads
    def get(self, market: Market, symbol: str, open_time_ms: int) -> StoredBar | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM bars_1m"  # noqa: S608 - fixed column list
                " WHERE market=? AND symbol=? AND open_time_ms=?",
                (str(market), symbol, open_time_ms),
            ).fetchone()
        return None if row is None else _from_row(row)

    def range(self, market: Market, symbol: str, start_ms: int, end_ms: int) -> list[StoredBar]:
        """Bars with start_ms <= open_time_ms < end_ms, ascending."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM bars_1m"  # noqa: S608 - fixed column list
                " WHERE market=? AND symbol=? AND open_time_ms>=? AND open_time_ms<?"
                " ORDER BY open_time_ms",
                (str(market), symbol, start_ms, end_ms),
            ).fetchall()
        return [_from_row(r) for r in rows]

    def open_times(self, market: Market, symbol: str, start_ms: int, end_ms: int) -> set[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT open_time_ms FROM bars_1m"
                " WHERE market=? AND symbol=? AND open_time_ms>=? AND open_time_ms<?",
                (str(market), symbol, start_ms, end_ms),
            ).fetchall()
        return {int(r[0]) for r in rows}

    def last(self, market: Market, symbol: str) -> StoredBar | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM bars_1m"  # noqa: S608 - fixed column list
                " WHERE market=? AND symbol=? ORDER BY open_time_ms DESC LIMIT 1",
                (str(market), symbol),
            ).fetchone()
        return None if row is None else _from_row(row)

    def first_open_time(self, market: Market, symbol: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(open_time_ms) FROM bars_1m WHERE market=? AND symbol=?",
                (str(market), symbol),
            ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def count(self, market: Market | None = None, symbol: str | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if market is not None:
            clauses.append("market=?")
            params.append(str(market))
        if symbol is not None:
            clauses.append("symbol=?")
            params.append(symbol)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            row = self._conn.execute(f"SELECT COUNT(*) FROM bars_1m{where}", params).fetchone()  # noqa: S608
        return int(row[0])


def _to_row(bar: Bar, observed_session_id: str | None) -> tuple[Any, ...]:
    return (
        str(bar.market),
        bar.symbol,
        bar.open_time_ms,
        bar.close_time_ms,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        bar.quote_volume,
        bar.trades,
        bar.taker_buy_volume,
        bar.taker_buy_quote_volume,
        str(bar.source),
        bar.ingested_at_ms,
        observed_session_id,
    )


def _from_row(row: Sequence[Any]) -> StoredBar:
    bar = Bar(
        market=Market(row[0]),
        symbol=row[1],
        interval=Interval.M1,
        open_time_ms=row[2],
        close_time_ms=row[3],
        open=row[4],
        high=row[5],
        low=row[6],
        close=row[7],
        volume=row[8],
        quote_volume=row[9],
        trades=row[10],
        taker_buy_volume=row[11],
        taker_buy_quote_volume=row[12],
        source=BarSource(row[13]),
        ingested_at_ms=row[14],
    )
    return StoredBar(bar=bar, observed_session_id=row[15])
