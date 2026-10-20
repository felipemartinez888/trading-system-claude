"""Certification of a strategy's win rate and expectancy from its net trade outcomes.

Implements ADR-0012 including **Amendment 1** (sample floors, realized-payoff uncertainty,
fail-closed degenerate samples, policy floors), **Amendment 2** (dependence always accounted
for, design p* checked against realized payoffs, trial counts from the registry) and
**Amendment 3** (effective-sample-size gate, single 300-trade floor, tail-loss reporting).
Inputs are net trade returns ``r_i`` in R (after fees, slippage, funding). A win is ``r_i > 0``;
zero outcomes count as non-wins (conservative).

Procedure (every step is conservative where a choice exists):

0. **Policy floors** (Amendment 1.4). ``alpha <= 0.10`` and ``min_power >= 0.80``; other values
   raise ``ValueError`` (invalid input, not a soft failure).
1. **Multiple testing.** ``alpha_adj = alpha / n_trials`` (Bonferroni). This is Holm's first-step
   threshold; with the other trials' p-values unknown at certification time it is the only level
   valid for *any* rank, so it is used for every candidate (``multiple_testing`` module).
   **``n_trials`` must come from the trial registry** (Amendment 2.3; from M4 the registry
   supplies it, never a hand-typed value): it is the number of configurations evaluated for the
   hypothesis, and every report shows it next to the result.
2. **Dependence and certified win rate** (Amendment 2.1). Independence is never assumed. The
   block length is ``L = max(mean_block or 1, b(outcomes), b(win indicators))`` with ``b`` the
   Politis-White / Patton estimate (``bootstrap.optimal_block_length``); every bootstrap below is
   a stationary block bootstrap with mean block ``L``. ``p_lcb_exact`` = one-sided
   Clopper-Pearson lower bound at level ``1 - alpha_adj``; ``p_lcb_bootstrap`` = block-bootstrap
   percentile lower bound of the win share at the same level; ``p_lcb`` = the smaller.
3. **Break-even.** ``p_star`` is the given design value (bracket exits: ``(1 + c)/(1 + b)``) or, if
   ``None``, ``L / (W + L)`` from the realized average win ``W`` and loss ``L``. When ``W`` or ``L``
   was not observed the realized ``p*`` is undefined, never assumed; the margin is then undefined
   and counts as not positive.
4. **Certified margin.** ``margin_lcb = min(p_lcb - p*, joint_lcb)`` for both sources of
   ``p*`` (Amendment 1.2; Amendment 2.2 extends it to a design ``p*``, so a declared break-even is
   checked against realized payoffs). ``joint_lcb`` is the one-sided ``alpha_adj`` percentile
   lower bound of the block-bootstrap distribution of ``p_hat* - L*/(W* + L*)`` with win rate
   and payoffs resampled **together** (same resample). Resamples with no win or no non-win
   (``W*`` or ``L*`` undefined) are scored ``-inf``: they count against certification.
5. **Expectancy.** ``mean(r)`` with a two-sided percentile bootstrap interval at level
   ``1 - alpha_adj`` (block bootstrap with mean block ``L``). Its lower end is
   the one-sided bound at ``alpha_adj / 2``, stricter than the one-sided ``alpha_adj`` bound.
6. **Sample size** (Amendment 1.1, Amendment 3). ``n_required = max(n_min, n_floor)`` where
   ``n_min = max(normal-approximation n, exact n)`` for power ``min_power`` at the observed effect
   ``p_alt = p_hat`` against ``p*`` at level ``alpha_adj`` (``power`` module), and ``n_floor`` is
   ``N_FLOOR = 300`` for both design and realized ``p*`` (gap risk is invisible in small samples:
   a 1 %-frequency tail event is absent from 300 trades with probability 0.99^300 = 4.9 %).
   A caller (preregistration) may raise ``n_floor`` but never lower it. The gate compares the
   **effective** sample size, ``n_eff = n * min(1, (p_hat (1 - p_hat) / n) / Var*(p_hat*))``
   with ``Var*`` the block-bootstrap variance of the win rate, against ``n_required``: clustered
   outcomes need proportionally more trades (``n_eff <= n`` always). For independent data the
   bootstrap ratio is ``1 +- sqrt(2/B)`` (about 1.4 % at ``B = 10 000``), so at ``n`` just above
   ``n_required`` about half of i.i.d. samples land slightly below it: the effective threshold is a
   few percent above the nominal one (conservative). ``n_min`` is undefined
   (gate fails) when ``p_hat <= p*``, ``p* >= 1``, or it exceeds ``N_MIN_SEARCH_LIMIT``.
7. **Degenerate samples** (Amendment 1.3). Fewer than two distinct outcome values, or any
   bootstrap whose replicates are all equal (up to rounding), fail closed. This gate is checked
   first and alone decides such samples: the margin and expectancy bounds are still reported but
   their gates are *not evaluated* (flags ``False``), because percentiles of a zero-width or
   all-undefined bootstrap carry no information. This keeps the gate essential (and testable)
   rather than a backstop that always coincides with the margin gate.
8. **Bootstrap resolution.** A percentile at tail ``t`` needs at least ``MIN_TAIL_REPLICATES / t``
   replicates (``20 n_trials / alpha`` for the two-sided expectancy interval). The count is raised
   to that, capped at ``max_resamples``; if the cap binds, certification fails closed.

Reported for display (Amendment 3.2): ``worst_trade_r = min(r)`` and ``share_beyond_minus_1r =``
the share of trades with ``r < -1`` (net R includes costs, so with a positive round-trip cost an
ordinary stop-out at ``-(1 + c)`` also counts; reports should read it together with the cost).

``certified`` is true iff none of the five independent gates fails: ``insufficient_n``,
``margin_not_positive``, ``expectancy_not_positive``, ``degenerate``, ``bootstrap_unresolved``.
(``p_star_undefined`` is reported but is not a separate gate: it forces ``margin_not_positive``,
or ``degenerate`` when no resample contains both a win and a non-win.)
Holdout, drawdown and cost-stress conditions of ADR-0012 are checked by the caller.

All bootstraps share one resample stream and are evaluated from block sums of three columns (win
indicator, positive part, negative part) with ``bootstrap.stationary_bootstrap_sums``; this is
the same stationary bootstrap at ``O(n / L)`` cost per resample.

Limitations: ``n_min`` at the observed effect is post-hoc power and optimistic after selection; the
hard floors are the safeguard. The percentile bootstrap of a mean is anti-conservative for heavily
skewed outcomes at small n; the floors and the two-sided ``alpha_adj`` expectancy bound mitigate it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from tsclaude.research.stats._validate import as_float, as_int, check_open_prob
from tsclaude.research.stats.binomial import Interval, clopper_pearson
from tsclaude.research.stats.bootstrap import (
    FloatArray,
    optimal_block_length,
    percentile_bounds,
    replicates_degenerate,
    stationary_bootstrap_sums,
)
from tsclaude.research.stats.breakeven import break_even_win_rate, realized_payoffs
from tsclaude.research.stats.multiple_testing import bonferroni_alpha
from tsclaude.research.stats.power import n_min_exact, n_min_one_sided

#: Policy floors (ADR-0012 Amendment 1.4).
MAX_ALPHA = 0.10
MIN_POWER = 0.80
#: Hard sample floor for design and realized p* (ADR-0012 Amendment 3.2); may be raised by a
#: preregistration, never lowered.
N_FLOOR = 300
#: Minimum expected number of bootstrap replicates beyond each percentile endpoint.
MIN_TAIL_REPLICATES = 10
#: Upper limit on bootstrap replicates (bounded run time); exceeding it fails closed.
DEFAULT_MAX_RESAMPLES = 1_000_000
#: Largest n searched by the exact power calculation; beyond it ``insufficient_n`` (fail closed).
N_MIN_SEARCH_LIMIT = 10_000_000


@dataclass(frozen=True, slots=True)
class Certification:
    """Result of ``certify``; every field is reported, including the reasons for failure."""

    n: int
    wins: int
    p_hat: float
    ci: Interval  # two-sided Clopper-Pearson at nominal alpha (display)
    alpha: float
    n_trials: int
    alpha_adj: float
    p_lcb_exact: float
    p_lcb_bootstrap: float
    p_lcb: float
    p_star: float | None
    p_star_source: Literal["given", "realized"] | None
    avg_win: float | None
    avg_loss: float | None
    margin_lcb_cp: float | None  # p_lcb - p*
    margin_lcb_joint: float | None  # joint bootstrap bound of p_hat* - L*/(W*+L*)
    margin_lcb: float | None
    expectancy: float
    expectancy_ci: Interval  # two-sided bootstrap at 1 - alpha_adj
    n_min: int | None
    n_min_normal: int | None
    n_min_exact: int | None
    n_floor: int
    n_required: int | None  # max(n_min, n_floor), compared with n_eff
    n_eff: float  # n * min(1, binomial var / block-bootstrap var of the win rate)
    variance_ratio: float  # binomial var / block-bootstrap var of the win rate (uncapped)
    worst_trade_r: float
    share_beyond_minus_1r: float
    distinct_values: int
    insufficient_n: bool
    margin_not_positive: bool
    expectancy_ci_includes_zero: bool
    expectancy_not_positive: bool
    p_star_undefined: bool
    degenerate: bool
    bootstrap_unresolved: bool
    certified: bool
    mean_block: float | None  # caller's value
    block_length: float  # mean block actually used (Amendment 2.1)
    seed: int
    n_resamples: int
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _resamples_for(tail: float, requested: int, cap: int) -> tuple[int, bool]:
    """``(B, resolved)`` with ``B >= MIN_TAIL_REPLICATES / tail`` unless capped at ``cap``."""
    needed = max(requested, math.ceil(MIN_TAIL_REPLICATES / tail))
    return min(needed, cap), needed <= cap


def _joint_margin(wins: FloatArray, sum_w: FloatArray, sum_l: FloatArray, n: int) -> FloatArray:
    """Per resample: ``p_hat* - L*/(W* + L*)``; ``-inf`` where ``W*`` or ``L*`` is undefined.

    Inputs are resample sums: win count, sum of positive outcomes, sum of |non-positive outcomes|.
    """
    wins = np.rint(wins)  # integer counts (prefix-sum differences of 0/1 are exact anyway)
    losses = n - wins
    ok = (wins > 0) & (losses > 0)
    w = np.divide(sum_w, wins, out=np.zeros_like(sum_w), where=ok)
    loss = np.divide(sum_l, losses, out=np.zeros_like(sum_l), where=ok)
    spread = w + loss
    ok &= spread > 0.0
    p_star = np.divide(loss, spread, out=np.zeros_like(loss), where=ok)
    out: FloatArray = np.where(ok, wins / n - p_star, -math.inf)
    return out


def _validate_policy(alpha: object, min_power: object) -> tuple[float, float]:
    a = check_open_prob(alpha, "alpha")
    if a > MAX_ALPHA:
        raise ValueError(f"alpha must be <= {MAX_ALPHA} (ADR-0012 policy floor), got {a}")
    pw = check_open_prob(min_power, "min_power")
    if pw < MIN_POWER:
        raise ValueError(f"min_power must be >= {MIN_POWER} (ADR-0012 policy floor), got {pw}")
    return a, pw


def certify(
    outcomes_r: Sequence[float] | NDArray[np.float64],
    *,
    p_star: float | None,
    alpha: float,
    n_trials: int,
    min_power: float,
    mean_block: float | None,
    seed: int,
    n_floor: int | None = None,
    n_resamples: int = 10_000,
    max_resamples: int = DEFAULT_MAX_RESAMPLES,
) -> Certification:
    """Decide whether outcomes certify a positive win-rate margin and expectancy (see module).

    All decision parameters are required keywords on purpose: in particular ``n_trials`` (every
    configuration evaluated for the hypothesis) must be supplied explicitly, never defaulted.
    ``n_floor=None`` uses ``N_FLOOR``; a value below it is rejected.
    """
    a, pw = _validate_policy(alpha, min_power)
    nt = as_int(n_trials, "n_trials")
    if nt < 1:
        raise ValueError(f"n_trials must be >= 1, got {nt}")
    sd = as_int(seed, "seed")
    nr = as_int(n_resamples, "n_resamples")
    if nr < 1:
        raise ValueError(f"n_resamples must be >= 1, got {nr}")
    cap = as_int(max_resamples, "max_resamples")
    if cap < nr:
        raise ValueError(f"max_resamples must be >= n_resamples, got {cap} < {nr}")
    mb: float | None = None
    if mean_block is not None:
        mb = as_float(mean_block, "mean_block")
        if mb < 1.0:
            raise ValueError(f"mean_block must be >= 1, got {mb}")
    ps_given: float | None = None
    if p_star is not None:
        ps_given = as_float(p_star, "p_star")
        if ps_given < 0.0:
            raise ValueError(f"p_star must be >= 0, got {ps_given}")
    floor = N_FLOOR
    if n_floor is not None:
        floor = as_int(n_floor, "n_floor")
        if floor < N_FLOOR:
            raise ValueError(f"n_floor may only raise the ADR floor {N_FLOOR}, got {floor}")

    pay = realized_payoffs(outcomes_r)
    r = np.asarray(outcomes_r, dtype=np.float64)
    n, wins = pay.n, pay.wins
    p_hat = wins / n
    alpha_adj = bonferroni_alpha(a, nt)
    reasons: list[str] = []

    # 1-2. exact certified win rate
    ci = clopper_pearson(wins, n, a, "two-sided")
    p_lcb_exact = clopper_pearson(wins, n, alpha_adj, "lower").lower

    # 3. break-even
    ps: float | None
    source: Literal["given", "realized"] | None
    if ps_given is not None:
        ps, source = ps_given, "given"
    elif pay.avg_win is not None and pay.avg_loss is not None and pay.avg_win + pay.avg_loss > 0:
        ps, source = break_even_win_rate(pay.avg_win, pay.avg_loss), "realized"
    else:
        ps, source = None, None
    p_star_undefined = ps is None
    if p_star_undefined:
        reasons.append(
            "p_star undefined: no p_star given and realized "
            + ("average win" if pay.avg_win is None else "average loss")
            + " not observed"
        )

    # 2. dependence: block length never below the data-driven estimates (Amendment 2.1)
    win_ind = (r > 0.0).astype(np.float64)
    block = max(1.0 if mb is None else mb, optimal_block_length(r), optimal_block_length(win_ind))
    block = min(block, float(n))

    # one shared stationary-bootstrap stream; statistics from block sums of three columns
    b_count, resolved = _resamples_for(alpha_adj / 2.0, nr, cap)
    columns = np.vstack([win_ind, np.where(r > 0.0, r, 0.0), np.where(r > 0.0, 0.0, -r)])
    sums = stationary_bootstrap_sums(columns, n_resamples=b_count, mean_block=block, seed=sd)
    reps: dict[str, FloatArray] = {
        "win_share": np.sort(sums[0] / n),
        "expectancy": np.sort((sums[1] - sums[2]) / n),
        "joint_margin": np.sort(_joint_margin(sums[0], sums[1], sums[2], n)),
    }
    p_lcb_boot = percentile_bounds(reps["win_share"], alpha_adj, "lower").lower
    p_lcb = min(p_lcb_exact, p_lcb_boot)

    # 7. degenerate samples / bootstraps (checked first: other bounds are meaningless on them)
    distinct = int(np.unique(r).size)
    degenerate_reps = [name for name, rep in reps.items() if replicates_degenerate(rep)]
    degenerate = distinct < 2 or bool(degenerate_reps)
    if distinct < 2:
        reasons.append(f"degenerate sample: {distinct} distinct outcome value(s)")
    if degenerate_reps:
        reasons.append("degenerate bootstrap (all replicates equal): " + ", ".join(degenerate_reps))

    if degenerate:
        reasons.append("margin and expectancy bounds not evaluated on a degenerate bootstrap")

    # 4. margin
    margin_cp = None if ps is None else p_lcb - ps
    margin_joint = percentile_bounds(reps["joint_margin"], alpha_adj, "lower").lower
    margin_lcb = None if margin_cp is None else min(margin_cp, margin_joint)
    margin_not_positive = not degenerate and (margin_lcb is None or margin_lcb <= 0.0)
    if margin_lcb is not None and margin_not_positive:
        reasons.append(f"certified margin = {margin_lcb:.6f} <= 0")

    # 5. expectancy
    e_ci = percentile_bounds(reps["expectancy"], alpha_adj, "two-sided")
    e_includes_zero = e_ci.lower <= 0.0 <= e_ci.upper
    e_not_positive = not degenerate and e_ci.lower <= 0.0
    if e_not_positive:
        reasons.append(f"expectancy bootstrap lower bound {e_ci.lower:.6f} <= 0")

    # 6. sample size: n_eff >= max(n_min, n_floor)  (Amendment 3.1)
    var_binom = p_hat * (1.0 - p_hat) / n
    var_boot = float(np.var(reps["win_share"]))
    variance_ratio = var_binom / var_boot if var_boot > 0.0 else 1.0
    n_eff = n * min(1.0, variance_ratio)
    nm_norm: int | None = None
    nm_exact: int | None = None
    n_min_too_large = False
    if ps is not None and ps < 1.0 and p_hat > ps:
        nm_norm = n_min_one_sided(ps, p_hat, alpha_adj, pw)
        try:
            nm_exact = n_min_exact(ps, p_hat, alpha_adj, pw, max_n=N_MIN_SEARCH_LIMIT)
        except ValueError:
            n_min_too_large = True  # effect too small to power within the search limit
    n_min = None if nm_norm is None or nm_exact is None else max(nm_norm, nm_exact)
    n_required = None if n_min is None else max(n_min, floor)
    insufficient_n = n_required is None or n_eff < n_required
    if n_min_too_large:
        reasons.append(f"n_min exceeds {N_MIN_SEARCH_LIMIT}: observed margin too small to power")
    elif n_min is None and ps is None:
        reasons.append("n_min undefined: p* undefined")
    elif n_min is None and ps is not None and ps >= 1.0:
        reasons.append(f"n_min undefined: p* = {ps:.6f} >= 1 (geometry cannot be profitable)")
    elif n_min is None:
        reasons.append("n_min undefined: observed win rate does not exceed p*")
    if n_eff < floor:
        reasons.append(f"n_eff = {n_eff:.1f} (n = {n}) < sample floor {floor}")
    if n_min is not None and n_eff < n_min:
        reasons.append(f"n_eff = {n_eff:.1f} (n = {n}) < n_min = {n_min}")

    # 8. bootstrap resolution
    bootstrap_unresolved = not resolved
    if bootstrap_unresolved:
        reasons.append(
            f"bootstrap percentiles at alpha_adj = {alpha_adj:.3g} need more than "
            f"max_resamples = {cap} replicates; bound not trustworthy (fail closed)"
        )

    certified = not (
        insufficient_n
        or margin_not_positive
        or e_not_positive
        or degenerate
        or bootstrap_unresolved
    )
    return Certification(
        n=n,
        wins=wins,
        p_hat=p_hat,
        ci=ci,
        alpha=a,
        n_trials=nt,
        alpha_adj=alpha_adj,
        p_lcb_exact=p_lcb_exact,
        p_lcb_bootstrap=p_lcb_boot,
        p_lcb=p_lcb,
        p_star=ps,
        p_star_source=source,
        avg_win=pay.avg_win,
        avg_loss=pay.avg_loss,
        margin_lcb_cp=margin_cp,
        margin_lcb_joint=margin_joint,
        margin_lcb=margin_lcb,
        expectancy=pay.mean,
        expectancy_ci=e_ci,
        n_min=n_min,
        n_min_normal=nm_norm,
        n_min_exact=nm_exact,
        n_floor=floor,
        n_required=n_required,
        n_eff=n_eff,
        variance_ratio=variance_ratio,
        worst_trade_r=float(np.min(r)),
        share_beyond_minus_1r=float(np.mean(r < -1.0)),
        distinct_values=distinct,
        insufficient_n=insufficient_n,
        margin_not_positive=margin_not_positive,
        expectancy_ci_includes_zero=e_includes_zero,
        expectancy_not_positive=e_not_positive,
        p_star_undefined=p_star_undefined,
        degenerate=degenerate,
        bootstrap_unresolved=bootstrap_unresolved,
        certified=certified,
        mean_block=mb,
        block_length=block,
        seed=sd,
        n_resamples=b_count,
        reasons=tuple(reasons),
    )
