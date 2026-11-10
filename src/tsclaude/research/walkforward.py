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

WALKFORWARD_VERSION = "walkforward-v1"

#: verdicts; none of them is a validation result
INSUFFICIENT_OOS_EVIDENCE = "INSUFFICIENT_OOS_EVIDENCE"
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
    if embargo_ms < 0 or embargo_ms % MS_PER_MINUTE:
        raise WalkForwardError("embargo_ms must be a non-negative whole number of minutes")
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


# ---------------------------------------------------------------- selection
@dataclass(frozen=True, slots=True)
class Score:
    """What selection needs from one train-window evaluation (the run id is kept for audit)."""

    config_id: str
    n: int
    margin_lcb: float | None
    p_lcb: float | None
    run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_id": self.config_id,
            "n": self.n,
            "margin_lcb": self.margin_lcb,
            "p_lcb": self.p_lcb,
            "run_id": self.run_id,
        }


def select(scores: Sequence[Score], *, n_min: int) -> tuple[Score | None, list[str]]:
    """The best eligible configuration by the preregistered objective, or None with reasons."""
    if n_min < 1:
        raise WalkForwardError("n_min must be >= 1")
    ranked = sorted(
        (
            (-s.margin_lcb, -s.p_lcb, s.config_id, i)
            for i, s in enumerate(scores)
            if s.n >= n_min and s.margin_lcb is not None and s.p_lcb is not None
        ),
    )
    if not ranked:
        return None, [
            f"no configuration reached n >= {n_min} trades with a certified margin bound on the "
            "train window"
        ]
    return scores[ranked[0][3]], []


# ---------------------------------------------------------------- orchestration
class Evaluator(Protocol):
    def score(self, config_id: str, fold: Fold) -> Score:
        """Evaluate a configuration on the fold's **train** window only."""
        ...

    def oos(self, config_id: str, fold: Fold) -> tuple[list[float], str | None]:
        """Outcomes (R, chronological) of a configuration on the fold's **test** window, and the
        id of the run that produced them."""
        ...


@dataclass
class FoldResult:
    fold: Fold
    scores: list[Score]
    selected: str | None
    reasons: list[str]
    oos_outcomes: list[float] = field(default_factory=list)
    oos_run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        wins = sum(1 for r in self.oos_outcomes if r > 0)
        return {
            **self.fold.to_dict(),
            "scores": [s.to_dict() for s in self.scores],
            "selected": self.selected,
            "reasons": list(self.reasons),
            "oos_trades": len(self.oos_outcomes),
            "oos_wins": wins,
            "oos_run_id": self.oos_run_id,
        }


@dataclass
class WalkForwardResult:
    config_ids: list[str]
    folds: list[FoldResult]

    @property
    def n_train_evaluations(self) -> int:
        return sum(len(f.scores) for f in self.folds)

    @property
    def n_test_evaluations(self) -> int:
        return sum(1 for f in self.folds if f.selected is not None)

    def oos_outcomes(self) -> list[float]:
        """Test-window outcomes stitched in fold order (chronological)."""
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
        scores = [evaluator.score(c, fold) for c in config_ids]
        best, reasons = select(scores, n_min=n_min)
        result = FoldResult(fold, scores, None if best is None else best.config_id, reasons)
        if best is not None:
            result.oos_outcomes, result.oos_run_id = evaluator.oos(best.config_id, fold)
        results.append(result)
    return WalkForwardResult(list(config_ids), results)


# ---------------------------------------------------------------- verdict
def verdict(
    result: WalkForwardResult, cert: Certification | None, *, n_trials: int
) -> dict[str, Any]:
    """The stitched out-of-sample verdict. ``cert`` certifies `result.oos_outcomes()` (None when
    fewer than two out-of-sample trades exist). Never a validation result."""
    outcomes = result.oos_outcomes()
    base: dict[str, Any] = {
        "oos_trades": len(outcomes),
        "oos_wins": sum(1 for r in outcomes if r > 0),
        "n_trials": n_trials,
        "folds_with_selection": result.n_test_evaluations,
        "folds": len(result.folds),
        "not_validation": (
            "Development-segment walk-forward only. Validation (2025, one preregistered run) and "
            "the sealed holdout are still required before any grade above C (RESEARCH_PROTOCOL "
            "§2, §6)."
        ),
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
        )

    def oos(self, config_id: str, fold: Fold) -> tuple[list[float], str | None]:
        from tsclaude.research.parquet import read_table

        out = self._run(config_id, fold.test_from_ms, fold.test_to_ms)
        _, rows = read_table(out.run_dir / "trades.parquet")
        base = [r for r in rows if r.get("book") == "baseline"]
        base.sort(key=lambda r: (int(r.get("entry_bar_open_ms") or 0), str(r.get("symbol"))))
        return [float(r["r"]) for r in base], out.run_id

    def max_loss(self) -> tuple[float, str]:
        """The most conservative measured max loss among the test runs (never below 1.5R)."""
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
    """Run the procedure end to end and return (and optionally write) the JSON report."""
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
    cert = certify_oos(
        result.oos_outcomes(),
        cfg=cfg,
        n_trials=n_trials,
        max_loss_r=max_loss,
        max_loss_source=source,
        seed=seed,
    )
    report = {
        "version": WALKFORWARD_VERSION,
        "provenance": "BACKTEST",
        "strategy_id": strategy_id,
        "hypothesis_id": hypothesis_id,
        "symbols": [f"{m}:{s}" for m, s in symbols],
        "grid": {k: dict(v) for k, v in grid.items()},
        "n_min": n_min,
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
    "NO_EDGE_DEMONSTRATED",
    "OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY",
    "WALKFORWARD_VERSION",
    "Evaluator",
    "Fold",
    "FoldResult",
    "RunnerEvaluator",
    "Score",
    "WalkForwardError",
    "WalkForwardResult",
    "add_months",
    "certify_oos",
    "config_id_of",
    "plan_folds",
    "run_walk_forward",
    "select",
    "verdict",
    "walk_forward",
]
