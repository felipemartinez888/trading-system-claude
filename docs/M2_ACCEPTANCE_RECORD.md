# M2 acceptance record (limited scope)

- **Status: PROPOSED and UNSIGNED — awaiting the owner's S7 sign-off.** This file does not accept M2. Acceptance
  takes effect only when the owner signs §6 in writing, and the sign-off is recorded in
  [DECISIONS_PENDING](DECISIONS_PENDING.md). On 2026-11-06 the owner stated support for the limited scope in §1 but
  did **not** grant S7, and asked that the exact wording and caveats (§6 and §4) be presented first.
- **Prepared:** 2026-11-06 by the Lead, from the published branch `task/m2-closure` at
  `9f908d996cbc4b496adbf0869628a9b5f0b02130`.
- **Basis:** the [closure checklist](M2_CLOSURE_CHECKLIST.md), [D-17](DECISIONS_PENDING.md), the
  [D-17 devlog](devlog/2026-11-06-m2-d17-canonical-promotion.md) and the
  [validation plan](tasks/M2-remaining-validation-plan.md).

## 1. Scope (owner-approved 2026-11-06; nothing wider)
- **Data in scope:** Binance **Spot 1m**, canonical lake, for **BTCUSDT, ETHUSDT, SOLUSDT, DOGEUSDT, XRPUSDT**.
- **Period:** **2022-01-01 00:00Z → 2025-12-31 23:59Z**: the development period (2022–2024) and the validation
  year (2025) of D-05.
- **Code in scope:** the M2 surface (importer, lake, QA, simulator, runner, statistics, Backtests pages) at the
  commit above.
- **Out of scope:**
  - the 2026 holdout, which stays sealed;
  - BNB, ADA, AVAX, TRX and HBAR;
  - USD-M, mark price and funding (O-9);
  - B01–B12 acceptance (D-15) and their batch promotion (D-17, deferred for batches);
  - any statistical or strategy validation.
- **M4 prerequisites:** the out-of-scope data items above.

## 2. Gate evidence (ROADMAP M2: "look-ahead canaries, golden trades, ledger property tests, window-parity test, cross-OS reproducibility hash — all green; Causality Auditor sign-off")
| # | Gate item | Evidence | Result |
|---|---|---|---|
| 1 | Look-ahead canaries | `test_m2t4_causality_audit.py`, `test_sim_causality.py`, `test_feat_parity_canaries.py`; included in the full suite of CI run `42322235295` | green on both OS |
| 2 | Golden trades | `test_sim_golden.py`; same run | green on both OS |
| 3 | Ledger property tests | `tests/property/sim/`, `test_paper_properties.py`, `test_sim_property_regressions.py`; same run | green on both OS |
| 4 | Window-parity | `test_feat_parity_canaries.py`, `test_bt_runner.py`; same run | green on both OS |
| 5 | Cross-OS reproducibility hash | CI `42322235295`, jobs `m2 cross-os gate (ubuntu-latest)` and `(windows-latest)`: `test_golden_fingerprint_is_pinned` and `test_golden_hashes_identical_across_platforms` PASSED on both OS against the same pinned constants (`PINNED_FINGERPRINT_SEED0 = 93ecca10…9089`) | green on both OS |
| 6 | Causality Auditor sign-off | Global M2 review 2026-11-06: **CONDITIONAL PASS** (see §3) | conditions satisfied; see §3 |

### CI run `42322235295`
- **Run:** push to `task/m2-closure`, headSha `9f908d996cbc4b496adbf0869628a9b5f0b02130`, conclusion `success`, all 7 jobs `success`:
  - `python` on ubuntu-latest and windows-latest;
  - `m2 cross-os gate` on ubuntu-latest and windows-latest;
  - `frontend` on ubuntu-latest and windows-latest;
  - `safety guards`.
- **Pytest:** ubuntu 1,729 passed; windows 1,727 passed. Each OS deselected 2 `live` tests. Ruff, format and
  strict mypy are clean on both.
- **Collection difference:** ubuntu collects 2 more tests than windows. No test failed or was skipped, and the
  cause was not investigated.
- **Scope of the suite:** `testpaths = ["tests"]` includes `tests/owner_checks/`, so the D-17 tool's tests ran on both OS.

## 3. Causality Auditor CONDITIONAL PASS: historical verdict and later status of its conditions
- **The verdict stands as issued.** The global review of 2026-11-06 gave CONDITIONAL PASS. It found no causality,
  look-ahead, determinism or data-quality defect.
- **No re-issue.** The auditor has not re-issued a verdict. The table below records facts established after the
  review; it does not change the review.

| Condition (as stated by the review) | Later evidence | Status |
|---|---|---|
| Governance record of D-17 and the audit (devlog, DECISIONS_PENDING, checklist, roadmap, validation plan) | Committed in `9f908d9` (`docs(m2): reconcile D-17 promotion and causality audit evidence`) and published | satisfied |
| CI coverage of the exact D-17 implementation commit, not only `b1da844` | The implementation is published as `b24f41e`; CI `42322235295` ran on its descendant `9f908d9` (a docs-only delta plus one synthetic test), 7/7 green | satisfied |
| Sealed-source guard had no direct test | `test_source_part_from_the_sealed_holdout_period_is_rejected`, in `9f908d9`, green in CI | satisfied |

