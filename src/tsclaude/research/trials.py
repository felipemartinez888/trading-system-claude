"""Trial registry (ADR-0009, ADR-0012 Amendment 2.3): every evaluated configuration is recorded.

A dedicated table ``research_trials`` in ``state.db`` (next to the event store's tables; the
event-type list is Lead-owned, so trials are not events yet -- change requested). One row per
**configuration**: ``trial_key`` = SHA-256 over the canonical JSON of hypothesis, strategy id,
version, params hash, symbols, date range, segment, cost model and backtest-config hash. The seed
is not part of the key (it drives the bootstrap and the random control, not the configuration).
Re-running an identical configuration does not add a trial; it increments ``evaluations``.

``n_trials(hypothesis_id)`` -- the count of distinct configurations recorded for the hypothesis --
is the only value the runner passes to `certify` as ``n_trials``. Random-entry controls and cost
stress variants are diagnostics of a configuration, not candidate configurations, and are not
counted. Rows are never deleted or updated except the evaluation counter.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tsclaude.contracts.events import canonical_json

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_trials (
    trial_key        TEXT PRIMARY KEY,
    hypothesis_id    TEXT NOT NULL,
    strategy_id      TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    params_hash      TEXT NOT NULL,
    config_json      TEXT NOT NULL,
    first_run_id     TEXT NOT NULL,
    first_seen_ms    INTEGER NOT NULL,
    last_run_id      TEXT NOT NULL,
    last_seen_ms     INTEGER NOT NULL,
    evaluations      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS research_trials_hypothesis ON research_trials(hypothesis_id);
"""


@dataclass(frozen=True, slots=True)
class TrialRecord:
    trial_key: str
    hypothesis_id: str
    n_trials: int  # distinct configurations for the hypothesis, including this one
    evaluations: int  # times this exact configuration was evaluated
    new: bool


def trial_key(config: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(config)).encode("utf-8")).hexdigest()


class TrialRegistry:
    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, isolation_level=None, timeout=30.0)

    def record(
        self,
        *,
        hypothesis_id: str,
        strategy_id: str,
        strategy_version: str,
        params_hash: str,
        config: Mapping[str, Any],
        run_id: str,
        now_ms: int,
    ) -> TrialRecord:
        """Record one evaluation atomically and return the hypothesis' trial count."""
        if not hypothesis_id:
            raise ValueError("hypothesis_id is required")
        body = {
            "hypothesis_id": hypothesis_id,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "params_hash": params_hash,
            **dict(config),
        }
        key = trial_key(body)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT evaluations FROM research_trials WHERE trial_key = ?", (key,)
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO research_trials VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                        (
                            key,
                            hypothesis_id,
                            strategy_id,
                            strategy_version,
                            params_hash,
                            canonical_json(body),
                            run_id,
                            now_ms,
                            run_id,
                            now_ms,
                        ),
                    )
                    evaluations = 1
                else:
                    evaluations = int(row[0]) + 1
                    conn.execute(
                        "UPDATE research_trials SET evaluations = ?, last_run_id = ?,"
                        " last_seen_ms = ? WHERE trial_key = ?",
                        (evaluations, run_id, now_ms, key),
                    )
                (count,) = conn.execute(
                    "SELECT COUNT(*) FROM research_trials WHERE hypothesis_id = ?",
                    (hypothesis_id,),
                ).fetchone()
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return TrialRecord(key, hypothesis_id, int(count), evaluations, row is None)

    def n_trials(self, hypothesis_id: str) -> int:
        conn = self._connect()
        try:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM research_trials WHERE hypothesis_id = ?", (hypothesis_id,)
            ).fetchone()
        finally:
            conn.close()
        return int(count)

    def list(self, hypothesis_id: str | None = None) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            sql = (
                "SELECT trial_key, hypothesis_id, strategy_id, strategy_version, params_hash,"
                " first_run_id, first_seen_ms, last_run_id, last_seen_ms, evaluations"
                " FROM research_trials"
            )
            args: tuple[str, ...] = ()
            if hypothesis_id is not None:
                sql += " WHERE hypothesis_id = ?"
                args = (hypothesis_id,)
            rows = conn.execute(sql + " ORDER BY first_seen_ms, trial_key", args).fetchall()
        finally:
            conn.close()
        cols = (
            "trial_key",
            "hypothesis_id",
            "strategy_id",
            "strategy_version",
            "params_hash",
            "first_run_id",
            "first_seen_ms",
            "last_run_id",
            "last_seen_ms",
            "evaluations",
        )
        return [dict(zip(cols, r, strict=True)) for r in rows]
