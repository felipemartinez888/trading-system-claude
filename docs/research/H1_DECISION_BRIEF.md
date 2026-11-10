# H1: decisions D-C, D-E, D-F1 and the path to the first valid real-data experiment

Status: **for owner review. Nothing here is decided.** Written 2026-11-10 on `task/mvp-integration`.
Decisions are made by the owner and recorded in [DECISIONS_PENDING](../DECISIONS_PENDING.md).
Every number below was computed with the repository's own code (commands in §6); none comes from a
backtest of H1. No H1 run, trial, freeze or holdout access has happened.

## 1. What "scientifically valid first experiment" requires here

The first H1 walk-forward on real development data (2022-01 → 2025-01) is valid only if every choice
that could be tuned on its result is fixed in a **frozen, committed preregistration** first:
the rule and grid, what counts as a trial and how many are allowed, the universe, the folds,
the backtest config (costs and risk policy) and the data pins. The three open decisions below are
the parts of that the code cannot choose. D-D (holdout procedure) and D-F2 (2026 holdout months)
are needed only later, for validation and the holdout. D-G (commit the lock files) is strongly
recommended before the run so the result is reproducible.

## 2. D-C: what counts as a trial

**Question.** A walk-forward evaluates each configuration on each training window and only the
selected one on each test window. Is the trial budget the number of *configurations* (24), or the
number of *registry rows* (one per configuration and window, about 200 for H1)?

**What the code does today.**
- The trial registry is permanent and counts registry rows, per strategy family.
- The multiple-testing correction (Holm–Bonferroni over the recorded trial count, ADR-0012) already
  uses the **conservative registry count**. The walk-forward fixes it at plan time as the family's
  count after every row the plan can add (`rescore_n_trials`), so a resumed run uses the same number.
- `trial_budget.counting` is PENDING; `freeze` refuses while it is.
- `counting: configurations` caps `len(search_space)` at `max_configurations`.
- `counting: registry_rows` also caps new registry rows at `max_registry_trials`.

**For H1 as drafted** (anchor 2022-02-01, 12-month initial train, 3-month tests): 7 folds. The run
can add at most 24 × 7 training rows + 7 test rows + 24 final-selection rows = **199 rows**.

**Alternatives and implications.**

| Option | Budget check | Correction | Effect |
|---|---|---|---|
| A. `configurations` (proposed) | 24 configurations | registry rows (199), as today | Budget stays readable (what H1-v1 meant by 24); the statistics stay conservative |
| B. `registry_rows` | 199 rows (`max_registry_trials`) | registry rows | Same statistics; a hard cap on permanent rows, so a later re-run or extension needs a new preregistration |
| C. Correct for configurations only (24) | 24 | 24 | Easier certification, but it ignores the repeated selection across 7 windows: **not recommended** |

How much the correction costs. These are the smallest observed win rates that certify a positive
margin (one-sided Clopper–Pearson, α = 0.05 / trials):

| Geometry | Trades | 24 trials | 199 trials |
|---|---|---|---|
| 2 R (p* ≈ 0.40) | 300 | 0.487 | 0.503 |
| 2 R | 1,000 | 0.446 | 0.455 |
| 3 R (p* ≈ 0.30) | 300 | 0.380 | 0.397 |
| 3 R | 1,000 | 0.343 | 0.352 |

**Recommendation: A.** The budget is 24 configurations, and the correction stays on the registry
count, which is what the code already does. Only option C would weaken the statistics, and it
should not be chosen.

## 3. D-E: the live risk floor against research geometries

**Question.** The paper and live baseline book refuses any entry whose cost-adjusted net
reward/risk is below `min_rr_net` = 1.5 (`configs/risk.yaml`). ADR-0012 asks research search spaces
to include high-win-rate take-profits of 0.5–1.5 R, and the floor rejects every one of those on
every trade.

