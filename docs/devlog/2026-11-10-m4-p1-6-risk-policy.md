# 2026-11-10 — M4 P1-6: the paper risk policy in backtests; live-compatible H1-v3 draft

Owner direction (session instruction, 2026-11-10, under the still-open decision D-E): *preserve the
net reward/risk ≥ 1.5 requirement; design a compatible research search space for owner review,
applying identical risk policies in backtesting and Paper.* No H1 run, trial, freeze or holdout
access happened. D-E, ADR-0012 Amendment 6 and the H1 freeze stay the owner's.

## What changed
| Commit | Change |
|---|---|
| `9f2cabc` | `research/risk_policy.py`. With `risk_policy: live`, every backtest book (base, cost stress, random control, max-loss estimate) asks the paper `RiskEngine` at submit and at fill, and halts at the paper kill switch. `configs/backtest.yaml` (now `backtest-config-v2`) enables it. The risk config enters the backtest config hash by content. allow_all configs, manifests and metrics are byte-identical (golden hashes unchanged) |
| `5ea287e` | With `risk_compatibility: live-floor-kept-geometries-dropped`, `prereg validate` refuses non-COMPATIBLE configurations, and `freeze` needs a backtest config with the live policy and checks that config's own floor. TEMPLATE and the dry-run grid move to 2 R and 3 R |
| `9c681ca` | [H1-v3 draft](../research/H1-v3.preregistration.DRAFT.yaml) (take-profits 2, 2.5, 3 R), [H1_DECISION_BRIEF](../research/H1_DECISION_BRIEF.md), the D-E row records the direction, and the smoke check verifies the agent mode |
| this commit | Independent review fixes (below) |

## Independent review (Fable, fresh context): CONDITIONAL PASS, 0 blocking
| Finding | Status |
|---|---|
| F1 (major) Stale bars passed in backtests while Paper blocks them. The three bar checks of `data_freshness` are computable from history | FIXED. `data_freshness` is back in the backtest rules. `BarViews` computes the last bar and the lookback gap with the paper tracker's own algorithm. A cutoff whose lookback reaches pruned history gets no view (fail closed). Tests: parity with `MarketStateTracker.view_at` on gapped bars; stale and gapped bars block |
| F2 (major) Three wiring properties untested (observe-before-step, kill switch, base cost model in stress books) | FIXED. A test for each, and all three review mutants are now killed (run in place on a copy, restored byte-identical) |
| F3 Halts invisible beyond the base book | FIXED. `halted_books` lists every halted book; the brief states that the control is filtered by the same portfolio rules |
| F4 "Every confirmable trade passes" holds only at zero decision latency | Wording added to the brief and the draft |
| F5 Sizing is bounded, not equal | Stated in the brief (both 0.005 today) |
| F6 The floor check uses `CostModel()`, as `RunEnv` does by default | FIXED on the run side. A walk-forward under a keep-the-floor preregistration re-checks every configuration with the run's own cost model and risk config before anything runs, and is refused without the live policy. Finding: scaling all costs leaves H1-style geometries unchanged, but take-profits filled as taker orders turn 2 R CONDITIONAL (1.4995); this is in the brief |
| F7 `from_mapping(to_dict())` does not round-trip for live configs (the path is deliberately not in the dict) | OPEN (minor). No caller does this |
| F8 Stale "Backtests use `allow_all`" text | FIXED in DECISIONS_PENDING and `live_compat.py` |
| F9 metrics and roadmap not updated in those commits | FIXED in this commit |
| F10 The volume field was unpinned | Covered by the tracker parity test (`last_quote_volume`) |

## Evidence
- `tests/unit/backtest/test_backtest_risk_policy.py`: 20 passed.
- `tests/unit/research/test_preregistration.py`: 47 passed.
- `test_bt_runner.py` golden hashes: pass.
- Full unit suite on `9c681ca`: 1901 passed, 2 skipped, 16 failed. All 16 also fail on the baseline:
  pytest-asyncio and httpx are not installed in this container. CI has both.
- CI on the pushed commit: see the [overnight report](../MVP_OVERNIGHT_REPORT.md) checkpoints.

## Consequences
- Backtests of 0.5–1.5 R geometries book no trades under the repo config. That is the intended
  effect of applying the paper policy.
- After any missing minute, a backtest refuses entries for 4 hours, as Paper does. On real data
  this costs some candidates near gaps.
