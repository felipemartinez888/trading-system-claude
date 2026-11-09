"""`tsclaude data backfill-plan`: what a Start / the history run would load, without loading it.

Strictly read-only: the live-bar database is opened with `mode=ro`, nothing is fetched (no
network) and nothing is written. The plan mirrors the engine's rules: a stored bar is never
re-fetched, the catch-up is the bounded window, and anything older than it is the history run's.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from tsclaude.contracts.timeutil import MS_PER_DAY
from tsclaude.marketdata.config import BackfillConfig

STEP = 60_000


def _stored(conn: sqlite3.Connection, market: str, symbol: str, lo: int, hi: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM bars_1m WHERE market=? AND symbol=? AND open_time_ms>=? "
        "AND open_time_ms<?",
        (market, symbol, lo, hi),
    ).fetchone()
    return int(row[0])


def _last(conn: sqlite3.Connection, market: str, symbol: str) -> int | None:
    row = conn.execute(
        "SELECT MAX(open_time_ms) FROM bars_1m WHERE market=? AND symbol=?", (market, symbol)
    ).fetchone()
    return None if row[0] is None else int(row[0])


def plan(
    db_path: Path, config: BackfillConfig, pairs: list[tuple[str, str]], end_ms: int
) -> dict[str, Any]:
    """Per pair: rows already stored and minutes still to fetch, for the catch-up window and the
    full history window, plus the number of REST requests and the configured per-run limits."""
    end_ms -= end_ms % STEP
    run = config.run
    cap = config.startup.catchup_max_minutes
    lookback_min = int(config.lookback_days * MS_PER_DAY) // STEP
    catchup_min = lookback_min if cap is None else min(cap, lookback_min)
    out: list[dict[str, Any]] = []
    conn: sqlite3.Connection | None = None
    if db_path.exists():
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5.0)
    try:
        for market, symbol in pairs:
            last = _last(conn, market, symbol) if conn is not None else None
            row: dict[str, Any] = {"market": market, "symbol": symbol, "last_stored_ms": last}
            for name, minutes in (("catchup", catchup_min), ("history", lookback_min)):
                lo = end_ms - minutes * STEP
                have = _stored(conn, market, symbol, lo, end_ms) if conn is not None else 0
                missing = max(minutes - have, 0)
                row[name] = {
                    "window_minutes": minutes,
                    "stored_minutes": have,
                    "missing_minutes": missing,
                    "chunks": -(-missing // run.chunk_minutes),
                }
            out.append(row)
    finally:
        if conn is not None:
            conn.close()
    return {
        "end_ms": end_ms,
        "database": str(db_path),
        "database_exists": db_path.exists(),
        "history_policy": "legacy" if cap is None else config.startup.history,
        "limits": {
            "chunk_minutes": run.chunk_minutes,
            "inter_chunk_pause_ms": run.inter_chunk_pause_ms,
            "pair_budget_s": run.pair_budget_s,
            "run_budget_s": run.run_budget_s,
        },
        "pairs": out,
        "note": "plan only: nothing was fetched or written; stored bars are never re-fetched",
    }
