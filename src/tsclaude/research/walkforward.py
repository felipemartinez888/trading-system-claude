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
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from tsclaude.contracts.timeutil import MS_PER_MINUTE

if TYPE_CHECKING:
    from tsclaude.contracts.market import Market
    from tsclaude.research.runner import BacktestConfig, RunEnv, RunOutcome
    from tsclaude.research.stats.certify import Certification

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
    return max(warmup_ms_of(make(strategy_id, dict(p))) for p in grid.values())


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
    result: WalkForwardResult, cert: Certification | None, *, n_trials: int
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
    no_valid_train = [
        f.fold.index for f in result.folds if f.scores and len(f.train_invalid) == len(f.scores)
    ]
    coverage: dict[str, Any] = {
        "n_trials": n_trials,
        "folds_planned": planned,
        "folds": len(result.folds),
        "folds_with_selection": result.n_test_evaluations,
        "folds_without_valid_train": no_valid_train,
        "failed_test_folds": result.failed_test_folds,
        "not_validation": not_validation,
    }
    if result.failed_test_folds:
        if cert is not None:
            raise WalkForwardError("a certification over a void out-of-sample set was given")
        reasons = [
            f"fold {f.fold.index} test run {f.oos_run_id} failed integrity: "
            + "; ".join(f.oos_problems)
            for f in result.folds
            if f.test_failed
        ]
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
            f"fold(s) {no_valid_train} had no valid train run (integrity), so their test windows "
            "are not in the stitched set; the omission depends on data quality only, never on "
            "a test result"
        )
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
    if not isinstance(integrity, Mapping) or "problems" not in integrity:
        return ["the run has no integrity record"]
    problems = [f"integrity: {p}" for p in integrity.get("problems") or []]
    grade = metrics.get("evidence_grade")
    if grade == "X" and not problems:
        problems = [f"graded X: {r}" for r in metrics.get("grade_reasons") or ["no reason"]]
    elif grade is None:
        problems.append("the run has no evidence grade")
    return problems


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

    path = run_dir / "trades.parquet"
    expected = (manifest.get("outputs") or {}).get("trades_parquet_sha256")
    if not path.is_file():
        return [], ["the run has no trades file"]
    if not expected or _sha256_file(path) != expected:
        return [], ["the trades file does not match the manifest hash"]
    _, rows = read_table(path)
    base = [r for r in rows if r.get("book") == "baseline"]
    base.sort(key=lambda r: (int(r.get("entry_bar_open_ms") or 0), str(r.get("symbol"))))
    return [float(r["r"]) for r in base], []


