# Mathematical strategy foundations for M4 (H1–H3): integrated research audit

**What this file is.** The independent mathematical research audit (Claude Fable 5.2,
2026-11-10), reproduced **unchanged** in Part 2. Part 1 is its reconciliation against the current
implementation. Nothing in either part is decided, frozen or approved. Where Part 1 corrects or
qualifies a statement in Part 2, Part 1 prevails, and the original wording is kept so the change is
visible. No accepted protocol (RESEARCH_PROTOCOL, ADR-0012 and its accepted amendments, the
decisions in DECISIONS_PENDING) is changed by this file.

| | Revision |
|---|---|
| Audited snapshot | `task/mvp-integration` @ `1cc2693` (read-only; CI `42837576325` 7/7) |
| Reconciled against | `task/mvp-integration` @ `2b63d4c`. Its code equals `1cc2693` except `f716a71` (walk-forward reports sum the risk-policy blocks) and `ce751fc` (a synthetic rehearsal test). The diagnostics added at this checkpoint are listed in 1.4 |
| Source of the text | claude.ai Project document `claude/TSC_MATHEMATICAL_STRATEGY_FOUNDATIONS.md`, retrieved 2026-11-10 |
| Number check | `tests/unit/research/test_math_foundations_numbers.py` recomputes every derived number below with the repository's own functions |

Evidence labels are the audit's: **[L]** literature (never a claim about TSC), **[D]** derived from
the repository's definitions and cost model, **[E]** needs original testing in TSC, no evidence yet.
Part 1 adds **[I]**: verified against the implementation at the revision above.

# Part 1. Reconciliation (2026-11-10)

## 1.1 Implementation claims, verified [I]

