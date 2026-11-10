# 2026-11-10: Track A closure and M4 walk-forward infrastructure

Source of truth: [MVP_CLOSURE_HANDOFF](../MVP_CLOSURE_HANDOFF.md) §4, §6, §7, §8. Branch
`task/mvp-integration`. Paper only, no real-data research run, nothing approved on the owner's
behalf.

## Track A (commits `67bb18e`, `6f9ee18`, `04307f5`; CI `42766333677` 7/7 green: python ubuntu 1902 passed, windows 1900 passed)

| Item | What changed | Evidence |
|---|---|---|
| A1 (WF-1) | `run_problems`: a run with integrity problems, no integrity record or no valid grade is unusable (fail closed). Train: such a run makes its configuration ineligible. Test: such a run voids the stitched verdict (`INVALID_OOS_EVIDENCE`) and stops the walk-forward. `rescore` re-reads every run and rejects unverifiable metrics or trades. Out-of-sample outcomes are read only after the trades file hash is checked. Invalid numbers are not reported | `tests/unit/research/test_walkforward.py` (invalid train ineligible, all-invalid fold, voided verdict and early stop, `run_problems` cases); `tests/unit/backtest/test_walkforward_runner.py` (real runner: gap in the train window → both train runs X and ineligible; gap in the test window → `INVALID_OOS_EVIDENCE`) |
| A2 (WF-2) | `runner.warmup_ms_of` is the single source of the warm-up a run reads. The anchor must be the first month start whose warm-up lies inside the data (`earliest_anchor_ms`, from `LakeReader.first_bar_ms`, index metadata only, sealed parts excluded). Earlier anchors are refused before anything is recorded; the CLI default is that anchor, never before 2022-01-01 | Synthetic end-to-end test asserts empty `integrity.problems` and zero warm-up gaps on every run; the audit's reproduction (data starting at the anchor) is refused with no run and no trial recorded; CLI refusal and default-anchor tests |
| A3 | `TrialRegistry.attach_run` replaces the provisional run id once the artifacts exist (only a field still holding it; counts never change) | `test_bt_runner.py`: registry ids equal `RunOutcome.run_id` and the directory exists; a crash after recording keeps the provisional id and its evaluation; conditional update test; golden hashes unchanged |
| A4 | README status; the broken link in `MVP_DELIVERY_TARGETS_PROPOSAL.md` | Link check over README and `docs/`: 0 broken |
| A5 | D-A (M3 acceptance) and D-B (kill-switch reset) written as OPEN proposals | `docs/DECISIONS_PENDING.md` |
| A7 | Independent review (Fable, read-only): **APPROVE WITH NOTES**, 0 blocking. Causality audit: **CONDITIONAL PASS**, 0 blocking, 1 major | Findings and fixes below |

### Review findings and what was done

| Finding | Fix |
|---|---|
| Causality M-1: a single train-window gap removes every later fold (anchored windows, zero gap tolerance), and the remaining fold could still be certified | Coverage floor `min_oos_coverage` (default 1.0: the whole planned test period); below it the verdict is `INSUFFICIENT_OOS_EVIDENCE` and no certification is reported. A preregistration may set a lower floor. Test reproduces the audit's case (8 folds, 1 tested) |
| Causality m-1 / review M2: the coverage note overclaimed "data quality only" | Reworded (decided before any test result: data gaps, funding, source, timing or ledger failures); verdict lists `folds_with_invalid_train_runs` |
| Causality m-2: development-only enforced in the CLI only | `run_walk_forward` refuses non-development folds itself |
| Causality m-3: anchor check passes over a gap in the warm-up | `check_warmup_complete`: a guarded read of the first warm-up window refuses missing minutes outside registered interruptions |
| Causality m-4: void test runs stay on disk | RESEARCH_PROTOCOL §3: void test runs are not inspected before a rerun is designed |
| Review M1: status docs and metrics not updated | This devlog, `metrics.csv`, ROADMAP, MVP_LIMITATIONS, MVP_VALIDATION_REPORT §12 |
| Review minor 1: `max_loss()` used invalid runs | Only valid runs count |
| Review minor 2, 3: `rescore` could fall back to a stale certification; did not check the trades file | Both now make the run ineligible |
| Review minor 4: `run_problems` fail-open edges (`problems` None or a string, empty grade) | `problems` must be a list, the grade one of A/B/C/D/X |
| Review minor 5: provisional ids could collide within one second | A random nonce in the provisional id |
| Review minor 6: a failing `attach_run` lost a complete run | A database error is logged; the run is kept with its provisional id (test) |
| Review minor 7: missing tests | Added: verified trades reading, read-back failure in `rescore`, the 2022 clamp of the default anchor, the unknown-parameter message |
| Pre-existing, not changed: every run's max-loss pass simulates the whole development segment, test windows included (L-10) | It feeds only the expectancy bound and grade D, never selection (`margin_lcb` and `p_lcb` do not use it). Conservative, not causal; recorded in MVP_LIMITATIONS |

