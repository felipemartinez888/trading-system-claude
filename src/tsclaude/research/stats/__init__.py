"""Statistics core for certified signal effectiveness (ADR-0012, RESEARCH_PROTOCOL §4-§6).

Pure Python + NumPy (no SciPy). Every function documents its formula and source.
"""

from tsclaude.research.stats.binomial import (
    Interval,
    binom_cdf,
    binom_logpmf,
    binom_pmf,
    binom_sf,
    binomial_test_greater,
    clopper_pearson,
    wilson,
)
from tsclaude.research.stats.bootstrap import (
    bootstrap_ci,
    bootstrap_distribution,
    optimal_block_length,
    percentile_bounds,
    stationary_bootstrap_indices,
)
from tsclaude.research.stats.breakeven import (
    Payoffs,
    bracket_break_even,
    break_even_win_rate,
    expectancy,
    expectancy_from_margin,
    expectancy_identity_holds,
    realized_payoffs,
)
from tsclaude.research.stats.certify import Certification, certify
from tsclaude.research.stats.multiple_testing import (
    bonferroni_alpha,
    holm_adjusted_alpha,
    holm_adjusted_p_values,
    holm_bonferroni,
)
from tsclaude.research.stats.normal import norm_cdf, norm_pdf, norm_ppf, norm_sf
from tsclaude.research.stats.power import (
    exact_test_power,
    n_min_exact,
    n_min_one_sided,
    normal_approx_power,
)
from tsclaude.research.stats.sharpe import (
    EULER_MASCHERONI,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    probabilistic_sharpe_ratio,
    sample_moments,
    sharpe,
)

__all__ = [
    "EULER_MASCHERONI",
    "Certification",
    "Interval",
    "Payoffs",
    "binom_cdf",
    "binom_logpmf",
    "binom_pmf",
    "binom_sf",
    "binomial_test_greater",
    "bonferroni_alpha",
    "bootstrap_ci",
    "bootstrap_distribution",
    "bracket_break_even",
    "break_even_win_rate",
    "certify",
    "clopper_pearson",
    "deflated_sharpe_ratio",
    "exact_test_power",
    "expectancy",
    "expectancy_from_margin",
    "expectancy_identity_holds",
    "expected_max_sharpe",
    "holm_adjusted_alpha",
    "holm_adjusted_p_values",
    "holm_bonferroni",
    "n_min_exact",
    "n_min_one_sided",
    "norm_cdf",
    "norm_pdf",
    "norm_ppf",
    "norm_sf",
    "normal_approx_power",
    "optimal_block_length",
    "percentile_bounds",
    "probabilistic_sharpe_ratio",
    "realized_payoffs",
    "sample_moments",
    "sharpe",
    "stationary_bootstrap_indices",
    "wilson",
]
