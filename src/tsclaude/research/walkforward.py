"""Anchored walk-forward on the development segment (RESEARCH_PROTOCOL §3; M4-T1 deliverable 4).

Procedure, fixed before any result is seen:

1. **Folds** (`plan_folds`): train windows are anchored at the segment start and grow; test windows
   have a fixed length and roll forward. An embargo of at least one maximum holding period
   separates every train end from its test start, so a trade that a training run could still hold
   never overlaps the test window (each run is also flat at its own window end: the runner's
   ``flat_from_ms``).
2. **Selection on train only** (`select`): every configuration of the grid is evaluated on the
   fold's train window and ranked by the preregistered objective (ADR-0012): certified win-rate
   margin lower bound ``margin_lcb`` first, then ``p_lcb``, then the configuration id (a stable,
   result-independent tie-break). A configuration needs ``n >= n_min`` trades to be eligible.
3. **One test evaluation per fold**: only the selected configuration runs on the test window.
4. **Stitched out-of-sample verdict** (`verdict`): the test-window outcomes of every fold, in fold
   order, are certified together with the family's trial count from the registry (every
   configuration *and window* evaluated is a trial there, train runs included: conservative). A
   positive verdict is never "validated": the protocol still requires the single preregistered
   validation run (2025) and the sealed holdout (RESEARCH_PROTOCOL §2, §6).

Fail closed on invalid runs (audit WF-1): a run with integrity problems (the runner grades it X:
data gaps, timing or ledger failures, source errors, missing costs) is never used. A train run
with problems makes its configuration ineligible for that fold; a test run with problems voids
the stitched verdict (`INVALID_OOS_EVIDENCE`), because dropping a failed test window would let
data quality decide which out-of-sample periods count. Both are reported per fold.

Warm-up-aware anchor (audit WF-2): every run reads the strategy's trailing window before its
``from`` (`runner.warmup_ms_of`). The anchor must leave that warm-up inside the stored data
(`earliest_anchor_ms`); an earlier anchor is refused before anything runs.

The core (`plan_folds`, `select`, `walk_forward`, `verdict`) is pure: it sees results only through
an `Evaluator`, so a canary test can prove that selection never touches a test window.
`RunnerEvaluator` runs each evaluation as a normal recorded backtest (`research.runner`), so every
run keeps its manifest, holdout guard and trial-registry entry. No result here is evidence of
profitability; "no edge demonstrated" is the expected and reportable outcome (RESEARCH_PROTOCOL).
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from tsclaude.contracts.timeutil import MS_PER_MINUTE

if TYPE_CHECKING:
    from tsclaude.contracts.market import Market
    from tsclaude.research.runner import BacktestConfig, BacktestRequest, RunEnv, RunOutcome
    from tsclaude.research.stats.certify import Certification
    from tsclaude.research.wf_progress import ProgressLedger

WALKFORWARD_VERSION = "walkforward-v2"  # v2: integrity gates (WF-1), warm-up anchor (WF-2)

#: verdicts; none of them is a validation result
INSUFFICIENT_OOS_EVIDENCE = "INSUFFICIENT_OOS_EVIDENCE"
INVALID_OOS_EVIDENCE = "INVALID_OOS_EVIDENCE"  # a test run failed integrity: nothing certified
NO_EDGE_DEMONSTRATED = "NO_EDGE_DEMONSTRATED"
OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY = "OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY"


class WalkForwardError(ValueError):
    pass


# ---------------------------------------------------------------- folds
@dataclass(frozen=True, slots=True)
class Fold:
    index: int
    train_from_ms: int
    train_to_ms: int  # exclusive
    test_from_ms: int  # train_to_ms + embargo
    test_to_ms: int  # exclusive

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "train": [_iso(self.train_from_ms), _iso(self.train_to_ms)],
            "test": [_iso(self.test_from_ms), _iso(self.test_to_ms)],
            "embargo_minutes": (self.test_from_ms - self.train_to_ms) // MS_PER_MINUTE,
        }


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_months(ms: int, months: int) -> int:
    """``ms`` (a UTC instant) moved by whole calendar months; the instant must be a month start
    at midnight so the result is unambiguous."""
    dt = datetime.fromtimestamp(ms / 1000, UTC)
    if (dt.day, dt.hour, dt.minute, dt.second, dt.microsecond) != (1, 0, 0, 0, 0):
        raise WalkForwardError(f"{_iso(ms)} is not a UTC month start")
    total = dt.year * 12 + (dt.month - 1) + months
    return int(datetime(total // 12, total % 12 + 1, 1, tzinfo=UTC).timestamp() * 1000)


def plan_folds(
    start_ms: int,
    end_ms: int,
    *,
    initial_train_months: int = 12,
    test_months: int = 3,
    embargo_ms: int,
) -> list[Fold]:
    """Anchored folds inside ``[start_ms, end_ms)``; only whole test windows are kept."""
    if initial_train_months < 1 or test_months < 1:
        raise WalkForwardError("initial_train_months and test_months must be >= 1")
    if embargo_ms < MS_PER_MINUTE or embargo_ms % MS_PER_MINUTE:
        raise WalkForwardError(
            "embargo_ms must be a whole number of minutes, at least one minute (use at least "
            "one maximum holding period; every run is also flat at its own window end)"
        )
    folds: list[Fold] = []
    train_to = add_months(start_ms, initial_train_months)
    while True:
        test_to = add_months(train_to, test_months)
        test_from = train_to + embargo_ms
        if test_to > end_ms:
            break
        if test_from >= test_to:
            raise WalkForwardError("the embargo is longer than a test window")
        folds.append(Fold(len(folds), start_ms, train_to, test_from, test_to))
        train_to = test_to
    if not folds:
        raise WalkForwardError(
            f"no whole fold fits in [{_iso(start_ms)}, {_iso(end_ms)}) with "
            f"{initial_train_months} train months and {test_months} test months"
        )
    return folds


def month_start_at_or_after(ms: int) -> int:
    """The first UTC month start (midnight) at or after ``ms``."""
    dt = datetime.fromtimestamp(ms / 1000, UTC)
    floor = int(datetime(dt.year, dt.month, 1, tzinfo=UTC).timestamp() * 1000)
    return floor if floor == ms else add_months(floor, 1)


def earliest_anchor_ms(data_start_ms: int, warmup_ms: int) -> int:
    """The first UTC month start whose warm-up ``[anchor - warmup_ms, anchor)`` lies inside data
    that starts at ``data_start_ms`` (audit WF-2)."""
    if warmup_ms < 0:
        raise WalkForwardError("warm-up must not be negative")
    return month_start_at_or_after(data_start_ms + warmup_ms)


def check_anchor(anchor_ms: int, *, data_start_ms: int | None, warmup_ms: int) -> None:
    """Refuse an anchor whose first training run could not read its full warm-up."""
    if data_start_ms is None:
        raise WalkForwardError("no stored 1m bars for the requested symbols")
    earliest = earliest_anchor_ms(data_start_ms, warmup_ms)
    if anchor_ms < earliest:
        raise WalkForwardError(
            f"anchor {_iso(anchor_ms)} leaves no full warm-up inside the data: the runs read "
            f"{warmup_ms // MS_PER_MINUTE} minutes before 'from' and the data starts at "
            f"{_iso(data_start_ms)}; the earliest valid anchor is {_iso(earliest)}"
        )


def data_start_ms(
    reader: Any, symbols: Sequence[tuple[Market, str]], dataset_ids: Sequence[str] | None = None
) -> int | None:
    """The latest of the symbols' first stored 1m bars (every symbol needs its warm-up), from the
    lake index; None when a symbol has no data."""
    firsts = [reader.first_bar_ms(m, s, dataset_ids=dataset_ids) for m, s in symbols]
    if not firsts or any(f is None for f in firsts):
        return None
    return max(int(f) for f in firsts if f is not None)


def grid_warmup_ms(
    strategy_id: str,
    grid: Mapping[str, Mapping[str, Any]],
    build: Callable[[str, Mapping[str, Any]], Any] | None = None,
) -> int:
    """The longest warm-up of any configuration in the grid (`runner.warmup_ms_of`)."""
    from tsclaude.research.runner import warmup_ms_of
    from tsclaude.strategies import build_strategy

    make = build or build_strategy
    if not grid:
        raise WalkForwardError("the grid is empty")
    warmups = []
    for config_id, params in grid.items():
        try:
            strategy = make(strategy_id, dict(params))
        except TypeError as exc:  # unknown or missing parameter names
            raise WalkForwardError(f"invalid parameters for {config_id}: {exc}") from exc
        warmups.append(warmup_ms_of(strategy))
    return max(warmups)


def default_anchor_ms(data_start: int, warmup_ms: int) -> int:
    """The default anchor: the earliest valid one (`earliest_anchor_ms`), never before the
    development segment starts."""
    from tsclaude.research.runner import DEV_START_MS

    return max(DEV_START_MS, earliest_anchor_ms(data_start, warmup_ms))


def check_warmup_complete(
    env: RunEnv,
    symbols: Sequence[tuple[Market, str]],
    anchor_ms: int,
    warmup_ms: int,
    dataset_ids: Sequence[str] | None = None,
) -> None:
    """Refuse an anchor whose warm-up window ``[anchor - warmup, anchor)`` has missing minutes
    (outside registered exchange interruptions): every first-fold train run would be graded X
    for it. A guarded research read of development data; nothing is recorded."""
    from tsclaude.store.holdout import Purpose

    lo = anchor_ms - warmup_ms
    if warmup_ms <= 0:
        return
    for market, symbol in symbols:
        res = env.reader.read_bars(
            market, symbol, lo, anchor_ms, purpose=Purpose.RESEARCH, dataset_ids=dataset_ids
        )
        missing = 0
        for a, b in res.missing:
            _, outside, _ = env.exchange_interruptions.split_missing(market, a, b)
            missing += outside
        if missing:
            raise WalkForwardError(
                f"the warm-up before the anchor {_iso(anchor_ms)} has {missing} missing 1m "
                f"bar(s) for {market}:{symbol}; every first-fold train run would be graded X. "
                "Choose a later anchor"
            )


# ---------------------------------------------------------------- selection
@dataclass(frozen=True, slots=True)
class Score:
    """What selection needs from one train-window evaluation (the run id is kept for audit).

    ``problems`` lists why the run cannot be used (integrity, unverifiable artifacts); a score
    with problems is never eligible, whatever its numbers say (audit WF-1)."""

    config_id: str
    n: int
    margin_lcb: float | None
    p_lcb: float | None
    run_id: str | None = None
    problems: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        # an invalid run's numbers are not evidence: they are not reported (the run id is)
        return {
            "config_id": self.config_id,
            "n": self.n if self.valid else None,
            "margin_lcb": self.margin_lcb if self.valid else None,
            "p_lcb": self.p_lcb if self.valid else None,
            "run_id": self.run_id,
            "valid": self.valid,
            "problems": list(self.problems),
        }


def select(scores: Sequence[Score], *, n_min: int) -> tuple[Score | None, list[str]]:
    """The best eligible configuration by the preregistered objective, or None with reasons.

    Eligible: a valid run (no integrity problems) with ``n >= n_min`` trades and a certified
    margin bound."""
    if n_min < 1:
        raise WalkForwardError("n_min must be >= 1")
    ranked = sorted(
        (
            (-s.margin_lcb, -s.p_lcb, s.config_id, i)
            for i, s in enumerate(scores)
            if s.valid and s.n >= n_min and s.margin_lcb is not None and s.p_lcb is not None
        ),
    )
    if not ranked:
        reasons = [
            f"no configuration reached n >= {n_min} trades with a certified margin bound on a "
            "valid train run"
        ]
        invalid = [s.config_id for s in scores if not s.valid]
        if invalid:
            reasons.append(
                f"{len(invalid)} of {len(scores)} train run(s) failed integrity and are "
                f"ineligible: {', '.join(invalid)}"
            )
        return None, reasons
    return scores[ranked[0][3]], []


# ---------------------------------------------------------------- orchestration
@dataclass(frozen=True, slots=True)
class OosRun:
    """One test-window evaluation: outcomes (R, chronological), the run that produced them and
    why it cannot be used, if it cannot (integrity, unverifiable artifacts)."""

    outcomes: tuple[float, ...]
    run_id: str | None
    problems: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.problems


class Evaluator(Protocol):
    def score(self, config_id: str, fold: Fold) -> Score:
        """Evaluate a configuration on the fold's **train** window only."""
        ...

    def oos(self, config_id: str, fold: Fold) -> OosRun:
        """Evaluate a configuration on the fold's **test** window."""
        ...

    def rescore(self, scores: list[Score], fold: Fold) -> list[Score]:
        """The fold's train scores made comparable before selection: every score certified with
        one common trial count (review finding: each run is certified with the registry count at
        its own record time, so later grid entries would face a stricter correction). An
        evaluator whose scores are already comparable returns them unchanged."""
        ...