## M4 infrastructure that needs no owner research-policy decision

| Piece | What it does | Tests |
|---|---|---|
| Verified max-loss cache (WF-4) | `research/max_loss_cache.py`; key = strategy, symbols, configuration, cost model, interruption registry, engine versions, code commit and the content hash of every lake part read. Unknown or dirty code: no cache. Entries are verified (key body and value hash); anything else is a miss. A hit gives the same manifest and metrics hashes as no cache | `test_max_loss_cache.py` (8): identical hashes hit vs. no cache; one entry serves every window; unidentified code never cached; tampered entry is a miss and is replaced; new lake data changes the key; preregistration override applied after the cache |
| Resumable walk-forward (WF-4) | `research/wf_progress.py`. A plan id hashes everything that decides the runs (also the Python and numpy versions and the lake parts). A progress ledger records each completed evaluation with its hashes; a restart reuses it only after full verification (manifest re-hash, metrics/trades/equity hashes, trial key, window, preregistration, code). An untrustworthy record refuses the resume (CLI exit 6). Every fold is rescored with one trial count fixed at the start and stored in the ledger, so a resumed walk-forward selects exactly as an uninterrupted one | `test_walkforward_resume.py`: completed plan reused with 0 runs; crash before the test run resumes to the uninterrupted result; a run finished but not logged is rerun without a new trial; partial last line repaired; tampered metrics/trades/ledger and a missing run refuse; another plan's ledger refused; no resume without identified code |
| Dataset pinning and preregistration ids (WF-6) | `--datasets` (every id must exist in the lake; recorded in the report and every manifest), `--prereg` (must be committed; refused before anything runs, exit 4) | pins checked and recorded; uncommitted preregistration refused with nothing recorded |
| Explicit train/OOS validity checks | `validity_checks` re-reads every run's manifest: window, train ends before test, test windows ordered and disjoint, development segment, same configuration, preregistration and code. Any failure voids the verdict | from real manifests; a shifted test window and other code are detected |
| Trial budget | `project_trials` before anything runs: new registry trials at most (ledger-aware on resume); `--max-new-trials` refuses above it; `--max-configs` stays the configuration budget. What counts is decision D-C | refused with nothing recorded; a completed plan resumes under a zero budget |
| Final selection (WF-7) | `--final-selection`: after a positive verdict only, the same objective on the whole development window; output marked **PENDING GOVERNANCE**, never a selection record (needs H1-2 and D-D). Its possible trials are counted in the verdict in advance | NOT_RUN on a non-positive verdict; the rule selects by the objective; no eligible configuration |
| P&L-free feasibility count (H1-1) | `tsclaude backtest feasibility` (quant-engineer agent, commits `213692a`, `a25cc47` cherry-picked): per symbol and year, decision bars passing each entry stage of a strategy, cumulatively, with no fill, exit or outcome; development segment only; no trial recorded | see [its devlog](2026-11-10-m4-feasibility-count.md) |

## Not done (and why)

- **No real-data walk-forward and no H1 run.** The lake is on the owner's PC, and the first H1 run waits for D-C, D-E, D-F1 and the freeze of a revised H1 (DECISIONS_PENDING).
- **A6, the owner's dashboard smoke check**, needs the owner's PC: `scripts/owner_checks/dashboard_smoke.ps1`.
- **Container limits:** fastapi, pyarrow, duckdb and pytest-asyncio are not installed here, so API and Parquet tests run only in CI.
