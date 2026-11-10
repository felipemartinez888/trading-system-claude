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

Run ids: an evaluation is recorded **before** it runs (a run that crashes is still a trial), so
it is recorded with a provisional run id; the real id (named after the manifest hash) exists only
once the artifacts are written. `attach_run` then replaces the provisional id: ``last_run_id``,
and ``first_run_id`` when this evaluation created the row. A run that fails after recording keeps
its provisional id, which names no run directory, and its evaluation stays counted.

``n_trials(family)`` -- the count of distinct configurations recorded for the strategy id -- is
the only value the runner passes to `certify` as ``n_trials``. Random-entry controls and cost
stress variants are diagnostics of a configuration, not candidate configurations, and are not
counted. Rows are never deleted. The only updates are the evaluation counter, the last-seen
fields, and `attach_run`'s replacement of a provisional run id by the run id it stood for.

TODO(M4, audit N5): enforce the preregistration's declared trial budget (refuse runs beyond it).
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Collection, Mapping
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

    def attach_run(self, trial_key: str, *, provisional_run_id: str, run_id: str) -> bool:
        """Replace an evaluation's provisional run id by the id of the run it produced.

        Only a field still holding ``provisional_run_id`` changes: ``last_run_id`` unless a later
        evaluation has been recorded since, and ``first_run_id`` only if this evaluation created
        the row. Returns whether ``last_run_id`` now names ``run_id``. Counts never change."""
        if not run_id or run_id == provisional_run_id:
            raise ValueError("run_id must be the real run id, different from the provisional id")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE research_trials SET first_run_id = ?"
                    " WHERE trial_key = ? AND first_run_id = ?",
                    (run_id, trial_key, provisional_run_id),
                )
                cur = conn.execute(
                    "UPDATE research_trials SET last_run_id = ?"
                    " WHERE trial_key = ? AND last_run_id = ?",
                    (run_id, trial_key, provisional_run_id),
                )
                attached = cur.rowcount == 1
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return attached

    def existing(self, trial_keys: Collection[str]) -> set[str]:
        """The subset of ``trial_keys`` already recorded (a projection of new trials)."""
        found: set[str] = set()
        conn = self._connect()
        try:
            for key in dict.fromkeys(trial_keys):
                row = conn.execute(
                    "SELECT 1 FROM research_trials WHERE trial_key = ?", (key,)
                ).fetchone()
                if row is not None:
                    found.add(key)
        finally:
            conn.close()
        return found

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