class RunnerEvaluator:
    """Each evaluation is a normal recorded backtest (manifest, holdout guard, trial registry)."""

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
    ) -> None:
        from tsclaude.strategies import build_strategy

        self._env = env
        self._cfg = cfg
        self._strategy_id = strategy_id
        self._grid = {k: dict(v) for k, v in grid.items()}
        self._symbols = list(symbols)
        self._hypothesis_id = hypothesis_id
        self._build = build or build_strategy
        self.runs: list[RunOutcome] = []

    def _run(self, config_id: str, from_ms: int, to_ms: int) -> RunOutcome:
        from tsclaude.research.runner import BacktestRequest, run_backtest

        strategy = self._build(self._strategy_id, self._grid[config_id])
        req = BacktestRequest(
            strategy=strategy,
            symbols=self._symbols,
            from_ms=from_ms,
            to_ms=to_ms,
            hypothesis_id=self._hypothesis_id,
        )
        out = run_backtest(req, self._env, self._cfg)
        self.runs.append(out)
        return out

    def score(self, config_id: str, fold: Fold) -> Score:
        out = self._run(config_id, fold.train_from_ms, fold.train_to_ms)
        cert = out.metrics.get("certification") or {}
        return Score(
            config_id=config_id,
            n=int(out.metrics.get("trades", {}).get("n") or 0),
            margin_lcb=cert.get("margin_lcb"),
            p_lcb=cert.get("p_lcb"),
            run_id=out.run_id,
            problems=tuple(run_problems(out.metrics)),
        )

    def rescore(self, scores: list[Score], fold: Fold) -> list[Score]:
        """Re-certify every train run of the fold with the registry's count **now** (the same
        for all of them: nothing is recorded in between), via the runner's own recompute
        (`load_run(..., recompute=True)`, audit N1). A run whose stored count already equals it
        keeps its stored certification, which used that count. The run is re-read from disk:
        unreadable or unverifiable metrics, or integrity problems, make it ineligible."""
        from tsclaude.research.runner import load_run

        out: list[Score] = []
        for s in scores:
            loaded = (
                None
                if s.run_id is None
                else load_run(self._env.runs_dir, s.run_id, self._env.trials, recompute=True)
            )
            if loaded is None:
                why = (*s.problems, "the run cannot be read back")
                out.append(Score(s.config_id, s.n, None, None, s.run_id, why))
                continue
            problems = list(s.problems)
            if loaded["summary"].get("metrics_verified") is not True:
                problems.append("the run's metrics do not match the manifest hash")
            for p in run_problems(loaded.get("metrics")):
                if p not in problems:
                    problems.append(p)
            current = loaded.get("current") or {}
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
        out = self._run(config_id, fold.test_from_ms, fold.test_to_ms)
        problems = run_problems(out.metrics)
        outcomes, file_problems = baseline_outcomes(out.run_dir, out.manifest)
        return OosRun(tuple(outcomes), out.run_id, tuple(problems + file_problems))

    def max_loss(self) -> tuple[float, str]:
        """The largest max-loss bound among this walk-forward's runs (train and test), as each
        run measured it (L-10: worst losing exit on the development segment x safety factor,
        never below the safety factor times the planned stop). It feeds only the expectancy
        bound of the stitched certification, never selection. 1.5R if no run recorded one."""
        values: list[tuple[float, str]] = []
        for out in self.runs:
            ctx = out.metrics.get("evaluation_inputs") or {}
            ml = ctx.get("max_loss") or {}
            if "value" in ml:
                values.append((float(ml["value"]), str(ml.get("source", "run"))))
        if not values:
            return 1.5, "default 1.5R (no measured value)"
        return max(values)


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
) -> dict[str, Any]:
    """Run the procedure end to end and return (and optionally write) the JSON report.

    Refused before any run is recorded when the anchor leaves no full warm-up inside the data
    (WF-2)."""
    if not folds:
        raise WalkForwardError("no folds")
    warmup = grid_warmup_ms(strategy_id, grid)
    start = data_start_ms(env.reader, symbols)
    for fold in folds:
        check_anchor(fold.train_from_ms, data_start_ms=start, warmup_ms=warmup)
    evaluator = RunnerEvaluator(
        env, cfg, strategy_id=strategy_id, grid=grid, symbols=symbols, hypothesis_id=hypothesis_id
    )
    result = walk_forward(list(grid), folds, evaluator, n_min=n_min)
    # the registry's count for the strategy family: every (configuration, window) evaluated,
    # train runs included and runs outside this walk-forward too (conservative, never reset)
    n_trials = env.trials.n_trials(strategy_id)
    max_loss, source = evaluator.max_loss()
    seed = int(
        hashlib.sha256(f"{WALKFORWARD_VERSION}:{hypothesis_id}".encode()).hexdigest()[:8], 16
    )
    cert = (
        None
        if result.failed_test_folds  # void: never certified (WF-1)
        else certify_oos(
            result.oos_outcomes(),
            cfg=cfg,
            n_trials=n_trials,
            max_loss_r=max_loss,
            max_loss_source=source,
            seed=seed,
        )
    )
    report = {
        "version": WALKFORWARD_VERSION,
        "provenance": "BACKTEST",
        "strategy_id": strategy_id,
        "hypothesis_id": hypothesis_id,
        "symbols": [f"{m}:{s}" for m, s in symbols],
        "grid": {k: dict(v) for k, v in grid.items()},
        "n_min": n_min,
        "warmup": {
            "max_warmup_minutes": warmup // MS_PER_MINUTE,
            "data_start": None if start is None else _iso(start),
            "earliest_valid_anchor": (
                None if start is None else _iso(earliest_anchor_ms(start, warmup))
            ),
            "anchor": _iso(folds[0].train_from_ms),
        },
        "n_train_evaluations": result.n_train_evaluations,
        "n_test_evaluations": result.n_test_evaluations,
        "folds": [f.to_dict() for f in result.folds],
        "oos": verdict(result, cert, n_trials=n_trials),
        "run_ids": [o.run_id for o in evaluator.runs],
    }
    if report_dir is not None:
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "walkforward.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
    return report


__all__ = [
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
    "config_id_of",
    "data_start_ms",
    "earliest_anchor_ms",
    "grid_warmup_ms",
    "month_start_at_or_after",
    "plan_folds",
    "run_problems",
    "run_walk_forward",
    "select",
    "verdict",
    "walk_forward",
]
