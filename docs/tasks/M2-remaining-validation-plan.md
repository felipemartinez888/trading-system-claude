# M2 remaining validation plan

- **Status:** proposal (2026-10-30). **Nothing here is authorized.** Each step below needs its own owner approval.
- **Starting point:** E-1, E-1b, E-2 and E-3 are integrated locally at `0c3f607`, not pushed and not in `main`. The O-7
  evidence is accepted with qualifications ([D-18](../DECISIONS_PENDING.md), [devlog](../devlog/2026-10-30-m2-o7-qa-rerun.md)).
- **Standing limits:** no 2026 holdout access, no canonical change outside an approved promotion, no M4 backtests, no
  hardware diagnostics. Exit code 0 is never QA acceptance, and an expected result is a hypothesis, never a target.

## Acceptance blockers
**Update 2026-11-06:** blockers 1, 6, 7 and 9 are resolved; see each row and
[DECISIONS_PENDING D-17](../DECISIONS_PENDING.md) / [the D-17 devlog](../devlog/2026-11-06-m2-d17-canonical-promotion.md)
for full evidence. Blockers 2–5, 8 (partially), 10 and 11 are unchanged.

**Update 2026-11-06 (publication):**
- Blocker 8 is resolved: CI `42322235295` covers the published D-17 work.
- Under the owner's five-symbol scope, blockers 2–5 and 10–11 are stated caveats or M4 prerequisites, not M2
  gate items. See [M2_ACCEPTANCE_RECORD](../M2_ACCEPTANCE_RECORD.md) §4–§5.
- M2 was formally accepted, limited scope, by the owner on 2026-11-07 ([D-22](../DECISIONS_PENDING.md),
  [record](../M2_ACCEPTANCE_RECORD.md)). That acceptance carries blockers 2–5 and 10–11 as stated caveats and
  deferred items; it does not resolve them.

| # | Blocker | Cleared by | State |
|---|---|---|---|
| 1 | C-1: canonical XRP 2025-04 and SOL 2022-12 each lack one real row | Steps 1–4 | **Resolved 2026-11-06**: D-17 `apply`, op id `45d9e72abf9c43458370a0b0a7e9e647`, `COMPLETE`/`accepted: true`; both rows added, independently re-verified with `LakeReader` |
| 2 | Canonical values not independently verified: BTC 2025; ETH, SOL, DOGE, XRP 2022–2023 | Steps 1–3 | Open (unchanged; D-17 covered only the two C-1 months, not a parity re-import of the rest) |
| 3 | B01–B12 not formally accepted (D-15) | Steps 5–6 | Deferred (unchanged) |
| 4 | Nine batches have only QA version 1 reports, with no class A check | Step 5 | Open (unchanged) |
| 5 | Cross-checks cover 2 of 24 months per batch | Step 5 | Open (unchanged) |
| 6 | Promotion procedure missing (E-5, D-17) | Step 4 | **Resolved 2026-11-06**: the D-17 tool was designed, reviewed five times and executed once against the real lake, scoped to C-1 only; batch promotion (B01–B12) under D-17 stays deferred |
| 7 | Simulator restore property fails on two classes (partial fix `904b84c`; seed 13073 reproduced) | Separate fix | **Resolved, stale entry corrected 2026-11-06**: this branch's HEAD already includes the completing fixes (`0435116`, `e87fe59`, `d1e280a`); `-k 13073`: 8 passed; full property suite (`test_sim_property_regressions.py` + `test_paper_properties.py`): 79 passed, 0 failed. A documented residual (≈0.05%, 3/6,000 random cases, absent an explicit `start_ms`) is unchanged and already pinned by its own test; it was never part of this blocker's "two classes" |
| 8 | No CI run covers the integrated work, so the cross-OS reproducibility gate has no current evidence | Push decision (D-07), then CI | **Resolved 2026-11-06**: the D-17 work was published as `b24f41e` + `9f908d9` (rebuilt from the local `60f5ce4`/`f18eb9f`, never published, to withhold sealed-holdout metadata); CI `42322235295` on `9f908d9` is green on all 7 jobs, Ubuntu and Windows, including the M2 cross-OS gate. The earlier run `42275943593` on `b1da844` is kept as history |
| 9 | No overall Causality Auditor sign-off for M2 is recorded | Review after blockers 1 and 7 | **Resolved 2026-11-06: CONDITIONAL PASS** ([D-17 devlog](../devlog/2026-11-06-m2-d17-canonical-promotion.md)); all five literal roadmap gate items independently verified green, open conditions are governance/traceability only |
| 10 | No backtest run is recorded locally | First recorded run, once data is ready | Open (unchanged) |
| 11 | Top 10, USD-M and funding coverage (O-9) | Owner scope decision below | Open (unchanged) |

**Owner scope decision needed.** The roadmap's M2 gate lists tests and a sign-off, while its status row also counts data
coverage as a blocker. My recommendation is to define M2 data acceptance as spot on the five complete symbols, and to
move O-9 to the M4 prerequisites, where the roadmap already lists Top 10 coverage. Otherwise M2 stays open for hours of
coverage imports that its gate does not name.

## Steps

