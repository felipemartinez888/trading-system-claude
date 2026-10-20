# Quantitative Research Protocol

Objective: find out whether any strategy shows a **credible, repeatable edge net of realistic costs**
that survives out-of-sample and forward testing. "No edge found" is a valid, reportable outcome.

## 1. Pre-registration

Before any validation run, a hypothesis is frozen in `preregistrations/<id>.yaml`:

- economic rationale and the market behaviour it exploits;
- exact entry, invalidation, exit and Early Watch rules;
- parameter search space and **trial budget** (max configurations to be evaluated);
- universe rule, timeframe, side(s), cost model version, risk settings;
- data splits; primary metric; secondary metrics;
- **rejection criteria** (e.g. reject if net expectancy CI includes 0 on validation, or if the
  deflated Sharpe probability < 0.95, or if fewer than 100 validation trades).

The file is hashed and committed **before** results exist; the hash goes into every run manifest.
Edits after freeze create a new version with a new ID; the old one and its results stay visible.

## 2. Data splits

| Segment | Period (UTC) | Use |
|---|---|---|
| Development | 2022-01-01 → 2024-12-31 | Exploration, anchored walk-forward parameter selection |
| Validation | 2025-01-01 → 2025-12-31 | One evaluation of the walk-forward-selected configuration |
| **Sealed holdout** | 2026-01-01 → 2026-09-30 | One-shot confirmation for strategies that pass validation |
| Forward trial | from MVP go-live onward | Prospective paper evidence; the only evidence for agents |

Exact dates are proposals (decision D-05) and become part of the frozen protocol.

**Holdout guard (enforced in code).** The data access layer refuses to serve holdout bars to
`research` jobs unless a `holdout_unlock` event exists for that exact `prereg_id` + strategy hash.
An unlock can happen once; the result is recorded whatever it is. Features with lookback windows
receive warm-up bars from before the holdout start, never from inside it.

## 3. Walk-forward

Anchored walk-forward on the development segment: train windows grow, test windows of fixed length
(e.g. 3 months) roll forward. Parameters are selected on train only, by the pre-declared objective
of §5 (certified win-rate margin with a minimum trade count, never total return). Purging/embargo of one
maximum holding period between train and test prevents overlapping labels from leaking.

## 4. Statistics

- **Uncertainty:** stationary block bootstrap (block length tied to holding period) for expectancy,
  Sharpe and drawdown; exact Clopper–Pearson interval for win rate. A win rate is never shown without n and CI.
- **Multiple testing:** every configuration evaluated counts as a trial; the deflated Sharpe ratio
  (Bailey & López de Prado) uses the trial count. The trial count is shown next to every result.
- **Null model:** each strategy is compared against a *random-entry control* with the same exits,
  frequency and time-of-day distribution. An entry rule that cannot beat its random twin has no
  demonstrated entry edge.
- **Robustness:** parameter-neighbourhood stability, cost stress (×1.5, ×2), per-symbol and per-regime
  breakdowns (causal regime labels), sub-period stability, ambiguity sensitivity.

## 5. Selection objective (owner priority)

The owner's priority is signal effectiveness and the **highest win rate backed by reliable
backtesting**. Formalized in [ADR-0012](adr/0012-win-rate-margin-objective.md):

- Every strategy reports win rate `p` with n and an exact CI, its **break-even win rate**
  `p* = L/(W+L)` (known in advance for bracket exits: `p* = (1+c)/(1+b)`), and the
  **certified margin** `Δ_LCB = p_LCB − p*`, where `p_LCB` is the multiple-testing-adjusted lower
  confidence bound of the win rate.
- Ranking: `Δ_LCB` first, then `p_LCB` — among equally certain edges the higher win rate wins.
- Admission: `Δ_LCB > 0` on validation and holdout, expectancy lower bound > 0, `n ≥ n_min` (power
  80 %), drawdown and cost-stress criteria.
- High-win-rate exit geometries (TP 0.5R–1.5R, time exits) are always part of the search space.

A raw win rate is never an objective: `E = (W+L)·(p − p*)`, so win rate only creates value through its
margin over break-even.

## 6. Evidence grades

Every run gets a machine-assigned grade with listed reasons:

| Grade | Meaning |
|---|---|
| `A – credible` | Passed pre-registered criteria on validation **and** holdout; robust to cost stress. |
| `B – promising` | Passed validation; holdout pending or robustness caveats. |
| `C – insufficient` | Too few trades, wide CIs, or high ambiguous-bar share. |
| `D – rejected` | Failed rejection criteria. |
| `X – invalid` | Integrity failure: data gaps in range, look-ahead canary failure, missing manifest fields. |

## 7. Promotion to live scanning

A strategy version runs in the live scanner only if frozen, its preregistration is committed, and it
has grade ≥ C (so the forward trial can collect evidence for promising-but-unproven ideas). Grades are
shown on every signal so a `C`-grade signal is never mistaken for a validated one.

## 8. Initial hypothesis slate (our own; none assumed profitable)

| ID | Hypothesis | TF | Side |
|---|---|---|---|
| H1 | **Compression → expansion breakout:** after volatility compression (low ATR percentile, narrow range), a range break with abnormal volume continues. | 15m (1h context) | long/short |
| H2 | **Trend pullback continuation:** in a causal 1h uptrend/downtrend, a pullback to a volatility band that resumes with momentum continues. | 1h context, 15m entry | long/short |
| H3 | **Range mean reversion:** in a low-trend regime, stretched moves (z-score of close vs. range mid) revert — only when expected move exceeds costs. | 15m | long/short |
| H4 | **Funding-extreme filter:** extreme perp funding/crowding as a contrarian filter on H1/H2 entries. | uses futures data | filter |
| H0 | Random-entry controls for each of the above. | — | — |

Short-side evaluation depends on decision D-02 (futures paper positions).

## 9. Known biases we cannot remove

- **Researcher knowledge:** the Claude models doing this research were trained on data that includes
  much of the 2022–2026 market history. Pre-registration, the holdout guard and the forward trial are the
  mitigations; the forward trial is the only fully clean evidence.
- **Survivorship** in the universe (see DATA_ARCHITECTURE §6).
- **Spread history** is modeled, not observed, before live capture began.