**Owner direction (2026-11-10).** Keep the 1.5 floor unchanged, and apply identical risk policies
in backtesting and Paper. This brief implements that direction for review. The formal D-E record,
the preregistration value and the ADR amendment remain the owner's acts.

**What the code does now (new on this branch, queue P1-6).**
- `configs/backtest.yaml` has `risk_policy: live`. Every backtest book (base, cost stress, random
  control, max-loss estimate) runs the paper engine's own trade and portfolio rules from
  `configs/risk.yaml`, with the same functions, thresholds and base cost model:
  - net reward/risk ≥ 1.5, take-profit required;
  - risk per trade, max positions, one position per symbol, correlation clusters;
  - gross exposure, daily loss, drawdown, signal validity;
  - and the same drawdown kill switch.
- `data_freshness` also applies. Its three bar checks are computed from the 1m bars exactly as
  the paper tracker computes them: no closed bar, a last bar older than 120 s, any missing minute
  in the last 240 minutes. Stream health has no historical input and passes.
- Three rules judge only the live environment: market view, clock skew and spread. They have no
  historical input; every manifest records the substitution (`research/risk_policy.py`). The one
  that matters is spread: backtests use modelled slippage only.
- A book halted by the kill switch stays halted for the rest of that run, as a paper book stays
  halted until a new session. Every halted book (base, cost stress, random control) is reported.
  The random-control book is filtered by the same portfolio rules, so control and strategy are
  compared under the same policy.
- Sizing: the backtest's `risk_per_trade` must not exceed the policy's `max_risk_per_trade`
  (refused at load). Paper sizes at exactly that maximum; both are 0.005 today.
- The risk config enters the backtest config hash by content, and a frozen preregistration pins
  that hash, so a later change to the floor cannot be mixed into a frozen experiment.
- Tests (`tests/unit/backtest/test_backtest_risk_policy.py`, 20):
  - on 400 random checks the backtest verdict equals the paper engine's;
  - the view's bar fields equal the paper tracker's on the same bars, gaps included;
  - stale bars and lookback gaps block, as in Paper;
  - views never use a bar closing after the cutoff, and the submit check sees the decision bar;
  - every book gets the kill switch and the base cost model;
  - configuration fails closed;
  - end to end, a 1 R strategy books 0 trades and a 2 R strategy keeps only the trades the floor
    accepts.
  The independent review's three surviving mutants are now killed.
- With `risk_compatibility: live-floor-kept-geometries-dropped`:
  - `prereg validate` refuses any configuration the floor would reject;
  - `freeze` refuses unless the pinned backtest config applies `risk_policy: live`, and checks the
    configurations against that config's own floor.

**Which take-profits can trade.** H1's cost filter (`max_cost_r` 0.2, spot round trip 0.26 %)
admits only stops ≥ 1.30 % of price. At that smallest stop the paper engine computes:

| tp_r | Net RR at the smallest stop | Status |
|---|---|---|
| 1.5 | 1.10 | INCOMPATIBLE |
| 1.75 | 1.31 | CONDITIONAL (only stops ≥ 2.48 %) |
| **2.0** | **1.52** | COMPATIBLE |
| **2.5** | **1.94** | COMPATIBLE |
| **3.0** | **2.35** | COMPATIBLE |

Net RR rises with the stop distance, so COMPATIBLE at the smallest stop means every confirmable
trade passes, at modelled slippage and at zero decision latency. The fill-stage check re-prices
from the close just before the executing bar. With `decision_latency_ms` above 0 (H1-11 plans to
set it), that close can differ from the decision close, and a trade near the 2 R margin can then
be refused at fill. The live engine also uses half the live spread when that is
larger; wide spreads can still reject a trade, and the ×1.5 and ×2 cost-stress runs carry that risk.
At 2 R the margin is thin: 1.52 against 1.5. It holds only because take-profits are modelled as
maker limit fills without slippage, which is how Paper and the backtests model them today
(`CostModel.tp_as_taker = False`). If take-profits were filled as taker orders with slippage, 2 R
would net 1.4995 and be CONDITIONAL; 2.5 R (1.92) and 3 R (2.33) keep a margin. Scaling every
cost (×1.5, ×3) changes nothing, because H1's cost filter scales the smallest stop with the costs.
**Option for the owner:** use {2.5, 3.0, 3.5} instead of {2.0, 2.5, 3.0}, to keep a margin even
if take-profits are filled as taker orders.