### Step 1: tooling, no data run
**Prerequisites:** the O-7 documentation commit is reviewed.
- **E-4(b), part-hash comparison.** No such script exists in the repository. O-6 cannot run until it does. It compares
  `content_hash` values from manifests, read-only, and refuses to overwrite.
- **Move the allowlist procedure into the repository.** `o7.py` is unversioned and fixed to three batches. O-5 needs the
  same rules for other sources, with focused unit tests on synthetic folders and one independent review of the final
  version, which also answers qualification 4.
- **Optional, read-only:** compare the Parquet footer `created_by` of one original B10 part and one O-7 part. It costs
  seconds and may narrow qualification 3. It cannot prove the cause.

### Step 2: O-5, C-1 parity batches
**Prerequisites:** Step 1; owner authorization; E-3 safeguards active on the importing commit, which `0c3f607` provides.

| Batch | Range | Hypothesis, not a target |
|---|---|---|
| BTC | 2025-01 to 2025-12 | Clean |
| ETH, DOGE, XRP | 2022-01 to 2023-12 | The B03 signature: 71 Z2, 81 registered downtime, 1 malformed |
| SOL | 2022-01 to 2023-12 | The same, plus the row 2022-12-21 15:23 present, and 2023-02-14 16:39 missing |

- **SOL should show `classification_ok` false.** C-2 is a genuine source gap outside any registered interval, so it is
  unexpected by design. It stays a recorded finding, and the rule is not loosened.
- **Sources must be settled first.** The allowlist comes from canonical manifests, and only exact paths are opened. No
  source cache is listed or used directly, because the downloader deletes a cached file that fails verification.
  Canonical manifests also cover sealed months, so the allowlist must filter by month and refuse any 2026 name.
- **Open question:** whether the 15m and 1h archives for each batch's first and last months exist locally. If they do
  not, the run needs `data.binance.vision` downloads, which the owner has to approve.
- **Where it writes:** a new folder outside the repository, as for O-7. This replaces the older `data_batches/c1_*`
  proposal, because `data_batches/` is not git-ignored.
- **Change from the 2026-10-28 proposal:** that text asked for hardware monitoring. Under the owner's rule against
  hardware diagnostics, this plan uses sequential, bounded runs with full logs instead.
- **Size, estimated from O-7:** 108 months, about 3 minutes in total and 450 MB of disk.

### Step 3: O-6, part-hash parity
**Prerequisites:** Step 2 and the E-4(b) script. Read-only, seconds.
- **Hypothesis:** every part matches canonical except SOL 2022-12. XRP 2025-04 is already known from B08.
- **Any other mismatch is a new C-1-type finding.** It stops the plan for an owner decision.

### Step 4: C-1 reconciliation
**Prerequisites:** Step 3; E-5 design approved; D-17 decided.
- **Repair by replacement only:** each failing canonical month is replaced by its independently verified part, through
  a logged, non-overwriting procedure that keeps the old manifest and records old and new content hashes. No hand edits.
- **Expected targets:** XRP 2025-04 from B08, and SOL 2022-12 from O-5. Dataset ids change as a result, and no recorded
  backtest depends on them.
- **Not a repair:** SOL 2023-02-14 16:39 (C-2) is absent from the source, so any range containing it keeps grade X.
- **Holdout parts stay unverified.** O-8 is a separate approval at its own stage and is not needed for this step.

### Step 5: QA version 2 coverage
**Prerequisites:** the Step 1 tooling. Independent of Steps 2–4.
- **QA version 2 for B01, B02, B04–B09 and B11.** Version 1 counted zero-volume minutes only in runs of at least 3
  minutes and has no class A check, so their PASS values cover neither. Same procedure as O-7, about 30 s per 24 months.
- **Cross-check coverage, recommended tiers:**

| Tier | Scope | New native archives | Why |
|---|---|---|---|
| A, recommended | B03 2023-03 alone, `--from 2023-03-01 --to 2023-03-31 --cross-check-months 1` | 2 | The halt month has never been cross-checked |
| B, recommended | B10 and B12, `--cross-check-months 24` | 44 per batch | Thin symbols with no reference parity |
| C, optional | B03, the other 21 months | 42 | Already has 24 of 24 parts of canonical parity |

- `--cross-check-months N` with N at or above the month count covers every month, with no code change.
- New archives need checking locally or downloading, and sealed months are never sampled.
- **Expect more findings.** Thin symbols should show more explained cases. An unexplained case, or a bucket at a month
  boundary that fails closed under ADR-0015, is a finding. Behaviour at the halt in Tier A has no prior evidence.

### Step 6: batch decisions (D-15)
**Prerequisites:** Step 5 for the batches concerned, or an owner choice to accept the listed limits. These are owner
decisions per batch, taken from the [acceptance matrix](../M2_DATASET_ACCEPTANCE.md).

## Order
1. Commit the O-7 documentation after review.
2. Decide the M2 data scope.
3. Step 1, then Steps 2–4 and Step 5 in either order. Step 5 can go first because it touches no canonical data.
4. Step 6, then the non-data blockers 7–9, then formal M2 review.

## Out of scope for this plan
O-8 (holdout parity), M4 research, Paper Trading migration or reset, new strategies, and merges or pushes, apart from the
owner decision that blocker 8 depends on.
