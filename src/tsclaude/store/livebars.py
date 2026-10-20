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
"""

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


def same_market_data(a: Bar, b: Bar) -> bool:
    return all(getattr(a, f) == getattr(b, f) for f in _MARKET_FIELDS)


class LiveBarStore:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self._path, check_same_thread=False, isolation_level=None, timeout=30.0
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> LiveBarStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ writes
    def write(self, bar: Bar, observed_session_id: str | None = None) -> WriteResult:
        if bar.interval is not Interval.M1:
            raise ValueError("bars_1m only stores 1m bars")
        if bar.source is BarSource.DERIVED:
            raise ValueError("derived bars are not canonical 1m bars")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
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
                    result = WriteResult(WriteOutcome.INSERTED)
                else:
                    existing = _from_row(row)
                    if same_market_data(existing.bar, bar):
                        result = WriteResult(WriteOutcome.DUPLICATE, existing)
                    elif bar.source is BarSource.WS_LIVE and existing.bar.source is not (
                        BarSource.WS_LIVE
                    ):
                        self._conn.execute(
                            "DELETE FROM bars_1m WHERE market=? AND symbol=? AND open_time_ms=?",
                            (str(bar.market), bar.symbol, bar.open_time_ms),
                        )
                        self._conn.execute(
                            f"INSERT INTO bars_1m({_COLUMNS})"  # noqa: S608 - fixed column list
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            _to_row(bar, observed_session_id),
                        )
                        result = WriteResult(WriteOutcome.REPLACED, existing)
                    else:
                        result = WriteResult(WriteOutcome.CONFLICT, existing)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return result

    # ------------------------------------------------------------------ reads
    def get(self, market: Market, symbol: str, open_time_ms: int) -> StoredBar | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM bars_1m"  # noqa: S608 - fixed column list
                " WHERE market=? AND symbol=? AND open_time_ms=?",
                (str(market), symbol, open_time_ms),
            ).fetchone()
        return None if row is None else _from_row(row)

    def range(
        self, market: Market, symbol: str, start_ms: int, end_ms: int
    ) -> list[StoredBar]:
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
