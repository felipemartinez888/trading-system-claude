# H1 decision package: D-series and R-series together

Status: **for owner decision. Nothing here is decided.** 2026-11-10, `task/mvp-integration`.
This package consolidates the open D-decisions ([DECISIONS_PENDING](../DECISIONS_PENDING.md), the
[H1 decision brief](H1_DECISION_BRIEF.md)) with the mathematical audit's R-questions
([MATHEMATICAL_STRATEGY_FOUNDATIONS](MATHEMATICAL_STRATEGY_FOUNDATIONS.md), §10 and Part 1). Every
number is mechanical: computed from the cost model, the risk engine and the power formula, never
from an outcome. `tests/unit/research/test_math_foundations_numbers.py` and
`test_power_plan.py` pin them.

## 0. What is authorized and what is not

| Authorized by an explicit owner instruction | Source |
|---|---|
| Keep the 1.5 net reward/risk floor and apply the same risk policy in backtests and Paper (a *direction* under D-E; the formal D-E record is still open) | Session instruction 2026-11-10; implemented, CI 7/7 (`0429135`, `1cc2693`) |
| Design a floor-compatible H1 search space for review (H1-v3 draft) | Same instruction |
| P&L-free H1 diagnostics: hour of day, stage filtering, power reporting, time-exit share, an ablation design that does not change the objective | Session instruction 2026-11-10 (this package's directive) |
| M5 runtime work on its own branch, mode off, mock clients only | Session instructions 2026-11-10 |

**Not authorized; awaiting a decision:**
- D-A, D-B, D-C, D-D, D-E (the formal record), D-F1, D-F2, D-G, D-H;
- ADR-0012 Amendment 6;
- the H1 take-profit set;
- R-1 … R-8;
- the H1 freeze and the promotion out of the draft registry;
- any holdout access.

Earlier recommendations, including {2.5, 3, 3.5}, are options only, not decisions.

## 1. Research accounting: D-C with R-4

| | D-C: what a trial is (H1) | R-4: error rate across H1–H3 |
|---|---|---|
| Question | Budget = 24 configurations, or a cap on registry rows (up to 199 for H1) | 0.05 per hypothesis, or 0.05 shared by the three |
| Today | Certification already corrects for the registry count (Bonferroni `0.05 / n_trials`, fixed at plan time) | Per strategy family, so per hypothesis |
| Consequence | Budget choice only; the correction stays conservative either way | Per hypothesis: up to 14.3 % chance that at least one of three is falsely certified at a single gate. Shared: about 14 % more trades per hypothesis (2,048 against 1,804 for a 5-point margin at p* 0.40) |
| Recommendation | Budget = configurations; keep the registry correction | Decide before H2 starts. H1 alone is unaffected, because its family count is its own. The validation and holdout gates lower the realized rate either way |

## 2. Risk and H1 geometry: D-E, Amendment 6 and the take-profit set

**D-E (formal record).** Keep `min_rr_net` 1.5 unchanged. Backtests apply the paper policy (done).
At the freeze, the preregistration's `risk_compatibility` value is
`live-floor-kept-geometries-dropped`; validate, freeze and run then refuse any configuration the
floor rejects (done).

**ADR-0012 Amendment 6 (proposed text, unchanged from the brief).** *"A preregistered search space
contains only exit geometries the live risk policy can trade: every configuration must be
COMPATIBLE with `min_rr_net` of the risk config the backtests apply. While `min_rr_net` = 1.5,
take-profits at or below 1.5 R are excluded. High-win-rate profiles compete only where they are
tradable."*

**R-1, reworded** (reconciliation 1.3 b) for the D-E record: *"Under the kept floor the planned
break-even of every admitted bracket is at most 0.40. Achieved and certified win rates are
measured, not implied by the floor; a win rate below 50 % is not by itself a failure."*

**Take-profit set: a neutral comparison.** The other three grid dimensions are unchanged (24
configurations either way). The feasibility count does not depend on `tp_r`, because no entry
stage uses it. So the count gives no information for this choice, and no outcome is involved.

| | Set A {2.0, 2.5, 3.0} (H1-v3 as drafted) | Set B {2.5, 3.0, 3.5} |
|---|---|---|
| Net RR at the smallest stop (1.30 %), default model | 1.52 / 1.94 / 2.35, all COMPATIBLE | 1.94 / 2.35 / 2.77, all COMPATIBLE |
| Take-profits filled as taker (with slippage) | 2 R → 1.4995, **CONDITIONAL** (the 8 configurations at 2 R would be refused) | all COMPATIBLE (lowest 1.92) |
| All costs × 2 (cost stress) | unchanged (the cost filter scales the minimum stop) | unchanged |
| Margin above the floor at the thinnest level | 0.02 | 0.44 |
| Planned break-even p* (cost at the 0.2 R cap) | 0.40 / 0.343 / 0.30 | 0.343 / 0.30 / 0.267 |
| Martingale P(target first) | 0.33 / 0.29 / 0.25 | 0.29 / 0.25 / 0.22 |
| Trades to certify a 5-point margin (199 rows, 80 % power) | 1,804 / 1,702 / 1,594 | 1,702 / 1,594 / 1,491 |
| Edge equivalent to a 5-point margin | 0.15 / 0.175 / 0.20 R | 0.175 / 0.20 / 0.225 R |
| Illustrative mean exit time, σ = 0.30 % per bar (audit A7) | 38 / 47 / 56 bars | 47 / 56 / 66 bars |
| Consequence of the 48-bar time exit | The 2 R level is mostly resolved inside 48 bars; at 3 R the mean exit time already exceeds 48 | Every level beyond 2.5 R sits past the 48-bar exit in this illustration, so more time exits and realized payoffs further from the bracket |

Read the table as trade-offs, not as a ranking:
- **Set A** keeps the most "high win rate" level the floor allows (2 R), but it is fragile to the
  take-profit fill assumption.
- **Set B** is robust to that assumption, but leans further toward time exits and lower win rates.

The choice must be made before any H1 outcome exists. Changing `time_exit_bars` to suit a set
would be a new choice of its own, and is not proposed.

## 3. Data integrity: D-F1 with D-G

| | D-F1: SOL and the research universe | D-G: lock files |
|---|---|---|
| Question | Exclude SOLUSDT (genuine source gap 2023-02-14 16:39), or preregister a per-window drop rule | Commit `uv.lock` and the pnpm lock file |
| Today | The drafts exclude SOL; with SOL every later anchored training window is grade X | Not committed; the walk-forward plan id records the Python and numpy versions |
| Consequence | 4 symbols. Fewer candidates; the feasibility count shows whether 300 trades are within reach | Without them a re-run elsewhere may resolve other versions, and reproducing hashes is not guaranteed |
| Recommendation | Exclude (decided on data quality before any result) | Commit after review, before the first H1 walk-forward. Generating `uv.lock` needs the network, so it runs on your PC |

## 4. Mathematical research diagnostics: R-1 … R-8

| ID | Question | Dependency | Recommendation | Consequence | Status |
|---|---|---|---|---|---|
| R-1 | Wording of the win-rate expectation | With D-E | The reworded text in §2 | Dashboard numbers are not misread | Open |
| R-2 | H2 and H3 long only on spot for the MVP | D-02, D-22 | Yes, until USD-M data exists | The "long/short" slate is reachable later | Open |
| R-3 | What to do if H3's feasibility is too low | H3 drafts, counts | (a) record as infeasible under current policy, then (b) USD-M | No mechanism is swapped without a new written hypothesis | Open, later |
| R-4 | Error rate across hypotheses | D-C | §1 | Before H2 | Open |
| R-5 | A P&L-free stylized-facts report on development data | None | Yes, logged in each preregistration | Exploration on the development segment only | Open; not built |
| R-6 | The ablation ladder and hour-of-day output in H1-v3 | Freeze | **P&L-free parts done as diagnostics** (feasibility-v2: hour of day, stage filter, ladder candidate counts). A conditional random control per rung in the runner is a design proposal (§5) | Diagnostics never enter selection | Diagnostics done; control open |
| R-7 | 1h default interval for the H2/H3 drafts | Counts | Yes, pending counts | Drafts only | Open, later |
| R-8 | `max_cost_r` 0.2 common to H1–H3 | Before any H2/H3 count | Common, fixed before any count | H1 uses 0.2 already | Open |

## 5. Ablation ladder: design (R-6), not implemented as a control

**Purpose (audit F1).** H1's random control replaces every entry condition at once. A long-only rule
with a 1h trend filter can beat random entries on trend exposure alone. The ladder asks which
condition carries information.

**Rungs** (`research/feasibility.py ABLATION_LADDERS`). Each rung keeps the structural stages
(warm-up, defined features, a valid stop, the cost filter):

| Rung | Conditions |
|---|---|
| 1 | trend |
| 2 | + breakout |
| 3 | + compression |
| 4 | + volume (= H1) |

**Done now (P&L-free).** Candidate counts per rung, overall and by UTC hour, in every feasibility
count. These show whether a conditional control would have enough candidates.

**Proposed for approval.** A secondary control per rung:
- **Design.** For rung `k`, draw random entries from the rung's candidate pool with H1's exits and
  the same per-(symbol, hour) schedule as today's control.
- **Comparison.** H1 against each rung's control, reported beside the certification. It is never a
  selection input and never a gate.
- **Preregistration.** It needs a preregistration field, so the comparison is fixed before results.
- **Cost.** Compute only: one extra simulation per rung, and no registry rows if the controls are
  not registered as trials (the existing control is not).
- **Not proposed.** Changing the objective, the rule or the grid on the basis of a rung.

## 6. Recommended order

1. **(owner, now)** The smoke check and the H1-1 feasibility count (§6 of the brief). The count now
   also returns hour-of-day, stage-filter and ladder diagnostics.
2. **(owner)** `tsclaude backtest power-plan --feasibility <file> --anchor 2022-02-01` on that count.
   It prints the out-of-sample trade upper bound and the detectable margin for both take-profit
   sets (`--tp-r 2,2.5,3` and `--tp-r 2.5,3,3.5`).
3. **(owner)** Decide D-C, D-E with Amendment 6 and R-1, the take-profit set, D-F1, D-G. R-4 can
   wait until H2.
4. Prepare the H1 preregistration from the chosen set. Then the owner promotes, freezes and
   commits it (brief §5).
