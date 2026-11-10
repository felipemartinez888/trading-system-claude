# 2026-11-10: M4 H1 preparation (draft strategy, draft preregistration v2)

Source: [MVP_CLOSURE_HANDOFF](../MVP_CLOSURE_HANDOFF.md) §5 (H1-1 … H1-12), §7 B4/B5, §8. Branch
`task/m4-h1-prep`. **Not an H1 research run.** No backtest, walk-forward or validation of H1 was
run, no trial was recorded, nothing was frozen and nothing was written to `preregistrations/`.

## What changed

| Item | Change | Evidence |
|---|---|---|
| Strategy | `src/tsclaude/strategies/h1_compression_breakout.py`, id `h1_compression_breakout`, version `0.1.0-draft`, long only, spot. H1-v1 rules with H1-6 (finite 1h trend: last closed 1h close > mean of the last `trend_n` closed 1h closes, built from complete hour-aligned groups of the window; forming hour never used) and H1-7 (required `time_exit_bars`). `interval` 15m (default) or 1h for H1-1. Warm-up 206 bars on 15m (51.5 h), 111 on 1h | `tests/unit/strategies/test_h1_compression_breakout.py` (45 tests: forming-hour canary, independent hourly reference, future-mutation canary, window parity, diagnose ⇔ on_bar over 10 seeds × 6 configurations, warm-up at every hour alignment, validation, geometry, live compatibility) |
| Draft registry | `strategies.DRAFT_REGISTRY` + `build_draft_strategy`. `REGISTRY` (used by `backtest run`, `walkforward`, `prereg`) does not contain H1 | `test_the_runnable_registry_refuses_the_draft_and_the_draft_registry_builds_it` |
| Feasibility CLI | `tsclaude backtest feasibility --draft` builds from `DRAFT_REGISTRY`; same development-only refusals; output carries `draft: true` | `tests/unit/backtest/test_h1_draft_feasibility.py` (10 tests: counts equal the `StrategySource` CONFIRMED proposals on 15m and 1h, gap reset restarts the warm-up in both paths, lake causality canary, CLI: ok with `--draft`, refused without it, holdout exit 3, validation exit 2, trial registry empty) |
| Preregistration | `validate_document` accepts a `DRAFT_REGISTRY` id and lists `"strategy is a DRAFT (not runnable until an owner-approved promotion to REGISTRY)"` as an open item; `freeze` refuses while it is open | `tests/unit/research/test_preregistration.py` (2 new tests; the 30 existing ones pass) |
| H1-v2 draft | `docs/research/H1-v2.preregistration.DRAFT.yaml` (tsc-prereg-v1): the H1-v1 grid (24 configurations, untuned), `n_min: 300`, `time_exit_required: true`, `time_exit_bars: 48`, embargo 1440 ≥ (48 + 1) × 15 = 735 min, universe without SOLUSDT (D-F), D-C/D-D/D-E PENDING, a comment block per finding H1-1 … H1-12. `H1-v1.preregistration.DRAFT.yaml` is unchanged | `tsclaude prereg validate /tmp/H1-v2.yaml`: ok, 0 problems, 4 open items, 24 configurations |

## Checks

`PYTHONPATH=src:tests/unit/backtest python3 -m pytest tests/unit/strategies tests/unit/research/test_preregistration.py tests/unit/backtest/test_feasibility.py tests/unit/backtest/test_bt_runner.py -q -p no:cacheprovider`:
145 passed, 1 skipped (pyarrow missing locally; it was skipped before the change too); with
`tests/unit/backtest/test_h1_draft_feasibility.py`: 155 passed, 1 skipped. `test_bt_runner` golden
hashes unchanged. All of `tests/unit/research` and `tests/unit/backtest` (`-m "not slow and not
live"`): 420 passed, 1 skipped, 3 deselected. `ruff check src tests`, `ruff format --check src tests`: clean. `mypy src`: 58
errors, all pre-existing (same count before the change), none in the touched files.

## Still open (owner / Lead)

- H1-1: run the P&L-free count on development data (15m and 1h, spot) and choose the timeframe
  from candidate counts only. If 1h, re-derive the embargo ((48 + 1) × 60 = 2940 min > 1440).
- H1-5 / D-E: with `min_rr_net` 1.5 every configuration in the grid is INCOMPATIBLE live.
- D-C, D-D, D-F (holdout months, SOL), H1-11 pins, the proposed drawdown bound (H1-12).
- Promotion of the strategy from `DRAFT_REGISTRY` to `REGISTRY` before any freeze.
- `metrics.csv` and ROADMAP were not updated here (outside this task's write scope).