| Audit statement (Part 2) | Implementation | Verdict |
|---|---|---|
| §1.1 `ATR` = simple mean of 14 true ranges, not Wilder | `features/core.py` `atr`: rolling mean of `true_range` | Correct |
| §1.1 `rank_i` = share of the last 96 ATRs `<=` the current one (current included) | `atr_percentile_rank`: `(w <= last).sum() / m` over a window of `m` that includes the last value | Correct |
| §1.1 `RH_i` = max of the last 20 highs including bar `i`; breakout `C_i > RH_{i-1}` | `range_high` (window includes `i`); the strategy reads `hi[-2]` | Correct |
| §1.1, A5 volume `z` uses population sd with bar `i` inside the window, so `z <= sqrt(19) = 4.36` | `volume_zscore`: `np.std` (ddof 0), window includes the last bar | Correct |
| §1.1 trend: last closed 1h close above the mean of the last 50 closed 1h closes | `trend_1h` / `closed_hour_closes` (complete, hour-aligned groups only) | Correct |
| §1.1 compression read at `i-1` | `atr_rank_prev = rank[-2]` | Correct |
| §1.1 stop `C - stop_atr × ATR`, target `C + tp_r × (C - S)`, `cost_ok` at 0.26 % / 0.2 | `_geometry`, `_bracket`, `PlanningCosts.cost_r`; round trip 0.26 % from `CostModel` | Correct (minimum stop 1.30 % of price) |
| §1.1 stop first when stop and target fall in one 1m bar | `sim/fills.py`: stop-loss first, flagged `ambiguous_bar`; a same-bar entry never books its take-profit in its fill bar (L-04) | Correct; L-04 is an additional conservative rule |
| §1.1 entry "at the 1m open after the 3,000 ms decision latency" | `sim/engine.py earliest_execution_ms`: first 1m open strictly after `close + 1 ms + 3,000 ms`, i.e. the **second** 1m open after the decision close, as in Paper (`docs/research/evidence/live_release_latency_2026-11-10.json`) | Correct; made precise here |
| A11 slippage fixed at 3 bps | `FixedSlippage`: 2 bps half-spread + 1 bp impact per side | Correct |
| §3.1 the engine's `rho(b, d)` with `c_e = c_sl = 0.13 %`, `c_tp = 0.10 %` (maker take-profit) | `risk/engine.py cost_adjusted_rr`; `CostModel.tp_as_taker = False` | Correct |
| §0, §3.5, §7 Bonferroni at `alpha / trials`, trials from the registry | `research/stats/certify.py`: `alpha_adj = alpha / n_trials` (Holm's first step), `n_trials` from the registry; the walk-forward fixes it at plan time | Correct |
| §3.5 199 registry rows for H1 as drafted | 24 × 7 train + 7 test + 24 final-selection rows, anchor 2022-02-01, 12/3-month folds | Correct (an upper bound: reused rows are not counted again) |
| §3.5 out of sample: 84 symbol-months; validation 48 | 7 folds × 3 months × 4 symbols; 12 × 4 | Correct |
| A9 four symbols in one `btc_beta` cluster | `configs/risk.yaml`: `default_cluster: btc_beta`, `max_per_cluster: 3` | Correct, see 1.3 (c) |
| Embargo covers the time exit | 1,440 min ≥ (48 + 1) × 15 = 735 min on 15m; validation refuses a shorter embargo; 1h needs ≥ 2,940 min | Correct |
| §6 item 4 "1.5 × ATR(15m) reaches the minimum 1.30 % stop on under 5 % of bars" | The closure handoff (H1-1) says: under 5 % of all real 15m bars pass the cost filter at a 1.0–1.5 × ATR stop | Equivalent; the handoff wording is the measured one |
| §8 I-9 "28–46 hours for 24 configurations on 5 symbols" | Measured for H1-v1 (5 symbols, 8 folds). H1-v3 has 4 symbols and 7 folds | A conservative upper estimate for H1-v3 |

## 1.2 Derived numbers, recomputed [D]

All recomputed with `research/stats/power.n_min_one_sided`, the engine's own reward/risk formula
and an independent numerical integral; the test file pins them. **Every value in §3.1, §3.2,
§3.3, §3.5, A5, A7 and A8 reproduces.** One rounding correction:

- **§3.4 "about 1.9 times as many trades"** → **about 2.0 times**. For E = +0.15 R the ADR-0012 sample
  size is 423 trades at `rho` = 1.52 against 215 at `rho` = 0.8 (ratio 1.97). The audit's 1.9 is
  the squared margin ratio (1.96) rounded down. The conclusion is unchanged.

## 1.3 Qualifications (the floor and win rates)

The 1.5 floor is a rule on the **planned geometry of each order**, evaluated by the risk engine at
submit and again at fill. It is not a rule on win rates. Four statements in Part 2 need this
qualification.

**(a) "It caps the break-even win rate at 0.40" (§0 item 4, §3.2).** This is true for the
*planned* break-even of a pure bracket, `p*_planned = 1/(1 + rho) <= 0.40` [D]. It is **not** a
bound on the *realized* break-even that ADR-0012 certifies against for H1, for three reasons [I]:
- H1 has a required 48-bar time exit, so payoffs vary. A time exit can close a winner below
  `rho` and a loser above −1 R. H1's `design_p_star` returns None, so certification uses the
  realized `p* = L / (W + L)`, which can sit above or below 0.40.
- Losses can exceed the planned stop: gap-through fills, stop slippage, up to `max_loss_r`
  (L-10, Amendment 5).
- The floor is judged at the decision close with modelled slippage. The order fills at the second
  open after the decision (3,000 ms latency), so the realized entry, and with it the realized
  `rho`, differs from the planned one.

**(b) "Certified win rates of a floor-compatible strategy will normally sit below 50 %" (§0
item 4) and R-1.** The floor places no bound on the win rate itself. A floor-compatible strategy
with a large edge could win more than half of its trades. The expectation of win rates below 50 %
is a prior, from the geometry (A8: about 1/(1 + `tp_r`) under a martingale) and from the
trend-following literature (Potters & Bouchaud 2005). It is [D]+[L] as a prior, and [E] as a
statement about H1. As a record for D-E, R-1 should therefore say: *under the kept floor the
planned break-even of every admitted bracket is at most 0.40; achieved and certified win rates are
measured, not implied, and a win rate below 50 % is not by itself a failure.*

**(c) "The floor removes the high-win-rate half of the profitable region" (§3.2).** The floor
removes **orders whose planned net reward/risk is below 1.5**, whatever their win probability.
A trade with a high probability of reaching a target at ≥ 1.5 R is allowed (the §3.3 OU example
shows this). The statement holds for the *classic* high-win-rate geometries (target below 1.5 R),
which is what the audit means.

**(d) Power is computed on trades, and trades can be fewer than candidates (§3.5, F4).** Since
`9f2cabc` (already in the audited snapshot), backtests apply the paper risk policy. The audit does
not mention the portfolio rules, which bind on H1's correlated universe [I]:
- **Concurrency:** at most 5 positions, 1 per symbol, and at most 3 in the `btc_beta` cluster.
  Four symbols that break out together can yield at most 3 concurrent trades.
- **Loss limits:** the daily loss limit (2 %) and the drawdown kill switch (10 %, which halts the
  book for the rest of that run).
- **Data freshness:** no entry within 240 minutes after any missing 1m bar.
- **The floor at fill:** it is re-checked at the second open after the decision.

The P&L-free feasibility count measures **candidates** before these rules. Before freezing, the §3.5
tables should be read against candidates minus these losses. Walk-forward reports now show the
checks each rule blocked (`risk_policy_blocks`, `f716a71`).

**(e) Take-profit fill assumption (§3.1, §3.2).** All `rho` values assume maker take-profit fills
without slippage, which is how Paper and the backtests model them. With taker take-profits
(`tp_as_taker`), H1 at 2 R and a 1.30 % stop nets 1.4995 < 1.5 (CONDITIONAL). 2.5 R nets 1.92 and
3 R nets 2.33. Scaling every cost by a factor leaves these values unchanged, because H1's cost filter
scales the minimum stop with the costs (H1_DECISION_BRIEF §3).

## 1.4 Status of the audit's recommendations

None of the audit's recommendations is authorized by this file. Their status on 2026-11-10:

| Item | Status | Where |
|---|---|---|
| F2 hour-of-day output in the P&L-free feasibility report | Implemented (feasibility-v2, `diagnostics.by_utc_hour`, per symbol and overall, with the share of candidates in the 6 busiest hours). Also stage-filter statistics (pass rate from the previous stage, marginal and leave-one-out counts). Outside selection and certification | `research/feasibility.py`; `tests/unit/backtest/test_feasibility.py` |
| F3 time-exit share per configuration | Implemented. Run metrics already recorded exit reasons; walk-forward reports now aggregate them per phase and configuration with the time-exit share (`exit_mix`). Run metrics are unchanged | `research/walkforward.py _exit_mix`; `tests/unit/research/test_power_plan.py` |
| F4 read power against counts | Implemented. Before a freeze, `tsclaude backtest power-plan --feasibility <file>` gives the out-of-sample trade upper bound and the minimum detectable margin per take-profit, at n_eff ratios 1, 0.75 and 0.5. After a run, walk-forward reports carry `power_reading` | `research/power_plan.py`, `power_plan_cli.py`; `test_power_plan.py` |
| F1 / R-6 ablation ladder | **Partly implemented, P&L-free:** candidate counts per rung (trend → + breakout → + compression → + volume) in every feasibility count. The conditional random control per rung is a **proposal** (decision package §5): owner approval and a preregistration field first | `ABLATION_LADDERS`; [H1 decision package](H1_DECISION_PACKAGE.md) |
| F5 fixed parameters stated as untested priors | Proposal for the H1 preregistration text | Decision package |
| R-1 … R-8 | **Owner decisions, open** | Decision package |
| I-2 … I-8 (H2, H3, features, overlap, limit entries, USD-M) | Deferred. H2 and H3 stay deferred until H1's protocol path is proven (MVP_TASK_QUEUE P2) | — |
| §7 no econometric or ML models before the MVP research milestone | Consistent with the roadmap; nothing of the kind is planned | — |

The H1–H3 slate (RESEARCH_PROTOCOL §8) is preserved as written. Part 2's §2 quotes it correctly,
and §6's design directions for H2 and H3 are proposals inside those definitions, not new
hypotheses.

# Part 2. The audit as delivered (unchanged)

The text below is the audit exactly as retrieved. Relative links resolve from `docs/research/`.

# Mathematical strategy foundations for M4 (H1–H3)

**Status: research audit for owner review. Nothing here is decided, frozen or promoted.**
Written 2026-11-10 by an independent reviewer (Claude Fable 5.2) from a read-only snapshot of
`task/mvp-integration` at `1cc2693`. No backtest, walk-forward, trial, registry write or holdout
access happened. No market data was read. No risk rule, config or code was changed.
Every number is either quoted from a repository document or computed in closed form from the
repository's cost model (Appendix B). Existing decisions are preserved; new questions for the owner
are in §10 and are labelled R-1 … R-8 so they cannot be confused with D-numbers.

**Reading guide.** Decisions: §0, §9, §10. Engineering: §6, §8, §11. The rest is the supporting
audit (H1 in §1, the floor in §3, the families in §4, the literature in §5 and the references).

Evidence labels used throughout:

| Label | Meaning |
|---|---|
| **[L]** | Supported by peer-reviewed literature, in the market, period and frequency that paper studied. Never a claim about TSC. |
| **[D]** | Derived mathematically from the repository's own definitions and cost model. |
| **[E]** | Needs original empirical testing in TSC. No evidence exists yet. |

## 0. Bottom line

1. **H1 is a trend-following hypothesis.** Its direction comes from a 20-bar breakout and a 1h
   moving-average filter (time-series momentum). Volatility compression is a regime filter and sets
   the stop size; it does not predict direction. H1 is not mean reversion.
2. **Indicators are measurements, not evidence.** ATR, percentile rank, range high, volume z-score
   and moving averages describe the past. Whether they change the conditional distribution of
   future returns, net of costs, is an empirical question that no H1 run has yet addressed.
3. **H2 and H3 are already defined** in [RESEARCH_PROTOCOL §8](../RESEARCH_PROTOCOL.md) and
   [M4-T1](../tasks/M4-T1.md): H2 trend pullback continuation, H3 range mean reversion. This report
   keeps both and recommends design directions inside those definitions.
4. **The 1.5 net reward/risk floor is a constraint on payoff shape, not on expectancy** [D]. It
   caps the break-even win rate at 0.40, so certified win rates of a floor-compatible strategy will
   normally sit below 50 %. It fits trend and breakout strategies naturally and excludes the
   classic mean-reversion profile (target at the mean, wide stop, 60–75 % wins).
5. **Mean reversion is still expressible under the floor**, as a "deep stretch, tight stop" trade.
   That turns H3 into a sharp test of stationarity, with far fewer candidate trades. Its
   feasibility must be counted P&L-free before any design effort.
6. **Statistical power is the binding constraint** [D]. With the registry trial count of 199, a
   5-point win-rate margin needs about 1,800 trades and a 10-point margin about 450. Hypotheses
   that trade rarely can only report "insufficient evidence".
7. **As slated, H1 and H2 are correlated bets** (both long in a 1h uptrend). They become distinct
   only if H2 is restricted to the complementary state: an established trend, not compressed,
   entered on weakness. H3 is the only diversifier.
8. **Do not build econometric or machine-learning models before the MVP research milestone.** The
   limits are costs, geometry and sample size, none of which a model relaxes; a fitted model adds
   trials and infrastructure.

No family has verified literature evidence of net profitability for a taker at 15m–1h on spot at a
0.26 % round trip in 2022–2025. "No demonstrated edge" remains the expected and reportable outcome.

## 1. H1 audit

Source: [`h1_compression_breakout.py`](../../src/tsclaude/strategies/h1_compression_breakout.py),
[`features/core.py`](../../src/tsclaude/features/core.py),
[H1-v3 draft](H1-v3.preregistration.DRAFT.yaml), [H1 decision brief](H1_DECISION_BRIEF.md).

### 1.1 Mathematical definition

For closed strategy bars `i` (15m, or 1h pending the H1-1 feasibility count) with `O, H, L, C, V`:

```
TR_i    = max(H_i, C_{i-1}) - min(L_i, C_{i-1})
ATR_i   = mean(TR_{i-13..i})                          simple mean, n = 14 (not Wilder smoothing)
rank_i  = #{k in 0..95 : ATR_{i-k} <= ATR_i} / 96     empirical CDF of ATR over m = 96 bars
RH_i    = max(H_{i-19..i})                            20-bar range high
z_i     = (V_i - mean(V_{i-19..i})) / sd(V_{i-19..i}) population sd; bar i is inside the window
T1h     = C1h_last > mean(last 50 closed 1h closes)   finite-window trend, closed hours only

compressed     rank_{i-1} <= rank_max                 rank_max in {0.25, 0.35}
breakout       C_i > RH_{i-1}
participation  z_i >= vol_z_min                       vol_z_min in {1, 2}
CONFIRMED      compressed and T1h and breakout and participation and cost_ok

stop S   = C_i - stop_atr * ATR_i                     stop_atr in {1.0, 1.5}
target   = C_i + tp_r * (C_i - S)                     tp_r in {2.0, 2.5, 3.0}
cost_ok  0.26 % * C_i / (C_i - S) <= 0.2              so (C_i - S)/C_i >= 1.30 %
exit     stop, target, or time exit after 48 bars; stop first if both fall in one 1m bar
entry    market, long only, at the 1m open after the 3,000 ms decision latency; valid 1 bar
```

### 1.2 Parameter inventory

| Kind | Parameters | Origin |
|---|---|---|
| Searched (24 configurations) | `rank_max` {0.25, 0.35}, `vol_z_min` {1, 2}, `stop_atr` {1.0, 1.5}, `tp_r` {2, 2.5, 3} | `tp_r` from the cost formula and the floor; the others are researcher priors |
| Fixed | `atr_n` 14, `rank_m` 96, `range_n` 20, `vol_n` 20, `trend_n` 50, `watch_atr` 0.5, `time_exit_bars` 48, `max_cost_r` 0.2, `interval` | Conventional values. None comes from the literature and none was optimized |

Thirteen parameters, four searched. The nine fixed values are untested choices: they are part of
the hypothesis, and a result holds only for them.

### 1.3 Assumption register

| # | Assumption | Label | Comment |
|---|---|---|---|
| A1 | Volatility clusters and is forecastable | [L] | Engle 1982; Bollerslev 1986; Cont 2001; Corsi 2009; for Bitcoin, Katsiampa 2017. This is about the size of moves, not their sign |
| A2 | Compression is followed by expansion soon enough to matter | [E] | Clustering says the opposite in the short run: low volatility tends to persist. H1 needs the turn to arrive within 48 bars |
| A3 | A close above the 20-bar high, with the 1h trend, continues | [L] elsewhere, [E] here | Trading-range breaks and moving averages: Brock et al. 1992, with the data-snooping caveat of Sullivan et al. 1999. Crypto momentum at daily and weekly horizons: Liu & Tsyvinski 2021. Nothing verified for this rule at 15m–1h net of 0.26 % |
| A4 | Abnormal volume marks moves that continue | [L] mixed | Campbell, Grossman & Wang 1993 find that daily return autocorrelation falls as volume rises (high-volume moves revert more). Shen et al. 2022 report stronger Bitcoin intraday momentum when volume or volatility is high. The sign is open |
| A5 | A z-score of 1 or 2 is a meaningful volume threshold | [D] | Volume is right-skewed and seasonal, and bar `i` is inside its own window, so `z` is bounded by `sqrt(19) = 4.36` and its tail frequencies are not Gaussian. Only the measured frequency means anything |
| A6 | `ATR` at the decision bar is the right stop scale | [D] | After compression `ATR` is at its low, so stops are small in price terms and costs are large in R. This is the known H1-1 conflict with `cost_ok` |
| A7 | 48 bars is enough for a 2–3 R target | [D] | For a driftless diffusion with barriers `+a`, `-c` the mean exit time is `a*c/sigma^2` bars. With `a = 2.6 %`, `c = 1.3 %`: 38 bars at `sigma = 0.30 %` per bar, 85 bars at 0.20 %, 14 bars at 0.50 % (illustrative volatilities, not measured). On 15m many trades will end at the time exit; on 1h few will |
| A8 | The edge is the win rate above geometry | [D] | Under a martingale, `P(target before stop) = 1/(1 + tp_r)`: 0.33, 0.29, 0.25 before costs. All of H1's value is the excess over the realized break-even |
| A9 | Trades are close to independent | [D] | Four symbols in one `btc_beta` cluster break out together. The block bootstrap and `n_eff` gate (ADR-0012 Amendments 2–3) handle it, at a cost in power |
| A10 | The relation is stable over 2022–2024 | [E] | A bear market, a recovery and a bull market. Long only, so the short side is never tested |
| A11 | Fixed 3 bps slippage holds at breakout bars | [E] | Breakouts on abnormal volume are when spread and impact are worst. The ×1.5 and ×2 cost stress covers part of it |
| A12 | Compression is not just time of day | [E] | On 15m, `rank_m = 96` is one day, so "lowest quartile" may largely select the quiet hours. The time-of-day-matched random control (RESEARCH_PROTOCOL §4) is the safeguard |

### 1.4 What mechanism H1 is

H1 is **volatility-regime-conditioned time-series momentum with volume confirmation, long only**.
The breakout and the trend filter are both trend-following entries. Compression decides *when* to
listen to them and how tight the stop is. It is not mean reversion, and it is not a volatility
forecast: nothing in the rule estimates future volatility.

One theoretical link exists between the two parts [L]. Christoffersen & Diebold (2006) show that
predictable volatility makes the *sign* of returns predictable when the expected return is not
zero, because `P(r > 0) = 1 - F(-mu/sigma)`. With a positive drift (the trend filter), lower
volatility raises the probability of a positive return. The effect is real in theory and very small
at intraday horizons, where `mu/sigma` is close to zero. It supports the direction of H1's
conditioning, not its size.

### 1.5 Indicators against demonstrated predictive power

An indicator is a deterministic function of past bars. It has predictive power only if the
distribution of future returns given the indicator differs from the unconditional one, and it has
*economic* value only if that difference exceeds costs. Demonstrating it needs four things the
protocol already requires: a null model (random entry with the same exits), out-of-sample data,
realistic costs, and a correction for every configuration tried. Until then H1 is a formal
conjecture. The literature's own record is a warning: rule profits shrink under data-snooping
control (Sullivan et al. 1999; Bajgrowicz & Scaillet 2012), and published predictors lose about a
quarter of their return out of sample and more than half after publication (McLean & Pontiff 2016,
equities).

### 1.6 Findings for engineering

| # | Finding | Action |
|---|---|---|
| F1 | The random control drops all entry conditions at once, so a pass cannot say which condition carried information. A long-only rule with a trend filter can beat random entries on trend exposure alone | Preregister an ablation ladder as secondary diagnostics, never used for selection: control given trend only; plus breakout; plus compression; plus volume (R-6) |
| F2 | Time-of-day confound (A12) | Add an hour-of-day histogram of candidates to the P&L-free feasibility output |
| F3 | Horizon and geometry (A7) | Report the share of time exits per configuration; choose 15m against 1h from counts only, as already planned |
| F4 | Power (§3.5) | Read the feasibility count against the trade numbers in §3.5 before freezing |
| F5 | Nine fixed conventional parameters | State in the preregistration that they are untested priors; do not vary them after seeing results |

No change to H1's rules or grid is proposed here.

## 2. Existing definitions of H2 and H3 (preserved)

| Item | Approved text | Source |
|---|---|---|
| H2 | Trend pullback continuation: in a causal 1h uptrend or downtrend, a pullback to a volatility band that resumes with momentum continues. 1h context, 15m entry, long/short | RESEARCH_PROTOCOL §8; M4-T1 deliverable 2 |
| H3 | Range mean reversion: in a low-trend regime, stretched moves (z-score of close against the range mid) revert, only when the expected move exceeds costs. 15m, long/short | RESEARCH_PROTOCOL §8; M4-T1 deliverable 2 |
| H4 | Funding-extreme filter on H1/H2 entries; needs futures data | RESEARCH_PROTOCOL §8; ROADMAP M4 ("H4 if futures data permits") |
| H0 | Random-entry control for each | RESEARCH_PROTOCOL §8 |
| Order | H2/H3 deferred until H1's protocol path is proven; no new hypothesis invented | MVP_TASK_QUEUE P2 |
| Sides | Shorts only on USD-M (D-02); USD-M history and funding are not imported (D-22) | DECISIONS_PENDING |

Two later facts change what these texts can mean in practice, without changing the texts:

- **Long only on spot** for now, as for H1. The "long/short" in the slate is not reachable until
  USD-M data exists (R-2).
- **The floor is kept** (owner direction 2026-11-10, D-E open). ADR-0012's 0.5–1.5 R geometries
  book no trades. H3's natural geometry is exactly that range (§3.3).

No ADR defines H2 or H3 beyond these lines. No parameter, band or z-threshold has been approved.

## 3. The fixed net reward/risk floor of 1.5

The rule is not changed here. This section says what it implies.

### 3.1 Algebra

The engine (`risk/engine.py`, `cost_adjusted_rr`) computes, for a long bracket with stop distance
`d` (fraction of price) and target `b*d`:

```
rho(b, d) = [ b*d - c_e - c_tp*(1 + b*d) ] / [ d + c_e + c_sl*(1 - d) ]

spot, market entry:  c_e = c_sl = 0.13 % (taker 0.10 % + slippage 0.03 %),  c_tp = 0.10 % (maker)
rho >= 1.5   <=>   b >= b_min(d) ~= 1.5 + 0.62 % / d        (limit entry: 0.545 %; USD-M: 0.34 %)
```

| Stop distance `d` | 0.8 % | 1.3 % | 2.0 % | 3.0 % | 5.0 % |
|---|---|---|---|---|---|
| `b_min`, spot, market entry | 2.28 | 1.98 | 1.81 | 1.71 | 1.62 |
| `b_min`, spot, limit entry | 2.18 | 1.92 | 1.77 | 1.68 | 1.61 |
| `b_min`, USD-M, market entry | 1.92 | 1.76 | 1.67 | 1.61 | 1.57 |

With H1's cost cap (`max_cost_r` 0.2, so `d >= 1.30 %`), **every spot trade needs a target at
least 2.57 % from the entry**. The cost cap is a strategy parameter, not a risk rule (R-8).

### 3.2 What the floor does to win rates

For a pure bracket in net units the break-even win rate is `p* = 1/(1 + rho)`, so
**`rho >= 1.5` means `p* <= 0.40`**. Expectancy per trade is `E = p*rho - (1 - p)`.

| Net `rho` | Win rate `p` | `p*` | `E` | Allowed by the floor |
|---|---|---|---|---|
| 0.50 | 0.75 | 0.667 | +0.13 R | No |
| 0.80 | 0.65 | 0.556 | +0.17 R | No |
| 1.52 | 0.45 | 0.397 | +0.13 R | Yes |
| 2.35 | 0.35 | 0.299 | +0.17 R | Yes |

The floor therefore removes the high-win-rate half of the profitable region and keeps the
low-win-rate half. It does not filter on expectancy: a floor-compatible trade can lose money and an
excluded one can make it. A 60 % win rate at `rho = 1.52` would mean +0.51 R per trade, far beyond
any edge reported net of costs in the literature reviewed here. **Under the floor, "highest win
rate" can only mean "highest certified margin above a break-even of at most 0.40"** (R-1). This is
consistent with trend following in general: in an exactly solvable trend-following model the share
of winning trades is below one half even when the average gain is zero, so the share of winners
describes the style, not the quality (Potters & Bouchaud 2005).

### 3.3 Effect on each family

| Family | Natural payoff | Under the floor |
|---|---|---|
| Momentum, trend, breakout | Few large wins, many small losses; `b` of 2–4 | Compatible as designed |
| Single-asset mean reversion | Target at the mean, stop beyond the extreme; `b` of 0.3–1; wins 60–75 % | Natural form excluded. Only "deep stretch, tight stop" remains: stop distance at most target / `b_min` |
| Pairs and statistical arbitrage | As mean reversion, per spread | Excluded twice: geometry, and no short leg on spot |
| Cross-asset lead–lag | Small, fast moves | A 2.57 % target is far larger than any minute-scale lead–lag effect |
| Probabilistic filter on a base strategy | Inherits the base geometry | Neutral |

**What a floor-compatible mean reversion looks like** [D]. If the deviation from the mean were an
Ornstein–Uhlenbeck process with stationary standard deviation 1, the probability of reaching the
mean before the stop, starting at `-k` with stop at `-k(1 + 1/b)`, is
`(S(L) - S(k)) / S(L)` with `S(a) = integral_0^a exp(y^2/2) dy`:

| Entry | `b` | Stop | P(win), OU | P(win), random walk |
|---|---|---|---|---|
| −1.5 sd | 2 | −2.25 sd | 0.68 | 0.33 |
| −2.0 sd | 2 | −3.00 sd | 0.87 | 0.33 |
| −2.0 sd | 3 | −2.67 sd | 0.71 | 0.25 |

So the floor does not forbid a high win rate *if the range is truly stationary*. It makes H3 a
clean test between two predictions: about 1/3 if prices are a random walk, much higher if the range
is real. The numbers are an idealized upper reference (continuous time, known mean and variance, no
time exit, no fat tails). Two cautions: tight stops lower expected return when returns are
unforecastable and add value mainly under momentum (Kaminski & Lo 2014), which is the opposite of
the regime H3 trades; and a 2.57 % target at `k = 2` needs a range whose standard deviation is at
least 1.3 % of price, which is a wide range for a "low-trend" 15m market.

### 3.4 What the floor buys and what it costs

- **Buys.** Costs stay small relative to the reward. It keeps out negatively skewed strategies
  (many small wins, rare large losses), which are the ones whose backtests are least trustworthy:
  a sample without the tail event looks profitable (ADR-0012 Amendment 5). One rule for research
  and Paper.
- **Costs.** The classic mean-reversion profile is unavailable, so the portfolio leans toward
  trend exposure. And certification gets harder: for the same expectancy, the margin is
  `Delta = E/(1 + rho)`, smaller at higher `rho`. A +0.15 R edge is a 6.0-point margin at
  `rho = 1.52` and an 8.3-point margin at `rho = 0.8`, so the floor-compatible strategy needs about
  1.9 times as many trades to certify the same edge [D].

### 3.5 Sample size under the floor

ADR-0012's `n_min` at 80 % power, one-sided, `alpha = 0.05 / trials` [D]:

| `p*` | True win rate | Edge `E` (approx.) | 1 trial | 24 trials | 199 trials |
|---|---|---|---|---|---|
| 0.40 | 0.43 | +0.08 R | 1,661 | 3,682 | 5,000 |
| 0.40 | 0.45 | +0.13 R | 600 | 1,329 | 1,804 |
| 0.40 | 0.48 | +0.20 R | 235 | 520 | 706 |
| 0.40 | 0.50 | +0.25 R | 151 | 333 | 452 |
| 0.30 | 0.35 | +0.17 R | 534 | 1,176 | 1,594 |
| 0.30 | 0.40 | +0.33 R | 136 | 298 | 403 |

The 300-trade floor still applies. Clustered trades reduce the effective count further. The
out-of-sample part of the development segment is 7 folds of 3 months on 4 symbols, 84
symbol-months; validation is 48. A hypothesis producing 300–500 out-of-sample trades can certify
only an edge of roughly +0.25 R or more. A smaller grid helps a little (about 16 % fewer trades at
55 registry rows than at 199); trade frequency and the true effect dominate.

## 4. Strategy families

Summary. Fit means fit to spot, long only, bracket exits, the 1.5 floor and the current engine.

| Family | Literature support at TSC's horizon | Cost sensitivity | Fit today | Build size | Role for H1–H3 |
|---|---|---|---|---|---|
| A. Mean reversion, stationary spreads | Weak for a taker | Very high | Single asset: constrained. Spreads: no | S–M / XL | H3 (single asset only) |
| B. Momentum, trend following | Best of the five, at daily to weekly horizons | Moderate | Good | S | H1 direction, H2 |
| C. Volatility breakout, regime conditioning | Strong for volatility, weak for direction | Moderate, high when stops are tight | Good | S (done for H1) | H1; regime labels for all |
| D. Cross-asset predictability, statistical arbitrage | Mixed, cost-fragile | Very high | No | L–XL | None before MVP |
| E. Econometric and probabilistic models | No short-horizon net evidence | Inherited | As a filter only | M–L | None before MVP |

### A. Mean reversion and stationary spread models

- **Formulation.** Single asset: `x_t = log P_t - m_t`, `z_t = x_t / s_t`, with `m_t`, `s_t`
  trailing. Model `dx = -theta*x dt + sigma dW`; half-life `ln 2 / theta`; stationary sd
  `sigma / sqrt(2*theta)`; discretely `x_{t+1} = phi*x_t + eps`, `phi < 1`. Diagnostic: variance
  ratio `VR(q) = Var(r_q) / (q*Var(r_1)) < 1` (Lo & MacKinlay 1988). Spread:
  `s_t = log P_A - beta*log P_B - mu` stationary (Engle & Granger 1987); optimal entry and exit
  bands with costs for an OU spread (Bertram 2010); residual s-scores (Avellaneda & Lee 2010).
- **Rationale.** Short-horizon reversal is the price of liquidity: the trader who absorbs a
  temporary imbalance is paid for it (Lehmann 1990; Jegadeesh 1990; Nagel 2012). The premium
  belongs to the liquidity *provider*. A taker pays the spread the effect is made of.
- **Data.** 1m bars for one asset. Synchronized bars for spreads; a short leg needs USD-M and
  funding, not imported.
- **Assumptions and failure modes.** The mean is stable; it is not in a trend or after a regime
  break. Fat tails and gaps. Non-converging pairs: the pairs-trading literature reports declining
  profits and results that are largely gone after trading costs (Gatev et al. 2006; Do & Faff
  2012; Krauss 2017).
- **Cost sensitivity.** Very high: small gross edge per trade, many trades. On Binance, pairs
  strategies on 26 coins underperformed benchmarks and were sensitive to costs and execution
  delay (Fil & Kristoufek 2020).
- **Spot and risk fit.** Single asset, long only, with a bracket: yes, in the tight-stop form of
  §3.3. Spreads: no; one bracket per symbol, no short, a cluster cap of 3.
- **Timeframes.** 1h; 15m only on the more volatile symbols. 5m is not viable for a taker.
- **Causal execution.** `m_t`, `s_t` and the regime from closed bars up to `T`; order at the next
  open or a resting limit; for spreads both legs closed at `T` and filled together.
- **Validation.** Stationarity diagnostics on the training window only, as fixed gates;
  certified margin with realized `p*`; random control; share of losses beyond the stop; regime
  breakdown.
- **Build.** Single asset: small to medium (z-score, range mid, trend-strength features; limit
  entries exist in the simulator). Spreads: very large (multi-leg orders, spread-level risk,
  USD-M data).

### B. Momentum and trend following

- **Formulation.** Time-series momentum: `r_{t,t+h} = a + b*r_{t-L,t} + e`, `b > 0`. Rules:
  `C_t > mean(C_{t-L+1..t})`; breakout `C_t > max(H_{t-N..t-1})`. Payoffs are positively skewed.
- **Rationale.** Slow diffusion of information, attention and feedback trading. In
  cryptocurrencies, momentum and investor-attention proxies predict returns (Liu & Tsyvinski
  2021).
- **Data.** 1m bars. Funding is optional (H4).
- **Assumptions and failure modes.** Trends persist longer than the lookback; fails in ranges
  (whipsaw) and at sharp reversals. Most exposed to data snooping, because rule variants are
  cheap to generate (Sullivan et al. 1999; White 2000).
- **Cost sensitivity.** Moderate: large targets, low turnover. For daily crypto rules through
  2017, break-even costs were mostly above the roughly 50 bps the authors take as Bitcoin's cost,
  and the best in-sample rules stayed profitable in early 2018 for three coins but not for
  Bitcoin (Hudson & Urquhart 2021).
- **Spot and risk fit.** Good. Long only means no trades in downtrends.
- **Timeframes.** Evidence is daily to weekly (Moskowitz et al. 2012 across asset classes; Liu &
  Tsyvinski 2021 for crypto) and a specific half-hour intraday pattern in Bitcoin (Shen et al.
  2022). For TSC: 1h decisions with multi-day context. 4h is post-MVP backlog.
- **Causal execution.** Closed bars only; higher-timeframe context from closed bars only
  (`closed_hour_closes` already does this).
- **Validation.** The protocol as it stands, plus the ablation ladder (F1).
- **Build.** Small. H1 exists; H2 reuses its machinery.

### C. Volatility breakout and regime-conditioned strategies

- **Formulation.** GARCH(1,1): `sigma2_t = w + alpha*e2_{t-1} + beta*sigma2_{t-1}`. HAR:
  `RV_{t+1} = b0 + bd*RV_d + bw*RV_w + bm*RV_m` (Corsi 2009). A regime label
  `R_t = 1{rank(ATR_t) <= rho}` multiplies a direction rule. Sign link:
  `P(r > 0) = 1 - F(-mu/sigma_t)` (Christoffersen & Diebold 2006).
- **Rationale.** Volatility is persistent and forecastable. A range break after a quiet period is
  read as the start of information arrival.
- **Data.** 1m bars. Intraday seasonality of range and volume should be measured.
- **Assumptions and failure modes.** Volatility forecastability is not direction forecastability.
  Low volatility persists, so expansion may not come. Most range breaks fail. Slippage is worst at
  the break.
- **Cost sensitivity.** Moderate, but high when the stop is sized on compressed volatility
  (H1-1).
- **Spot and risk fit.** Good on the floor; the cost cap is the binding limit.
- **Timeframes.** 1h; 15m only if the feasibility count allows.
- **Causal execution.** Rank at `i-1`, break at the close of `i`, entry after the latency.
- **Validation.** P&L-free feasibility; time-of-day-matched control; ablation ladder.
- **Build.** Done for H1. A fitted volatility model would be medium and is not needed.

### D. Cross-asset return predictability and statistical arbitrage

- **Formulation.** Lead–lag: `r_alt,t+1 = a + b*r_BTC,t + c*r_alt,t + e`. Residual reversion:
  `r_i = beta_i*F + eps_i`, cumulative residual as an OU process, trade its s-score (Avellaneda &
  Lee 2010). Distance pairs (Gatev et al. 2006).
- **Rationale.** Common information reaches the most liquid asset first; residual reversion is
  liquidity provision again.
- **Data.** Synchronized multi-symbol bars. A usable cross-section needs far more than five
  symbols (Top-10 coverage is deferred), and the short side needs USD-M and funding.
- **Assumptions and failure modes.** Lead–lag lives at horizons shorter than TSC's latency plus
  next-minute fill. Correlations break in stress. Five symbols in one cluster is not a
  cross-section.
- **Cost sensitivity.** Very high: two legs and high turnover. In the Binance study the gap
  between daily and 5-minute results was large and depended on costs and execution windows (Fil &
  Kristoufek 2020).
- **Spot and risk fit.** No. The strategy interface is single-symbol
  (`on_bar(window, costs)`); cross-asset features need a contract change.
- **Timeframes.** 1m–5m, which conflicts with a 1.30 % minimum stop.
- **Causal execution.** The leader's bar must be closed before the follower's decision; identical
  timestamps; no same-bar follower information.
- **Validation.** Predictive regressions on training data, placebo leaders, costs.
- **Build.** Large to very large.

### E. Simple econometric and probabilistic models

- **Formulation.** AR and ARMA for returns; logistic model
  `P(target before stop | x) = 1 / (1 + exp(-x'beta))` on a base strategy's candidates; fitted
  volatility for stop sizing.
- **Rationale.** No new economics. A way to combine features and output a calibrated win
  probability, which suits a win-rate-margin objective.
- **Data.** Thousands of labelled candidate trades from a base strategy.
- **Assumptions and failure modes.** Stable coefficients; overlapping labels leak; every
  hyperparameter is a trial. Classical return predictors failed out of sample (Welch & Goyal
  2008). Backtest selection overfits unless measured (Bailey et al. 2017).
- **Cost sensitivity.** Inherited. For Bitcoin at 1–60 minute horizons, machine-learning
  forecasts beat a random classifier but a long–short strategy was negative after costs (Jaquart
  et al. 2021).
- **Spot and risk fit.** Yes, as a veto on a base bracket strategy.
- **Timeframes.** Any.
- **Causal execution.** Fit on the training window only; freeze and hash the coefficients into
  the run; embargo between fit and test.
- **Validation.** Nested walk-forward; calibration of the predicted probability; comparison with
  the unfiltered base strategy.
- **Build.** Medium to large: a fitted-artifact pipeline, preregistration fields, trial accounting.

## 5. Literature-supported against original testing

| Claim | Status | Basis and limits |
|---|---|---|
| Volatility clusters and can be forecast | [L] strong | Engle 1982; Bollerslev 1986; Cont 2001; Corsi 2009; Katsiampa 2017. Not directional |
| Crypto returns show time-series momentum at daily and weekly horizons | [L] | Liu & Tsyvinski 2021. Persistence after their sample: [E] |
| Trend rules were profitable on daily crypto data through 2017 | [L], dated | Hudson & Urquhart 2021; Corbet et al. 2019; Grobys et al. 2020; Deprez & Frömmel 2024 report out-of-sample outperformance of buy-and-hold on a risk-adjusted basis, daily and intraday. Early samples, mixed out-of-sample result for Bitcoin |
| Bitcoin has intraday time-series momentum | [L], narrow | Shen et al. 2022: a half-hour pattern, attributed to liquidity provision. Not a breakout rule |
| Short-horizon reversal is a liquidity-provision premium | [L] equities | Lehmann 1990; Jegadeesh 1990; Nagel 2012. For a crypto taker: [E] |
| High volume means continuation | [L] mixed | Against: Campbell et al. 1993. For, in Bitcoin intraday: Shen et al. 2022 |
| Rule profits shrink with snooping control, out of sample and after costs | [L] | Sullivan et al. 1999; Park & Irwin 2007; Bajgrowicz & Scaillet 2012; McLean & Pontiff 2016; Harvey et al. 2016 |
| Compression is followed by a directional move in the trend direction (H1) | [E] | Theory link only (Christoffersen & Diebold 2006) |
| A pullback entry beats a breakout entry on win rate or geometry (H2) | [E] | None found |
| Range reversion is profitable with reward/risk of at least 1.5 (H3) | [E] | OU reference in §3.3; Kaminski & Lo 2014 argue against tight stops there |
| Short-horizon ML adds net value in crypto | [L] against | Jaquart et al. 2021. Large-panel monthly equities: out-of-sample R² below 0.5 % (Gu et al. 2020) |
| Any of H1–H3 is profitable at 15m–1h on spot at 0.26 % | No evidence | Nothing found in this review. All three are original hypotheses |

## 6. Recommended research portfolio for H1–H3

The slate is kept. The recommendation is to make the three hypotheses claims about **three
different market states**, so that they are not three versions of one bet.

| | H1 (existing) | H2 (slate) | H3 (slate) |
|---|---|---|---|
| Claim | A break out of a quiet range, with the trend, continues | A retracement inside an established trend resumes | A stretch inside a trendless range reverts |
| Family | C + B | B | A |
| Trend state | Up (1h) | Up, established on a longer window than H1's range | None (low trend strength) |
| Volatility state | Compressed | Not compressed | Any; the range must be wide enough for the geometry |
| Enters on | Strength: a new 20-bar high | Weakness, then a close back up from a volatility band | Weakness: `z <= -k` below the range mid |
| Stop logic | `k × ATR` | Below the pullback low | Target / `b_min` beyond the entry |
| Target | 2–3 R | 2–3 R, floor-compatible | The range mid, which must be at least `b_min × stop` away |
| Expected payoff | Low win rate, large wins | As H1, possibly better geometry | Higher win rate only if the range is stationary |
| When it should fail | Ranges, false breaks | Trend reversals | Trends, range breaks |
| Basis | A1, A3 [L] elsewhere; rule [E] | Momentum [L] at longer horizons; entry timing [E] | Reversal [L] for providers; taker form [E] |
| Main risk to feasibility | Cost cap against compression (H1-1) | Overlap with H1 | Too few floor-compatible candidates |

Design directions, none of which fixes a number:

1. **H1.** Keep H1-v3 as drafted. Choose the interval from the feasibility count. Add the
   ablation ladder and hour-of-day histogram if the owner agrees (R-6).
2. **H2.** State in the rule that compression is *absent*, so H1 and H2 cannot both fire on the
   same state. Define the band and the "resumes" trigger with finite-window features. Before any
   run, measure overlap with H1 P&L-free: the share of H2 candidates that fall within one H1
   holding period on the same symbol. If most do, H2 is redundant and should be redesigned, not
   run.
3. **H3.** Write "the expected move exceeds costs" as the geometry test
   `(mid - entry) / entry >= b_min(d) × d`, with `d` from the same cost cap as H1. Use a causal
   trend-strength measure for "low-trend". Count candidates P&L-free on 15m and 1h first. If the
   count cannot reach the numbers in §3.5, stop and take R-3 to the owner; do not loosen the
   geometry.
4. **All three.** Small grids (12–24 configurations), a required time exit, the embargo derived
   from it, the same selection rule and objective as H1, long only on spot, SOL handled as D-F1
   decides. Default decision interval 1h for H2 and H3, pending counts (R-7): the closure audit
   measured that 1.5 × ATR(15m) reaches the minimum 1.30 % stop on under 5 % of bars.

What is left out, deliberately: pairs and statistical arbitrage, cross-asset lead–lag, and any
fitted model. Each fails on fit or cost today (§4 D, E).

## 7. Econometric and machine-learning models before the MVP milestone

**Recommendation: no.** Reasons, in order of weight:

1. The binding limits are the cost cap, the floor geometry and the number of trades. A model
   changes none of them.
2. There is no certified base strategy to improve. A probability filter needs thousands of
   labelled candidates; H1 may not produce 300.
3. Every model choice is a trial. The Holm correction already costs 36 % more trades at 199 rows
   than at 24.
4. It needs a fitted-artifact pipeline and nested walk-forward, which is weeks of work competing
   with MVP closure.
5. The literature gives no reason to expect a net gain at these horizons (§5).

Two inexpensive exceptions, both P&L-free and both optional:

- A **stylized-facts report** on development data: variance ratios at 15m, 1h and 4h, the
  autocorrelation of absolute returns, and hour-of-day profiles of range and volume. It would
  ground the H2 and H3 timeframes in measurement. It is exploration, so it needs the owner's
  approval and a line in each preregistration (R-5).
- Revisit a logistic filter only when a hypothesis reaches grade B with at least about 2,000
  development trades.

## 8. Implementation dependencies

| # | Dependency | Needed for | Size | Notes |
|---|---|---|---|---|
| I-1 | Owner decisions D-C, D-E with Amendment 6, D-F1; D-G recommended | H1 freeze | Owner | Unchanged from the decision brief |
| I-2 | Finite-window features: range mid, close z-score against it, volatility band, trend strength | H2, H3 | S | ADR-0006 parity and causality canaries |
| I-3 | Draft strategies `h2_*`, `h3_*` in `DRAFT_REGISTRY` with `diagnose` stages | H2, H3 | M | No trial possible, as for H1 |
| I-4 | Feasibility count for the drafts, plus hour-of-day and time-exit-share outputs | H1–H3 | S | The tool already takes `--draft --strategy` |
| I-5 | P&L-free overlap count between draft strategies | H2 against H1 | S | New, small |
| I-6 | Conditional random controls (ablation ladder) in the runner | H1–H3 | M | Secondary diagnostics only; needs a preregistration field |
| I-7 | Limit-entry path through strategy, live compatibility check and Paper | H3 (optional) | S–M | The simulator supports it; saves 3 bps per entry |
| I-8 | USD-M bars and funding | H3 option (b), H4, shorts | L | Deferred (D-02, D-22) |
| I-9 | Compute | Each hypothesis | — | The audit estimated 28–46 hours for 24 configurations on 5 symbols; runs are resumable |

Nothing in I-2 … I-7 touches the paper runtime, contracts or risk rules.

## 9. Recommendation

1. Proceed with H1 exactly as planned in the decision brief. Add F1 and F2 before the freeze if
   the owner agrees.
2. Keep H2 and H3 as slated and deferred. When their turn comes, build them as the complementary
   states of §6, feasibility and overlap counts first.
3. Treat H3 as a hypothesis that may be infeasible under current policy, and say so in advance.
4. Read every future result against §3.5: with a few hundred trades, "not certified" is not
   evidence of no edge, and it is not evidence of one.
5. Build no fitted model before the MVP research milestone.

## 10. Unresolved owner decisions

Existing and unchanged: D-C, D-D, D-E (with ADR-0012 Amendment 6), D-F1, D-F2, D-G.

| ID | Question | Reviewer's suggestion |
|---|---|---|
| R-1 | Record that under the kept floor, certified win rates are measured against a break-even of at most 0.40 and will usually be below 50 % | Record it with D-E, so the dashboard's numbers are not read as failure |
| R-2 | H2 and H3 long only on spot for the MVP (the slate says long/short) | Yes, until USD-M data exists |
| R-3 | If H3's feasibility count is too low: (a) record H3 as infeasible under current policy; (b) evaluate it on USD-M after the import; (c) replace its mechanism with a new slate entry | (a), then (b). Not (c) without a new written hypothesis |
| R-4 | Error rate across hypotheses: 0.05 per hypothesis (up to 14.3 % that at least one of three is falsely certified at a single gate) or 0.05 shared | Owner's choice; a research-integrity rule. The validation and holdout gates reduce the realized rate |
| R-5 | Authorize the P&L-free stylized-facts report on development data | Yes, logged in each preregistration |
| R-6 | Add the ablation ladder and hour-of-day output to H1-v3 before the freeze | Yes; diagnostics only, selection unchanged |
| R-7 | 1h as the default decision interval for H2 and H3 drafts, pending counts (the slate says 15m) | Yes; D-04 already allows 1h |
| R-8 | `max_cost_r` 0.2 as a common convention for H1–H3, or declared per hypothesis | Common, fixed before any count is seen |

## 11. Handoff for the implementation agent

Do not start any of this until Track A and the H1 path are closed. None of it is on the MVP
closure path, and none of it may delay it.

| Step | Task | Done when | Guard |
|---|---|---|---|
| 1 | Commit this file as `docs/research/MATHEMATICAL_STRATEGY_FOUNDATIONS.md` at a safe checkpoint (docs only) | It is on the integration branch; add the devlog metrics line then | No other file changes; the reviewer had read access only and did not push |
| 2 | H1: implement F1 and F2 if R-6 is approved | Feasibility output has hour-of-day and time-exit share; the runner reports the ladder as secondary | Selection and certification code untouched; mutation tests for the new controls |
| 3 | Features I-2 | Window-parity and look-ahead canaries pass in CI | Finite windows only; no EMA |
| 4 | Draft strategies I-3 | `prereg validate` lists only open owner items; no run or trial is possible | `DRAFT_REGISTRY` only |
| 5 | Counts I-4 and I-5, run by the owner on development data | `feasibility.json` and the overlap report exist for H2 and H3 on 15m and 1h | P&L-free; no registry write; development segment only |
| 6 | Draft preregistrations H2-v1, H3-v1 | Every configuration COMPATIBLE with the floor; embargo covers the time exit; grid of at most 24 | Numbers chosen from counts and the cost formula only, never from an outcome |
| 7 | Independent review, then the owner's freeze | Review findings fixed | No run before the freeze |

Rules that hold throughout: no official trial, no holdout access, no parameter chosen from a
result, no change to `configs/risk.yaml`, no promotion out of the draft registry without the owner.
If a count shows a hypothesis cannot reach the trade numbers in §3.5, report that and stop.

## References

Verification on 2026-11-10: **V** = bibliographic record located online (publisher, DOI, NBER or
RePEc record, or the reference list of a peer-reviewed article); **V+** = the finding cited here
was also checked against the abstract; **U** = standard reference cited from the reviewer's
knowledge, not re-checked online in this session. Findings attributed to **V** entries beyond their
titles rest on the reviewer's knowledge of the paper.

| Reference | Check |
|---|---|
| Avellaneda, M. & Lee, J.-H. (2010). Statistical arbitrage in the US equities market. *Quantitative Finance* 10(7), 761–782. | V |
| Bailey, D. H. & López de Prado, M. (2014). The deflated Sharpe ratio: correcting for selection bias, backtest overfitting, and non-normality. *Journal of Portfolio Management* 40(5), 94–107. | V+ |
| Bailey, D. H., Borwein, J. M., López de Prado, M. & Zhu, Q. J. (2017). The probability of backtest overfitting. *Journal of Computational Finance* 20(4). doi:10.21314/JCF.2016.322 | V+ |
| Bajgrowicz, P. & Scaillet, O. (2012). Technical trading revisited: false discoveries, persistence tests, and transaction costs. *Journal of Financial Economics* 106(3), 473–491. | V |
| Benjamini, Y. & Hochberg, Y. (1995). Controlling the false discovery rate. *Journal of the Royal Statistical Society B* 57, 289–300. | V |
| Bertram, W. K. (2010). Analytic solutions for optimal statistical arbitrage trading. *Physica A* 389(11), 2234–2243. | V |
| Bollerslev, T. (1986). Generalized autoregressive conditional heteroskedasticity. *Journal of Econometrics* 31(3), 307–327. | U |
| Brock, W., Lakonishok, J. & LeBaron, B. (1992). Simple technical trading rules and the stochastic properties of stock returns. *Journal of Finance* 47(5), 1731–1764. | V |
| Campbell, J. Y., Grossman, S. J. & Wang, J. (1993). Trading volume and serial correlation in stock returns. *Quarterly Journal of Economics* 108(4), 905–939. | V+ |
| Christoffersen, P. F. & Diebold, F. X. (2006). Financial asset returns, direction-of-change forecasting, and volatility dynamics. *Management Science* 52(8), 1273–1287. | V+ |
| Clopper, C. J. & Pearson, E. S. (1934). The use of confidence or fiducial limits illustrated in the case of the binomial. *Biometrika* 26(4), 404–413. | U |
| Cont, R. (2001). Empirical properties of asset returns: stylized facts and statistical issues. *Quantitative Finance* 1(2), 223–236. | U |
| Corbet, S., Eraslan, V., Lucey, B. & Sensoy, A. (2019). The effectiveness of technical trading rules in cryptocurrency markets. *Finance Research Letters* 31, 32–37. | V |
| Corsi, F. (2009). A simple approximate long-memory model of realized volatility. *Journal of Financial Econometrics* 7(2), 174–196. | V+ |
| Deprez, N. & Frömmel, M. (2024). Are simple technical trading rules profitable in bitcoin markets? *International Review of Economics & Finance* 93, 858–874. | V+ |
| Do, B. & Faff, R. (2012). Are pairs trading profits robust to trading costs? *Journal of Financial Research* 35(2), 261–287. | V |
| Engle, R. F. (1982). Autoregressive conditional heteroscedasticity with estimates of the variance of United Kingdom inflation. *Econometrica* 50(4), 987–1007. | U |
| Engle, R. F. & Granger, C. W. J. (1987). Co-integration and error correction: representation, estimation, and testing. *Econometrica* 55(2), 251–276. | V |
| Fil, M. & Kristoufek, L. (2020). Pairs trading in cryptocurrency markets. *IEEE Access* 8, 172644–172651. | V+ |
| Gatev, E., Goetzmann, W. N. & Rouwenhorst, K. G. (2006). Pairs trading: performance of a relative-value arbitrage rule. *Review of Financial Studies* 19(3), 797–827. | V |
| Grobys, K., Ahmed, S. & Sapkota, N. (2020). Technical trading rules in the cryptocurrency market. *Finance Research Letters* 32. | V |
| Gu, S., Kelly, B. & Xiu, D. (2020). Empirical asset pricing via machine learning. *Review of Financial Studies* 33(5), 2223–2273. | V+ |
| Harvey, C. R., Liu, Y. & Zhu, H. (2016). … and the cross-section of expected returns. *Review of Financial Studies* 29(1), 5–68. | V+ |
| Holm, S. (1979). A simple sequentially rejective multiple test procedure. *Scandinavian Journal of Statistics* 6(2), 65–70. | U |
| Hudson, R. & Urquhart, A. (2021). Technical trading and cryptocurrencies. *Annals of Operations Research* 297, 191–220. | V+ |
| Jaquart, P., Dann, D. & Weinhardt, C. (2021). Short-term bitcoin market prediction via machine learning. *Journal of Finance and Data Science* 7, 45–66. | V+ |
| Jegadeesh, N. (1990). Evidence of predictable behavior of security returns. *Journal of Finance* 45(3), 881–898. | V |
| Jegadeesh, N. & Titman, S. (1993). Returns to buying winners and selling losers. *Journal of Finance* 48(1), 65–91. | V |
| Kaminski, K. M. & Lo, A. W. (2014). When do stop-loss rules stop losses? *Journal of Financial Markets* 18, 234–254. | V+ |
| Katsiampa, P. (2017). Volatility estimation for Bitcoin: a comparison of GARCH models. *Economics Letters* 158, 3–6. | V+ |
| Krauss, C. (2017). Statistical arbitrage pairs trading strategies: review and outlook. *Journal of Economic Surveys* 31(2), 513–545. | V+ |
| Lehmann, B. N. (1990). Fads, martingales, and market efficiency. *Quarterly Journal of Economics* 105(1), 1–28. | V |
| Liu, Y. & Tsyvinski, A. (2021). Risks and returns of cryptocurrency. *Review of Financial Studies* 34(6), 2689–2727. | V+ |
| Lo, A. W. & MacKinlay, A. C. (1988). Stock market prices do not follow random walks: evidence from a simple specification test. *Review of Financial Studies* 1(1), 41–66. | U |
| McLean, R. D. & Pontiff, J. (2016). Does academic research destroy stock return predictability? *Journal of Finance* 71(1), 5–32. | V+ |
| Moskowitz, T. J., Ooi, Y. H. & Pedersen, L. H. (2012). Time series momentum. *Journal of Financial Economics* 104(2), 228–250. | V |
| Nagel, S. (2012). Evaporating liquidity. *Review of Financial Studies* 25(7), 2005–2039. | V+ |
| Park, C.-H. & Irwin, S. H. (2007). What do we know about the profitability of technical analysis? *Journal of Economic Surveys* 21(4), 786–826. | V |
| Politis, D. N. & Romano, J. P. (1994). The stationary bootstrap. *Journal of the American Statistical Association* 89, 1303–1313. | V |
| Potters, M. & Bouchaud, J.-P. (2005). Trend followers lose more often than they gain. arXiv:physics/0508104; *Wilmott Magazine*. | V+ |
| Shen, D., Urquhart, A. & Wang, P. (2022). Bitcoin intraday time series momentum. *Financial Review* 57(2), 319–344. | V+ |
| Sullivan, R., Timmermann, A. & White, H. (1999). Data-snooping, technical trading rule performance, and the bootstrap. *Journal of Finance* 54(5), 1647–1691. | V |
| Urquhart, A. (2016). The inefficiency of Bitcoin. *Economics Letters* 148, 80–82. | V |
| Welch, I. & Goyal, A. (2008). A comprehensive look at the empirical performance of equity premium prediction. *Review of Financial Studies* 21(4), 1455–1508. | V+ |
| White, H. (2000). A reality check for data snooping. *Econometrica* 68(5), 1097–1126. | V (pages; the checked source prints volume 65, the reviewer's record is 68) |

Already cited in ADR-0012 and not re-examined here: Bahadur & Savage (1956); Maurer & Pontil (2009).

## Appendix A. Limits of this review

- The literature search was targeted, not systematic. A missing paper is not evidence of absence.
- Most cited evidence is from equities, or from crypto samples that end before 2022.
- Illustrative volatilities in A7 are not measurements. Nothing here was estimated from TSC data.
- The reviewing model, like the models that wrote the hypotheses, was trained on text that covers
  much of 2022–2026 (RESEARCH_PROTOCOL §9). The forward trial remains the only clean evidence.

## Appendix B. Reproducing the numbers

Closed-form only; no data. Cost inputs are the `CostModel` defaults in `sim/costs.py`.

```python
import math
from scipy import integrate
from scipy.stats import norm

def net_rr(b, d, e=0.0013, tp=0.0010, sl=0.0013):       # spot, market entry
    return (b*d - e - (1 + b*d)*tp) / (d + e + (1 - d)*sl)

def n_min(p_star, p_alt, alpha, power=0.80):             # ADR-0012 formula
    za, zb = -norm.ppf(alpha), norm.ppf(power)
    num = za*math.sqrt(p_star*(1 - p_star)) + zb*math.sqrt(p_alt*(1 - p_alt))
    return math.ceil((num / (p_alt - p_star))**2)

def p_win_ou(k, b):                                      # entry -k sd, stop -k(1 + 1/b) sd
    S = lambda a: integrate.quad(lambda y: math.exp(y*y/2), 0, a)[0]
    L = k*(1 + 1/b)
    return (S(L) - S(k)) / S(L)

assert round(net_rr(2.0, 0.013), 2) == 1.52              # matches the decision brief
assert n_min(0.40, 0.45, 0.05/199) == 1804
assert round(p_win_ou(2.0, 2.0), 2) == 0.87
```
