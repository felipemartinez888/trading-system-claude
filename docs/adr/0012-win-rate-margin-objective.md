# ADR-0012: Selection objective — statistically certified win-rate margin over break-even

- **Status:** Accepted (2026-10-19)

## Context
The owner prioritizes signal effectiveness and the highest win rate backed by reliable backtesting,
using pure mathematics. A raw win rate is not a valid objective on its own: any strategy can reach a
90 % win rate with a tiny take-profit and a wide stop while losing money. The objective must reward
high win rates only to the extent that they are profitable after costs and statistically real.

## Decision

### The quantity that matters
For trades with net returns `r_i` in R units (1R = planned risk, after fees, slippage and funding),
let `p` be the win rate (share with `r_i > 0`), `W` the average win, `L` the average loss (positive).

- Expectancy: `E = p·W − (1 − p)·L`
- Break-even win rate: `p* = L / (W + L)`
- Therefore `E = (W + L)·(p − p*)` — expectancy is exactly the **win-rate margin** `Δ = p − p*`
  scaled by the payoff spread. A win rate is "good" only by how far it exceeds `p*`.

For bracket exits (take-profit at `b·R`, stop at `1R`, round-trip cost `c` in R), wins pay `b − c` and
losses cost `1 + c`, so `p* = (1 + c)/(1 + b)` is **known before the test** from the strategy's
design and cost model. A strategy is then a binomial hypothesis: `H0: p ≤ p*` versus `H1: p > p*`.

### Ranking metric
1. **Certified win rate** `p_LCB`: one-sided lower confidence bound of `p` — exact Clopper–Pearson,
   at level `α_adj` corrected for multiple testing (Holm–Bonferroni over the hypothesis's recorded
   trial count). If trades overlap or cluster in time, the bound comes from a stationary block bootstrap
   instead, whichever is more conservative.
2. **Certified margin** `Δ_LCB = p_LCB − p*` (with `p*` computed from realized `W` and `L` for
   non-bracket exits).
3. Configurations are ranked by `Δ_LCB`; ties broken by `p_LCB` (so between equally certain edges the
   higher-win-rate one is preferred, as the owner asked).

### Admission constraints (all must hold)
- `Δ_LCB > 0` on the validation segment, and again on the holdout.
- Bootstrap lower bound of net expectancy `E > 0`.
- `n ≥ n_min`, the sample size giving 80 % power to detect the claimed margin:
  `n_min = ( z_α·√(p*(1−p*)) + z_β·√(p(1−p)) )² / Δ²`.
  Example: `p* = 0.52`, `p = 0.60`, `α = 0.05` one-sided → `n_min ≈ 240`; stricter with
  multiple-testing correction.
- Max drawdown and cost-stress (×1.5, ×2 costs) criteria from the preregistration.

### Search space
Exit geometries favouring high win rates (take-profit between 0.5R and 1.5R, time exits) are
explicitly included in the preregistered search spaces, so high-win-rate profiles compete on equal,
cost-aware terms with trend-following ones.

## Consequences
- The dashboard shows for every strategy and signal: win rate with n and exact CI, `p*`, certified
  margin, and whether it is certified — never a bare win rate.
- Fewer strategies will qualify; that is the intended effect.
- The signal engine stays purely mathematical; LLM agents do not influence entries (ADR-0007).
