# MVP closure audit and implementation handoff

**Date:** 2026-11-10 · **Branch:** `task/mvp-integration` @ `65c0e2f` (CI `42761802261`, 7/7 green) ·
**Auditor:** Claude Fable 5.2, independent of the implementation session · **For:** the next Opus
implementation session.

This is an audit. It changes no code and no gate, approves nothing that belongs to the owner, and
merges nothing. Gate texts are quoted from [ROADMAP](ROADMAP.md); nothing here redefines them.
Evidence for earlier work is in [MVP_VALIDATION_REPORT](MVP_VALIDATION_REPORT.md) and is not repeated.

## 0. Bottom line

1. **M3's literal gate has no open technical item.** Both halves have evidence on both operating
   systems. What remains is the owner's formal acceptance, which this audit does not give.
2. **One M3 behaviour will not be validated by waiting:** a strategy book trading in a real session.
   The reference strategy almost never confirms an entry on spot at current costs (§2, U1), and the
   live risk gate would reject it if it did.
3. **The walk-forward tool is not yet safe on real data.** It accepts runs the runner itself grades
   invalid, and its default start date makes every training run invalid (§4, WF-1 and WF-2). Both
   are small fixes and must land before any real-data walk-forward.
4. **H1-v1 should not be frozen as drafted.** Its cost filter contradicts its compression rule, so
   it would produce far fewer than the 300 trades certification needs. Five further protocol gaps
   need a decision first (§5).
5. **The live risk gate and the research objective disagree.** Backtests apply no risk gate; the
   live baseline book requires a net reward/risk of 1.5, which rejects every take-profit geometry
   ADR-0012 requires in the search space. This needs an owner decision before M4's "runs live" gate
   can mean anything (§6, I-1).

## 1. M3 gate audit

**Literal gate** (ROADMAP "M3"): *"recorded live session replays to an identical ledger hash;
fault-injection tests (stale stream, exception in risk rule, crash mid-fill) all block or recover
correctly."*

| # | Requirement | Evidence | State |
|---|---|---|---|
| G1 | A recorded live session replays to an identical ledger hash | Three real Windows sessions, each `verify-replay` PASS. **All three also replay identically on Linux**: the 1 h session of 2026-11-06 (360 batches, 348 observed bars) and the crash-recovery session (3,498 batches) in this audit at `65c0e2f`, with the store file's SHA-256 unchanged before and after; the 25-minute session of 2026-11-09 (150 batches) in the previous session at `2c7d343` | **Met.** This also closes the "independent review of the session evidence" item of the ROADMAP reconciliation table |
| G2 | Stale stream blocks | `tests/unit/sim/test_paper_session.py:240`. Real sessions: a clock 3.4 s and 6.9 s off blocked all observation, fail closed | **Met (tests)**, and seen in the real app |
| G3 | Exception in a risk rule blocks | `tests/unit/sim/test_paper_session.py:223`, `tests/unit/risk/test_risk_rules.py:187,197` | **Met (tests)** |
| G4 | Crash mid-fill blocks or recovers | `tests/unit/sim/test_paper_restart.py:168`. Real: a PC restart during a session, then recovery with replay PASS | **Met (tests)**; a real crash recovered, though not one timed mid-fill |
| G5 | The tests run green | Re-run in this audit: the seven fault-injection tests above plus the 13 Target A tests, **20 passed**. CI on both OS: `42754283876`, `42756815061`, `42761802261`, all 7/7 green | **Met** |
| G6 | Formal acceptance | None recorded. The owner's 2026-11-06 decision covers Target A only and says it "does not replace … the literal M3 fault-injection acceptance gate" | **Owner-only, open** |

**M3 scope items beyond the literal gate** (ROADMAP "M3": *LiveClock, paper books (baseline +
benchmark), risk engine (fail-closed), kill switch, restart resolution over backfill, Paper pages*):

| Item | State | Evidence or gap |
|---|---|---|
| LiveClock, three books, risk engine, Paper page | Implemented | CI; real sessions |
| Restart resolution over backfill | Implemented and shown in the real app | Recovery session: a 9.3 h downtime gap detected, backfilled and processed unobserved |
| Kill switch | Engine and replay implemented (`sim/book.py:843-927`, test `test_paper_restart.py:205`) | **No operator reset in the app.** `docs/tasks/M3-T2.md` deliverable 2 (token-protected reset with a note) has no endpoint. A halted book stays halted until a new paper session is recorded |
| "Accept known gap" (L-11), `/api/paper/latency` | Not implemented | `docs/tasks/M3-T2.md` deliverables 2 and 3. The owner kept the empty-minute case fail-closed (OD-1) |

