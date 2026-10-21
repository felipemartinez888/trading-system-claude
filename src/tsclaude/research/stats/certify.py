"""Certification of a strategy's win rate and expectancy from its net trade outcomes.

Implements ADR-0012 with Amendments 1-4: sample floors, realized-payoff uncertainty, fail-closed
degenerate samples and policy floors (1); dependence always accounted for, design p* checked
against realized payoffs, trial counts from the registry (2); effective-sample-size gate and a
single 300-trade floor (3); block cap, crisp floor, robust expectancy bound, beyond-stop reporting
and ``None`` for gates that were not evaluated (4); a declared worst-case loss ``max_loss_r`` with
its source, a ``max_loss_exceeded`` gate and an empirical-Bernstein expectancy bound (5).
Inputs are net trade returns ``r_i`` in R (after fees, slippage, funding). A win is ``r_i > 0``;
zero outcomes count as non-wins (conservative).

Procedure (every step is conservative where a choice exists):

0. **Policy floors** (Amendment 1.4). ``alpha <= 0.10`` and ``min_power >= 0.80``; other values
   raise ``ValueError`` (invalid input, not a soft failure). With a design ``p*`` the planned
   loss at the stop including costs, ``stop_loss_r`` (> 0), is a required input (Amendment 4.5).
1. **Multiple testing.** ``alpha_adj = alpha / n_trials`` (Bonferroni). This is Holm's first-step
   threshold; with the other trials' p-values unknown at certification time it is the only level
   valid for *any* rank, so it is used for every candidate (``multiple_testing`` module).
   **``n_trials`` must come from the trial registry** (Amendment 2.3; from M4 the registry
   supplies it, never a hand-typed value): it is the number of configurations evaluated for the
   hypothesis, and every report shows it next to the result.
2. **Dependence** (Amendments 2.1, 4.1). Independence is never assumed. The required block length
   is ``L_req = max(mean_block or 1, b(outcomes), b(win indicators))`` with ``b`` the Politis-White
   / Patton estimate (``bootstrap.optimal_block_length``). It may not exceed the cap
   ``L_cap = min(3 sqrt(n), n/3)``: beyond it the stationary-bootstrap variance collapses (as the
   block approaches ``n`` every resample approaches the sample itself) and would *loosen*
   certification, so ``L_req > L_cap`` fails closed with ``dependence_unresolved``. Every
   bootstrap uses mean block ``L = min(L_req, L_cap)``.
3. **Certified win rate.** ``p_lcb_exact`` = one-sided Clopper-Pearson lower bound at level
   ``1 - alpha_adj``; ``p_lcb_bootstrap`` = block-bootstrap percentile lower bound of the win share
   at the same level; ``p_lcb`` = the smaller.
4. **Break-even.** ``p_star`` is the given design value (bracket exits: ``(1 + c)/(1 + b)``) or, if
   ``None``, ``L / (W + L)`` from the realized average win ``W`` and loss ``L``. When ``W`` or ``L``
   was not observed the realized ``p*`` is undefined, never assumed; the margin is then undefined
   and counts as not positive.
5. **Certified margin.** ``margin_lcb = min(p_lcb - p*, joint_lcb)`` for both sources of
   ``p*`` (Amendments 1.2, 2.2). ``joint_lcb`` is the one-sided ``alpha_adj`` percentile lower
   bound of the block-bootstrap distribution of ``p_hat* - L*/(W* + L*)`` with win rate and
   payoffs resampled **together**. Resamples with no win or no non-win (``W*`` or ``L*``
   undefined) are scored ``-inf``: they count against certification.
6. **Expectancy** (Amendments 4.3, 5.2). ``expectancy_lcb = min(percentile, t-type, EB)`` where
   ``percentile`` is the lower end of the two-sided block-bootstrap interval at ``1 - alpha_adj``
   and ``t-type = mean - t_{1 - alpha_adj, n_eff - 1} * s / sqrt(n_eff)`` (``s`` the sample
   standard deviation, ``t`` the Student quantile from ``student_t``; ``-inf`` if ``n_eff <= 1``).
   The t-type bound does not depend on the bootstrap, so it cannot collapse with the block.
   ``EB`` is the empirical-Bernstein bound (Maurer & Pontil 2009, Thm 4, applied to the
   rescaled variable ``(max_win_r - r)/R``) for outcomes in ``[-max_loss_r, max_win_r]``,
   ``R = max_win_r + max_loss_r``, at ``delta = alpha_adj`` with ``n_eff``:
   ``mean - sqrt(2 s^2 ln(2/delta) / n_eff) - 7 R ln(2/delta) / (3 (n_eff - 1))``
   (``-inf`` if ``n_eff <= 1``). It is valid for *any* distribution on that interval, so a sample
   that happens to contain no tail event cannot certify unless ``n`` is large relative to the
   declared range (Amendment 5; Bahadur & Savage 1956 show no range-free test can do this).
   ``max_win_r`` defaults to the largest observed outcome (source ``"observed"``); bracket designs
   should pass the take-profit net of costs.
   Dominance: EB <= t-type whenever ``sqrt(2 ln(2/alpha_adj)) >= t_{1-alpha_adj, n_eff-1}``
   (the EB range term is positive). That holds for every ``n_eff >= 300`` (required to certify)
   and ``alpha_adj >= 5e-10``, so the t-type term, kept as Amendment 4 specifies, cannot change
   a decision there; it can bind only on samples that fail the sample-size gate anyway.
6b. **Declared worst-case loss** (Amendment 5.1). ``max_loss_r`` (> 0, R net of costs) and its
   ``max_loss_source`` are required and recorded. Any realized trade with ``r < -max_loss_r``
   voids certification (gate ``max_loss_exceeded``). certify does not compute the default; the
   backtest runner supplies the development-segment gap-through measurement (x 1.5).
7. **Sample size** (Amendments 1.1, 3, 4.2). ``n_required = max(n_min, n_floor)`` where
   ``n_min = max(normal-approximation n, exact n)`` for power ``min_power`` at the observed effect
   ``p_alt = p_hat`` against ``p*`` at level ``alpha_adj`` (``power`` module) and
   ``n_floor = N_FLOOR = 300`` (a preregistration may raise it, never lower it). The gate compares
   the **effective** sample size ``n_eff = n * min(1, rho)``, ``rho = (p_hat (1 - p_hat)/n) /
   Var*(p_hat*)`` with ``Var*`` the block-bootstrap variance of the win rate, against
   ``n_required``. **Crisp floor:** ``rho`` within ``3 sqrt(2/B)`` of 1 (three standard errors of
   a variance ratio estimated from ``B`` replicates) is treated as exactly 1, so ``n_eff = n`` on
   independent data. ``n_min`` is undefined (gate fails) when ``p_hat <= p*``, ``p* >= 1``, or it
   exceeds ``N_MIN_SEARCH_LIMIT``.
8. **Degenerate samples** (Amendment 1.3). Fewer than two distinct outcome values, or any
   bootstrap whose replicates are all equal (up to rounding), fail closed.
9. **Bootstrap resolution.** A percentile at tail ``t`` needs at least ``MIN_TAIL_REPLICATES / t``
   replicates (``20 n_trials / alpha`` for the two-sided expectancy interval). The count is raised
   to that, capped at ``max_resamples``; if the cap binds, certification fails closed.

**Gates and ``None``** (Amendment 4.5). The six gates are ``insufficient_n``,
``margin_not_positive``, ``expectancy_not_positive``, ``degenerate``, ``dependence_unresolved``,
``bootstrap_unresolved`` and ``max_loss_exceeded`` (seven); ``certified`` is true iff none is
``True``. When the bootstrap is
not trustworthy (``degenerate`` or ``dependence_unresolved``), the bootstrap-based gates are not
evaluated and report ``None`` (their bounds are still reported): ``margin_not_positive`` and
``expectancy_not_positive`` always, ``insufficient_n`` unless it is decided without the bootstrap
(``n < n_required`` or ``n_min`` undefined, then ``True``). ``None`` therefore only occurs
alongside a failing decisive gate. ``p_star_undefined`` is reported, not a gate: it forces
``margin_not_positive`` (or ``degenerate`` when no resample has both a win and a non-win).
Holdout, drawdown and cost-stress conditions of ADR-0012 are checked by the caller.

**Tail reporting** (Amendments 3.2, 4.5). ``worst_trade_r = min(r)``; ``count_beyond_stop`` /
``share_beyond_stop`` count trades with ``r < -(stop_loss_r) - stop_tolerance_r`` (losses beyond
the planned stop, i.e. gaps). Without ``stop_loss_r`` (realized ``p*`` only) the threshold is
``-1R`` and ``beyond_stop_label`` reads ``"below -1R"``.

All bootstraps share one resample stream and are evaluated from block sums of three columns (win
indicator, positive part, negative part) with ``bootstrap.stationary_bootstrap_sums``.

Limitations: no outcomes-only test can bound the false-certification rate for every heavy-tailed
distribution (Bahadur & Savage 1956: no test of a mean has power above its level uniformly over
distributions with a finite mean). In particular a sample that contains no tail event at all is
indistinguishable from a strategy without that tail. ``n_min`` at the observed effect is post-hoc
power; the hard floor is the safeguard.
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
from tsclaude.research.stats.student_t import t_isf

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
#: Crisp-floor band: variance ratios within this many standard errors sqrt(2/B) of 1 count as 1.
CRISP_FLOOR_SE = 3.0
#: Numeric tolerance below -stop_loss_r before a trade counts as beyond the stop.
DEFAULT_STOP_TOLERANCE_R = 1e-9


def block_cap(n: int) -> float:
    """Largest usable mean block ``min(3 sqrt(n), n / 3)`` (ADR-0012 Amendment 4.1)."""
    return min(3.0 * math.sqrt(n), n / 3.0)


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
    expectancy_lcb_t: float  # mean - t_{1-alpha_adj, n_eff-1} s / sqrt(n_eff)
    expectancy_lcb_eb: float  # empirical-Bernstein bound on [-max_loss_r, max_win_r]
    expectancy_lcb: float  # min(expectancy_ci.lower, expectancy_lcb_t, expectancy_lcb_eb)
    max_loss_r: float
    max_loss_source: str
    max_win_r: float
    max_win_source: str
    max_loss_exceeded: bool
    n_min: int | None
    n_min_normal: int | None
    n_min_exact: int | None
    n_floor: int
    n_required: int | None  # max(n_min, n_floor), compared with n_eff
    n_eff: float  # n * min(1, binomial var / block-bootstrap var of the win rate)
    variance_ratio: float  # binomial var / block-bootstrap var of the win rate (raw)
    variance_ratio_used: float  # after the crisp-floor rule (Amendment 4.2)
    worst_trade_r: float
    stop_loss_r: float | None
    beyond_stop_label: Literal["beyond stop", "below -1R"]
    count_beyond_stop: int
    share_beyond_stop: float
    distinct_values: int
    insufficient_n: bool | None
    margin_not_positive: bool | None
    expectancy_ci_includes_zero: bool
    expectancy_not_positive: bool | None
    p_star_undefined: bool
    degenerate: bool
    dependence_unresolved: bool
    bootstrap_unresolved: bool
    certified: bool
    mean_block: float | None  # caller's value
    block_required: float  # max(caller, estimates)
    block_cap: float  # min(3 sqrt(n), n/3)
    block_length: float  # mean block actually used = min(block_required, block_cap)
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
    max_loss_r: float,
    max_loss_source: str,
    max_win_r: float | None = None,
    stop_loss_r: float | None = None,
    n_floor: int | None = None,
    n_resamples: int = 10_000,
    max_resamples: int = DEFAULT_MAX_RESAMPLES,
    stop_tolerance_r: float = DEFAULT_STOP_TOLERANCE_R,
) -> Certification:
    """Decide whether outcomes certify a positive win-rate margin and expectancy (see module).

    All decision parameters are required keywords on purpose: in particular ``n_trials`` (every
    configuration evaluated for the hypothesis) must be supplied explicitly, never defaulted.
    ``n_floor=None`` uses ``N_FLOOR``; a value below it is rejected. ``stop_loss_r`` (planned
    loss at the stop including costs, as a positive number of R) is required with a design
    ``p_star`` and used only for the beyond-stop report.
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
    stop: float | None = None
    if stop_loss_r is not None:
        stop = as_float(stop_loss_r, "stop_loss_r")
        if stop <= 0.0:
            raise ValueError(f"stop_loss_r must be > 0 (a loss magnitude in R), got {stop}")
    elif ps_given is not None:
        raise ValueError("stop_loss_r is required when p_star is given by design")
    max_loss = as_float(max_loss_r, "max_loss_r")
    if max_loss <= 0.0:
        raise ValueError(f"max_loss_r must be > 0 (a loss magnitude in R), got {max_loss}")
    if not isinstance(max_loss_source, str) or not max_loss_source.strip():
        raise ValueError(
            "max_loss_source must be a non-empty description of where max_loss_r came from"
        )
    tol = as_float(stop_tolerance_r, "stop_tolerance_r")
    if tol < 0.0:
        raise ValueError(f"stop_tolerance_r must be >= 0, got {tol}")
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

    # 2. dependence: block never below the estimates (2.1), never above the cap (4.1)
    win_ind = (r > 0.0).astype(np.float64)
    block_req = max(
        1.0 if mb is None else mb, optimal_block_length(r), optimal_block_length(win_ind)
    )
    cap_block = block_cap(n)
    dependence_unresolved = block_req > cap_block
    block = max(1.0, min(block_req, cap_block))
    if dependence_unresolved:
        reasons.append(
            f"required block length {block_req:.1f} exceeds the cap min(3 sqrt(n), n/3) = "
            f"{cap_block:.1f}: dependence cannot be resolved with n = {n} (fail closed)"
        )

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

    trusted = not (degenerate or dependence_unresolved)
    if not trusted:
        reasons.append("bootstrap-based gates not evaluated (reported as None)")

    # 4. margin
    margin_cp = None if ps is None else p_lcb - ps
    margin_joint = percentile_bounds(reps["joint_margin"], alpha_adj, "lower").lower
    margin_lcb = None if margin_cp is None else min(margin_cp, margin_joint)
    margin_not_positive: bool | None = None
    if trusted:
        margin_not_positive = margin_lcb is None or margin_lcb <= 0.0
    if margin_lcb is not None and margin_not_positive:
        reasons.append(f"certified margin = {margin_lcb:.6f} <= 0")

    # 7a. effective sample size with the crisp-floor rule (Amendments 3.1, 4.2)
    var_binom = p_hat * (1.0 - p_hat) / n
    var_boot = float(np.var(reps["win_share"]))
    variance_ratio = var_binom / var_boot if var_boot > 0.0 else 1.0
    ratio_used = variance_ratio
    if abs(variance_ratio - 1.0) <= CRISP_FLOOR_SE * math.sqrt(2.0 / b_count):
        ratio_used = 1.0
    n_eff = n * min(1.0, ratio_used)

    # 6. expectancy: min(block-bootstrap percentile, t-type bound)  (Amendment 4.3)
    e_ci = percentile_bounds(reps["expectancy"], alpha_adj, "two-sided")
    e_includes_zero = e_ci.lower <= 0.0 <= e_ci.upper
    sd_r = float(np.std(r, ddof=1)) if n > 1 else math.inf
    if n_eff > 1.0 and math.isfinite(sd_r):
        e_lcb_t = pay.mean - t_isf(alpha_adj, n_eff - 1.0) * sd_r / math.sqrt(n_eff)
    else:
        e_lcb_t = -math.inf
    # empirical Bernstein on [-max_loss_r, max_win_r]  (Amendment 5.2)
    if max_win_r is None:
        max_win, win_source = max(float(np.max(r)), 0.0), "observed"
    else:
        max_win, win_source = as_float(max_win_r, "max_win_r"), "given"
        if max_win < float(np.max(r)):
            raise ValueError(f"max_win_r = {max_win} is below the largest observed outcome")
    r_range = max_win + max_loss
    if n_eff > 1.0 and math.isfinite(sd_r):
        log_term = math.log(2.0 / alpha_adj)
        e_lcb_eb = (
            pay.mean
            - math.sqrt(2.0 * sd_r * sd_r * log_term / n_eff)
            - 7.0 * r_range * log_term / (3.0 * (n_eff - 1.0))
        )
    else:
        e_lcb_eb = -math.inf
    e_lcb = min(e_ci.lower, e_lcb_t, e_lcb_eb)
    e_not_positive: bool | None = None
    if trusted:
        e_not_positive = e_lcb <= 0.0
    if e_not_positive:
        reasons.append(
            f"expectancy lower bound {e_lcb:.6f} <= 0 (bootstrap {e_ci.lower:.6f}, "
            f"t-type {e_lcb_t:.6f}, empirical Bernstein {e_lcb_eb:.6f})"
        )

    # 7b. sample size: n_eff >= max(n_min, n_floor)
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
    insufficient_n: bool | None
    if n_required is None or n < n_required:
        insufficient_n = True  # decided without the bootstrap (n_eff <= n)
    elif not trusted:
        insufficient_n = None
    else:
        insufficient_n = n_eff < n_required
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

    # 6b. declared worst-case loss (Amendment 5.1)
    max_loss_exceeded = bool(np.any(r < -max_loss))
    if max_loss_exceeded:
        reasons.append(
            f"realized loss {float(np.min(r)):.4f}R below -max_loss_r = {-max_loss:.4f}R "
            f"({max_loss_source}): certification void"
        )

    # 9. bootstrap resolution
    bootstrap_unresolved = not resolved
    if bootstrap_unresolved:
        reasons.append(
            f"bootstrap percentiles at alpha_adj = {alpha_adj:.3g} need more than "
            f"max_resamples = {cap} replicates; bound not trustworthy (fail closed)"
        )

    certified = not (
        insufficient_n is True
        or margin_not_positive is True
        or e_not_positive is True
        or degenerate
        or dependence_unresolved
        or bootstrap_unresolved
        or max_loss_exceeded
    )
    threshold = -(1.0 if stop is None else stop) - tol
    beyond = int(np.count_nonzero(r < threshold))
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
        expectancy_lcb_t=e_lcb_t,
        expectancy_lcb_eb=e_lcb_eb,
        expectancy_lcb=e_lcb,
        max_loss_r=max_loss,
        max_loss_source=max_loss_source,
        max_win_r=max_win,
        max_win_source=win_source,
        max_loss_exceeded=max_loss_exceeded,
        n_min=n_min,
        n_min_normal=nm_norm,
        n_min_exact=nm_exact,
        n_floor=floor,
        n_required=n_required,
        n_eff=n_eff,
        variance_ratio=variance_ratio,
        variance_ratio_used=ratio_used,
        worst_trade_r=float(np.min(r)),
        stop_loss_r=stop,
        beyond_stop_label="below -1R" if stop is None else "beyond stop",
        count_beyond_stop=beyond,
        share_beyond_stop=beyond / n,
        distinct_values=distinct,
        insufficient_n=insufficient_n,
        margin_not_positive=margin_not_positive,
        expectancy_ci_includes_zero=e_includes_zero,
        expectancy_not_positive=e_not_positive,
        p_star_undefined=p_star_undefined,
        degenerate=degenerate,
        dependence_unresolved=dependence_unresolved,
        bootstrap_unresolved=bootstrap_unresolved,
        certified=certified,
        mean_block=mb,
        block_required=block_req,
        block_cap=cap_block,
        block_length=block,
        seed=sd,
        n_resamples=b_count,
        reasons=tuple(reasons),
    )
