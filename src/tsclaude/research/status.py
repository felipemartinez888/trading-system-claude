"""Research status: which strategies are REFERENCE, candidates or validated (read-only, MVP).

Derived only from evidence that already exists, never from a manual flag:

- the strategy registry (`tsclaude.strategies.REGISTRY`);
- the pipeline references, which are never research evidence (owner decision D-14,
  RESEARCH_PROTOCOL §6: "REFERENCE / NOT VALIDATED" whatever grade they carry);
- the recorded backtest runs under ``<data_dir>/runs/`` as `research.runner.load_run` reads them.
  A run counts only if its ``metrics.json`` still matches the SHA-256 in its manifest
  (``metrics_verified``) and it carries a preregistration id (`research.prereg` refuses a run whose
  id is not a committed ``preregistrations/<id>.yaml``).

Tiers, first match wins:

=============  ===============================================================================
REFERENCE      a pipeline reference strategy (`REFERENCE_STRATEGIES`)
VALIDATED      a verified, preregistered **holdout** run graded ``A`` ("credible": validation
               **and** holdout passed, robust to cost stress); grade A is the only grade called
               validated, and an A on another segment does not count
REJECTED       a verified, preregistered validation or holdout run graded ``D``
CANDIDATE      at least one verified, preregistered run, none of them A or D
UNREGISTERED   no verified preregistered run: exploratory at best, never evidence
=============  ===============================================================================

The overall ``state`` is ``NO_VALIDATED_STRATEGY`` unless at least one strategy is VALIDATED. This
module says nothing about profitability; a VALIDATED tier is the protocol's grade, not a promise.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

#: Pipeline strategies that are never research evidence (MVP-3, D-14). Mirrors the dashboard's
#: `REFERENCE_STRATEGIES`; a strategy is added here only by a reviewed code change.
REFERENCE_STRATEGIES: frozenset[str] = frozenset({"bracket_reference"})

NO_VALIDATED_STRATEGY = "NO_VALIDATED_STRATEGY"
VALIDATED_STRATEGY_AVAILABLE = "VALIDATED_STRATEGY_AVAILABLE"

_GRADED_SEGMENTS = frozenset({"validation", "holdout"})


class StrategyTier(StrEnum):
    REFERENCE = "REFERENCE"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    CANDIDATE = "CANDIDATE"
    UNREGISTERED = "UNREGISTERED"


@dataclass(frozen=True, slots=True)
class RunEvidence:
    """The fields of one recorded run that the status needs (from `load_run`)."""

    run_id: str
    strategy_id: str
    segment: str | None
    evidence_grade: str | None
    metrics_verified: bool | None
    prereg_id: str | None
    created_at: str | None = None

    @classmethod
    def from_loaded(cls, loaded: Mapping[str, Any]) -> RunEvidence:
        summary = loaded.get("summary") or {}
        manifest = loaded.get("manifest") or {}
        return cls(
            run_id=str(summary.get("run_id") or manifest.get("run_id") or ""),
            strategy_id=str(summary.get("strategy_id") or ""),
            segment=summary.get("segment"),
            evidence_grade=summary.get("evidence_grade"),
            metrics_verified=summary.get("metrics_verified"),
            prereg_id=manifest.get("prereg_id"),
            created_at=summary.get("created_at"),
        )

    @property
    def trusted(self) -> bool:
        """Verified metrics and a preregistration id: the only runs that can be evidence."""
        return self.metrics_verified is True and bool(self.prereg_id)


def _tier(strategy_id: str, runs: Sequence[RunEvidence]) -> tuple[StrategyTier, list[str]]:
    if strategy_id in REFERENCE_STRATEGIES:
        return StrategyTier.REFERENCE, [
            "pipeline reference strategy: exercises the system, not claimed profitable and "
            "never research evidence (D-14); any grade it carries is not a validation result"
        ]
    trusted = [r for r in runs if r.trusted]
    untrusted = len(runs) - len(trusted)
    notes: list[str] = []
    if untrusted:
        notes.append(
            f"{untrusted} run(s) ignored: metrics not verified against the manifest or no "
            "preregistration id"
        )
    # grade A means validation AND holdout passed (RESEARCH_PROTOCOL §6): only a holdout run
    # can carry it; an A on any other segment is not trusted as validation (fail closed)
    credible = [r for r in trusted if r.evidence_grade == "A" and r.segment == "holdout"]
    if credible:
        ids = sorted(r.run_id for r in credible)
        return StrategyTier.VALIDATED, [
            f"grade A (credible) on holdout run(s) {', '.join(ids)}",
            *notes,
        ]
    rejected = [
        r for r in trusted if r.evidence_grade == "D" and (r.segment or "") in _GRADED_SEGMENTS
    ]
    if rejected:
        ids = sorted(r.run_id for r in rejected)
        return StrategyTier.REJECTED, [
            f"grade D (rejected) on preregistered run(s) {', '.join(ids)}",
            *notes,
        ]
    if trusted:
        grades = sorted({r.evidence_grade or "ungraded" for r in trusted})
        return StrategyTier.CANDIDATE, [
            f"preregistered, not validated (best evidence: {', '.join(grades)})",
            *notes,
        ]
    return StrategyTier.UNREGISTERED, [
        "no verified preregistered run: not a candidate, not evidence",
        *notes,
    ]


def assess(
    strategy_ids: Iterable[str],
    runs: Sequence[RunEvidence],
    *,
    preregistration_files: Sequence[str] = (),
) -> dict[str, Any]:
    """The research status as a JSON-ready dict (pure: no I/O)."""
    registered = frozenset(strategy_ids)
    by_strategy: dict[str, list[RunEvidence]] = {}
    for run in runs:
        by_strategy.setdefault(run.strategy_id, []).append(run)
    known = sorted(registered | {r.strategy_id for r in runs if r.strategy_id})
    strategies: list[dict[str, Any]] = []
    for sid in known:
        own = by_strategy.get(sid, [])
        tier, why = _tier(sid, own)
        strategies.append(
            {
                "strategy_id": sid,
                "tier": str(tier),
                "registered": sid in registered,
                "runs": len(own),
                "trusted_runs": sum(r.trusted for r in own),
                "reasons": why,
            }
        )
    counts = {str(t): sum(s["tier"] == str(t) for s in strategies) for t in StrategyTier}
    validated = counts[str(StrategyTier.VALIDATED)]
    reasons: list[str] = []
    if validated == 0:
        if strategies and all(s["tier"] == str(StrategyTier.REFERENCE) for s in strategies):
            names = ", ".join(s["strategy_id"] for s in strategies)
            reasons.append(f"only pipeline REFERENCE strategies exist ({names})")
        if not preregistration_files:
            reasons.append("no preregistration file exists (preregistrations/)")
        if not runs:
            reasons.append("no backtest run is recorded")
        elif not any(r.trusted for r in runs):
            reasons.append("no recorded run is both verified and preregistered")
        if counts[str(StrategyTier.REJECTED)]:
            reasons.append(f"{counts[str(StrategyTier.REJECTED)]} strategy(ies) rejected")
        if counts[str(StrategyTier.CANDIDATE)]:
            reasons.append(f"{counts[str(StrategyTier.CANDIDATE)]} candidate(s) not validated")
    return {
        "state": VALIDATED_STRATEGY_AVAILABLE if validated else NO_VALIDATED_STRATEGY,
        "validated": validated,
        "counts": counts,
        "strategies": strategies,
        "runs_recorded": len(runs),
        "runs_trusted": sum(r.trusted for r in runs),
        "preregistration_files": sorted(preregistration_files),
        "reasons": reasons,
        "statement": (
            "A VALIDATED tier is the research protocol's grade A, not a profitability promise."
            if validated
            else "NO VALIDATED STRATEGY: no strategy has passed the preregistered research "
            "protocol. Paper results of REFERENCE strategies show the pipeline working, "
            "not an edge."
        ),
    }


def load_status(runs_dir: Path, repo_root: Path) -> dict[str, Any]:
    """Read the registry, the recorded runs and the preregistration files, then `assess`."""
    from tsclaude.research.prereg import PREREG_DIR
    from tsclaude.research.runner import load_run
    from tsclaude.strategies import REGISTRY

    runs: list[RunEvidence] = []
    if runs_dir.is_dir():
        for d in sorted(runs_dir.iterdir()):
            if not (d / "manifest.json").is_file():
                continue
            loaded = load_run(runs_dir, d.name, recompute=False)
            if loaded is not None:
                runs.append(RunEvidence.from_loaded(loaded))
    prereg_dir = repo_root / PREREG_DIR
    files = (
        sorted(
            p.name
            for p in prereg_dir.glob("*.yaml")
            if p.is_file() and p.stem.upper() != "TEMPLATE"  # a template is not a registration
        )
        if prereg_dir.is_dir()
        else []
    )
    status = assess(REGISTRY.keys(), runs, preregistration_files=files)
    status["walk_forwards"] = walk_forward_summaries(runs_dir.parent / "walkforward")
    return status


def walk_forward_summaries(root: Path) -> list[dict[str, Any]]:
    """Every walk-forward report under ``<data_dir>/walkforward/``, newest first, as recorded:
    verdict, trial count, coverage, failed folds, live compatibility and the final-selection
    status. An unreadable report is listed as such, never skipped. None of these verdicts is a
    validation result (RESEARCH_PROTOCOL §2)."""
    import json

    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*/walkforward.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True):  # fmt: skip
        try:
            r = json.loads(path.read_text(encoding="utf-8"))
            oos = r.get("oos") or {}
            live = r.get("live_compatibility") or {}
            final = r.get("final_selection") or {}
            out.append(
                {
                    "report": path.parent.name,
                    "plan_id": (r.get("plan") or {}).get("plan_id"),
                    "strategy_id": r.get("strategy_id"),
                    "reference": r.get("strategy_id") in REFERENCE_STRATEGIES,
                    "hypothesis_id": r.get("hypothesis_id"),
                    "prereg_id": r.get("prereg_id"),
                    "verdict": oos.get("verdict"),
                    "n_trials": oos.get("n_trials"),
                    "oos_trades": oos.get("oos_trades"),
                    "oos_coverage": (oos.get("oos_coverage") or {}).get("share"),
                    "failed_test_folds": oos.get("failed_test_folds"),
                    "checks_passed": all((r.get("checks") or {}).values())
                    if r.get("checks")
                    else None,
                    "live_deployable": live.get("any_deployable") if live else None,
                    # which risk rules the runs applied (P1-6); a report without the field
                    # predates it, when every backtest applied no risk rule
                    "risk_policy": (r.get("risk_policy") or {}).get("mode") or "not_recorded",
                    "final_selection": final.get("status") if final else None,
                    "not_validation": True,
                }
            )
        except (OSError, ValueError, AttributeError) as exc:
            out.append({"report": path.parent.name, "unreadable": f"{type(exc).__name__}"})
    return out


__all__ = [
    "NO_VALIDATED_STRATEGY",
    "REFERENCE_STRATEGIES",
    "VALIDATED_STRATEGY_AVAILABLE",
    "RunEvidence",
    "StrategyTier",
    "assess",
    "load_status",
    "walk_forward_summaries",
]
