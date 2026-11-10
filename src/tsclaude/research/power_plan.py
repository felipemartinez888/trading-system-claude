"""Statistical-power planning and reading for preregistered research (mathematical audit F4, §3.5).

Two uses, neither of which touches selection or certification:

- **Before a freeze** (`plan_from_feasibility`): from a P&L-free feasibility count, how many
  candidates fall inside the walk-forward's test windows, and the smallest win-rate margin over
  the planned break-even that many trades could certify with 80 % power at the trial-corrected
  level (ADR-0012's ``n_min`` formula, `research.stats.power.n_min_one_sided`, inverted).
- **After a run** (`read_power`): the same question for the trades a walk-forward actually
  certified, so a ``NO_EDGE_DEMONSTRATED`` verdict is read correctly: with ``n`` trades a margin
  smaller than the reported minimum detectable margin is not ruled out.

The candidates inside the test windows (``oos_candidates``) are counted from the feasibility
count's ``candidates_by_utc_month`` (feasibility-v2): whole months exactly, a partial month pro
rata. Without month counts (an older count) they are estimated from the windows' share of the
counted range, which assumes candidates are spread evenly over time; ``oos_candidates_basis``
says which. Given that basis, the numbers are **optimistic** about what can be certified:

- feasibility counts candidates of one configuration *before* the portfolio rules, freshness
  gaps, the kill switch, validity windows and exits remove some of them;
- clustered trades lower the effective count further (``n_eff``, ADR-0012 Amendment 3), shown
  here at fixed ratios because it is only known after a run;
- the planned break-even ``p* = (1 + c) / (1 + b)`` holds for a pure bracket at the cost cap; with a
  time exit the realized ``p*`` decides (`MATHEMATICAL_STRATEGY_FOUNDATIONS` Part 1, 1.3 (a));
- certification divides alpha by the strategy family's whole registry count
  (`research.walkforward`), so rows already recorded for the family before this plan
  (``prior_family_rows``) lower the level further. For H1 the family is empty today.

Nothing here is evidence of an edge or of its absence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from tsclaude.research.stats.power import n_min_one_sided

POWER_PLAN_VERSION = "power-plan-v2"  # v2: candidates counted inside the test windows
N_FLOOR = 300  # the certification floor (research/stats/certify.py, L-06)
N_EFF_RATIOS = (1.0, 0.75, 0.5)
_STEP = 0.0005  # margin resolution (win-rate points / 100)


def planned_p_star(tp_r: float, cost_r: float) -> float:
    """ADR-0012's planned break-even of a pure bracket: wins pay ``b - c``, losses ``1 + c``."""
    if tp_r <= 0 or cost_r < 0:
        raise ValueError("tp_r must be > 0 and cost_r >= 0")
    return (1.0 + cost_r) / (1.0 + tp_r)


def min_detectable_margin(
    n: float, p_star: float, alpha: float, power: float = 0.80
) -> float | None:
    """Smallest margin ``p - p*`` (grid of 0.0005) whose ADR-0012 ``n_min`` at this level and
    power is at most ``n``; None if no margin up to ``1 - p*`` is detectable with ``n`` trades."""
    if not 0.0 < p_star < 1.0:
        raise ValueError("p_star must be in (0, 1)")
    if n < 1:
        return None
    k = 1
    while p_star + k * _STEP < 1.0:
        margin = k * _STEP
        if n_min_one_sided(p_star, p_star + margin, alpha, power) <= n:
            return round(margin, 4)
        k += 1
    return None


def registry_rows(configurations: int, folds: int, final_selection: bool = True) -> int:
    """Upper bound of new registry rows of one walk-forward plan: every configuration on every
    training window, one selected configuration per test window, and the final selection pass."""
    return configurations * folds + folds + (configurations if final_selection else 0)


