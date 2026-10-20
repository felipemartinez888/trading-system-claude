"""Family-wise error control: Bonferroni and Holm (1979) step-down.

Bonferroni: test each of ``m`` hypotheses at ``alpha / m``; by the union bound the probability of
any false rejection is at most ``alpha``.

Holm-Bonferroni (Holm 1979, "A simple sequentially rejective multiple test procedure"): sort the
p-values ascending ``p_(1) <= ... <= p_(m)``; reject ``H_(1), ..., H_(j-1)`` where ``j`` is the
first rank with ``p_(j) > alpha / (m - j + 1)``. It controls the family-wise error rate at
``alpha`` under arbitrary dependence and is uniformly more powerful than Bonferroni. Its first step
equals Bonferroni, so for a single hypothesis evaluated among ``m`` trials whose other p-values are
not at hand, ``alpha / m`` is the (conservative) level valid whatever the others turn out to be.

Adjusted p-values (Holm): ``p~_(j) = max_{i <= j} min(1, (m - i + 1) p_(i))``; ``H_(j)`` is rejected
iff ``p~_(j) <= alpha``.
"""

from __future__ import annotations

from collections.abc import Sequence

from tsclaude.research.stats._validate import as_int, check_open_prob, check_prob


def _check_m(m: object) -> int:
    mi = as_int(m, "m")
    if mi < 1:
        raise ValueError(f"number of trials m must be >= 1, got {mi}")
    return mi


def bonferroni_alpha(alpha: float, m: int) -> float:
    """Per-test level ``alpha / m``."""
    return check_open_prob(alpha, "alpha") / _check_m(m)


def holm_adjusted_alpha(alpha: float, m: int, rank: int) -> float:
    """Holm threshold ``alpha / (m - rank + 1)`` for the ``rank``-th smallest p-value (1-based)."""
    a = check_open_prob(alpha, "alpha")
    mi = _check_m(m)
    r = as_int(rank, "rank")
    if not 1 <= r <= mi:
        raise ValueError(f"rank must satisfy 1 <= rank <= m, got rank={r}, m={mi}")
    return a / (mi - r + 1)


def _sorted_order(p_values: Sequence[float]) -> tuple[list[float], list[int]]:
    ps = [check_prob(p, "p_value") for p in p_values]
    if not ps:
        raise ValueError("p_values must be non-empty")
    order = sorted(range(len(ps)), key=lambda i: (ps[i], i))  # stable: ties keep input order
    return ps, order


def holm_bonferroni(p_values: Sequence[float], alpha: float = 0.05) -> list[bool]:
    """Holm step-down rejection flags, returned in the input order."""
    a = check_open_prob(alpha, "alpha")
    ps, order = _sorted_order(p_values)
    m = len(ps)
    reject = [False] * m
    for j, idx in enumerate(order, start=1):
        if ps[idx] <= a / (m - j + 1):
            reject[idx] = True
        else:
            break
    return reject


def holm_adjusted_p_values(p_values: Sequence[float]) -> list[float]:
    """Holm adjusted p-values in the input order (monotone step-down maximum)."""
    ps, order = _sorted_order(p_values)
    m = len(ps)
    out = [0.0] * m
    running = 0.0
    for j, idx in enumerate(order, start=1):
        running = max(running, min(1.0, (m - j + 1) * ps[idx]))
        out[idx] = running
    return out