**Proposed search space: [H1-v3 draft](H1-v3.preregistration.DRAFT.yaml).** It is H1-v2 with only
the take-profits changed to {2.0, 2.5, 3.0}. The grid stays 2 × 2 × 2 × 3 = 24, with rank_max
{0.25, 0.35}, vol_z_min {1, 2} and stop_atr {1.0, 1.5}. With D-E set to keep the floor it validates
with 0 problems; H1-v2 gets 24 problems. The values come from the cost formula only, never from an
outcome.

**Implications to weigh.**
1. These are trend-style payoffs, not high-win-rate ones. The break-even win rate p* ≤ 0.40 at 2 R,
   0.34 at 2.5 R and 0.30 at 3 R (realized p* applies, because the time exit makes payoffs vary).
2. **ADR-0012 must be amended.** Its "Search space" section says the 0.5–1.5 R geometries are
   explicitly included. Proposed wording, *Amendment 6 (PROPOSED)*: *"A preregistered search space
   contains only exit geometries the live risk policy can trade: every configuration must be
   COMPATIBLE with `min_rr_net` of the risk config the backtests apply (`research/live_compat.py`).
   While `min_rr_net` = 1.5, take-profits at or below 1.5 R are excluded. High-win-rate profiles
   compete only where they are tradable."*
3. More trades will end at the 48-bar (12 h) time exit with 2–3 R targets. That is to be measured,
   not tuned now.
4. Backtests of the reference strategy at its 0.5–1.5 R defaults now book no trades, by design. The
   mechanics dry-run grid and the TEMPLATE now use 2 R and 3 R.

**Alternative (not recommended under your direction):** `live-floor-replaced-for-validated`, a
different floor for strategies that pass validation. It keeps ADR-0012 unchanged, but research and
Paper would then judge trades differently until validation, which is what this direction rules out.

**Recommendation.**
- Record D-E as "keep the floor".
- Approve ADR-0012 Amendment 6.
- Review H1-v3 as the H1 search space, in place of v2.
- At the freeze, set `risk_compatibility: live-floor-kept-geometries-dropped`.

## 4. D-F1: research universe and the SOL source gap

**Question.** SOLUSDT has a genuine source gap at 2023-02-14 16:39 (C-2; Binance has no bar, and it
cannot be repaired). Backtests have zero gap tolerance. Every range that contains the minute is
grade X and cannot be used for selection.

**What the code does today.** Nothing symbol-specific. With anchored training windows, every
training window from the fold that includes 2023-02-14 onward contains the gap. With SOL in the
universe:
- every one of those folds loses its selection;
- under the default coverage floor (1.0) the verdict is `INSUFFICIENT_OOS_EVIDENCE` before any edge
  is looked at.

The H1-v2 and H1-v3 drafts already leave SOL out, pending this decision.

**Alternatives and implications.**

| Option | Effect |
|---|---|
| A. Exclude SOL from H1 (recommended) | 4 symbols (BTC, ETH, DOGE, XRP). Decided on data quality, known before any result, so it is not selection on outcome. Fewer trades: the feasibility count shows whether 4 symbols can reach 300 |
| B. Preregister a per-symbol, per-window rule: a symbol whose window holds a source gap is dropped from that window only | Keeps SOL elsewhere. Needs new code and its own review; the universe then differs between folds, which complicates the stitched out-of-sample result |
| C. Tolerate one missing minute | Weakens a data-integrity rule that M2 was accepted with: **not recommended** |

