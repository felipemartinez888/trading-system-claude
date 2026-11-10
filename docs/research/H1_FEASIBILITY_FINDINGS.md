# H1-1 feasibility findings (P&L-free)

Status: **recorded finding for 15m; 1h measured and awaiting the owner's review.** 2026-11-10.
Every number below is a count, or a closed-form value computed from counts. No outcome, trade, P&L or
holdout data was read, and no trial was recorded. H1-v3 is unchanged and unfrozen:
- the 1.5 net reward/risk floor is unchanged;
- the cost model is unchanged;
- the hypothesis definition is unchanged.

Sources:
- the owner's two counts, at code `0955749`, as reported by the owner;
- the independent review `claude/TSC_H1_FEASIBILITY_INDEPENDENT_REVIEW.md` (Claude Fable 5.2,
  read-only clone at `0955749`);
- this session's reconciliation against `task/mvp-integration` after `52b8a75`.

## 1. Evidence and provenance

| | 15m | 1h |
|---|---|---|
| Owner file | `data\feasibility\feas-7b59bd4cee56\feasibility.json` | `data\feasibility\feas-df6adc0d843f\feasibility.json` |
| Written (file time, UTC) | 2026-11-10T17:07:19Z | 2026-11-10T17:38:40Z |
| Copy in the repository (byte-identical) | [evidence/h1_feasibility_15m_feas-7b59bd4cee56.json](evidence/h1_feasibility_15m_feas-7b59bd4cee56.json) | [evidence/h1_feasibility_1h_feas-df6adc0d843f.json](evidence/h1_feasibility_1h_feas-df6adc0d843f.json) |
| SHA-256 of the file | `17433166af2b40ce4f2dab38c90251a3aa77a6104cb946452b11c6ef25b556a9` | `33907a13987d3a603787b4ab7823234b66753491f45ef77197660f78a1694d95` |
| `content_hash` (recomputed, matches) | `7b59bd4cee56…` | `df6adc0d843f…` |
| `config_id` | `44136fa355b3` | `6a122349cc2f` |
| `backtest_config_hash` | `1b67ca1a8d29…`, equal to `configs/backtest.yaml` at HEAD | same |
| Format | feasibility-v2 | feasibility-v2 |
| Range | 2022-03-01 → 2025-01-01, development segment | same |
| Symbols | spot BTC, ETH, DOGE, XRP | same |
| Configuration | the H1 draft defaults: `rank_max` 0.35, `vol_z_min` 1.0, `stop_atr` 1.5, `max_cost_r` 0.2. This is the most permissive entry setting of the H1-v3 grid, and `tp_r` enters no stage, so it is the ceiling for all 24 configurations at its interval | same |

Copied from the owner's PC read-only, and checked here: the content hash, the config hash and
every per-symbol counter. `tests/unit/research/test_h1_feasibility_evidence.py` pins the bytes,
the counts and the numbers below. Notes on the format:
- A v2 count does not record its code commit. From feasibility-v3 on, each file carries `code`
  (commit, dirty).
- The owner's files have CRLF line endings: feasibility-v2 wrote in text mode, which gives CRLF on
  Windows. `.gitattributes` keeps these two files byte for byte.
- From this change on, counts and power plans are written with LF on every platform.

## 2. 15m: recorded finding

**H1-v3 at 15m cannot produce a selection under the rules it would be frozen with
(`n_min` 300 trades in every training window).** Its walk-forward verdict would be
`INSUFFICIENT_OOS_EVIDENCE` whatever the outcomes, at a cost of permanent registry rows. Do not
freeze or run it. This is a P&L-free finding, and it consumes no trial.

| | Value |
|---|---|
| Decision bars | 398,184 (99,546 per symbol) |
| Before the cost filter → candidates | 2,826 → **167** (BTC 8, ETH 13, DOGE 94, XRP 52) |
| By year (2022 from March / 2023 / 2024) | 68 / 14 / 85 |
| Candidates inside the test windows | **42.7**, from month counts |
| Training-window ceilings, folds 0–6 (anchor 2022-02-01, 12 + 3 months) | 70, 76, 78, 78, 82, 108, 109. Every fold is below 300, even counting February 2022 at the busiest month's rate (`power-plan-v3`) |
| Smallest detectable win-rate margin at 42.7 trades (alpha / 199, 80 % power) | 32.2 pts (2 R), 31.7 (2.5 R), 31.0 (3 R), 30.2 (3.5 R) |

**Why the cost filter removes 94.1 %.** This is the rule's own arithmetic; no defect was found.
- `cost_ok` holds when 0.26 % / stop ≤ 0.2, i.e. a stop of at least 1.30 %.
- With `stop_atr` 1.5 that needs ATR14 ≥ 0.867 % of price per 15m bar.
- Only 10.85 % of all 15m bars meet it (43,119 of 397,364).
- The binding rule is H1's own `max_cost_r` 0.2 (R-8), not the 1.5 floor.

## 3. Data integrity: the 81 missing minutes

- Each symbol, at both intervals, misses 81 minutes, all on 2023-03-24, 12:39–13:59 UTC.
- The repository registers this as `binance-spot-2023-03-24` (ADR-0013, absence period
  12:39 → 14:00 = 81 minutes).
- The independent review checked Binance's public klines: the halt is absent at the source in all
  four symbols.
