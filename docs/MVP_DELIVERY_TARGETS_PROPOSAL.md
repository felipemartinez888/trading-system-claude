# MVP delivery targets, two roadmaps and estimates: proposal

- **Status: PROPOSAL (2026-11-06).** Owner decisions of 2026-11-06 are recorded in §6; the rest awaits approval.
  It changes **no** definition in
  [ROADMAP](ROADMAP.md): M0–M7, their gates, the 16 MVP acceptance criteria and "MVP = end of M5 plus the export
  part of M6" stay exactly as written. M5 and M6 are not new. This file adds a delivery view on top of them. Any
  change to the roadmap contract needs separate owner approval.
- **Basis:** repository state at `9f908d9` (CI `42322235295`, 7/7 green), the
  [M2 acceptance record](M2_ACCEPTANCE_RECORD.md) and the [walkthrough proposal](MVP_WALKTHROUGH_PROPOSAL.md).
  Where a statement is a judgment and not a measurement, it says so.

## 1. Four deliverables that must not be confused
| | What it is | Formal gate? | Needs M5/M6? |
|---|---|---|---|
| **Target A: Technical MVP demonstration** | Paper-only chain: Binance data → ingestion → reference signal → risk checks → simulated execution → ledger → deterministic replay → dashboard | No: it draws on the M0–M3 gates but is not one | No |
| **Target B: Quantitative research MVP** | The essential M4: pre-registered hypotheses, causal features, walk-forward and out-of-sample evaluation, costs, overfitting controls, graded evidence, holdout governance | Part of M4's gate | No |
| **Target C: full roadmap MVP** | The original definition: M0–M5 plus the M6 export, and the remaining acceptance criteria | Yes (the original formal MVP) | Yes |
| **Post-MVP** | M7 (forward trial and hardening) and the enhancement backlog (Roadmap B below) | No | — |

- A working research engine does not imply a profitable strategy. Nothing here predicts returns or success rates.
- `bracket_reference` is a pipeline reference ("not claimed profitable"). It is never evidence of edge.

## 2. Target A: what exists, what is missing
**Exists, with evidence:**
- **Ingestion and data:** live ingestion (spot and USD-M public streams), historical lake, QA, and the M2 gate
  (green on both OS in CI `42322235295`).
- **Strategy to ledger:**
  - the reference strategy and its paper adapter: `test_reference_intents_in_the_paper_engine_match_the_backtest_and_are_risk_gated`
    shows paper decisions equal backtest decisions, the risk engine stays active, the shadow book records the
    hypothetical fills, and no fill happens before the decision;
  - three paper books and the risk engine with its fault-injection tests (stale stream, rule exception, crash
    mid-fill) green in CI.
- **Replay and dashboard:** `verify-replay`; the dashboard pages Health, Markets, Signals, Logs, Paper and Backtests.
- **Recorded real sessions** (not every successful real replay had empty ledgers). Both were replayed with
  `verify-replay`; the second was re-verified read-only from its own artifacts on 2026-11-06:

  | Session | Strategy | Batches | Observed bars | Result | Trading |
  |---|---|---|---|---|---|
  | 30 min, `paper-20261106-090120` | none | 180 / 180 | 0 (clock 6,886 ms off, fail-closed as designed) | PASS | none: every ledger holds only its deposit |
  | 1 h, `paper-bracket-20261106-104529` | `bracket_reference` | 360 / 360 | 348 | PASS; all three ledger hashes equal the session-end evidence | benchmark only: 3 simulated entries, 7 ledger postings including the deposit; baseline and shadow identical, deposit only; `strategy_books_exercised: false` |

  - The second session's `state.db` hash was identical before and after the check (the verifier copies the store).
  - The evidence file is written by the same process that ran the session: it shows reproducibility, not the
    honesty of the run. No independent review of either session exists yet.
  - Seven ledger postings are not seven trades: three are simulated entries.

**Missing (evidence, not features):**
1. ~~**M2 sign-off S7**~~ **Done 2026-11-07T02:37:00Z** (limited scope, [D-22](DECISIONS_PENDING.md)).
2. **Strategy execution in a real session.** The 1 h session shows the live-to-replay pipeline with real observed
   bars and a nonempty benchmark ledger. Whether `bracket_reference` produces a CONFIRMED signal in a short window is
   **not controllable**: compression plus breakout plus volume is rare by construction, which is why the
   deterministic test (item 3) carries that evidence.