@dataclass
class FoldResult:
    fold: Fold
    scores: list[Score]
    selected: str | None
    reasons: list[str]
    oos_outcomes: list[float] = field(default_factory=list)
    oos_run_id: str | None = None
    oos_problems: list[str] = field(default_factory=list)

    @property
    def train_invalid(self) -> list[str]:
        return [s.config_id for s in self.scores if not s.valid]

    @property
    def test_failed(self) -> bool:
        """A test run was made and cannot be used: the stitched verdict is void."""
        return self.selected is not None and bool(self.oos_problems)

    @property
    def status(self) -> str:
        if self.selected is None:
            return "NO_SELECTION"
        return "TEST_INVALID" if self.oos_problems else "TESTED"

    def to_dict(self) -> dict[str, Any]:
        wins = sum(1 for r in self.oos_outcomes if r > 0)
        return {
            **self.fold.to_dict(),
            "status": self.status,
            "scores": [s.to_dict() for s in self.scores],
            "train_invalid": self.train_invalid,
            "selected": self.selected,
            "reasons": list(self.reasons),
            "oos_trades": None if self.test_failed else len(self.oos_outcomes),
            "oos_wins": None if self.test_failed else wins,
            "oos_run_id": self.oos_run_id,
            "oos_valid": None if self.selected is None else not self.oos_problems,
            "oos_problems": list(self.oos_problems),
        }


@dataclass
class WalkForwardResult:
    config_ids: list[str]
    folds: list[FoldResult]
    planned_folds: int | None = None  # None: every planned fold ran

    @property
    def n_train_evaluations(self) -> int:
        return sum(len(f.scores) for f in self.folds)

    @property
    def n_test_evaluations(self) -> int:
        return sum(1 for f in self.folds if f.selected is not None)

    @property
    def failed_test_folds(self) -> list[int]:
        return [f.fold.index for f in self.folds if f.test_failed]

    def oos_outcomes(self) -> list[float]:
        """Test-window outcomes stitched in fold order (chronological). Refused when a test run
        failed integrity: such a set must never be certified (audit WF-1)."""
        if self.failed_test_folds:
            raise WalkForwardError(
                f"test run(s) of fold(s) {self.failed_test_folds} failed integrity; the stitched "
                "out-of-sample set is void"
            )
        out: list[float] = []
        for f in self.folds:
            out.extend(f.oos_outcomes)
        return out