**Optional:** the owner may ask the auditor for a short confirmation that these three conditions are closed. It
is not required by the roadmap gate.

### Commit mapping (public history)
The D-17 implementation and governance commits were rebuilt before their first push, to withhold sealed-holdout
metadata from two evidence inventories. Earlier records cite the local, never-published SHAs:

| Local SHA (never published, kept privately) | Published SHA | Difference |
|---|---|---|
| `60f5ce4` | `b24f41ea679f0b9900163999156b0d5037aff7ab` | the two inventory JSONs drop the 45 `SEALED:` entries (1,940 → 1,895); new [redaction note](tasks/M2-D17-evidence/inventory_real/README.md); one evidence row of the promotion plan reworded; `d17_promote.py` byte-identical |
| `f18eb9f` | `9f908d996cbc4b496adbf0869628a9b5f0b02130` | same governance delta |

## 4. Data-quality caveats carried by the acceptance (stated, not hidden)
1. **C-2.** SOLUSDT 2023-02-14 16:39 is a genuine source gap. It is absent from Binance's own archive, it is not
   repaired, and any range containing it is grade X.
2. **2023-03-24 Binance spot halt.** It is a registered interruption under ADR-0013 (D-10, D-10A): observed
   zero-trade candles run 11:28–12:38 and absent minutes run 12:39–13:59. No fills happen there (D-12).
3. **No independent re-import parity** for canonical BTC 2025 and for ETH, SOL, DOGE and XRP 2022–2023 (O-5 and
   O-6 not run). The checksum of the source archives is still verified at import.
4. **C-1 repair.** SOL 2022-12 and XRP 2025-04 were repaired by D-17. Research must pin the new composite dataset
   ids:
   - SOL `3e2f33e9…f8a`;
   - XRP `cf1f67e9…acc`.

   The `gaps` field of those composite manifests is inherited and not recomputed: it still lists the repaired
   minute. `LakeReader` is correct; that field must not be read as ground truth. The old ids still reproduce the
   original one-minute gap when pinned.
5. **QA coverage.** Nine of the owner batches have version-1 QA reports only, and cross-checks cover 2 of 24 months
   per batch. These batches are not canonical (D-15 deferred), so this caveat does not apply to the canonical data
   in scope; it bounds what B01–B12 can be used for.
6. **D-18 qualifications.**
   - Parquet file SHA-256 values differ while content hashes match; the cause is unknown.
   - Some E-2 mutants ran on a prior revision.
   - The final O-7 tool changes were not re-reviewed.
7. **Unpinned dependencies.** `uv.lock` is not committed, so each CI job resolves its dependencies at run time.
   The cross-OS hashes are reproduced within one run, not pinned against future dependency releases.
8. **No backtest run recorded** on real data (validation plan blocker 10). The M2 gate does not require one; MVP
   AC-06 does.
9. **Hardware.** The WHEA hardware investigation on the owner's machine is open. CI evidence is independent of it;
   local timings are not.

## 5. Deferred work (not part of this acceptance)
| Item | Where it goes |
|---|---|
| O-5 / O-6 parity re-import (108 months) and E-4(b) part-hash script | optional hardening, or an M4 prerequisite if the owner prefers |
| B01–B12 decisions (D-15), QA v2 for nine batches, cross-check tiers A/B | M4 prerequisites |
| Top 10 symbols, USD-M, mark price, funding (O-9) | M4 prerequisites |
| First real-data backtest run | M4 / MVP AC-06 |
| Recompute or annotate composite `gaps` metadata | M4 hygiene |
| Committing `uv.lock` | separate decision (pinned reproducibility) |

## 6. Proposed owner sign-off text (S7)
> I, the owner, accept milestone M2 (Historical data + backtest core) of Trading System Claude, **limited to**
> Binance Spot 1m canonical data for BTCUSDT, ETHUSDT, SOLUSDT, DOGEUSDT and XRPUSDT from 2022-01-01 to
> 2025-12-31, and to the M2 code at commit `9f908d996cbc4b496adbf0869628a9b5f0b02130` (CI run `42322235295`,
> 7/7 jobs green on Ubuntu and Windows).
>
> I accept it with the data-quality caveats in §4 of `docs/M2_ACCEPTANCE_RECORD.md`, and with the Causality
> Auditor's CONDITIONAL PASS of 2026-11-06, whose conditions are recorded as satisfied in §3.
>
> This acceptance does not cover:
> - the 2026 holdout;
> - other symbols;
> - USD-M, mark price or funding;
> - the B01–B12 batches;
> - any strategy, statistical or performance claim.
>
> It authorizes M4 research only on pinned dataset ids within this scope, and only after M4's own prerequisites
> are met.
>
> Signed: ____________  Date (UTC): ____________

## 7. Sign-off log
| Date | By | Decision | Reference |
|---|---|---|---|
| — | — | not yet signed | — |