## 2. What genuinely remains unvalidated in M3

- **U1. A strategy book trading in a real session.** In all three sessions only the benchmark
  traded (`strategy_books_exercised: false`). This cannot be closed by running longer:
  - The reference strategy confirms only when planned costs are at most 0.2 R
    (`strategies/bracket_reference.py`, `max_cost_r`). Spot planning cost is 0.26 % round trip, so
    the stop must sit at least 1.30 % from price.
  - Measured on the 9.4 days of real 15m bars in the recovery store, 1.5 × ATR reaches 1.30 % on
    0.7 % of BTC bars, 4.2 % of ETH and 4.6 % of SOL. Those are the high-volatility bars; the
    strategy also requires *low* volatility (ATR rank at most 0.35).
  - Consistent real evidence: 0 trades in the BTC 2024-Q1 backtest; 0 proposals in every live
    session.
  - If a signal did confirm, the baseline book would reject it: net reward/risk 0.75 against a
    required 1.5 (Target A test, 3 of 3 rejected).
  - **Closure path:** the owner accepts the deterministic Target A tests as the strategy-to-fill
    evidence for the formal gate, as already decided for Target A. Decision D-A in §8.
- **U2. Restart equivalence** is not guaranteed and never will be shown by a real session: there is
  no uninterrupted twin to compare with. The recovery replay shows the journal is deterministic. The
  limitation is accepted for the supervised MVP (D-19, D-20, D-21). Settled; do not reopen.
- **U3. The branch's dashboard has not been rendered on the owner's PC.** The research panel and the
  "What to do" hint are covered by vitest and a CI build only. Five minutes of owner time (A6).
- **U4. Operator kill-switch reset** (above). Not part of the literal gate; decision D-B in §8.

Resolved since the 2026-11-09 evidence matrix (`claude/TSC_M3_EVIDENCE_MATRIX.md`): row 7 (paper
anchor and backfill on Windows), row 10 (green CI), row 11 (clock), row 12 (a restarted real session now replays; U2 says what that does and does not show).

## 3. M4, M5 and M6 as originally defined

Scope is quoted from ROADMAP. Nothing is added.

| Milestone | Gate (ROADMAP) | Exists | Missing |
|---|---|---|---|
| **M4** | "each hypothesis has a committed preregistration and a graded report with CIs and trial counts; holdout unlock events recorded; at least one strategy runs live (any grade ≥ C, labeled)". Scope: preregistration tooling, holdout guard, walk-forward, bootstrap + deflated Sharpe, random-entry controls, evidence grades, **H1–H3** (H4 if futures data permits), Research pages, promotion to the live scanner (Early Watch + Confirmed Entry) | Holdout guard and owner unlock CLI (`store/holdout.py`, `store/holdout_cli.py`); trial registry (`research/trials.py`); runner with certification, cost stress, random-entry control and grades (`research/runner.py`, `research/report.py`); statistics (`research/stats/`); walk-forward driver (`research/walkforward.py`, synthetic tests only); research status (`research/status.py`); Backtests page | Preregistration schema, `validate`/`freeze` and template (`docs/tasks/M4-T1.md` deliverable 1); trial-budget enforcement (`research/trials.py:19` TODO); H1, H2, H3 strategies and frozen preregistrations; any real-data research run; Research pages beyond Backtests; promotion to live (`runtime/paper_runtime.py:158` allows only `none` and `bracket_reference`; `strategies/live_adapter.py:130` is hard-wired); Early Watch is counted, not emitted, live (`live_adapter.py:155`) |
| **M5** | "all agent paths tested with a mocked client (valid, invalid, timeout, over-budget, API down); app fully functional with mode `off`; with a key: measured cost/latency per signal on live signals" | Nothing. There is no `agents/` package. The app runs with no agent code, which is not the same as a tested `off` mode | The whole milestone. The last clause needs live signals, which need a strategy that signals |
| **M6** (MVP needs the export part) | "export validates against schema; a scorer reproduces dashboard metrics from the export alone" | Nothing | JSONL export, schemas, scorer (`docs/EVALUATION_PROTOCOL.md` §4) |

M4's data prerequisites, from `docs/M2_ACCEPTANCE_RECORD.md` §5, are still deferred: B01–B12
(D-15), Top-10 coverage, USD-M, mark price and funding.

## 4. M4 readiness for the first real-data walk-forward