def walk_forward(
    config_ids: Sequence[str], folds: Sequence[Fold], evaluator: Evaluator, *, n_min: int
) -> WalkForwardResult:
    """Select on each train window, then evaluate only the selected configuration on its test."""
    if not config_ids:
        raise WalkForwardError("the grid is empty")
    if len(set(config_ids)) != len(config_ids):
        raise WalkForwardError("configuration ids must be unique")
    results: list[FoldResult] = []
    for fold in folds:
        scores = evaluator.rescore([evaluator.score(c, fold) for c in config_ids], fold)
        if [s.config_id for s in scores] != list(config_ids):
            raise WalkForwardError("rescore must keep one score per configuration, in order")
        best, reasons = select(scores, n_min=n_min)
        result = FoldResult(fold, scores, None if best is None else best.config_id, reasons)
        if best is not None:
            run = evaluator.oos(best.config_id, fold)
            result.oos_outcomes = list(run.outcomes)
            result.oos_run_id = run.run_id
            result.oos_problems = list(run.problems)
        results.append(result)
        if result.test_failed:
            # the stitched verdict is void: every further run would only add trials
            break
    return WalkForwardResult(list(config_ids), results, planned_folds=len(folds))


# ---------------------------------------------------------------- verdict
def verdict(
    result: WalkForwardResult,
    cert: Certification | None,
    *,
    n_trials: int,
    void_reasons: Sequence[str] = (),
    min_oos_coverage: float = 1.0,
) -> dict[str, Any]:
    """The stitched out-of-sample verdict. ``cert`` certifies `result.oos_outcomes()` (None when
    fewer than two out-of-sample trades exist, and always None when a test run failed
    integrity). Never a validation result."""
    not_validation = (
        "Development-segment walk-forward only. Validation (2025, one preregistered run) and "
        "the sealed holdout are still required before any grade above C (RESEARCH_PROTOCOL "
        "§2, §6)."
    )
    planned = result.planned_folds if result.planned_folds is not None else len(result.folds)
    if not 0.0 < min_oos_coverage <= 1.0:
        raise WalkForwardError("min_oos_coverage must be in (0, 1]")
    no_valid_train = [
        f.fold.index for f in result.folds if f.scores and len(f.train_invalid) == len(f.scores)
    ]
    planned_minutes = sum(f.fold.test_to_ms - f.fold.test_from_ms for f in result.folds)
    tested_minutes = sum(
        f.fold.test_to_ms - f.fold.test_from_ms for f in result.folds if f.status == "TESTED"
    )
    share = tested_minutes / planned_minutes if planned_minutes else 0.0
    coverage: dict[str, Any] = {
        "oos_coverage": {
            "tested_test_minutes": tested_minutes // MS_PER_MINUTE,
            "planned_test_minutes": planned_minutes // MS_PER_MINUTE,
            "share": share,
            "min_required": min_oos_coverage,
        },
        "n_trials": n_trials,
        "folds_planned": planned,
        "folds": len(result.folds),
        "folds_with_selection": result.n_test_evaluations,
        "folds_without_valid_train": no_valid_train,
        "folds_with_invalid_train_runs": [f.fold.index for f in result.folds if f.train_invalid],
        "failed_test_folds": result.failed_test_folds,
        "not_validation": not_validation,
    }
    if result.failed_test_folds or void_reasons:
        if cert is not None:
            raise WalkForwardError("a certification over a void out-of-sample set was given")
        reasons = [
            f"fold {f.fold.index} test run {f.oos_run_id} failed integrity: "
            + "; ".join(f.oos_problems)
            for f in result.folds
            if f.test_failed
        ]
        reasons += list(void_reasons)
        if len(result.folds) < planned:
            reasons.append(
                f"stopped after fold {result.folds[-1].fold.index}: {planned - len(result.folds)} "
                "planned fold(s) not run (the verdict was already void)"
            )
        return {
            **coverage,
            "oos_trades": None,
            "oos_wins": None,
            "verdict": INVALID_OOS_EVIDENCE,
            "reasons": reasons,
        }
    outcomes = result.oos_outcomes()
    base: dict[str, Any] = {
        **coverage,
        "oos_trades": len(outcomes),
        "oos_wins": sum(1 for r in outcomes if r > 0),
    }
    if no_valid_train:
        base["coverage_note"] = (
            f"fold(s) {no_valid_train} had no valid train run, so their test windows are not in "
            "the stitched set. The omission depends only on data and runs before each test "
            "window (train-run integrity: data gaps, funding not applied, signal-source, timing "
            "or ledger failures), never on a test result. With anchored windows one train-window "
            "gap removes every later fold"
        )
    if share < min_oos_coverage:
        # a certified-looking result on a small part of the planned period is not reported
        untested = [f.fold.index for f in result.folds if f.status != "TESTED"]
        return {
            **base,
            "verdict": INSUFFICIENT_OOS_EVIDENCE,
            "reasons": [
                f"out-of-sample coverage {share:.3f} of the planned test period is below the "
                f"required {min_oos_coverage:.3f}; untested fold(s) {untested}"
            ],
        }
    if cert is None:
        return {
            **base,
            "verdict": INSUFFICIENT_OOS_EVIDENCE,
            "reasons": [f"{len(outcomes)} out-of-sample trade(s): nothing to certify"],
        }
    base.update(
        {
            "win_rate": cert.p_hat,
            "win_rate_ci": {"lower": cert.ci.lower, "upper": cert.ci.upper},
            "p_lcb": cert.p_lcb,
            "p_star": cert.p_star,
            "p_star_source": cert.p_star_source,
            "margin_lcb": cert.margin_lcb,
            "expectancy_r": cert.expectancy,
            "expectancy_lcb": cert.expectancy_lcb,
            "n_required": cert.n_required,
            "n_eff": cert.n_eff,
        }
    )
    if not cert.certified:
        return {**base, "verdict": NO_EDGE_DEMONSTRATED, "reasons": list(cert.reasons)}
    return {
        **base,
        "verdict": OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY,
        "reasons": ["stitched out-of-sample margin and expectancy certified on development data"],
    }


def certify_oos(
    outcomes: Sequence[float],
    *,
    cfg: BacktestConfig,
    n_trials: int,
    max_loss_r: float,
    max_loss_source: str,
    seed: int,
) -> Certification | None:
    """Certify stitched outcomes. The selected configurations may differ per fold (and so their
    designed break-even), so the break-even is the **realized** one (``p_star=None``)."""
    from tsclaude.research.stats.certify import certify

    if len(outcomes) < 2:
        return None
    return certify(
        list(outcomes),
        p_star=None,
        alpha=cfg.alpha,
        n_trials=max(1, n_trials),
        min_power=cfg.min_power,
        mean_block=None,
        seed=seed,
        max_loss_r=max_loss_r,
        max_loss_source=max_loss_source,
        n_resamples=cfg.n_resamples,
    )


