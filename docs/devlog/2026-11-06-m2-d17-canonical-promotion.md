# M2 D-17: real canonical promotion and M2 global causality audit (2026-11-06)

> Factual record of the authorized D-17 `apply` against the real canonical lake, its independent
> post-apply verification, and the independent M2 global Causality Auditor review that followed.
> **Not formal M2 acceptance** — see [`DECISIONS_PENDING.md`](../DECISIONS_PENDING.md) (D-17) and the
> [closure checklist](../M2_CLOSURE_CHECKLIST.md) for the remaining open steps.

## Status
- **Authorization:** owner, 2026-11-06, in the session transcript, for exactly two months (SOLUSDT
  2022-12, XRPUSDT 2025-04 — the C-1 defect). The journal's `approval_ref` field
  (`"D-17 owner approval 2026-11-06"`) is a free-text label copied verbatim into the evidence; it is
  **not a cryptographic signature**, and it is not what establishes the authorization — the owner's
  written instruction is.
- **Implementation checkpoint:** `60f5ce4` on `task/m2-closure` (uncommitted beyond this record; not
  pushed). Five independent reviews on the tool closed B1–B3, M1, F1–F8, N1–N6 and N7 in turn; the fifth
  gave PASS on the N7 fix and its documentation changes.
- **Preflight:** a ten-point, read-only operational preflight ran immediately before the apply (no
  process touching the lake; `InstanceLock` free, tested without creating it; the four part SHA-256
  values still matched the reviewed plan; no catalog or `.staging` surprise; sufficient disk space and
  AC power; the C-1 evidence intact). All ten: GO.
- **Execution:** one `apply`. Journal: `begin → intent/done ×4 → data_committed → catalog_done (no
  catalog existed on the real lake) → complete`. `status`, called again afterward, repeats the same
  result.
- **Not done:** any other symbol or month, the genuine SOL 2023-02-14 gap (C-2), B01–B12 (D-15), a
  catalog build, holdout access, or any commit/push/merge/rebase.

## Result
Operation id: `45d9e72abf9c43458370a0b0a7e9e647`. Journal state: `COMPLETE`, `accepted: true`.

| | SOLUSDT 2022-12 | XRPUSDT 2025-04 |
|---|---|---|
| Rows | 44,639 → 44,640 | 43,199 → 43,200 |
| Added minute (UTC) | 2022-12-21T15:23:00Z | 2025-04-03T17:24:00Z |
| New part, `file_sha256` | `84b034b7d54c9e8d49b322e473ff62d63c711a3a13d10e27ef7ad35655604d6e` | `8ea7e07d047e4586b931e8e7d3ce363ad3745d7f481abdb8eb668abf2a5102c8` |
| New composite dataset id | `3e2f33e9e04627acb6dbc903546ff84faf648c8ac844f6146ecdb01ceea34f8a` | `cf1f67e941c0eb8b7ec7e99b11608667ff650fcf1982eff101963030a1159acc` |
| Superseded dataset id (unchanged, still readable) | `fff8e3f6138ef64cc4a50b63329011d890809b5fe039e5a70092a8257ddf78f0` | `629c732853c8ba90fbb3bc2f083f46f490bc29fe202dc72f4491ccd0f6c9f9f5` |
| Old part, `file_sha256` (untouched) | `d2f48000bc0cf44bc1269a03ecc0086a585c998eece6e1626814a6229a781361` | `00004d8ad827efc2b3d2b301ad849916eaf796fbf761614bbe59b746a3bfc763` |

Exactly four canonical objects added (two replacement parts, two composite manifests). A full inventory
diff of the whole data folder (1,940 → 1,944 entries) shows zero removed and zero changed among the
pre-existing ones.

The C-1 verification evidence bound into the two new manifests' `supersedes.bound_evidence_sha256` is
tied by SHA-256 (`a85427c0…`, `62132ccd…`, `9126a310…`); these were independently re-hashed against the
source files after the apply and matched, rather than trusted from the journal alone.

## Independent post-apply verification (not the D-17 tool's own postcondition call)
A fresh `Lake`/`LakeReader` was built from scratch (not the instance the tool itself used) and re-checked,
after the apply:
- unpinned and new-composite-pinned reads: exactly `new_rows`, no duplicate `open_time_ms`, no missing
  minute, no conflict, for both months;
- old-dataset-id-pinned reads: exactly `old_rows`, exactly one missing minute (the original gap), for
  both months — historical pins and any prior research reproducibility are unaffected;
- a row-level check (pyarrow, reading the new SOL part directly): the added row carries
  `source: "archive"`, `trades: 12`, `volume: 300.62` — a real, low-liquidity market observation, not a
  synthesized fill;