3. **A bounded, deterministic end-to-end test** (owner-approved as the next, separate implementation task; not
   part of this documentation checkpoint). It covers market data → reference signal → risk evaluation → accepted or
   rejected simulated execution → ledger → replay consistency:
   - the real `bracket_reference` keeps its rules; if its 1.0 R target fails the baseline's 1.5 R net requirement,
     the rejection is preserved and shown as fail-closed behavior;
   - a separate, risk-compatible synthetic scenario may exercise an accepted baseline entry, without changing
     production risk limits and without presenting it as `bracket_reference`;
   - no manufactured live signals, no lowered thresholds, no profitability claim.
4. **Gate wording (owner decision, 2026-11-06).** For Target A, a real observed-bar replay with a nonempty benchmark
   ledger and matching hashes is acceptable as evidence of the live-to-replay pipeline. It does **not** replace
   the deterministic strategy and risk execution tests or the literal M3 fault-injection acceptance gate.
5. **Known limits that stay stated:**
   - the baseline book will mostly reject `bracket_reference` signals, because the strategy targets 1.0 R and
     the risk config requires 1.5 net after costs (read from the code, seen in the walkthrough proposal, not
     changed here);
   - the real 9-day startup backfill is unmeasured;
   - the WHEA hardware investigation is open, and Target A does not wait on it unless a session fails because of it;
   - restart equivalence is covered by tests and not by a real restarted session.

**Does not block Target A:** Top 10 expansion, full USD-M or funding history, Graphify or GraphQL, Rust, paid APIs,
dashboard polish, large reimports, the M1 2-hour gate and the hardware investigation.

## 3. Roadmap A: path to the MVP (status as of 2026-11-06)
"Formal" means the milestone's literal gate with owner acceptance. "Demonstrated" means shown working with
evidence, not a gate. No percentages: there is no defensible measurement basis for them.

| Milestone | Formal state | Demonstrated | Real blocker | Next action |
|---|---|---|---|---|
| M0 Foundations | Not accepted | CI 7/7 both OS; launcher run by the owner (2026-10-28) | Owner Windows check and spike write-up | Owner records acceptance, or lists what is missing |
| M1 Live tape | Not accepted | Live tape and dashboard; replay E2E in CI | The 2-hour `verify_live` run | **Deferred**: not needed for Target A unless the owner wants the gate |
| **M2 Data + backtest core** | **Accepted, limited scope (S7, 2026-11-07, D-22)** | CI cross-OS hash green; C-1 repaired and verified | Owner S7 sign-off | Owner signs or amends the proposed text |
| M3 Paper + risk | Not accepted: real replays PASS (30 min with empty ledgers; 1 h with a nonempty benchmark ledger), strategy books never exercised in a real session, fault-injection gate not formally accepted | Paper runtime, risk, books, replay tooling; fault-injection tests green in CI | Strategy and risk execution shown by deterministic tests; acceptance of the literal fault-injection gate; independent review of the session evidence | Target A deterministic end-to-end test: implemented locally (2 test files, 13 tests, reviewer APPROVE WITH NOTES, causality auditor PASS at closure; [devlog](devlog/2026-11-07-target-a-e2e-test.md)), uncommitted, no CI yet; then commit/push approval |
| M4 Research | Not started | Stats library, preregistration, trials, runner, holdout guard and unlock CLI | Walk-forward, random-entry controls, hypotheses, grade wiring, Research pages, real-data run | After Target A |
| M5 Agents | Not started (no `agents/` package) | — | The whole milestone | After Target B |
| M6 Analytics + export | Not started | — | Export, schemas, scorer | After Target B |
| M7 Forward trial | Post-MVP | — | — | — |

**Readiness**

| Target | Readiness | Basis |
|---|---|---|
| A, technical demo | Closest. The live-to-replay pipeline is evidenced by a real 1 h session (348 observed bars, matching hashes); missing: the deterministic strategy and risk end-to-end test and S7 | Code, CI and the re-verified session above; no new feature required |
| B, quantitative MVP | Foundations present, core research flow absent | Stats and guards exist; no walk-forward, controls or hypotheses |
| C, full roadmap MVP | Farthest | No M5 code, no M6 export, AC-08 depends on research results |

**Changes since the previous update:** the D-17 repair is published and CI-verified (7/7); M2's literal gate is
fully evidenced and was then accepted by the owner (S7, 2026-11-07, limited scope); M3's tests are now on the remote and ran on Ubuntu and Windows; the sealed-holdout
metadata leak was caught and kept out of the public history; a 1 h real session (348 observed bars, nonempty
benchmark ledger, all hashes matching) was re-verified from its artifacts; the owner decided the Target A
gate-wording question and approved the deterministic end-to-end test as the next separate task.

