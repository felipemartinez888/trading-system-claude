"""Break-even win rate and expectancy (ADR-0012).

For net trade returns ``r_i`` in R units, a *win* is ``r_i > 0`` and everything else (including
``r_i == 0``) is a *non-win*; ``p`` is the win share, ``W`` the mean of the wins and ``L`` the mean
magnitude of the non-wins (``L >= 0``). Then::

    E  = p W - (1 - p) L                (expectancy per trade)
    p* = L / (W + L)                    (break-even win rate: E = 0 iff p = p*)
    E  = (W + L) (p - p*)               (expectancy is the win-rate margin times the payoff spread)

Proof of the identity: ``(W + L)(p - L/(W+L)) = (W + L) p - L = p W - (1 - p) L``.

With the empirical ``p_hat``, ``W`` and ``L`` the identity holds *exactly* for the sample mean of
the outcomes, because ``mean(r) = (sum of wins + sum of non-wins)/n = p_hat W - (1 - p_hat) L``.
Counting zero outcomes as non-wins is the conservative choice (it lowers ``p_hat``).

Bracket exits (take-profit ``b R``, stop ``1 R``, round-trip cost ``c R``): wins pay ``b - c`` and
losses cost ``1 + c``, so ``p* = (1 + c)/((b - c) + (1 + c)) = (1 + c)/(1 + b)``, known before any
test. If ``c >= b`` then ``p* >= 1``: no win rate can be profitable.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from tsclaude.research.stats._validate import as_float, check_prob


def break_even_win_rate(avg_win: float, avg_loss: float) -> float:
    """``p* = L / (W + L)`` for average win ``W >= 0`` and average loss magnitude ``L >= 0``.

    Raises ``ValueError`` for negative or non-finite inputs and when ``W + L == 0`` (undefined).
    ``L == 0`` gives ``p* = 0`` and ``W == 0`` gives ``p* = 1``; callers estimating ``W`` and ``L``
    from data must not pass a value they have not observed (see ``certify``).
    """
    w = as_float(avg_win, "avg_win")
    loss = as_float(avg_loss, "avg_loss")
    if w < 0.0 or loss < 0.0:
        raise ValueError(f"avg_win and avg_loss must be >= 0 (loss as magnitude), got {w}, {loss}")
    if w + loss == 0.0:
        raise ValueError("break-even win rate undefined when avg_win + avg_loss == 0")
    return loss / (w + loss)


def bracket_break_even(b: float, cost_r: float) -> float:
    """``p* = (1 + c)/(1 + b)``: take-profit ``b R`` (``b > 0``), stop ``1 R``, cost ``c >= 0``.

    Values ``>= 1`` (when ``c >= b``) mean the geometry cannot be profitable at any win rate.
    """
    bf = as_float(b, "b")
    c = as_float(cost_r, "cost_r")
    if bf <= 0.0:
        raise ValueError(f"take-profit multiple b must be > 0, got {bf}")
    if c < 0.0:
        raise ValueError(f"round-trip cost in R must be >= 0, got {c}")
    return (1.0 + c) / (1.0 + bf)


def expectancy(p: float, avg_win: float, avg_loss: float) -> float:
    """``E = p W - (1 - p) L`` (``L`` is the loss magnitude, ``L >= 0``)."""
    pf = check_prob(p)
    w = as_float(avg_win, "avg_win")
    loss = as_float(avg_loss, "avg_loss")
    if w < 0.0 or loss < 0.0:
        raise ValueError(f"avg_win and avg_loss must be >= 0, got {w}, {loss}")
    return pf * w - (1.0 - pf) * loss


def expectancy_from_margin(p: float, avg_win: float, avg_loss: float) -> float:
    """``E = (W + L)(p - p*)`` — the margin form of the expectancy."""
    pf = check_prob(p)
    return (avg_win + avg_loss) * (pf - break_even_win_rate(avg_win, avg_loss))


def expectancy_identity_holds(
    p: float, avg_win: float, avg_loss: float, rel_tol: float = 1e-12, abs_tol: float = 1e-12
) -> bool:
    """Check ``p W - (1 - p) L == (W + L)(p - p*)`` to floating-point tolerance."""
    return math.isclose(
        expectancy(p, avg_win, avg_loss),
        expectancy_from_margin(p, avg_win, avg_loss),
        rel_tol=rel_tol,
        abs_tol=abs_tol * (avg_win + avg_loss + 1.0),
    )


@dataclass(frozen=True, slots=True)
class Payoffs:
    """Empirical win/loss decomposition of a sample of net R outcomes.

    ``avg_win`` is ``None`` when there are no wins and ``avg_loss`` is ``None`` when there are no
    non-wins: those quantities were not observed and must not be replaced by 0.
    """

    n: int
    wins: int
    losses: int
    avg_win: float | None
    avg_loss: float | None
    mean: float

    @property
    def p_hat(self) -> float:
        return self.wins / self.n


def realized_payoffs(outcomes_r: Sequence[float] | np.ndarray) -> Payoffs:
    """Split outcomes into wins (``r > 0``) and non-wins (``r <= 0``) and average each part."""
    r = np.asarray(outcomes_r, dtype=np.float64)
    if r.ndim != 1 or r.size == 0:
        raise ValueError("outcomes_r must be a non-empty 1-D sequence")
    if not np.all(np.isfinite(r)):
        raise ValueError("outcomes_r must be finite")
    win_mask = r > 0.0
    wins = int(np.count_nonzero(win_mask))
    losses = int(r.size - wins)
    avg_win = float(np.mean(r[win_mask])) if wins else None
    avg_loss = float(-np.mean(r[~win_mask])) if losses else None
    return Payoffs(
        n=int(r.size),
        wins=wins,
        losses=losses,
        avg_win=avg_win,
        avg_loss=avg_loss,
        mean=float(np.mean(r)),
    )