def _geometry_rows(
    oos_trades: float,
    geometries: Sequence[tuple[str, float]],
    *,
    cost_r: float,
    alpha_adj: float,
    power: float,
    n_eff_ratios: Sequence[float],
) -> list[dict[str, Any]]:
    rows = []
    for label, tp_r in geometries:
        p_star = planned_p_star(tp_r, cost_r)
        by_ratio = []
        for ratio in n_eff_ratios:
            n = oos_trades * ratio
            margin = min_detectable_margin(n, p_star, alpha_adj, power)
            by_ratio.append(
                {
                    "n_eff_ratio": ratio,
                    "n_eff": round(n, 1),
                    "below_certification_floor": n < N_FLOOR,
                    "min_detectable_margin": margin,
                    # E = margin x (W + L) = margin x (1 + b) for a pure bracket, in R
                    "edge_r_equivalent": None if margin is None else round(margin * (1 + tp_r), 3),
                }
            )
        rows.append({"geometry": label, "tp_r": tp_r, "p_star_planned": round(p_star, 4),
                     "by_n_eff": by_ratio})  # fmt: skip
    return rows


def _month_bounds(month: str) -> tuple[int, int]:
    year, mon = (int(x) for x in month.split("-"))
    lo = datetime(year, mon, 1, tzinfo=UTC)
    hi = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=UTC)
    return int(lo.timestamp() * 1000), int(hi.timestamp() * 1000)


def _overlap(a: int, b: int, windows: Sequence[tuple[int, int]]) -> int:
    return sum(max(0, min(b, hi) - max(a, lo)) for lo, hi in windows)


def oos_candidates_of(
    feasibility: Mapping[str, Any], windows: Sequence[tuple[int, int]]
) -> tuple[float, str]:
    """Candidates inside the test windows ``[from_ms, to_ms)`` and the basis of the number."""
    rng = feasibility["range"]
    lo, hi = int(rng["from_ms"]), int(rng["to_ms"])
    months = (feasibility.get("diagnostics") or {}).get("candidates_by_utc_month")
    if isinstance(months, Mapping):
        total = 0.0
        for month, n in months.items():
            a, b = _month_bounds(str(month))
            a, b = max(a, lo), min(b, hi)  # the counted part of the month
            if b > a:
                total += int(n) * _overlap(a, b, windows) / (b - a)
        return total, "months inside the test windows (partial months pro rata)"
    candidates = _candidates(feasibility)
    return candidates * _overlap(lo, hi, windows) / (hi - lo), (
        "time share of the test windows (no month counts: assumes candidates are spread evenly "
        "over the counted range)"
    )


def _candidates(feasibility: Mapping[str, Any]) -> int:
    return sum(
        int(sym["total"]["cumulative"][-1]["bars"]) for sym in feasibility["symbols"].values()
    )