- the full inventory diff above;
- `lake_sealed` modification times: nothing under it changed (checked by `stat` only; its content was
  never opened or decoded, at any point in this operation or its review).

## Composite manifest `gaps` metadata — not authoritative for the repaired minutes
The two new composite manifests inherit their `gaps` list from the dataset they supersede and are **not**
recomputed (`qa_summary_note: "not recomputed for the composite; see supersedes and the source dataset
QA"`). Both still list the just-repaired minute as a gap, even though the part embedded in that same
manifest now has 44,640 / 43,200 rows. This does **not** affect `LakeReader`, which recomputes `missing`
from the actual parts (confirmed above: 0 missing on the repaired minute, both unpinned and pinned). It
is documented in [the D-17 promotion plan](../tasks/M2-D17-promotion-plan.md) so that **any future M4
code pins the new composite dataset ids explicitly and never reads a composite manifest's `gaps` field as
ground truth for these two months**. The genuine SOL gap (2023-02-14 16:39, C-2) is unaffected and
correctly remains a gap in both the manifest and the reader.

## M2 global Causality Auditor review — CONDITIONAL PASS
An independent, fresh-context review of the whole M2 acceptance gate
(`docs/ROADMAP.md`: *"look-ahead canaries, golden trades, ledger property tests, window-parity test,
cross-OS reproducibility hash — all green; Causality Auditor sign-off"*), re-verified afterward by the
implementing session (CI `headSha`, `DECISIONS_PENDING.md`/`metrics.csv` absence, the `seed=13073`
property, the `gaps` field above, the sealed-source guard and its missing test, `data/runs` absence):

| Gate | Evidence | Result |
|---|---|---|
| Look-ahead canaries | `test_m2t4_causality_audit.py`, `test_sim_causality.py`, `test_feat_parity_canaries.py` | 151 passed |
| Golden trades | `test_sim_golden.py` | included above, PASS |
| Ledger property tests | `test_paper_properties.py`, `test_sim_property_regressions.py` | 100 passed, 1 skipped (symlinks unavailable in this environment, not a failure) |
| Window-parity | `test_feat_parity_canaries.py` | included above, PASS |
| Cross-OS reproducibility hash | CI `42275943593` (both OS, `success`) on `b1da844`; the two pinned-hash tests re-run locally on `60f5ce4` (Windows): 2 passed | Green on `b1da844`; `60f5ce4` not independently confirmed on Linux |
| Causality Auditor sign-off | This review | First one recorded for M2: **CONDITIONAL PASS** |

No causality, look-ahead, determinism or data-quality defect was found anywhere in scope.
`HoldoutGuard` remains fail-closed by design; no 2026 content was opened or decoded by the review or by
this reconciliation.

**Stale entry found and corrected the same day:** the validation-plan blocker "simulator restore property
fails on two classes" (`904b84c` partial fix, `seed=13073` reproduced) is resolved on this branch — its
completing fixes (`0435116`, `e87fe59`, `d1e280a`) are already ancestors of `60f5ce4`.
`tests/unit/sim/test_sim_property_regressions.py -k 13073`: 8 passed. The full property suite
(`test_sim_property_regressions.py` + `test_paper_properties.py`): 79 passed, 0 failed. A documented
residual (≈0.05%, 3 of 6,000 random cases, absent an explicit `start_ms`) is unchanged and already pinned
by its own regression test; it was never part of this blocker and is not new.

**Test-coverage gap found and closed the same day:** `_src_monthly`'s existing rejection of a sealed or
`year >= 2026` source part (`d17_promote.py`, the check right after `PartInfo.from_dict`) had no direct
regression test — the guard itself was not changed, only exercised. Added:
`test_source_part_from_the_sealed_holdout_period_is_rejected`
(`tests/owner_checks/test_d17_promote.py`), built entirely from freshly generated synthetic rows; no real
holdout data is read.

## Open conditions (governance/traceability, not causality)
1. This devlog entry and the `DECISIONS_PENDING.md` / `M2_CLOSURE_CHECKLIST.md` / roadmap /
   validation-plan updates that accompany it — previously the only recorded gap.
2. Whether the owner requires pushing `task/m2-closure` (D-07) so CI covers `60f5ce4` on both OS.
   Currently only `b1da844` (the prior commit) has a CI run; D-17's diff does not touch `src/`.
3. Validation-plan blockers resolved by this entry: #1 (C-1), #6 (promotion procedure), #7 (seed 13073,
   stale), #9 (Causality Auditor sign-off). Blockers #2–5, #8 (partially), #10, #11 are unchanged.

This entry, the `DECISIONS_PENDING.md`/`M2_CLOSURE_CHECKLIST.md`/roadmap/validation-plan edits, and the
new regression test are the proposed commit scope for this reconciliation. Nothing has been committed.
