"""Trial registry (ADR-0009, ADR-0012 Amendment 2.3): every evaluated configuration is recorded.

A dedicated table ``research_trials`` in ``state.db`` (next to the event store's tables; the
event-type list is Lead-owned, so trials are not events yet -- change requested). One row per
**configuration**: ``trial_key`` = SHA-256 over the canonical JSON of the strategy id (the
*family*), version, params hash, symbols, date range, segment, the full cost-model parameters,
the backtest-config hash and -- only when the caller chose one explicitly -- the seed (Lead
decision on the M2-T4 audit: a research run's seed is derived from its trial key, so re-rolling
seeds is only possible by recording new trials). The hypothesis / preregistration id is a
**label** stored with the row; it is not part of the key and does not partition the count, so a
parameter search cannot reset the count by renaming its hypothesis.
Re-running an identical configuration does not add a trial; it increments ``evaluations``.

``n_trials(family)`` -- the count of distinct configurations recorded for the strategy id -- is
the only value the runner passes to `certify` as ``n_trials``. Random-entry controls and cost
stress variants are diagnostics of a configuration, not candidate configurations, and are not
counted. Rows are never deleted or updated except the evaluation counter and last-seen fields.

TODO(M4, audit N5): enforce the preregistration's declared trial budget (refuse runs beyond it).
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
    hypothesis_label TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS research_trials_family ON research_trials(strategy_id);
"""


@dataclass(frozen=True, slots=True)
class TrialRecord:
    trial_key: str
    family: str  # strategy id
    n_trials: int  # distinct configurations of the family, including this one
    evaluations: int  # times this exact configuration was evaluated
    new: bool


def trial_body(
    *, strategy_id: str, strategy_version: str, params_hash: str, config: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "params_hash": params_hash,
        **dict(config),
    }


def trial_key(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(body)).encode("utf-8")).hexdigest()


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
        hypothesis_label: str,
        strategy_id: str,
        strategy_version: str,
        params_hash: str,
        config: Mapping[str, Any],
        run_id: str,
        now_ms: int,
    ) -> TrialRecord:
        """Record one evaluation atomically and return the family's trial count."""
        if not strategy_id:
            raise ValueError("strategy_id is required")
        body = trial_body(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            params_hash=params_hash,
            config=config,
        )
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
                            hypothesis_label,
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
                    "SELECT COUNT(*) FROM research_trials WHERE strategy_id = ?",
                    (strategy_id,),
                ).fetchone()
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return TrialRecord(key, strategy_id, int(count), evaluations, row is None)

    def n_trials(self, family: str) -> int:
        """Distinct configurations recorded for a strategy id (the family)."""
        conn = self._connect()
        try:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM research_trials WHERE strategy_id = ?", (family,)
            ).fetchone()
        finally:
            conn.close()
        return int(count)

    def list(self, family: str | None = None) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            sql = (
                "SELECT trial_key, hypothesis_label, strategy_id, strategy_version, params_hash,"
                " first_run_id, first_seen_ms, last_run_id, last_seen_ms, evaluations"
                " FROM research_trials"
            )
            args: tuple[str, ...] = ()
            if family is not None:
                sql += " WHERE strategy_id = ?"
                args = (family,)
            rows = conn.execute(sql + " ORDER BY first_seen_ms, trial_key", args).fetchall()
        finally:
            conn.close()
        cols = (
            "trial_key",
            "hypothesis_label",
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