Mechanically runnable today: `tsclaude backtest walkforward` on the canonical lake, spot, the five
accepted symbols, 2022–2024. **Not safe to run yet.** In order of severity:

| ID | Finding | Evidence | Fix |
|---|---|---|---|
| **WF-1** | **Invalid runs are used.** The walk-forward reads `margin_lcb`, `p_lcb` and trades from each run and never checks `integrity.problems` or grade X | `research/walkforward.py:371-380` (`score`), `:407-414` (`oos`). Shown in this audit on the synthetic lake: a run graded **X** ("data gaps in warm-up") still carried `margin_lcb = −0.048` and 45 trades | In `score`, `rescore` and `oos`, treat a run with integrity problems as ineligible (train) or as a failed fold that voids the stitched verdict (test). Report it per fold |
| **WF-2** | **The default start makes every training run invalid.** A run reads its warm-up before `from`; the lake starts 2022-01-01, so `--from 2022-01-01` has no warm-up and grades X | `research/runner.py:852`, `:948-960`; CLI default `research/backtest_cli.py:310`; reproduced as above. The existing synthetic walk-forward test starts at the first stored minute, so it exercises exactly this case without noticing | Start the anchor at the first month start that leaves the strategy's warm-up inside the data, and refuse earlier starts. Fix the synthetic test to assert clean integrity |
| **WF-3** | **SOLUSDT has a permanent source gap** at 2023-02-14 16:39 (C-2). Any range containing it is grade X, which is every anchored training window from the second fold on | `docs/M2_ACCEPTANCE_RECORD.md` §4.1 | A preregistration decision: exclude SOL, or define how a single-symbol source gap is handled. Not a code default |
| **WF-4** | **Runtime.** Every run re-simulates the whole development segment to measure the maximum loss. One real run of 3 months of BTC took about 90 s on the owner PC; 36 of the 42 symbol-months it simulated were that pass | `research/runner.py:939`, `:546-564`; owner run `mvp-evidence-20261109-2312` | Cache the max-loss measurement per (configuration, symbols, dataset ids). Make the walk-forward resumable: reuse a verified run with the same trial key. The owner PC restarts, so resumability is required, not optional |
| **WF-5** | **Trial count.** The trial key includes the date range, so a grid of G configurations over F folds registers about G × F trials. H1-v1 as drafted gives about 200, not its declared budget of 24. At a 60 % win rate over 1,000 trades the exact lower bound is 0.555 with 24 trials and 0.544 with 224 | `research/runner.py:877-896`; exact Clopper–Pearson computed with `research/stats/binomial.py` | A methodology decision (D-C): what counts as a trial for the budget and for the correction. Decide before the first real research run; registry rows are permanent |
| **WF-6** | **No preregistration id and no dataset pinning.** Walk-forward runs carry no `prereg_id`, so they can never count for a strategy's tier. The acceptance record says research must pin the repaired SOL and XRP dataset ids; no CLI option does it | `research/walkforward.py:356-369` (`_run`); `docs/M2_ACCEPTANCE_RECORD.md` §4.4 | Add `--prereg` and `--datasets` to `walkforward`; record both in the report |
| **WF-7** | **No final selection.** The driver selects per fold. Nothing produces "the walk-forward-selected configuration" that RESEARCH_PROTOCOL §2 sends to validation | `research/walkforward.py:235-244` | See H1-2 |

**Runtime estimate, with its assumptions.** Assume about 2.1 s per symbol-month, from the single
90 s run (36 + 3 + 3 symbol-months; it had 0 trades, so a trading strategy will be slower). Each
run costs one 36-month max-loss pass plus two passes over its window (strategy and random control).

| Run | Symbol-months | Estimate |
|---|---|---|
| Dry run: `bracket_reference`, BTC only, 2 configurations, 8 folds | about 1,600 | about 1 hour |
| H1-v1 as drafted: 24 configurations, 5 symbols, 8 folds | about 79,000 | about 46 hours |
| The same with the max-loss pass cached per configuration | about 48,000 | about 28 hours |

## 5. H1-v1 review (`docs/research/H1-v1.preregistration.DRAFT.yaml`)

The draft is correctly kept out of `preregistrations/`, is not frozen, and no H1 code or result
exists. Its causality statements are right: decisions at bar close, a closed 1h context bar only,
entry at the next 1m open. **Do not freeze it as written.**