## 4. Roadmap B: enhancements and post-MVP (prioritized; none blocks A, B or C)
| Priority | Item | Note |
|---|---|---|
| 1 | Observability and alerting for the paper runtime | Clock-skew and fail-closed reasons are already persisted; surface them (the empty-session finding came from digging in logs) |
| 2 | Additional symbols (Top 10) and USD-M/funding coverage | Needed for H4 and futures research (D-02), not for A or for a five-symbol B |
| 3 | Dashboard and performance analytics (the eleven empty pages, analytics by regime and hour) | The M6 analytics part beyond the export is post-MVP |
| 4 | Ingestion and processing optimization | Only against a measured bottleneck |
| 5 | Dedicated deployment and operational resilience (mini-PC, unattended running) | Part of the M7 backlog |
| 6 | Advanced multicrypto opportunity scanning | After there is at least one graded strategy |
| 7 | Graphify/GraphQL evaluation | Evaluation only; no integration without a measured need |
| 8 | Python versus Rust benchmarking | Benchmark first; migration only on evidence |
| 9 | Real-money integration | Out of scope. Separate owner approval, controls and validation; contradicts the "paper only" rule until then |

Not enhancements: M4's core research requirements and the formal M5/M6 MVP requirements. They belong to Roadmap A.

## 5. Planning ranges (judgment, not commitments, not a velocity model)
Units are working days or weeks of effort, with owner-run waiting time separated. The source of the earlier
1–3 / 4–8 / 6–12 week ranges is not in this repository, so I could not check how they were built.

| | Target A | Target B | Target C (incremental after B) |
|---|---|---|---|
| Remaining implementation | ≈ 0; a bounded demo test or script, about 1–2 days | Walk-forward, random-entry controls, grade wiring, research CLI and page, features, H1–H3: about 3–5 weeks | M5 (client, packets, validation, synthesis, cache, budget, agent-off, mocked paths): about 2–3 weeks; M6 export, schemas, scorer: about 1–2 weeks |
| Testing and auditing | Independent review of the demo test and of the session evidence: about 1–2 days | Causality and statistical audit per hypothesis and per new module: about 1–2 weeks | Review of agent paths and of the export: about 1 week |
| Owner-run validation | S7 and the fault-injection gate decision; a further real session only if the owner wants one (the 1 h session already evidences the pipeline): about 1–2 days of owner time | Bounded backtests (minutes); one holdout unlock per preregistration, by the owner | An API key; measured cost and latency on live signals, which need signals to occur |
| Dependencies and risks | The 9-day startup is not re-checked here; hardware; no CONFIRMED signal in a short real window (hence the deterministic test) | Hypotheses may show no edge (the plan itself says that is likely); one holdout unlock per preregistration means no iteration on the holdout | AC-08 and "one strategy runs live, grade ≥ C" depend on B's results; live-signal measurement needs a strategy producing signals |
| **Range** | **about 1–2 weeks** | **about 4–7 weeks** | **about 4–6 weeks; cumulative from today about 9–15 weeks** |

**Corrections against the earlier ranges:**
- **A (1–3 weeks).** The upper end is only justified if the clock, the 9-day startup or a failed session forces
  rework. Nothing in the repository asks for new features. 1–2 weeks is the evidence-based range.
- **B (4–8 weeks).** The statistics, preregistration, trial registry and holdout guard already exist, so the
  engine share is smaller than the earlier range implied; strategies and audits dominate. 4–7 weeks.
- **C (6–12 weeks).** Read as a total from today, it is too low: M5 and M6 have no code. Read as an increment
  after B, it is about right at 4–6. The cumulative figure above is the consistent one.
- **What cannot be estimated:** whether any strategy reaches grade C or better, and therefore AC-08.

## 6. Owner decisions
| # | Decision | State (2026-11-06) |
|---|---|---|
| 1 | **S7:** sign, amend or decline the M2 text in the [acceptance record](M2_ACCEPTANCE_RECORD.md) | **Granted 2026-11-07T02:37:00Z**, wording unchanged, limited scope ([D-22](DECISIONS_PENDING.md)); no research claim, M3 acceptance, extra dataset or holdout access is authorized |
| 2 | M3 gate wording for Target A | **Decided:** a real observed-bar replay with a nonempty benchmark ledger and matching hashes is acceptable evidence of the live-to-replay pipeline; it does not replace the deterministic strategy and risk tests or the literal fault-injection gate |
| 3 | The Target A deterministic end-to-end test (§2 item 3) | **Approved** as the next focused implementation task, authorized separately; not part of the documentation checkpoint |
| 4 | Commit and push of these documents (D-07) | **Authorized**: one documentation-only commit, one normal fast-forward push to `origin/task/m2-closure` |
