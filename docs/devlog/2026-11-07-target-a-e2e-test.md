# 2026-11-07 — Target A: bounded deterministic end-to-end test

**Status: implemented locally as two new test files, uncommitted, not pushed, no CI run yet.** Tests only: no
change in `src/`, no production rule, parameter or threshold changed. Owner decision of 2026-11-06/07 (Target A,
"Begin Target A Deterministic End-to-End Demonstration"). This is **not** M3 acceptance and says nothing about
any strategy's performance.

## What was built
- `tests/unit/sim/target_a_fixture.py`: a fully synthetic, deterministic (no random), causal 1m series built
  from a 15m plan (calm → breakout cycles). Its 15m aggregates equal the plan exactly (checked by the auditor
  through the production `BarAggregator`).
- `tests/unit/sim/test_target_a_e2e.py`: 13 tests (about 15 s, no network, no lake, no sleep, no wall clock).

Chain exercised through production code: closed bars → unmodified `bracket_reference` (default parameters, via
`ReferenceLiveStrategy.create`) → signal lifecycle → risk engine (`configs/risk.yaml`, real market views from
snapshots built from the previous close) → baseline / shadow / benchmark books → ledger → journal → `replay`.

## Results (on the fixture; facts, not performance)
| Item | Result |
|---|---|
| Real `bracket_reference` confirmed signals | 3, each the close of a planned breakout bar; all CONFIRMED conditions read from the record (compressed, trend, breakout, volume) |
| Baseline verdicts | 3 of 3 rejected, reason exactly `reward_risk:rr_net:0.75367094<1.5` and no other rule; independent recomputation agrees to 1e-6 |
| Signal status | VETOED; no real outcome recorded |
| Baseline book | 0 orders, 0 trades, ledger = starting capital only |
| Shadow book | 2 hypothetical trades (target hit, an artifact of the fixture path) and 1 position still open at the end of data |
| Fill timing | release 1.5 s after close: fill at the open after (decision + 60,001 ms); release at the close: fill at decision + 1 ms; entry at the bar open plus slippage, not the close |
| Synthetic risk-compatible scenario (`synthetic_risk_compatible_probe`, labelled, not `bracket_reference`) | baseline accepted 2 entries (3 R target): one target exit, one stop exit; fees, slippage, exit prices, sizing and ledger reconciliation checked |
| Replay | every batch reproduced; ledger hashes and fingerprint equal in both scenarios; two independent runs give identical journals |

## Independent review
- **Reviewer (fresh context): APPROVE WITH NOTES**, no blocking finding. Notes closed: look-ahead canary scope,
  tolerance 2e-3 → 1e-6, weak exit/sizing/cost assertions (now pinned).
- **Causality Auditor: CONDITIONAL PASS**, then **PASS** at closure. Findings and disposition:
  - F1 (fill priced at the close was invisible, flat entry bars): fixed; mutant now caught.
  - F2 (a fill earlier than the next open is masked by the release bound in the live path): **not closed in
    this file, by design**. The pure `earliest_execution_ms` rule is covered by the simulator's own causality
    tests (a two-minute-earlier mutant fails `test_m3_audit_carryovers.py`); the limit is stated in the test.
  - F3 (canary blind to a short peek): canary now parametrized (cut at the decision, with queued minutes,
    3 h after); a one-minute submit-cutoff mutant is caught by the queued-minutes variant.
  - F4 (adapter warm-up guards not covered here): wording corrected; covered under `tests/unit/strategies`
    (not verified by the auditor).
  - N1 (flat decision bar hid a stale or open price in the risk view): fixed; mutant now caught.
- Mutants run on the final file, all caught: market fill at close, no exit slippage, sizing ÷10, risk threshold
  ÷3, release bound ignored, fee ×2, risk view storing the open. Survivors, disclosed: earlier-than-next-open
  fill (F2) and a maker/taker fee mix-up (spot maker = taker = 0.1 % in this cost model).

## Limits
- Synthetic data only; the shadow take-profits are artifacts of the fixture path and are not evidence of edge.
- The fixture has no gaps between a bar's open and the previous close: gap handling is covered by the
  simulator's own tests.
- Spot maker and taker fees are equal, so that distinction is not exercised.
- No real session is replaced by this: the 1 h real replay (348 observed bars) and this test are
  complementary. The literal M3 fault-injection acceptance gate is a separate item.
- Local evidence only: 13 new tests, related suites 645 passed, `ruff` and `mypy src` clean; full collection
  1,740 tests. No CI run.