| ID | Severity | Finding | Fix before freeze |
|---|---|---|---|
| H1-1 | **Blocking** | **The cost filter contradicts the compression rule.** `max_cost_r: 0.2` needs a stop at least 1.30 % from price on spot. The stop is 1.0–1.5 × ATR(15m), and entry requires ATR in its lowest 25–35 %. Measured on real 15m bars (§2, U1), under 5 % of all bars pass the cost filter, and they are the high-ATR ones. Expected trades are far below the 300 that certification requires (`research/stats/certify.py:136`) | Run a P&L-free count on development data: candidate bars before and after the cost filter, per symbol and year. Then change the design, not the threshold after the fact: a 1h decision timeframe, or USD-M (planning cost 0.16 %; 6–24 % of bars pass at 1.5 × ATR), which needs the deferred USD-M import |
| H1-2 | **Blocking** | **The configuration sent to validation is undefined.** Folds can select different configurations | State the rule in the file: for example, the configuration selected by the same objective on the whole development segment, and only if the stitched out-of-sample verdict is positive |
| H1-3 | **Blocking** | **The holdout unlock cannot work for a search preregistration.** The guard requires the committed file to declare one `strategy_hash`, and the runner passes the parameter hash of the single configuration it runs (`store/holdout.py:232`, `research/runner.py:316`). A file frozen before selection cannot know that hash | Decide a two-stage form (D-D): the search preregistration, then a committed selection record naming the selected hash, made before the validation run. Document it as an ADR-0009 amendment |
| H1-4 | **Blocking** | **`trial_budget: 24` is neither enforced nor what the registry counts** (WF-5) | Decide D-C; implement the budget check (`research/trials.py:19`) |
| H1-5 | **Blocking** | **The live risk gate rejects every listed geometry.** `tp_r` of 0.5, 1.0 and 1.5 all give a net reward/risk under 1.5 (`configs/risk.yaml:26`) | Decide D-E (§6, I-1) |
| H1-6 | Major | **The 1h EMA(50) needs 741 hourly bars of warm-up** (`features/core.py:175`, tolerance 1e-12): 31 days. That exceeds the live `max_backfill_days: 30` (`configs/data_quality.yaml:51`), and one unobserved live minute would restart it | Use a finite-window trend filter, such as the close above the mean of the last 50 closed 1h closes |
| H1-7 | Major | **`time_exit_bars: null` leaves the holding period unbounded**, so "embargo ≥ one maximum holding period" (RESEARCH_PROTOCOL §3) cannot be shown | Set a time exit and derive the embargo from it |
| H1-8 | Major | **SOLUSDT** is in the universe and makes most training windows grade X (WF-3) | Exclude it or define the handling |
| H1-9 | Major | **The holdout months are outside the accepted data.** M2 was accepted for 2022-01 → 2025-12 only (D-22). Grade A needs the 2026 holdout months accepted first | Record it as a prerequisite; owner decision D-F |
| H1-10 | Minor | **Wrong order.** The draft says code comes after the freeze; `docs/tasks/M4-T1.md` has `freeze` compute the strategy hash from the implementation | Implement the strategy (no runs), then freeze |
| H1-11 | Minor | **Unpinned or placeholder inputs:** "cost model at freeze time"; instrument filters in `configs/backtest.yaml` are marked UNVERIFIED; `decision_latency_ms: 0` although the config says to raise it to the observed live p95 | Pin explicit versions and hashes; load real filters; set the latency from the recorded release latencies |
| H1-12 | Minor | **Admission criteria are incomplete.** RESEARCH_PROTOCOL §5 lists a drawdown criterion; the draft has none | Add a numeric drawdown bound |

The rejection criteria that are present are measurable with existing code: certified margin and
expectancy bounds, the 300-trade floor, cost stress × 1.5, the random-entry control, and integrity.

## 6. Trial-registry defect and other integration problems

**Trial-registry run ids (confirmed, low severity, small fix).**
- The trial is recorded before the run, with `run_id = bt-<stamp>-<trial_key[:8]>`
  (`research/runner.py:903`). The run directory is named after the manifest hash,
  `bt-<stamp>-<manifest_hash[:10]>` (`:1020`), which is only known afterwards.
- So `first_run_id` and `last_run_id` never name an existing run. Owner evidence: the registry shows
  `bt-…-a36e06b3` while the directory is `bt-…-3e89ba79b8`.
- Nothing reads those two fields. Trial counting uses `trial_key` and is unaffected. Manifest →
  trial navigation already works (`manifest.trials.trial_key`).
