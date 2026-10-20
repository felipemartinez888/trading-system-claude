"""Certification of a strategy's win rate and expectancy from its net trade outcomes (ADR-0012).

Inputs are net trade returns ``r_i`` in R (after fees, slippage, funding). A win is ``r_i > 0``;
zero outcomes count as non-wins (conservative).

Procedure (every step is conservative where a choice exists):

1. **Multiple testing.** ``alpha_adj = alpha / n_trials`` (Bonferroni). This is Holm's first-step
   threshold, the level Holm-Bonferroni applies to the most significant hypothesis; with the other
   trials' p-values unknown at certification time it is the only level valid for *any* rank, so it
   is used for every candidate (``multiple_testing`` module).
2. **Certified win rate.** ``p_lcb_exact`` = one-sided Clopper-Pearson lower bound at level
   ``1 - alpha_adj``. If ``mean_block`` is given (overlapping or clustered trades), a stationary
   block-bootstrap percentile lower bound of the win share at the same level is also computed and
   ``p_lcb = min(p_lcb_exact, p_lcb_bootstrap)``.
3. **Break-even.** ``p_star`` is the given design value (bracket exits: ``(1 + c)/(1 + b)``) or, if
   ``None``, ``L / (W + L)`` from the realized average win ``W`` and loss ``L``. When ``W`` or ``L``
   was not observed (zero wins or zero non-wins) the realized ``p*`` is undefined, never assumed;
   certification then fails with flag ``p_star_undefined``.
4. **Certified margin.** ``margin_lcb = p_lcb - p_star``.
5. **Expectancy.** ``mean(r)`` with a two-sided percentile bootstrap interval at level
   ``1 - alpha_adj`` (block bootstrap with ``mean_block`` if given, else i.i.d.). Its lower end is
   the one-sided bound at ``alpha_adj / 2``, stricter than the one-sided ``alpha_adj`` bound.
6. **Sample size.** ``n_min = max(normal-approximation n, exact n)`` for power ``min_power`` at the
   observed effect ``p_alt = p_hat`` against ``p_star`` at level ``alpha_adj`` (``power`` module).
   Undefined (``None``) when ``p_hat <= p_star`` or ``p_star >= 1``.
7. ``certified`` is true iff ``margin_lcb > 0``, the expectancy interval's lower end ``> 0``,
   ``n >= n_min`` and the bootstrap percentiles were resolvable (below). Holdout, drawdown and
   cost-stress conditions of ADR-0012 are not computable from outcomes alone and are checked by
   the caller.

Bootstrap resolution: a percentile at tail ``t`` is only meaningful with at least
``MIN_TAIL_REPLICATES / t`` replicates, so the replicate count is raised to that (for ``alpha_adj``
tails this is ``20 n_trials / alpha``). It is capped at ``max_resamples``; if the cap binds, the
bound is not trustworthy and certification **fails closed** (flag ``bootstrap_unresolved``).

Limitations (documented, not hidden): a realized ``p*`` is itself an estimate and its uncertainty is
not propagated into ``margin_lcb``; the expectancy bound (step 5) is the joint check that covers it.
``n_min`` at the observed effect is a post-hoc power figure and is optimistic when ``p_hat`` is
inflated by selection; it is a necessary, not a sufficient, condition.
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
from tsclaude.research.stats.bootstrap import bootstrap_distribution, percentile_bounds, row_mean
from tsclaude.research.stats.breakeven import break_even_win_rate, realized_payoffs
from tsclaude.research.stats.multiple_testing import bonferroni_alpha
from tsclaude.research.stats.power import n_min_exact, n_min_one_sided

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
    p_lcb_bootstrap: float | None
    p_lcb: float
    p_star: float | None
    p_star_source: Literal["given", "realized"] | None
    avg_win: float | None
    avg_loss: float | None
    margin_lcb: float | None
    expectancy: float
    expectancy_ci: Interval  # two-sided bootstrap at 1 - alpha_adj
    n_min: int | None
    n_min_normal: int | None
    n_min_exact: int | None
    insufficient_n: bool
    margin_not_positive: bool
    expectancy_ci_includes_zero: bool
    expectancy_not_positive: bool
    p_star_undefined: bool
    bootstrap_unresolved: bool
    certified: bool
    mean_block: float | None
    seed: int
    n_resamples: int
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _resamples_for(tail: float, requested: int, cap: int) -> tuple[int, bool]:
    """``(B, resolved)`` with ``B >= MIN_TAIL_REPLICATES / tail`` unless capped at ``cap``."""
    needed = max(requested, math.ceil(MIN_TAIL_REPLICATES / tail))
    return min(needed, cap), needed <= cap


def certify(
    outcomes_r: Sequence[float] | NDArray[np.float64],
    *,
    p_star: float | None,
    alpha: float,
    n_trials: int,
    min_power: float,
    mean_block: float | None,
    seed: int,
    n_resamples: int = 10_000,
    max_resamples: int = DEFAULT_MAX_RESAMPLES,
) -> Certification:
    """Decide whether outcomes certify a positive win-rate margin and expectancy (see module).

    All decision parameters are required keywords on purpose: in particular ``n_trials`` (every
    configuration evaluated for the hypothesis) must be supplied explicitly, never defaulted.
    """
    a = check_open_prob(alpha, "alpha")
    nt = as_int(n_trials, "n_trials")
    if nt < 1:
        raise ValueError(f"n_trials must be >= 1, got {nt}")
    pw = check_open_prob(min_power, "min_power")
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

    pay = realized_payoffs(outcomes_r)
    r = np.asarray(outcomes_r, dtype=np.float64)
    n, wins = pay.n, pay.wins
    p_hat = wins / n
    alpha_adj = bonferroni_alpha(a, nt)
    reasons: list[str] = []

    # 2. certified win rate
    ci = clopper_pearson(wins, n, a, "two-sided")
    p_lcb_exact = clopper_pearson(wins, n, alpha_adj, "lower").lower
    p_lcb_boot: float | None = None
    n_e_res, e_resolved = _resamples_for(alpha_adj / 2.0, nr, cap)
    used_resamples = n_e_res
    resolved = e_resolved
    if mb is not None:
        win_ind = (r > 0.0).astype(np.float64)
        n_win_res, w_resolved = _resamples_for(alpha_adj, nr, cap)
        resolved = resolved and w_resolved
        reps = bootstrap_distribution(
            win_ind, row_mean, n_resamples=n_win_res, mean_block=mb, seed=sd, vectorized=True
        )
        p_lcb_boot = percentile_bounds(reps, alpha_adj, "lower").lower
        used_resamples = max(used_resamples, n_win_res)
    p_lcb = p_lcb_exact if p_lcb_boot is None else min(p_lcb_exact, p_lcb_boot)

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

    # 4. margin
    margin_lcb = None if ps is None else p_lcb - ps
    margin_not_positive = margin_lcb is None or margin_lcb <= 0.0
    if margin_lcb is not None and margin_not_positive:
        reasons.append(f"certified margin p_lcb - p* = {margin_lcb:.6f} <= 0")

    # 5. expectancy (seed offset so the two bootstraps use independent streams)
    e_reps = bootstrap_distribution(
        r,
        row_mean,
        n_resamples=n_e_res,
        mean_block=mb,
        seed=sd + 1,
        vectorized=True,
    )
    e_ci = percentile_bounds(e_reps, alpha_adj, "two-sided")
    e_includes_zero = e_ci.lower <= 0.0 <= e_ci.upper
    e_not_positive = e_ci.lower <= 0.0
    if e_not_positive:
        reasons.append(f"expectancy bootstrap lower bound {e_ci.lower:.6f} <= 0")

    # 6. sample size at the observed effect
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
    insufficient_n = n_min is None or n < n_min
    if n_min_too_large:
        reasons.append(f"n_min exceeds {N_MIN_SEARCH_LIMIT}: observed margin too small to power")
    elif n_min is None and ps is None:
        reasons.append("n_min undefined: p* undefined")
    elif n_min is None and ps is not None and ps >= 1.0:
        reasons.append(f"n_min undefined: p* = {ps:.6f} >= 1 (geometry cannot be profitable)")
    elif n_min is None:
        reasons.append("n_min undefined: observed win rate does not exceed p*")
    elif insufficient_n:
        reasons.append(f"n = {n} < n_min = {n_min}")

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
        or p_star_undefined
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
        margin_lcb=margin_lcb,
        expectancy=pay.mean,
        expectancy_ci=e_ci,
        n_min=n_min,
        n_min_normal=nm_norm,
        n_min_exact=nm_exact,
        insufficient_n=insufficient_n,
        margin_not_positive=margin_not_positive,
        expectancy_ci_includes_zero=e_includes_zero,
        expectancy_not_positive=e_not_positive,
        p_star_undefined=p_star_undefined,
        bootstrap_unresolved=bootstrap_unresolved,
        certified=certified,
        mean_block=mb,
        seed=sd,
        n_resamples=used_resamples,
        reasons=tuple(reasons),
    )