# ---------------------------------------------------------------- adapter to the runner
def config_id_of(params: Mapping[str, Any]) -> str:
    """A stable id for a parameter set (content hash), independent of results."""
    text = json.dumps(dict(params), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def run_problems(metrics: Mapping[str, Any] | None) -> list[str]:
    """Why a recorded run cannot be used by the walk-forward (empty: usable). Fail closed: a run
    without metrics or without an integrity block is unusable, and so is a grade-X run."""
    if not metrics:
        return ["the run has no metrics"]
    integrity = metrics.get("integrity")
    if not isinstance(integrity, Mapping) or not isinstance(integrity.get("problems"), list):
        return ["the run has no integrity record"]
    problems = [f"integrity: {p}" for p in integrity["problems"]]
    grade = metrics.get("evidence_grade")
    if grade == "X" and not problems:
        problems = [f"graded X: {r}" for r in metrics.get("grade_reasons") or ["no reason"]]
    elif grade not in _GRADES:
        problems.append(f"the run has no valid evidence grade ({grade!r})")
    return problems


_GRADES = frozenset({"A", "B", "C", "D", "X"})


def trades_file_problems(run_dir: Path, manifest: Mapping[str, Any]) -> list[str]:
    """Problems if a run's trades file is missing or does not match its manifest hash."""
    path = run_dir / "trades.parquet"
    expected = (manifest.get("outputs") or {}).get("trades_parquet_sha256")
    if not path.is_file():
        return ["the run has no trades file"]
    if not expected or _sha256_file(path) != expected:
        return ["the trades file does not match the manifest hash"]
    return []


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def baseline_outcomes(run_dir: Path, manifest: Mapping[str, Any]) -> tuple[list[float], list[str]]:
    """The baseline book's outcomes (R) in entry order, read from a run's trades file after its
    hash is checked against the manifest. Problems instead of outcomes if it does not match."""
    from tsclaude.research.parquet import read_table

    problems = trades_file_problems(run_dir, manifest)
    if problems:
        return [], problems
    _, rows = read_table(run_dir / "trades.parquet")
    base = [r for r in rows if r.get("book") == "baseline"]
    base.sort(key=lambda r: (int(r.get("entry_bar_open_ms") or 0), str(r.get("symbol"))))
    return [float(r["r"]) for r in base], []


class RunnerEvaluator:
    """Each evaluation is a normal recorded backtest (manifest, holdout guard, trial registry).

    With a progress ledger (`research.wf_progress`), a completed evaluation of the same plan is
    reused after verification instead of run again (resume after a restart, WF-4)."""

    def __init__(
        self,
        env: RunEnv,
        cfg: BacktestConfig,
        *,
        strategy_id: str,
        grid: Mapping[str, Mapping[str, Any]],
        symbols: Sequence[tuple[Market, str]],
        hypothesis_id: str | None = None,
        build: Callable[[str, Mapping[str, Any]], Any] | None = None,
        prereg_id: str | None = None,
        dataset_ids: Sequence[str] | None = None,
        ledger: ProgressLedger | None = None,
        code_commit: str | None = None,
    ) -> None:
        from tsclaude.strategies import build_strategy

        if ledger is not None and not code_commit:
            raise WalkForwardError("resuming needs an identified code commit")
        self._env = env
        self._cfg = cfg
        self._strategy_id = strategy_id
        self._grid = {k: dict(v) for k, v in grid.items()}
        self._symbols = list(symbols)
        self._hypothesis_id = hypothesis_id
        self._build = build or build_strategy
        self._prereg_id = prereg_id
        self._pins = None if dataset_ids is None else sorted(dataset_ids)
        self._ledger = ledger
        self._code_commit = code_commit
        self.runs: list[RunOutcome] = []
        self.by_key: dict[tuple[str, int, str], RunOutcome] = {}
        self.reused = 0
        self.executed = 0
        #: the trial count every fold is rescored with; None: the registry's count at the time
        self.rescore_trials: int | None = None

    def attach_ledger(self, ledger: ProgressLedger, code_commit: str) -> None:
        if not code_commit:
            raise WalkForwardError("resuming needs an identified code commit")
        self._ledger = ledger
        self._code_commit = code_commit

    def request(self, config_id: str, from_ms: int, to_ms: int) -> BacktestRequest:
        from tsclaude.research.runner import BacktestRequest

        pins = None if self._pins is None else {"main": self._pins, "max_loss": self._pins}
        return BacktestRequest(
            strategy=self._build(self._strategy_id, self._grid[config_id]),
            symbols=self._symbols,
            from_ms=from_ms,
            to_ms=to_ms,
            hypothesis_id=self._hypothesis_id,
            prereg_id=self._prereg_id,
            dataset_ids=pins,
        )

    def trial_key(self, config_id: str, from_ms: int, to_ms: int) -> str:
        from tsclaude.research.runner import trial_identity

        return trial_identity(self.request(config_id, from_ms, to_ms), self._env, self._cfg)[0]

    def _run(self, phase: str, index: int, config_id: str, from_ms: int, to_ms: int) -> RunOutcome:
        from tsclaude.research.runner import RunOutcome, run_backtest
        from tsclaude.research.wf_progress import (
            ResumeRefused,
            ReuseExpectation,
            entry_for,
            verify_reused_run,
        )

        entry = None if self._ledger is None else self._ledger.get(phase, index, config_id)
        if entry is not None:
            expect = ReuseExpectation(
                trial_key=self.trial_key(config_id, from_ms, to_ms),
                from_ms=from_ms,
                to_ms=to_ms,
                prereg_id=self._prereg_id,
                code_commit=str(self._code_commit),
            )
            if not self._env.trials.existing({expect.trial_key}):
                raise ResumeRefused(
                    f"recorded run {entry.run_id} ({entry.key}) cannot be reused: its trial is not "
                    "in this trial registry (another or a restored state.db); reusing it would "
                    "under-count trials"
                )
            run_dir, manifest, metrics, sha = verify_reused_run(self._env.runs_dir, entry, expect)
            out = RunOutcome(str(manifest["run_id"]), run_dir, manifest, metrics, sha)
            self.reused += 1
        else:
            out = run_backtest(self.request(config_id, from_ms, to_ms), self._env, self._cfg)
            self.executed += 1
            if self._ledger is not None:
                self._ledger.append(entry_for(phase, index, config_id, out.run_dir, out.manifest))
        self.runs.append(out)
        self.by_key[(phase, index, config_id)] = out
        return out

    def evaluate_window(
        self, config_id: str, phase: str, index: int, from_ms: int, to_ms: int
    ) -> Score:
        """A train-like evaluation (selection input) of one configuration on one window."""
        out = self._run(phase, index, config_id, from_ms, to_ms)
        cert = out.metrics.get("certification") or {}
        return Score(
            config_id=config_id,
            n=int(out.metrics.get("trades", {}).get("n") or 0),
            margin_lcb=cert.get("margin_lcb"),
            p_lcb=cert.get("p_lcb"),
            run_id=out.run_id,
            problems=tuple(run_problems(out.metrics)),
        )

    def score(self, config_id: str, fold: Fold) -> Score:
        return self.evaluate_window(
            config_id, "train", fold.index, fold.train_from_ms, fold.train_to_ms
        )

    def rescore(self, scores: list[Score], fold: Fold | None = None) -> list[Score]:
        """Re-certify every train run of the fold with one common trial count, via the runner's
        own recompute (`load_run(..., recompute=True)`, audit N1): `rescore_trials` when set (a
        walk-forward fixes it at its start for every fold, so a resumed walk-forward selects
        exactly as an uninterrupted one), else the registry's count now. A run whose stored
        count already equals it keeps its stored certification, which used that count. The run
        is re-read from disk: unreadable or unverifiable metrics or trades, integrity problems,
        or a failed recompute make it ineligible."""
        from tsclaude.research.runner import load_run

        target = (
            self.rescore_trials
            if self.rescore_trials is not None
            else self._env.trials.n_trials(self._strategy_id)
        )
        out: list[Score] = []
        for s in scores:
            loaded = (
                None
                if s.run_id is None
                else load_run(
                    self._env.runs_dir,
                    s.run_id,
                    self._env.trials,
                    recompute=True,
                    n_trials=target,
                )
            )
            if loaded is None:
                why = (*s.problems, "the run cannot be read back")
                out.append(Score(s.config_id, s.n, None, None, s.run_id, why))
                continue
            problems = list(s.problems)
            if loaded["summary"].get("metrics_verified") is not True:
                problems.append("the run's metrics do not match the manifest hash")
            for p in run_problems(loaded.get("metrics")) + trades_file_problems(
                self._env.runs_dir / str(s.run_id), loaded.get("manifest") or {}
            ):
                if p not in problems:
                    problems.append(p)
            current = loaded.get("current") or {}
            if not current and loaded["summary"].get("n_trials_recorded") != target:
                problems.append(  # no stale certification with a smaller trial count
                    "the run could not be re-certified with the current trial count"
                )
            if current.get("evidence_grade") == "X" and not problems:
                problems.append("graded X on recompute")
            cert = current.get("certification") if current else None
            if cert is None and not current:
                cert = (loaded.get("metrics") or {}).get("certification")
            cert = cert or {}
            out.append(
                Score(
                    s.config_id,
                    s.n,
                    cert.get("margin_lcb"),
                    cert.get("p_lcb"),
                    s.run_id,
                    tuple(problems),
                )
            )
        return out

    def oos(self, config_id: str, fold: Fold) -> OosRun:
        out = self._run("test", fold.index, config_id, fold.test_from_ms, fold.test_to_ms)
        problems = run_problems(out.metrics)
        outcomes, file_problems = baseline_outcomes(out.run_dir, out.manifest)
        return OosRun(tuple(outcomes), out.run_id, tuple(problems + file_problems))

    def strategy_hash(self, config_id: str) -> str:
        return str(self._build(self._strategy_id, self._grid[config_id]).strategy_hash)

    def max_loss(self) -> tuple[float, str]:
        """The largest max-loss bound among this walk-forward's runs (train and test), as each
        run measured it (L-10: worst losing exit on the development segment x safety factor,
        never below the safety factor times the planned stop). It feeds only the expectancy
        bound of the stitched certification, never selection. Only valid runs count. 1.5R if
        no valid run recorded one."""
        values: list[tuple[float, str]] = []
        for out in self.runs:
            if run_problems(out.metrics):
                continue  # an invalid run's measurement is not used either
            ctx = out.metrics.get("evaluation_inputs") or {}
            ml = ctx.get("max_loss") or {}
            if "value" in ml:
                values.append((float(ml["value"]), str(ml.get("source", "run"))))
        if not values:
            return 1.5, "default 1.5R (no measured value)"
        return max(values)


# ---------------------------------------------------------------- plan identity (resume)
def plan_identity(
    env: RunEnv,
    cfg: BacktestConfig,
    *,
    strategy_id: str,
    grid: Mapping[str, Mapping[str, Any]],
    symbols: Sequence[tuple[Market, str]],
    folds: Sequence[Fold],
    n_min: int,
    hypothesis_id: str,
    prereg_id: str | None,
    dataset_ids: Sequence[str] | None,
    final_selection_rule: str | None,
    warmup_ms: int,
    min_oos_coverage: float = 1.0,
) -> dict[str, Any]:
    """Everything that decides the runs of a walk-forward, hashed into its plan id. A progress
    ledger only ever serves the plan it was written for. Resumable only when the code commit is
    known and the working tree is clean (the runs must come from one identified code state)."""
    from tsclaude.contracts.market import Market as _Market
    from tsclaude.features.core import FEATURES_VERSION
    from tsclaude.research.prereg import check_preregistration
    from tsclaude.research.report import REPORT_VERSION
    from tsclaude.research.runner import (
        DEV_END_MS,
        DEV_START_MS,
        RUNNER_VERSION,
        cost_identity,
    )
    from tsclaude.research.wf_progress import LEDGER_VERSION, file_sha256
    from tsclaude.sim.engine import SIM_ENGINE_VERSION

    code = dict(env.code_version())
    commit = code.get("commit")
    resumable = code.get("dirty") is False and bool(commit) and commit != "unknown"
    prereg: dict[str, Any] | None = None
    if prereg_id is not None:  # refused here, before anything runs, if not committed
        rel = check_preregistration(env.repo_root, prereg_id)
        prereg = {"id": prereg_id, "sha256": file_sha256(Path(env.repo_root) / rel)}
    if min_oos_coverage < 1.0:
        # a lower floor only from a committed preregistration that declares it, never chosen
        # after a result is seen
        declared = None
        if prereg_id is not None:
            import yaml

            raw = yaml.safe_load((Path(env.repo_root) / rel).read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                declared = raw.get("min_oos_coverage")
                if declared is None and isinstance(raw.get("walk_forward"), dict):
                    declared = raw["walk_forward"].get("min_oos_coverage")
        if declared is None or float(declared) != min_oos_coverage:
            raise WalkForwardError(
                f"min_oos_coverage {min_oos_coverage} below 1.0 needs a committed preregistration "
                "(--prereg) declaring the same min_oos_coverage"
            )
    pins = None if dataset_ids is None else sorted(dataset_ids)
    lo = min(folds[0].train_from_ms - warmup_ms, DEV_START_MS)
    parts: dict[str, list[str]] = {}
    for market, symbol in symbols:
        name = f"{market}:{symbol}"
        parts[f"{name}:bars_1m"] = env.reader.parts_fingerprint(
            market, "bars_1m", symbol, lo, DEV_END_MS, dataset_ids=pins
        )
        if market is _Market.USDM:
            for kind in ("funding", "mark_price_1m"):
                parts[f"{name}:{kind}"] = env.reader.parts_fingerprint(
                    market, kind, symbol, lo, DEV_END_MS
                )
    import platform

    import numpy as np

    body = {
        "environment": {"python": platform.python_version(), "numpy": np.__version__},
        "versions": {
            "walkforward": WALKFORWARD_VERSION,
            "ledger": LEDGER_VERSION,
            "runner": RUNNER_VERSION,
            "sim_engine": SIM_ENGINE_VERSION,
            "features": FEATURES_VERSION,
            "report": REPORT_VERSION,
        },
        "strategy_id": strategy_id,
        "grid": {k: dict(v) for k, v in sorted(grid.items())},
        "symbols": [f"{m}:{s}" for m, s in symbols],
        "folds": [f.to_dict() for f in folds],
        "n_min": n_min,
        "hypothesis_id": hypothesis_id,
        "preregistration": prereg,
        "dataset_ids": pins,
        "config_hash": cfg.config_hash,
        "cost_model": cost_identity(env.cost_model),
        "exchange_interruptions": env.exchange_interruptions.manifest(),
        "final_selection_rule": final_selection_rule,
        "min_oos_coverage": min_oos_coverage,
        "code_commit": commit if resumable else None,
        "lake_parts": parts,
    }
    plan_id = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()[:16]
    return {
        "plan_id": plan_id,
        "resumable": resumable,
        "not_resumable_reason": None
        if resumable
        else "the code commit is unknown or the working tree has uncommitted changes",
        "code": code,
        "body": body,
    }


def _canonical(obj: Any) -> str:
    from tsclaude.contracts.events import canonical_json

    return canonical_json(obj)


def _months_between(a_ms: int, b_ms: int) -> int:
    da = datetime.fromtimestamp(a_ms / 1000, UTC)
    db = datetime.fromtimestamp(b_ms / 1000, UTC)
    return (db.year - da.year) * 12 + (db.month - da.month)


def _prereg_conformance(
    env: RunEnv,
    prereg_id: str,
    *,
    strategy_id: str,
    grid: Mapping[str, Mapping[str, Any]],
    symbols: Sequence[tuple[Market, str]],
    folds: Sequence[Fold],
    n_min: int,
    min_oos_coverage: float,
    dataset_ids: Sequence[str] | None,
    final_selection: bool,
    config_hash: str | None = None,
) -> Mapping[str, Any]:
    """A walk-forward under ``--prereg`` must run exactly what the frozen search preregistration
    (schema tsc-prereg-v1) fixes; refused otherwise, before anything runs. Returns its budget."""
    from tsclaude.research.prereg import check_preregistration
    from tsclaude.research.preregistration import SCHEMA, load, walkforward_conformance

    rel = check_preregistration(env.repo_root, prereg_id)
    doc = load(Path(env.repo_root) / rel)
    if doc.get("schema") != SCHEMA:
        raise WalkForwardError(f"a walk-forward preregistration must use schema {SCHEMA}")
    first = folds[0]
    problems = walkforward_conformance(
        doc,
        strategy_id=strategy_id,
        grid=grid,
        symbols=[f"{m}:{s}" for m, s in symbols],
        initial_train_months=_months_between(first.train_from_ms, first.train_to_ms),
        test_months=_months_between(first.train_to_ms, first.test_to_ms),
        embargo_minutes=(first.test_from_ms - first.train_to_ms) // MS_PER_MINUTE,
        n_min=n_min,
        min_oos_coverage=min_oos_coverage,
        anchor=_iso(first.train_from_ms),
        dataset_ids=dataset_ids,
        final_selection=final_selection,
        folds=[f.to_dict() for f in folds],
        config_hash=config_hash,
        path=str(Path(env.repo_root) / rel),
    )
    if problems:
        raise WalkForwardError("not the preregistered walk-forward: " + "; ".join(problems))
    return dict(doc.get("trial_budget") or {})


def _describe_policy(cfg: BacktestConfig) -> dict[str, Any]:
    from tsclaude.research.risk_policy import describe

    return describe(cfg.risk_policy, cfg.risk)


def _live_compatibility(
    env: RunEnv,
    strategy_id: str,
    grid: Mapping[str, Mapping[str, Any]],
    symbols: Sequence[tuple[Market, str]],
    risk_config: Any | None,
    policy_live: bool = False,
) -> dict[str, Any] | None:
    """Whether each configuration could ever trade in the live baseline book (handoff I-1);
    None when no risk configuration was given. Informational: it never changes selection (under
    ``risk_policy: live`` the backtests themselves already refuse what the live book refuses)."""
    if risk_config is None:
        return None
    from tsclaude.research.live_compat import assess
    from tsclaude.strategies import build_strategy

    worst: dict[str, dict[str, Any]] = {}
    order = {"INCOMPATIBLE": 0, "UNKNOWN": 1, "CONDITIONAL": 2, "COMPATIBLE": 3}
    for config_id, params in grid.items():
        strategy = build_strategy(strategy_id, dict(params))
        for market, symbol in symbols:  # the least compatible symbol decides
            got = assess(
                strategy, risk=risk_config, cost_model=env.cost_model, market=market,
                symbol=symbol,
            ).to_dict()  # fmt: skip
            got["symbol"] = f"{market}:{symbol}"
            cur = worst.get(config_id)
            if cur is None or order[got["status"]] < order[cur["status"]]:
                worst[config_id] = got
    return {
        "risk_config_version": getattr(risk_config, "version", None),
        "configurations": worst,
        "any_deployable": any(v["deployable_in_baseline_book"] for v in worst.values()),
        "note": (
            "The backtests applied the paper risk policy (risk_policy live): a configuration "
            "that is not COMPATIBLE had its rejected trades refused in the backtests too."
            if policy_live
            else "Backtests applied no risk gate (risk_policy allow_all); this shows what the "
            "live baseline book would accept. It never changes selection (decision D-E)."
        ),
    }


# ---------------------------------------------------------------- trial budget
def project_trials(
    evaluator: RunnerEvaluator,
    config_ids: Sequence[str],
    folds: Sequence[Fold],
    *,
    final_window: tuple[int, int] | None,
    env: RunEnv,
    strategy_id: str,
    tested_folds: Collection[int] = (),
) -> dict[str, Any]:
    """New registry trials this walk-forward can add at most, before anything runs (keys
    already recorded, e.g. by an interrupted attempt, are not new). Which configuration a fold
    tests is unknown in advance, so a test window counts as new if any configuration's key for
    it is new, unless the plan's ledger already holds that fold's test run (``tested_folds``:
    a resumed plan reuses it). What counts toward a *budget* is decision D-C; both counts are
    reported."""
    train_keys = [
        evaluator.trial_key(c, f.train_from_ms, f.train_to_ms) for f in folds for c in config_ids
    ]
    test_keys = {
        f.index: [evaluator.trial_key(c, f.test_from_ms, f.test_to_ms) for c in config_ids]
        for f in folds
    }
    final_keys = (
        []
        if final_window is None
        else [evaluator.trial_key(c, final_window[0], final_window[1]) for c in config_ids]
    )
    every = train_keys + [k for ks in test_keys.values() for k in ks] + final_keys
    existing = env.trials.existing(every)
    before = env.trials.n_trials(strategy_id)
    train_new = len(set(train_keys) - existing)
    test_new = sum(
        1
        for index, ks in test_keys.items()
        if index not in tested_folds and any(k not in existing for k in ks)
    )
    final_new = len(set(final_keys) - existing)
    return {
        "family": strategy_id,
        "family_trials_before": before,
        "configurations": len(config_ids),
        "train_evaluations": len(train_keys),
        "train_new_trials": train_new,
        "test_evaluations_max": len(folds),
        "test_new_trials_max": test_new,
        "final_selection_new_trials_max": final_new,
        "new_trials_max": train_new + test_new + final_new,
        "family_trials_after_max": before + train_new + test_new + final_new,
        "counting": "registry rows (one per configuration and window); D-C decides the budget",
    }


# ---------------------------------------------------------------- validity checks
def validity_checks(
    result: WalkForwardResult,
    evaluator: RunnerEvaluator,
    folds: Sequence[Fold],
    *,
    config_hash: str,
    prereg_id: str | None,
    code_commit: str | None,
) -> tuple[dict[str, bool], list[str]]:
    """Train/OOS validity read back from the runs' manifests (not from the plan): every run's
    window is its fold's, train ends an embargo before test, test windows are ordered and
    disjoint, every run is on the development segment with the plan's configuration,
    preregistration and code. Any failure voids the stitched verdict."""
    from tsclaude.research.runner import DEV_END_MS, DEV_START_MS

    fails: dict[str, list[str]] = {
        "anchored_folds": [],
        "embargo_at_least_one_minute": [],
        "test_windows_ordered_and_disjoint": [],
        "runs_match_their_windows": [],
        "train_ends_before_test": [],
        "development_segment_only": [],
        "same_configuration": [],
        "same_preregistration": [],
        "same_code": [],
    }
    for f in folds:
        if f.train_from_ms != folds[0].train_from_ms:
            fails["anchored_folds"].append(f"fold {f.index} starts elsewhere")
        if f.test_from_ms - f.train_to_ms < MS_PER_MINUTE:
            fails["embargo_at_least_one_minute"].append(f"fold {f.index}")
    for a, b in itertools.pairwise(folds):
        if b.test_from_ms < a.test_to_ms or b.index != a.index + 1:
            fails["test_windows_ordered_and_disjoint"].append(f"folds {a.index}/{b.index}")

    def check(out: RunOutcome, label: str, lo: int, hi: int, test_from: int | None) -> None:
        m = out.manifest
        rng = m.get("range") or {}
        if (rng.get("from_ms"), rng.get("to_ms")) != (lo, hi):
            fails["runs_match_their_windows"].append(label)
        if test_from is not None and int(rng.get("to_ms") or 0) >= test_from:
            fails["train_ends_before_test"].append(label)
        warm = int(rng.get("warmup_from_ms") or 0)
        if m.get("segment") != "development" or warm < 0 or int(rng.get("to_ms") or 0) > DEV_END_MS:
            fails["development_segment_only"].append(label)
        if int(rng.get("from_ms") or 0) < DEV_START_MS:
            fails["development_segment_only"].append(label)
        if m.get("config_hash") != config_hash:
            fails["same_configuration"].append(label)
        if m.get("prereg_id") != prereg_id:
            fails["same_preregistration"].append(label)
        if code_commit is not None and (m.get("code") or {}).get("commit") != code_commit:
            fails["same_code"].append(label)

    for fr in result.folds:
        f = fr.fold
        for s in fr.scores:
            out = evaluator.by_key.get(("train", f.index, s.config_id))
            if out is None:
                fails["runs_match_their_windows"].append(f"fold {f.index} train {s.config_id}")
                continue
            check(out, f"fold {f.index} train {s.config_id}", f.train_from_ms, f.train_to_ms,
                  f.test_from_ms)  # fmt: skip
        if fr.selected is not None:
            out = evaluator.by_key.get(("test", f.index, fr.selected))
            if out is None:
                fails["runs_match_their_windows"].append(f"fold {f.index} test")
            else:
                check(out, f"fold {f.index} test {fr.selected}", f.test_from_ms, f.test_to_ms,
                      None)  # fmt: skip
    checks = {name: not bad for name, bad in fails.items()}
    failures = [f"validity check {name} failed: {', '.join(bad)}" for name, bad in fails.items()
                if bad]  # fmt: skip
    return checks, failures


# ---------------------------------------------------------------- final selection
#: the configuration sent to validation: the same objective on the whole development window,
#: only after a positive stitched verdict. A CANDIDATE rule pending governance (H1-2, D-D).
FINAL_SELECTION_RULE = "full-development-train-v1"
GOVERNANCE_PENDING = (
    "PENDING GOVERNANCE: not a selection record. Before any validation run, a frozen "
    "preregistration must name this rule (audit H1-2) and the owner must adopt the two-stage "
    "preregistration (D-D); the record naming the selected strategy hash is then committed."
)


def final_selection(
    evaluator: RunnerEvaluator,
    config_ids: Sequence[str],
    *,
    window: tuple[int, int],
    n_min: int,
    oos_verdict: str,
    last_fold: Fold,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "rule": FINAL_SELECTION_RULE,
        "window": [_iso(window[0]), _iso(window[1])],
        "governance": GOVERNANCE_PENDING,
    }
    if oos_verdict != OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY:
        return {
            **base,
            "status": "NOT_RUN",
            "reasons": [f"the stitched verdict is {oos_verdict}: nothing goes to validation"],
        }
    scores = [evaluator.evaluate_window(c, "final", -1, window[0], window[1]) for c in config_ids]
    scores = evaluator.rescore(scores, last_fold)
    best, reasons = select(scores, n_min=n_min)
    out = {**base, "scores": [s.to_dict() for s in scores], "reasons": reasons}
    if best is None:
        return {**out, "status": "NO_ELIGIBLE_CONFIGURATION", "selected": None}
    return {
        **out,
        "status": "CANDIDATE_PENDING_GOVERNANCE",
        "selected": best.config_id,
        "params": dict(evaluator._grid[best.config_id]),
        "strategy_hash": evaluator.strategy_hash(best.config_id),
    }


def run_walk_forward(
    env: RunEnv,
    cfg: BacktestConfig,
    *,
    strategy_id: str,
    grid: Mapping[str, Mapping[str, Any]],
    symbols: Sequence[tuple[Market, str]],
    folds: Sequence[Fold],
    n_min: int,
    hypothesis_id: str,
    report_dir: Path | None = None,
    work_root: Path | None = None,
    prereg_id: str | None = None,
    dataset_ids: Sequence[str] | None = None,
    resume: bool = True,
    max_new_trials: int | None = None,
    final_selection_enabled: bool = False,
    min_oos_coverage: float = 1.0,
    risk_config: Any | None = None,
) -> dict[str, Any]:
    """Run the procedure end to end and return (and write) the JSON report.

    Refused before any run is recorded: an anchor without full warm-up in the data (WF-2), a
    preregistration id that is not committed, dataset pins that are unknown, a projected trial
    count above ``max_new_trials``, or a progress ledger that cannot be trusted. The report goes
    to ``report_dir``, else ``work_root/<plan id>`` (resumable) or ``work_root/<plan id>-<ms>``.
    With a resumable plan and ``resume``, completed evaluations recorded in the directory's
    progress ledger are verified and reused."""
    from tsclaude.research.runner import segment_of
    from tsclaude.research.wf_progress import ProgressLedger, ResumeRefused

    if not folds:
        raise WalkForwardError("no folds")
    if segment_of(folds[0].train_from_ms, folds[-1].test_to_ms) != "development":
        raise WalkForwardError(
            "walk-forward selection runs on the development segment only "
            "(2022-01-01 to 2025-01-01); validation and holdout are evaluated once, later"
        )
    if not 0.0 < min_oos_coverage <= 1.0:
        raise WalkForwardError("min_oos_coverage must be in (0, 1]")
    if max_new_trials is not None and max_new_trials < 0:
        raise WalkForwardError("max_new_trials must not be negative")
    if dataset_ids is not None:
        known = set(env.reader.known_dataset_ids())
        unknown = sorted(set(dataset_ids) - known)
        if not dataset_ids or unknown:
            raise WalkForwardError(f"dataset pins not in the lake: {unknown or 'empty pin'}")
    warmup = grid_warmup_ms(strategy_id, grid)
    start = data_start_ms(env.reader, symbols, dataset_ids)
    for fold in folds:
        check_anchor(fold.train_from_ms, data_start_ms=start, warmup_ms=warmup)
    check_warmup_complete(env, symbols, folds[0].train_from_ms, warmup, dataset_ids)
    rule = FINAL_SELECTION_RULE if final_selection_enabled else None
    plan = plan_identity(
        env, cfg, strategy_id=strategy_id, grid=grid, symbols=symbols, folds=folds, n_min=n_min,
        hypothesis_id=hypothesis_id, prereg_id=prereg_id, dataset_ids=dataset_ids,
        final_selection_rule=rule, warmup_ms=warmup, min_oos_coverage=min_oos_coverage,
    )  # fmt: skip
    prereg_budget: Mapping[str, Any] | None = None
    if prereg_id is not None:
        prereg_budget = _prereg_conformance(
            env, prereg_id, strategy_id=strategy_id, grid=grid, symbols=symbols, folds=folds,
            n_min=n_min, min_oos_coverage=min_oos_coverage, dataset_ids=dataset_ids,
            final_selection=final_selection_enabled, config_hash=cfg.config_hash,
        )  # fmt: skip
    if report_dir is None and work_root is not None:
        name = plan["plan_id"] if plan["resumable"] and resume else None
        report_dir = work_root / (name or f"{plan['plan_id']}-{env.now_ms()}")
    commit = plan["code"].get("commit") if plan["resumable"] else None
    evaluator = RunnerEvaluator(
        env, cfg, strategy_id=strategy_id, grid=grid, symbols=symbols,
        hypothesis_id=hypothesis_id, prereg_id=prereg_id, dataset_ids=dataset_ids,
    )  # fmt: skip
    final_window = (folds[0].train_from_ms, folds[-1].test_to_ms) if rule else None
    ledger_path = (
        report_dir / "progress.jsonl"
        if plan["resumable"] and resume and report_dir is not None and commit
        else None
    )
    # an existing ledger is verified now (it refuses before anything runs) and tells which test
    # windows are done; a new one is written below, with the plan values, before the first run
    ledger = (
        ProgressLedger(ledger_path, plan["plan_id"])
        if ledger_path is not None and ledger_path.exists()
        else None
    )
    tested = set() if ledger is None else {k[1] for k in ledger.entries if k[0] == "test"}
    budget = project_trials(
        evaluator, list(grid), folds, final_window=final_window, env=env,
        strategy_id=strategy_id, tested_folds=tested,
    )  # fmt: skip
    if (
        prereg_budget is not None
        and prereg_budget.get("counting") == "registry_rows"
        and prereg_budget.get("max_registry_trials") is not None
    ):
        cap = int(prereg_budget["max_registry_trials"])
        max_new_trials = cap if max_new_trials is None else min(max_new_trials, cap)
    if max_new_trials is not None and budget["new_trials_max"] > max_new_trials:
        raise WalkForwardError(
            f"the walk-forward can add up to {budget['new_trials_max']} new trials to "
            f"{strategy_id} (now {budget['family_trials_before']}), above --max-new-trials "
            f"{max_new_trials}; nothing was run"
        )
    # every fold is rescored with one count fixed now: the family's count after every trial
    # this plan can add (conservative). A resumed plan reads it from its ledger.
    plan_values = {"rescore_n_trials": budget["family_trials_after_max"]}
    if ledger is None and ledger_path is not None:
        ledger = ProgressLedger(ledger_path, plan["plan_id"], plan_values)
    if ledger is not None:
        evaluator.attach_ledger(ledger, str(commit))
    stored = (ledger.meta or {}).get("rescore_n_trials") if ledger is not None else None
    if ledger is not None and (not isinstance(stored, int) or isinstance(stored, bool)):
        raise ResumeRefused(f"{ledger.path}: the plan's rescore trial count is missing or invalid")
    # never below what the registry implies now (a lowered or stale value stays conservative)
    rescore_n = max(int(stored) if stored is not None else 0, budget["family_trials_after_max"])
    evaluator.rescore_trials = rescore_n
    result = walk_forward(list(grid), folds, evaluator, n_min=n_min)
    checks, check_failures = validity_checks(
        result, evaluator, folds, config_hash=cfg.config_hash, prereg_id=prereg_id,
        code_commit=commit,
    )  # fmt: skip
    # the registry's count for the strategy family: every (configuration, window) evaluated,
    # train runs included and runs outside this walk-forward too (conservative, never reset);
    # with a final selection, its possible new trials are counted in advance (conservative)
    n_trials = env.trials.n_trials(strategy_id)
    if rule is not None:
        n_trials += budget["final_selection_new_trials_max"]
    max_loss, source = evaluator.max_loss()
    seed = int(
        hashlib.sha256(f"{WALKFORWARD_VERSION}:{hypothesis_id}".encode()).hexdigest()[:8], 16
    )
    void = bool(result.failed_test_folds or check_failures)
    cert = (
        None
        if void  # never certified (WF-1, validity checks)
        else certify_oos(
            result.oos_outcomes(),
            cfg=cfg,
            n_trials=n_trials,
            max_loss_r=max_loss,
            max_loss_source=source,
            seed=seed,
        )
    )
    oos = verdict(
        result,
        cert,
        n_trials=n_trials,
        void_reasons=check_failures,
        min_oos_coverage=min_oos_coverage,
    )
    final = None
    if rule is not None:
        final = final_selection(
            evaluator, list(grid), window=(folds[0].train_from_ms, folds[-1].test_to_ms),
            n_min=n_min, oos_verdict=oos["verdict"], last_fold=folds[-1],
        )  # fmt: skip
    try:  # informational: a failure here must never lose the report
        live = _live_compatibility(
            env, strategy_id, grid, symbols, cfg.risk if cfg.risk is not None else risk_config,
            policy_live=cfg.risk is not None,
        )  # fmt: skip
    except Exception as exc:
        live = {"error": f"{type(exc).__name__}: {exc}"}
    if final is not None and final.get("selected") and live and "configurations" in live:
        compat = live["configurations"].get(final["selected"], {})
        final["live_compatibility"] = compat
        if not compat.get("deployable_in_baseline_book"):
            final["governance"] += (
                " The candidate cannot trade in the live baseline book under the current risk "
                "policy (live_compatibility; decision D-E)."
            )
    cache = env.max_loss_cache
    report = {
        "version": WALKFORWARD_VERSION,
        "provenance": "BACKTEST",
        "strategy_id": strategy_id,
        "hypothesis_id": hypothesis_id,
        "prereg_id": prereg_id,
        "dataset_ids": None if dataset_ids is None else sorted(dataset_ids),
        "symbols": [f"{m}:{s}" for m, s in symbols],
        "grid": {k: dict(v) for k, v in grid.items()},
        "n_min": n_min,
        "plan": {k: plan[k] for k in ("plan_id", "resumable", "not_resumable_reason", "code")},
        "resume": {
            "ledger": None if ledger is None else str(ledger.path),
            "reused_runs": evaluator.reused,
            "executed_runs": evaluator.executed,
            "ignored_partial_line": bool(ledger and ledger.ignored_partial_line),
        },
        "max_loss_cache": None if cache is None else {"hits": cache.hits, "misses": cache.misses},
        "trial_budget": budget,
        "rescore_n_trials": rescore_n,
        "rescore_n_trials_stored": stored,
        "warmup": {
            "max_warmup_minutes": warmup // MS_PER_MINUTE,
            "data_start": None if start is None else _iso(start),
            "earliest_valid_anchor": (
                None if start is None else _iso(earliest_anchor_ms(start, warmup))
            ),
            "anchor": _iso(folds[0].train_from_ms),
        },
        "checks": checks,
        "n_train_evaluations": result.n_train_evaluations,
        "n_test_evaluations": result.n_test_evaluations,
        "folds": [f.to_dict() for f in result.folds],
        "oos": oos,
        "final_selection": final,
        "live_compatibility": live,
        "risk_policy": _describe_policy(cfg),
        "run_ids": [o.run_id for o in evaluator.runs],
        "n_trials_family_now": env.trials.n_trials(strategy_id),
    }
    if report_dir is not None:
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / "walkforward.json"
        report["report_path"] = str(path)
        text = json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
        if path.exists() and path.read_text(encoding="utf-8") != text:
            # an earlier report is kept, never overwritten
            path.rename(report_dir / f"walkforward.previous-{env.now_ms()}.json")
        path.write_text(text, encoding="utf-8")
    return report


__all__ = [
    "FINAL_SELECTION_RULE",
    "INSUFFICIENT_OOS_EVIDENCE",
    "INVALID_OOS_EVIDENCE",
    "NO_EDGE_DEMONSTRATED",
    "OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY",
    "WALKFORWARD_VERSION",
    "Evaluator",
    "Fold",
    "FoldResult",
    "OosRun",
    "RunnerEvaluator",
    "Score",
    "WalkForwardError",
    "WalkForwardResult",
    "add_months",
    "baseline_outcomes",
    "certify_oos",
    "check_anchor",
    "check_warmup_complete",
    "config_id_of",
    "data_start_ms",
    "default_anchor_ms",
    "earliest_anchor_ms",
    "final_selection",
    "grid_warmup_ms",
    "month_start_at_or_after",
    "plan_folds",
    "plan_identity",
    "project_trials",
    "run_problems",
    "run_walk_forward",
    "select",
    "trades_file_problems",
    "validity_checks",
    "verdict",
    "walk_forward",
]
