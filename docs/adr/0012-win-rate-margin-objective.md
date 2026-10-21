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

## Amendment 1 (2026-10-20) — small-sample and realized-payoff safeguards

The independent statistical review of M2-T3 found paths that certified under-sampled or losing
strategies: with p* estimated from realized payoffs, a fat-tailed losing strategy (true E = −0.05R)
was certified in 54 % of simulated runs at n = 10 and 8 % at n = 80; constant samples produced
zero-width bootstrap intervals; and n_min computed at the observed effect is post-hoc power, so a
large observed margin made the sample-size gate vacuous. The following are therefore added to the
admission constraints. They make certification strictly harder, never easier.

1. **Hard sample floor.** `n ≥ max(n_min, N_FLOOR)` with `N_FLOOR = 100` when p* is fixed by design
   (bracket exits) and `N_FLOOR = 300` when p* is estimated from realized payoffs. Preregistrations
   may raise but never lower these floors.
2. **Realized-payoff uncertainty is carried.** When p* is realized, the certified margin is the
   *minimum* of (a) the Clopper–Pearson-based margin and (b) the lower bound of a joint block
   bootstrap of `p̂* − L̂*/(Ŵ* + L̂*)` (win rate and payoffs resampled together), both at the adjusted
   level. Resamples in which W or L is undefined count against certification.
3. **Degenerate samples fail closed.** Fewer than two distinct outcome values, or a bootstrap whose
   replicates are all equal, cannot be certified.
4. **Policy floors.** `alpha ≤ 0.10` and `min_power ≥ 0.80`; other values are rejected as invalid
   input.
5. **Every admission gate must be individually tested**: for each gate a case where it is the only
   failing condition, so that deleting the gate fails a test (mutation check in review).

## Amendment 2 (2026-10-20) — dependence and payoff consistency

The re-review of M2-T3 showed that independence was silently assumed when no block length was
given: autocorrelated outcomes from a losing strategy (stationary win rate 0.51 vs p* 0.525,
persistence 0.98, n = 2000) were certified in 30 % of runs. A declared design p* was also never
checked against realized payoffs (losses far beyond the stop certified 35 % at n = 100).

1. **Dependence is always accounted for.** The block length used is
   `max(caller value or 1, estimated block length of the outcomes, estimated block length of the
   win indicators)`. The win-rate lower bound is the minimum of the Clopper–Pearson bound and the
   block-bootstrap bound at that length. Independence is never assumed by default.
2. **Design p* is checked against reality.** When p* is given, the realized joint margin
   (Amendment 1, item 2) is computed as well, and the certified margin is the minimum of both.
3. **Trial counts come from the registry.** From M4, `n_trials` is supplied by the trial registry,
   never by hand, and every report shows it.
4. Tests must kill these mutations: realized-p* floor lowered below 300; nominal instead of
   adjusted alpha in any bootstrap.

## Amendment 3 (2026-10-20) — effective sample size and a single floor

1. **Effective-sample-size gate.** `n_eff = n · min(1, binomial variance of the win rate ÷
   block-bootstrap variance of the win rate)`; certification requires `n_eff ≥ max(n_min, N_FLOOR)`.
   Strongly clustered outcomes therefore need proportionally more trades. (Measured: removes the
   residual ≈ 4 % false certification of the persistent Markov case at n = 2000; no effect on
   independent data.)
2. **One floor: `N_FLOOR = 300` for both design and realized p*.** Losses beyond the planned stop
   (gap risk) are invisible in samples that happen not to contain them; with 300 trades the chance of
   missing a 1 %-frequency tail event is ≈ 5 %. Backtests on real data contain real gaps, and
   reports must show the worst trade in R and the share of trades closed beyond −1R.

## Amendment 4 (2026-10-20) — block cap, crisp floor, robust expectancy bound

Final re-review findings: a large caller block length collapses stationary-bootstrap variance and
*loosens* certification (gap-risk false certification 0.19 at block = n = 300); n_eff noise at the
floor masked a ≈ 6 % gap-risk rate for 310 ≤ n ≤ 400; few huge losses are handled poorly by the
percentile bootstrap.

1. **Block cap.** The usable block length is capped at `min(3·√n, n/3)`. If the required block
   (caller or estimate) exceeds the cap, certification fails closed with `dependence_unresolved`.
2. **Crisp floor.** A variance ratio within `3·√(2/B)` of 1 (B = bootstrap replicates) is treated
   as exactly 1, so `n_eff = n` on independent data.
3. **Robust expectancy bound.** The expectancy lower bound is the minimum of the block-bootstrap
   percentile bound and the t-type bound `mean − t_{1−α_adj, n_eff−1} · s / √n_eff`. Samples
   containing a few very large losses are therefore rejected; the residual risk is samples
   containing no tail event at all, which no outcomes-only test can detect.
4. **Acceptance target.** For the gap-risk, fat-tail and persistent-Markov reference strategies,
   the simulated false-certification rate + 2·SE must be ≤ α for every n in {300, 310, 350, 400,
   500, 700, 1000} and for caller block lengths {None, 1, 5, 25, 50, √n, n}.
5. **Reporting.** `share_beyond_stop` / `count_beyond_stop` = trades with `r < −(stop_loss_r) −
   tolerance`, where `stop_loss_r` (planned loss at the stop incl. costs) is a required input when
   p* is given by design; with realized p* and no stop the field is labelled "below −1R". Gates not
   evaluated report `None`, never `False`.

## Amendment 5 (2026-10-21) — declared worst-case loss (closing the gap-risk case)

No test based on outcomes alone can hold its level for every heavy-tailed distribution
(Bahadur & Savage, 1956): samples that happen to contain no tail event look exactly like a
profitable strategy. The gap-risk reference strategy therefore still certified at 4–10 % for
300 ≤ n ≤ 500 under Amendment 4. Certification is made valid under an explicit, auditable
assumption instead:

1. **`max_loss_r` is required** — the worst plausible loss per trade in R, net of costs. It is
   not chosen freely: the default is the instrument's worst historical gap-through stop loss in
   R measured on the development segment by the simulator (×1.5 safety factor), and a
   preregistration may only raise it. Any realized trade below `−max_loss_r` voids certification.
2. **Expectancy lower bound** = min(percentile bootstrap, t-type, empirical-Bernstein bound
   (Maurer & Pontil 2009) on outcomes bounded by `[−max_loss_r, max_win_r]`), at the adjusted
   level and with n_eff.
3. **Acceptance:** the Amendment 4 grid must pass for all three reference strategies with
   `max_loss_r` set to their true worst loss. The certificate states `max_loss_r` and its source.