def plan_from_feasibility(
    feasibility: Mapping[str, Any],
    *,
    test_windows: Sequence[tuple[int, int]],
    configurations: int,
    geometries: Sequence[tuple[str, float]],
    cost_r: float,
    alpha: float = 0.05,
    power: float = 0.80,
    n_eff_ratios: Sequence[float] = N_EFF_RATIOS,
    prior_family_rows: int = 0,
) -> dict[str, Any]:
    """The planning report for one feasibility count (one configuration's candidate pool), with
    one test window ``(from_ms, to_ms)`` per walk-forward fold."""
    if feasibility.get("kind") != "feasibility_count" or feasibility.get("pnl_free") is not True:
        raise ValueError("not a P&L-free feasibility count")
    if not test_windows:
        raise ValueError("no test windows")
    if prior_family_rows < 0:
        raise ValueError("prior_family_rows must be >= 0")
    rng = feasibility["range"]
    lo, hi = int(rng["from_ms"]), int(rng["to_ms"])
    if hi <= lo:
        raise ValueError("empty range")
    candidates = _candidates(feasibility)
    oos, basis = oos_candidates_of(feasibility, test_windows)
    folds = len(test_windows)
    new_rows = registry_rows(configurations, folds)
    trials = prior_family_rows + new_rows
    alpha_adj = alpha / trials
    return {
        "kind": "power_plan",
        "version": POWER_PLAN_VERSION,
        "pnl_free": True,
        "source_content_hash": feasibility.get("content_hash"),
        "candidates_in_range": candidates,
        "test_window_share_of_range": round(_overlap(lo, hi, test_windows) / (hi - lo), 4),
        "oos_candidates": round(oos, 1),
        "oos_candidates_basis": basis,
        "trials": {
            "configurations": configurations,
            "folds": folds,
            "new_registry_rows_max": new_rows,
            "prior_family_rows": prior_family_rows,
            "n_trials_planned": trials,
            "alpha": alpha,
            "alpha_adj": alpha_adj,
        },
        "power": power,
        "cost_r": cost_r,
        "geometries": _geometry_rows(
            oos,
            geometries,
            cost_r=cost_r,
            alpha_adj=alpha_adj,
            power=power,
            n_eff_ratios=n_eff_ratios,
        ),
        "optimistic_because": [
            "candidates are counted for one configuration before the portfolio rules (max 3 "
            "positions in the btc_beta cluster, 1 per symbol), data-freshness blocks, the daily "
            "loss limit and kill switch, validity windows and exits",
            "clustered trades lower the effective count (shown at fixed n_eff ratios)",
            "the planned break-even assumes a pure bracket at the cost cap; with a time exit the "
            "realized p* decides",
            "certification divides alpha by the family's whole registry count; rows recorded "
            "before this plan must be passed as prior_family_rows",
        ],
        "note": "planning only: not evidence of an edge or of its absence; never used by selection",
    }


def read_power(
    oos: Mapping[str, Any], *, alpha: float, power: float = 0.80
) -> dict[str, Any] | None:
    """After a walk-forward: the smallest margin its certified trades could detect (80 % power,
    at the run's trial-corrected level, at its realized ``p*`` and ``n_eff``). None without a
    certification (no trades, invalid or insufficient evidence)."""
    p_star = oos.get("p_star")
    n_trials = oos.get("n_trials")
    n = oos.get("n_eff") if oos.get("n_eff") is not None else oos.get("oos_trades")
    if not isinstance(p_star, int | float) or not isinstance(n_trials, int) or not n:
        return None
    if not 0.0 < p_star < 1.0 or n_trials < 1:
        return None
    alpha_adj = alpha / n_trials
    margin = min_detectable_margin(float(n), float(p_star), alpha_adj, power)
    return {
        "n_used": n,
        "n_basis": "n_eff" if oos.get("n_eff") is not None else "oos_trades",
        # below the floor nothing is certified and the normal approximation is poor
        "below_certification_floor": float(n) < N_FLOOR,
        "p_star": p_star,
        "alpha_adj": alpha_adj,
        "power": power,
        "min_detectable_margin": margin,
        "reading": (
            f"a verdict without certification does not rule out a true margin below {margin}"
            if margin is not None
            else "too few trades to detect any margin"
        ),
        "note": "diagnostic only; never used by selection or certification",
    }


def test_windows_of(folds: Sequence[Any]) -> list[tuple[int, int]]:
    """``(test_from_ms, test_to_ms)`` of each ``Fold``."""
    return [(int(f.test_from_ms), int(f.test_to_ms)) for f in folds]


def oos_share_of(folds: Sequence[Any], from_ms: int, to_ms: int) -> float:
    """Share of ``[from, to)`` covered by the folds' test windows (``Fold`` objects)."""
    if to_ms <= from_ms:
        raise ValueError("empty range")
    return _overlap(from_ms, to_ms, test_windows_of(folds)) / (to_ms - from_ms)


__all__ = [
    "N_EFF_RATIOS",
    "N_FLOOR",
    "POWER_PLAN_VERSION",
    "min_detectable_margin",
    "oos_candidates_of",
    "oos_share_of",
    "plan_from_feasibility",
    "planned_p_star",
    "read_power",
    "registry_rows",
    "test_windows_of",
]