- There are no other gaps (1m bars read = minutes in range − 81) and no conflicts.
- Each symbol has one history reset and the warm-up that follows it.
- No effect on the candidate counts.
- A v2 count reported these as plain `missing_minutes`. From feasibility-v3 the count applies the
  backtest runner's own registry and `split_missing`, and reports them as
  `registered_downtime_minutes` with `unexplained_missing_minutes` 0.
  `test_the_repository_registry_explains_the_2023_03_24_gap` reproduces this case.

## 4. 1h: measured, awaiting the owner's review

The file is complete. It has every structural value the independent review stated in advance:
- `config_id` `6a122349cc2f`;
- 24,886 decision bars per symbol, 24,776 `warmup_ready`;
- 1,500,159 1m bars;
- 81 missing minutes and 1 reset;
- 0 conflicts.

| | Value |
|---|---|
| Before the cost filter → candidates | 1,089 → **351** (BTC 29, ETH 78, DOGE 155, XRP 89); cost pass 32.2 % (15m: 5.9 %) |
| By year (2022 from March / 2023 / 2024) | 141 / 77 / 133 |
| Candidates inside the test windows (embargo 4,320 min) | **146.1** |
| Training-window ceilings, folds 0–6 | 153, 189, 197, 200, 220, 265, 289. Folds 0–5 are below 300 even with February 2022 at the busiest month's rate. Fold 6 is undetermined, because February is not counted |
| Smallest detectable margin at 146.1 trades | 17.6 pts (2 R), 17.2 (2.5 R), 16.7 (3 R), 16.2 (3.5 R) |

Read against the thresholds the independent review fixed before the file existed (its §5):
- **T1** (≥ 300 candidates in the first training window): 153 counted, **not met**.
- **T2** (≥ 300 candidates out of sample): 146.1, **not met**. The test windows start in 2023,
  so February 2022 cannot change this.
- **T3** needs the smallest margin the owner wants detectable. It is not fixed yet.

These are measurements only. No redesign is proposed here. The research direction is the owner's
decision, after the owner has reviewed this evidence.

## 5. Independent review reconciled against HEAD

| Review claim | Check here | Result |
|---|---|---|
| The 15m counts (398,184; 2,826 → 167; per symbol, per year) | recomputed from the file | agrees |
| 42.7 out of sample; the training windows 70 … 109 | `power-plan-v3` on the file | agrees |
| Margins 32.2 / 31.7 / 31.0 / 30.2 pts; 1,222 trades for one configuration (15 rows) | `min_detectable_margin`, `n_min_one_sided` | agrees |
| Leave-one-out counts: cost 2,826, compression 938, breakout 611, trend 255, volume 217 | `diagnostics.stage_filter` | agrees |
| Compressed share by UTC hour, 9.6 % (16h) to 64.6 % (06h) | `diagnostics.by_utc_hour` | agrees |
| Three months hold 53 % (2024-11: 45, 2024-03: 23, 2022-06: 21); 13 of 34 months empty; 70 in the first training year; 54 in Nov–Dec 2024 | month counts | agrees |
| Ladder: trend + breakout 1,247 | `ablation_ladder` | agrees |
| 4.48 % against 14.76 % (cost pass among compressed bars and the others) | not derivable from the file (needs per-bar flags); the review used its own script | not re-derived |
| `select` needs n ≥ `n_min` trades in each training window | `walkforward.select` | agrees |
| 2 R at the minimum stop falls below 1.5 if the fill reference is 1.2 bps above the decision close (2.5 R 27, 3 R 53 bps) | engine `net_rr`, with stop and target fixed while the reference moves | agrees (1.2 / 27.1 / 53.1; 3.5 R 79.0 bps) |
| The count ignored the exchange-interruption registry | `feasibility.py` at `0955749` | agrees; fixed (feasibility-v3) |
| The power plan did not check training windows | `power_plan.py` at `0955749` | agrees; fixed (power-plan-v3) |
| Count range 2022-03-01 against anchor 2022-02-01 | docs and CLI | agrees; see §6 |
| Expected 1h structural values | 1h file | all match |

## 6. The February 2022 coverage mismatch

- **What.** The counts start on 2022-03-01. The planned anchor, the first training month, is
  2022-02-01. Every anchored training window therefore includes February 2022, which no count
  covered: `counted_share` is 0.923 for fold 0.
- **Origin.** `--from 2022-03-01` first appeared in `08f8953` (overnight), in the user guide's
  reference-strategy probe, with no stated reason. It was copied into the H1 brief, the overnight
  report and the run sheet. The H1 anchor comes separately from the draft's warm-up (15m 52.8 h,
  1h 116 h; both fit inside January 2022). `check_request` accepts `--from 2022-02-01` for both
  intervals.
- **Effect on these findings.**
  - The out-of-sample numbers are complete, since the test windows start in 2023.
  - Every 15m fold and 1h folds 0–5 stay below 300 even if February ran at the busiest month's
    rate.
  - Only 1h fold 6 is undetermined.
- **Fix.** The run sheet now counts from 2022-02-01, the anchor. `power-plan-v3` reports
  `counted_share` per fold and decides a partly counted window only when the uncounted part could
  not change the answer.

## 7. Decisions that remain the owner's

1. The research direction for H1 after reviewing §4: do not redesign before this.
2. T3, the smallest win-rate margin the experiment should be able to detect.
3. The open items of the [decision record](H1_DECISION_RECORD.md): D-C, D-E with Amendment 6,
   R-1, the take-profit set, D-F1, D-G, R-6, F5. R-8 (`max_cost_r` as a common convention) is the
   rule that binds the counts.
4. Optional: a count from 2022-02-01, which would settle 1h fold 6. It cannot change T1 or T2.