- Recording before the run is correct and must stay: a run that crashes is still a trial.
- **Fix:** keep the provisional id at record time. After the artifacts are written, call a new
  `TrialRegistry.attach_run(trial_key, run_id, now_ms)` that sets `last_run_id`, and `first_run_id`
  when this evaluation created the row. Update the module docstring's rule on which fields may
  change. Leave the owner's existing row as it is.
- **Tests:** after `run_backtest`, the registry's `last_run_id` equals `RunOutcome.run_id` and that
  directory exists; a run that fails after recording keeps the provisional id and its evaluation
  count.

**Other integration problems, most important first.**

| ID | Problem | Evidence | Owner of the fix |
|---|---|---|---|
| I-1 | **Backtests and live trading apply different rules.** The runner uses `allow_all` and says so in every manifest; the live baseline book requires net reward/risk ≥ 1.5. ADR-0012 requires take-profit geometries of 0.5–1.5 R in every search space, and all of them fail that rule live | `research/runner.py:352`, `:1017`; `configs/risk.yaml:26`; Target A test | **Owner decision D-E**, then wire the chosen risk rules into the runner |
| I-2 | No path from a validated strategy to the live scanner | `runtime/paper_runtime.py:158`, `strategies/live_adapter.py:130` | M4 work |
| I-3 | Early Watch is never emitted live | `strategies/live_adapter.py:155` | M4 work (AC-09) |
| I-4 | Dependencies are unpinned: `uv.lock` is not committed, so each CI job resolves at run time | `docs/M2_ACCEPTANCE_RECORD.md` §4.7; `git_status.txt` of the owner runs | Owner decision D-G |
| I-5 | `README.md:7` still says "planning only. Nothing in this repository is implemented yet" | — | Track A |
| I-6 | `docs/MVP_DELIVERY_TARGETS_PROPOSAL.md:9` links to `MVP_WALKTHROUGH_PROPOSAL.md`, which does not exist | — | Track A |
| I-7 | One paper test still asserts real-time gaps (`test_retries_back_off_and_are_capped`, ≤ 0.31 s) | Flake-fix report | Lead decision; low |
| I-8 | `RUNNING` is published before the first poll can turn it into `AWAITING_BACKFILL`; nothing is processed in that window | Flake-fix report | Low |

Not software: the owner PC's instability (a restart, one unrepeatable access violation, one
impossible `TypeError`). Out of scope by the owner's directive. Keep real runs supervised and short.

## 7. Prioritized handoff

### Track A — mandatory to close the engineering MVP (small; no new features)

| # | Task | Files | Done when | Size |
|---|---|---|---|---|
| A1 | Walk-forward fails closed on invalid runs (WF-1) | `src/tsclaude/research/walkforward.py`; `tests/unit/research/test_walkforward.py`; `tests/unit/backtest/test_walkforward_runner.py` | A grade-X train run is ineligible; a grade-X test run voids the verdict; both are reported per fold; tests cover each | S |
| A2 | Warm-up-aware anchor (WF-2) | `walkforward.py` (`plan_folds` caller), `src/tsclaude/research/backtest_cli.py:310`; the synthetic tests | A start without warm-up is refused; the synthetic end-to-end test asserts empty `integrity.problems` on every run | S |
| A3 | Trial-registry run ids (§6) | `src/tsclaude/research/trials.py`, `src/tsclaude/research/runner.py:897-903,1020`; `tests/unit/backtest/test_bt_runner.py` | The two tests in §6 pass | S |
| A4 | Stale README status; broken link (I-5, I-6) | `README.md`, `docs/MVP_DELIVERY_TARGETS_PROPOSAL.md` | README states the real status and points to MVP_VALIDATION_REPORT | XS |
| A5 | M3 acceptance package for the owner (no approval) | `docs/DECISIONS_PENDING.md` (proposal rows only) | D-A and D-B are written as open proposals with the evidence in §1 and §2 | XS |
| A6 | Owner smoke check of the branch dashboard | `docs/MVP_USER_GUIDE.md` §1 and §3 | The owner reports the research panel and Paper hint render; recorded in MVP_VALIDATION_REPORT | Owner, 5 min |
| A7 | Reviewer and causality-auditor pass on A1–A3, then CI | — | Both green; CI 7/7 | S |

After Track A the honest status is still **MVP FUNCTIONAL — VALIDATION INCOMPLETE** until the owner
decides D-A. Nothing in Track A depends on the owner's PC except A6.

### Track B — quantitative research (after Track A; in this order)

