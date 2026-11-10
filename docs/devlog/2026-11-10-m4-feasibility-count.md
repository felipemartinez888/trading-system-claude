# 2026-11-10 — M4: P&L-free feasibility count (audit H1-1)

Branch `task/m4-feasibility` (base `04307f5`). Role: Quant Engineer.

## What it counts
For a strategy that implements `Strategy.diagnose`, the count reports how many **decision bars**
(closed strategy bars closing inside `[from, to)`) pass each entry stage of the strategy, per
`market:symbol` and per UTC year of the decision bar's close. The counts are **cumulative in stage
order**: the k-th number counts the bars passing stages 1..k. Coverage is reported next to them:
`missing_minutes` (inside `[from, to)`), `history_resets` (incomplete strategy buckets; the warm-up
restarts after each), warm-up gaps, read conflicts and the dataset ids read. The result carries a
`content_hash`, `"provenance": "BACKTEST"`, `"kind": "feasibility_count"` and `"pnl_free": true`.

`bracket_reference` stages, in the order its LONG CONFIRMED rule applies them: `warmup_ready`,
`features_defined`, `compressed` (ATR rank at i-1 <= `rank_max`), `trend` (close > EMA),
`breakout` (close > prior range high), `volume_ok`, `stop_valid`, `cost_ok` (planned all-taker
round trip in R <= `max_cost_r`). "All stages pass" holds exactly when `on_bar` returns a CONFIRMED
proposal (WATCH proposals are not entries). `on_bar` and `diagnose` share the same feature and
geometry code; the backtest golden hashes are unchanged.

Data path: the same as a backtest. Guarded `LakeReader.read_bars(purpose=research)` month by month
from `from - warm-up`, `BarAggregator` (complete buckets only), trailing window of `window_bars`,
planning costs from the default cost model as `backtest run` uses them.

## Safety properties
- Development segment only (2022-01-01 -> 2025-01-01). Validation, holdout, mixed, pre-2022 and
  misaligned ranges are refused before any read; a range touching the holdout exits 3.
- Evaluates no outcome: no trade, exit, R, win/loss or equity is computed, and **no trial is
  recorded**. The output key set is asserted in tests.
- Causality canary: rewriting every 1m bar after a cut never changes the stage flags of decision
  bars closing before it.

## How to run
After the Lead wires `register_feasibility` into `backtest_cli.register`:

```
tsclaude backtest feasibility --strategy bracket_reference --params '{"interval": "15m"}' \
  --symbols spot:BTCUSDT,spot:ETHUSDT --from 2022-01-01 --to 2025-01-01
```

`--grid grid.json` gives one count per configuration; `--datasets ids.json` pins the read (every
id must exist in the lake). Output: `<data_dir>/feasibility/feas-<hash12>/feasibility.json`.

## What it cannot tell
- It is **not evidence of an edge**. It says nothing about returns.
- The count after `cost_ok` is an **upper bound on entries**: position overlap (one position per
  symbol), the one-bar validity window, fills and exits are not simulated.
- Bars whose warm-up is not ready (data start, after a gap) count as decision bars that fail
  `warmup_ready`; read the coverage fields before comparing years.
- A real-data count has not been run here (no lake on this machine); that is the owner's step.

## Evidence
Tests: `tests/unit/strategies/test_strat_diagnose.py` (9), `tests/unit/backtest/test_feasibility.py`
(20), golden hashes `tests/unit/backtest/test_bt_runner.py` (15 passed, 1 skipped: pyarrow).
