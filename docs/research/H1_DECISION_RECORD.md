# H1 decision record (for the owner to fill in)

Status: **decided 2026-11-10 by the owner ([D-25](../DECISIONS_PENDING.md#2026-11-10-h1-research-decisions-d-25)) except R-1, R-4 and the later items.** Originally blank; Each item lists the options and a recommendation; only
the owner's entry in "Decision" counts. Reasoning and numbers are in the
[H1 decision package](H1_DECISION_PACKAGE.md); the open items are also in
[DECISIONS_PENDING](../DECISIONS_PENDING.md).

**Kept fixed, by the owner's direction of 2026-11-10:**
- the live net reward/risk floor of 1.5;
- the same risk policy in backtests and Paper.

H1-v3 stays a draft, unchanged and unfrozen, until the items marked *before the freeze* are filled
in.

To decide, reply with the item ids and the chosen option letters, for example:
`D-C a; R-4 b; D-E a; TP A; D-F1 a; D-G a 12.18.0; R-1 a; R-6 b; F5 a`.

## Before the freeze

### D-C: what a trial is, and the correction
- **a. (recommended)** The budget is the 24 grid configurations. The multiple-testing correction
  uses the registry's conservative count: up to 199 rows for H1, as alpha / n_trials.
- **b.** The budget is a cap on registry rows (`max_registry_trials`).
- Consequence: certification is conservative either way. The choice only sets which number the
  budget caps.
- Decision: **a** (D-25, 2026-11-10)

### R-4: error rate across H1, H2 and H3
- **a.** 0.05 per hypothesis (today's behaviour). Up to a 14.3 % chance that one of three is falsely
  certified at a single gate.
- **b.** 0.05 shared by the three. Each hypothesis then needs about 14 % more trades (2,048 instead of
  1,804 for a 5-point margin at p* 0.40).
- Recommendation: decide before H2 starts. **H1 alone is unaffected either way.**
- Decision: open (before H2; H1 unaffected)

### D-E: the live floor in research, with ADR-0012 Amendment 6
- **a. (recommended)** Keep `min_rr_net` 1.5. The preregistration records
  `risk_compatibility: live-floor-kept-geometries-dropped`. Adopt Amendment 6 (text in the package
  §2): research geometries are only those the live policy can trade.
- **b.** Replace the fixed floor for validated strategies. Not proposed: it would change a risk
  threshold, and the owner's direction is to keep it.
- Decision: **a** (D-25, 2026-11-10). Amendment 6 adopted: **yes** (ADR-0012)

### R-1: what win rates mean under the floor (recorded with D-E)
- **a. (recommended)** Adopt the reworded text: *"Under the kept floor the planned break-even of every
  admitted bracket is at most 0.40. Achieved and certified win rates are measured, not implied by
  the floor; a win rate below 50 % is not by itself a failure."*
- **b.** Other wording.
- Decision: open (not decided in D-25)

### H1 take-profit set (the `tp_r` values of the grid)
- **A.** {2.0, 2.5, 3.0} (H1-v3 as drafted). 2 R nets 1.52, a margin of 0.02 above the floor. If
  take-profits filled as taker orders, 2 R would net 1.4995 and the 8 configurations at 2 R would be
  refused.
- **B.** {2.5, 3.0, 3.5}. All levels stay compatible under the taker assumption (lowest 1.92). The
  targets are further away, so more trades end at the 48-bar time exit.
- No recommendation: the package lays out the trade-off mechanically. The feasibility count does not
  depend on this choice, so it can be made now. It must be made before any H1 outcome exists.
- Decision: **B {2.5, 3.0, 3.5}** (D-25, 2026-11-10)

### Decision interval (15m or 1h)
- Chosen from the H1-1 feasibility counts only ([run sheet](H1_FEASIBILITY_RUN.md)). Fixing a rule
  now is optional, for example *"the interval with the larger out-of-sample candidate count after
  the power plan; ties go to 1h"*.
- Decision: **1h** (D-25, 2026-11-10): 15m recorded as a no-go; 1h with the `rank_max` ladder rule of D-25

### D-F1: SOL and the research universe
- **a. (recommended)** Exclude SOLUSDT from H1. Its source gap of 2023-02-14 16:39 cannot be
  recovered, and it would void every later anchored training window. Universe: BTC, ETH, DOGE, XRP.
- **b.** Keep SOL with a preregistered per-window drop rule.
- Decision: **a, exclude SOL** (D-25, 2026-11-10)

### D-G: reproducibility (lock files and tool versions)
- **a. (recommended)** Before the first H1 walk-forward:
  1. commit `uv.lock`, generated on your PC, since this workspace has no network;
  2. pick one pnpm version, used by both your PC and CI and pinned with `packageManager`;
  3. commit `frontend/pnpm-lock.yaml` generated with it;
  4. switch CI to frozen installs.
  
  Today CI uses pnpm 9 and your PC pnpm 12.18.0. A lock file written by one may not be read the
  same way by the other.
- **b.** Defer. Runs stay resumable and record their Python and numpy versions, but a rerun
  elsewhere may resolve other versions.
- Decision: **a** (D-25, 2026-11-10). `uv.lock` prepared (provenance: the M3-validated lock, SHA-256 `f6b75e9a…`). pnpm version and lock: open

### R-6: the ablation ladder in H1-v3
- The P&L-free parts are already in every feasibility count: rung counts and the hour-of-day split.
- **a.** Also preregister the conditional random control per rung: a secondary diagnostic, never
  used for selection, one extra simulation per rung, no registry rows. It must be built before the
  freeze.
- **b.** Counts only for H1. Revisit the control for H2.
- Decision: **a, rung 1 only** (trend-only control; secondary diagnostic, never a gate) (D-25, 2026-11-10)

### F5: fixed parameters as untested priors
- **a. (recommended)** The preregistration states that the nine fixed parameters (`atr_n` 14,
  `rank_m` 96, `range_n` 20, `vol_n` 20, `trend_n` 50, `watch_atr` 0.5, `time_exit_bars` 48,
  `max_cost_r` 0.2, interval) are untested priors, never varied after results.
- **b.** Leave the text unchanged.
- Decision: **a** (D-25, 2026-11-10), plus: `rank_max` is chosen from P&L-free counts only, by the D-25 rule

## Later (not needed for the first H1 walk-forward)

| Item | Needed before | Recommendation |
|---|---|---|
| D-D, two-stage preregistration and selection record | validation (2025) | adopt |
| D-F2, accept the 2026 holdout months as data | holdout | — |
| R-2, H2 and H3 long only on spot for the MVP | H2/H3 drafts | yes |
| R-3, if H3's count is too low | H3 count | record as infeasible, then USD-M |
| R-5, stylized-facts report on development data | optional | yes, logged per preregistration |
| R-7, 1h default interval for the H2/H3 drafts | H2/H3 drafts | yes, pending counts |
| R-8, `max_cost_r` 0.2 common to H1–H3 | H2/H3 counts | common, fixed before any count |

## After the decisions (no further owner input until the freeze)

1. The H1 preregistration is prepared from the decided items and the counts:
   - take-profit set and interval;
   - fixed anchor, policy fields and dataset pins;
   - the F5 statement.
   
   `prereg validate` must list no open item.
2. Independent review of the prepared file.
3. The owner promotes H1 out of the draft registry, then freezes and commits the preregistration
   from a clean checkout. Only then can the first H1 walk-forward run.