| # | Task | Notes |
|---|---|---|
| B0 | **Decisions D-C, D-D, D-E, D-F** (§8) | Research-integrity and risk policy. Nothing below should start a real research run before these |
| B1 | Walk-forward hardening: max-loss cache, resume, `--prereg`, `--datasets`, final-selection step (WF-4, WF-6, WF-7) | `research/walkforward.py`, `research/runner.py`, `research/backtest_cli.py` |
| B2 | **First real-data walk-forward: a mechanics dry run.** `bracket_reference`, BTC, 2 configurations, about 1 hour on the owner PC | Purpose: runtime and integrity on real data. REFERENCE label; its results are never evidence. It adds trials only to the `bracket_reference` family |
| B3 | Preregistration tooling: schema, `validate`, `freeze`, template, budget enforcement | `docs/tasks/M4-T1.md` deliverable 1 |
| B4 | H1 feasibility count (H1-1), P&L-free, on development data | Decides timeframe and market before any outcome is seen |
| B5 | Revise H1 (H1-1 to H1-12); implement the strategy with causality canaries and window parity; owner review; freeze | No run before the freeze |
| B6 | H1 walk-forward → one validation run → holdout only if validation passes and D-F is done | The expected and reportable outcome is no demonstrated edge |
| B7 | H2 and H3; Research pages; promotion to live with Early Watch (I-2, I-3) | The rest of M4's gate |

### Track C — the rest of the formal roadmap MVP

M5 (agents) and the M6 export, exactly as ROADMAP defines them. No code exists. M5's live-signal
clause depends on Track B producing a strategy that signals.

## 8. Owner-only decisions (proposed, not approved here)

| ID | Decision | Recommended default | If delayed |
|---|---|---|---|
| D-A | Accept the literal M3 gate on the evidence in §1, with the deterministic Target A tests standing for strategy-to-fill (U1) | Accept, with the U2 limitation restated | M3 stays "not formally accepted"; no engineering is blocked |
| D-B | Operator kill-switch reset in the app (M3-T2 deliverable 2) | Defer; document "record a new paper session" as the recovery | A halted book needs a new session |
| D-C | What counts as a trial for the budget and for the multiple-testing correction under walk-forward | Budget = distinct configurations; correction = keep the registry's conservative count | Blocks B5 |
| D-D | Two-stage preregistration for the holdout (search file, then selection record) | Adopt as an ADR-0009 amendment | Blocks any holdout run |
| D-E | Live risk rule against ADR-0012 geometries: keep net reward/risk ≥ 1.5 and drop the 0.5–1.5 R geometries, or replace the fixed floor for validated strategies | Wire the risk rules into backtests either way, so research evaluates what live will take | A validated strategy could not trade in the baseline book |
| D-F | Accept the 2026 holdout months as data, and the SOL gap handling | Decide with H1's universe | Blocks grade A only |
| D-G | Commit `uv.lock` and the pnpm lock files | Commit after the owner's review in RUNBOOK_INGESTION | CI keeps resolving dependencies per run |
| D-H | Merge `task/mvp-integration` | After Track A | The branch stays unmerged |

M0 and M1 are also not formally accepted (ROADMAP live status). They are outside this audit's
assignment and are owner acceptances, not engineering work, except M1's 2-hour run.

## 9. Evidence used

- **CI:** runs `42754283876`, `42756815061`, `42761802261` (7/7 green each).
- **Owner sessions** (`C:\Users\felip\tsc_runs\`): `paper-bracket-20261106-104529`,
  `paper-20261109-2201`, `recovery-20261109-2227`, `mvp-evidence-20261109-2312`.
- **Run in this audit** (Linux, branch head, on copies):
  - `verify-replay` of the 1 h and the recovery sessions: PASS, hashes equal, store SHA-256
    unchanged;
  - the 20 gate tests;
  - the grade-X demonstration on a synthetic lake;
  - exact win-rate bounds for 1, 24 and 224 trials;
  - the 15m ATR measurement on the recovery store (9.4 days, 6 instruments, simple-mean ATR(14)).

## 10. Settled — do not reopen

- The paper-runtime CI flakes: root cause proven, tests-only fix, three green runs.
- The clock finding: the measurement was right; the PC clock was off.
- P-1, NB-1 and the observation anchor: decided (D-19, D-20, D-21).
- M2's limited acceptance (D-22) and the bounded ingestion (D-23).
- Graphify (REJECT for the MVP) and Rust (deferred).
- The 2026-11-09 review findings on the walk-forward's comparability, embargo floor and the
  VALIDATED tier: fixed in `54af1ef`.