**Recommendation: A.** Revisit B only if the feasibility count shows the 4-symbol universe cannot
reach the 300-trade floor.

## 5. Sequence to the first valid H1 walk-forward

Owner steps are marked **(owner)**; everything else is prepared on the branch.

1. **(owner)** Dashboard smoke check and H1-1 feasibility count, 15m against 1h (§6). The count is
   P&L-free, records no trial and is independent of tp_r: no entry stage uses the take-profit.
2. **(owner)** Decide D-C, D-E with ADR-0012 Amendment 6, D-F1 and D-G.
3. Choose the interval from the feasibility counts only. 1h needs the embargo raised to cover
   (48 + 1) × 60 min, for example 4,320 min; validation enforces this.
4. Prepare `preregistrations/H1.yaml` from H1-v3. Set:
   - the anchor: a fixed month start, `auto` is refused at freeze;
   - the decided policy values;
   - pinned `data.dataset_ids`. The feasibility run's `feasibility.json` records the exact dataset
     ids it read (`dataset_ids`); for the 4-symbol universe these are the pins. The D-17 composite
     id applies to XRP;
   - `decision_latency_ms` and instrument filters, per H1-11.
5. **(owner)** Promote `h1_compression_breakout` from DRAFT to the runnable registry and freeze. The
   freeze pins the code, the implementation hash, the anchor and the backtest config hash, including
   the risk policy. Commit the file.
6. Run the walk-forward with `--prereg H1 --datasets ...`. Conformance refuses any deviation before
   anything runs, and the run is resumable.
7. The development-only verdict is reported as is. Validation (2025) and the holdout need D-D and
   D-F2 and a selection record, and come later.

## 6. Commands (NORMAL PowerShell, no Administrator)

None of these places an order, uses an API key or calls a paid service.

**Smoke check of the branch dashboard.** About 5 minutes. It uses a new empty data folder and port
8799, so your real app and data are untouched. It checks the research, Paper and agent status
endpoints, then asks you to look at the dashboard:
```powershell
cd C:\Users\felip\Projects\trading-system-claude
git fetch origin task/mvp-integration
if (Test-Path ..\tsc-mvp) { git -C ..\tsc-mvp checkout --detach origin/task/mvp-integration } else { git worktree add --detach ..\tsc-mvp origin/task/mvp-integration }
cd ..\tsc-mvp
powershell -ExecutionPolicy Bypass -File scripts\owner_checks\dashboard_smoke.ps1
```
Result: `C:\Users\felip\tsc_runs\smoke-<stamp>\summary.json`.

**H1-1 feasibility count, 15m against 1h.** Development data only, P&L-free, records no trial. It
writes only `<data>\feasibility\` and reads your lake:
```powershell
cd C:\Users\felip\Projects\tsc-mvp
$env:TSC_DATA_DIR = 'C:\Users\felip\Projects\trading-system-claude\data'
uv run tsclaude backtest feasibility --draft --strategy h1_compression_breakout `
  --grid docs\research\grids\feasibility_h1_draft_probe.json `
  --symbols spot:BTCUSDT,spot:ETHUSDT,spot:DOGEUSDT,spot:XRPUSDT --from 2022-03-01 --to 2025-01-01
Remove-Item Env:\TSC_DATA_DIR
```
The JSON summary is printed; the file is `<data>\feasibility\feas-<hash>\feasibility.json`. Send me
that file. The decision on the interval is made from its per-stage and per-year counts only.

**Reproducing the numbers in this brief** (from the checkout):
```powershell
uv run tsclaude prereg validate docs\research\H1-v3.preregistration.DRAFT.yaml   # prereg_id/file-name mismatch is expected for a DRAFT copy
uv run pytest tests\unit\backtest\test_backtest_risk_policy.py tests\unit\research\test_preregistration.py -q
```
