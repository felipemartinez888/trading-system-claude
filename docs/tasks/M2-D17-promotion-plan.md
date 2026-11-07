# M2 D-17: minimal, additive repair of canonical XRP 2025-04 and SOL 2022-12

- **Status (updated 2026-11-06): executed.** The owner authorized, in writing, exactly this repair, and the tool
  applied it once against the real canonical lake: op id `45d9e72abf9c43458370a0b0a7e9e647`, `COMPLETE`,
  `accepted: true`. See [DECISIONS_PENDING D-17](../DECISIONS_PENDING.md) and
  [the execution and audit devlog](../devlog/2026-11-06-m2-d17-canonical-promotion.md) for the full result,
  independent post-apply verification and the M2 Causality Auditor's CONDITIONAL PASS. Everything below this line
  describes the plan as reviewed before that apply; it is kept for the record and because it still describes the
  tool's behaviour accurately. Rolling back, or running the tool again for any other symbol or month, still needs a
  separate, explicit written owner authorization.
- **Tool:** `tests/owner_checks/d17_promote.py`, regression tests `tests/owner_checks/test_d17_promote.py` (synthetic
  lakes only; 80 tests, run together with the 3 tests of `tests/unit/archive/test_arc_import_guard.py`: 83). No
  promotion mechanism existed (E-5 was never designed); this is the smallest one. It uses the existing `Lake`,
  `LakeReader` and `InstanceLock` APIs; no product code changed.
- **History:** the first version was BLOCKED by an adversarial review (B1 no crash recovery, B2 unsafe rollback,
  B3 no instance lock, M1 premature publication). The second version addressed all four. A fresh review gave it a
  CONDITIONAL PASS with conditions F1-F8: power-loss durability, ownership-proof tests, the no-catalog path, debris,
  ordering and wording. A third independent review closed F1-F8 and gave a CONDITIONAL PASS with one medium condition,
  N1: the evidence written before `begin` was not durable, and unreadable evidence crashed instead of refusing. It also
  listed low items N2-N6 (untested guards, catalog-path wording, a double journal tear, a stray lock folder). This
  version fixes N1, adds the N2 tests, and corrects or documents N3-N6 (see "Remaining limitations").
  A fourth independent review confirmed N1-N6 and gave a CONDITIONAL PASS with one new journal finding, N7 (a whole
  record whose line end was lost merged with the next record; after two interruptions of a rollback, `status` could
  report COMPLETE / accepted while one symbol was already unrepaired), and low items N8-N10 (manifest visibility
  wording, recovery sources, a non-UTF-8 journal tail) plus a nuance of N6. This version fixes N7 with its
  regression tests and documents N6 and N8-N10. A fifth, narrow review confirmed the N7 fix and its tests and asked
  for six text corrections, applied here. The owner then authorized and the tool executed the single real apply
  described in the "Status" line above; a sixth, fresh-context review (the M2 global Causality Auditor) covered it
  as part of the full M2 acceptance gate and gave CONDITIONAL PASS, finding no causality or data-safety defect and
  one test-coverage gap (the existing sealed/2026-source rejection had no direct regression test), closed the same
  day with one new synthetic test; see the devlog above.

## Evidence checked on current bytes (read-only)
| Item | XRPUSDT 2025-04 | SOLUSDT 2022-12 |
|---|---|---|
| Source ZIP SHA-256 | `a3619422…8590` = Binance CHECKSUM = canonical manifest = B08 manifest | `c0437752…a125` = Binance CHECKSUM = canonical manifest |
| Source rows / minutes | 43,200 / 43,200, no duplicates, close = open + 59,999 | 44,640 / 44,640, same |
| New part rows, `content_hash` | 43,200, `5301f963…600c70` | 44,640, `7bd1ab63…3932c149` |
| Independent parity | `content_hash` identical to B08's 2025-04 part (AP-1 build `7be73eb`) | none exists (single import) |
| `data verify` / `c1_verify` | ok / findings `[]` (owner) | ok / findings `[]` (owner) |
| Old canonical part | 43,199 rows, `25ee3d07…` | 44,639 rows, `cea7758d…` |
| `plan` on the real lakes (read-only, tool as of 2026-11-06T20:58Z, run 2026-11-06T21:00:25Z; [`M2-D17-evidence/plan_real/plan.json`](M2-D17-evidence/plan_real/plan.json); inventory of `data\` taken at 21:00:23Z and 21:00:27Z, identical, 1,940 entries (original, kept private); the published copy is a redacted derivative with the 45 sealed-2026-holdout entries withheld, 1,895 entries, still identical before/after, SHA-256 `754744a2…8899` — see [the redaction note](M2-D17-evidence/inventory_real/README.md): [`before`](M2-D17-evidence/inventory_real/inventory_before_plan.json), [`after`](M2-D17-evidence/inventory_real/inventory_after_plan.json)) | old part ⊂ new part, all value columns equal, exactly one added minute `2025-04-03T17:24:00Z` | same, `2022-12-21T15:23:00Z` |
| Composite dataset id (would be created) | `cf1f67e9…159acc` (supersedes `629c7328…`) | `3e2f33e9…ea34f8a` (supersedes `fff8e3f6…`) |

File SHA vs content hash: `ingested_at_ms` is written into the Parquet file but excluded from `content_hash`
(`EXCLUDED_FROM_HASH`); likely cause of D-18 qualification 3. Hypothesis from the code, not measured.

## Lake model (why a file copy is the wrong tool)
Parts are named by content hash; manifests are immutable and named by the hash of their parts; `Lake.index()` (hence
`LakeReader` and the catalog) sees only parts referenced by a manifest in `imports/<64-hex>/`. The repair is additive:
no existing file is overwritten and old dataset ids keep working. The only exception is the derived `catalog.duckdb`,
and only if one exists. The real `data\` has none today (checked read-only), so on the real lake no catalog is created
or modified.

## Objects the operation creates (the only ones its rollback may delete)
| Object | Where | Ownership proof used by recovery/rollback |
|---|---|---|
| P_SOL, P_XRP: replacement parts | `lake/…/part-<hash32>.parquet` (new names) | announced in the journal before creation, absent in `inventory_pre.json`, and SHA-256 equal to the planned bytes. *Incomplete* (power loss) is allowed only if there is no `done` record and the file does not even read back as the planned content |
| C_SOL, C_XRP: composite manifests | `imports/<composite id>/manifest.json` | announced in the journal, absent before, `d17_op_id` and `dataset_id` match, SHA-256 equal to the journaled `done` value. *Incomplete* is allowed only if unreadable, with no `done` record, in a directory the operation created |
| `.staging/d17-<op>/`, and `.staging/` if it was absent | data root | removed after publication and by rollback (`.staging/` only if empty) |
| `catalog.duckdb`, **only if one existed before** | data root | backup in the evidence folder. Forward: rebuilt if its SHA is still the pre-operation one or its `datasets` table equals the pre-existing datasets plus the two composites (a racing import before the rebuild is not detected here; see limitations). Rollback: restored only under the same `datasets` condition. With no catalog before (the real lake), none is created and rollback never touches one (tested) |

Anything else refuses before any change, for example:
- a valid part with the planned content but other bytes, which is another import's file;
- an object recorded as pre-existing;
- an object without an intent;
- a composite with another `d17_op_id` or edited after `done`.

M1: the monthly source datasets are **not** published into `imports/`; their manifest and QA are copied to
`<evidence>/source_evidence/` and bound by SHA-256 in each composite's `supersedes` (with optional `--bind` files, e.g.
the `c1_verify` and `verify` JSONs).

## Stages, atomicity and visibility
| Stage | Atomic unit | Visible to `LakeReader` / catalog after it | Journal state |
|---|---|---|---|
| begin (plan, inventory, catalog backup if any, evidence) | none (evidence only) | nothing | IN_PROGRESS |
| publish P_SOL, P_XRP (fsync, then rename) | each `os.rename` | nothing (unreferenced parts are invisible) | IN_PROGRESS |
| commit C_SOL (manifest published by `os.replace`, then fsynced, then `done`) | `os.replace` of one manifest | SOL repaired for unpinned reads; XRP not. A power loss before the fsync may leave the manifest empty but present: every reader then fails until `recover`/`rollback` (N8) | IN_PROGRESS |
| commit C_XRP | `os.replace` of one manifest | both repaired | IN_PROGRESS |
| postconditions + preservation pass | — | same | DATA_COMMITTED |
| catalog: rebuilt if one existed, otherwise skipped (real lake) | `os.replace` / none | exploration views updated / no catalog | CATALOG_DONE |
| postconditions + preservation pass | — | same | **COMPLETE (only accepted state)** |

Order: items follow the source dataset ids, which on the real plan puts SOL (`217c…`) before XRP (`4d3f…`).

**Each object is published atomically; the two-symbol repair is not.** The lake has no single pointer across
manifests, so between the two composite commits one symbol is repaired and the other is not (an interval of two
consecutive local writes, but unbounded if the process dies there). The enforced acceptance boundary is the journal's
`complete` record, written only after both symbols pass every postcondition: `status` reports `accepted: false` in every
other state, and a second `apply` refuses while a composite exists.

Postconditions (both symbols): the new part reads back with its content hash; the composite id equals the hash of its
parts and carries this `d17_op_id`; `LakeReader` over the month returns exactly 43,200 / 44,640 rows, all distinct, no
missing minute, no conflict, both unpinned and pinned to the composite; every file in `inventory_pre.json` is
unchanged (catalog excepted; `lake_sealed` by size and mtime only).

## Durability and recovery
- `<evidence>/journal.jsonl`: every step is appended and fsynced **before** the change it announces.
  - A torn last record (power loss mid-write) is ignored. The next append detects it by parsing, not by the last byte,
    and seals it with a `torn_sealed` marker. Any other corruption refuses.
  - A whole record whose line end was lost (N7) is kept: the next append first ends its line, so the two records never
    share a line (before the fix both were dropped as a torn tail). Tested for a lost part `intent` and for a lost
    `rollback_begin` followed by a second interruption; in both cases the state never reads COMPLETE / accepted
    while the repair is incomplete (after the first case, `recover` legitimately finishes it to COMPLETE).
  - The staged part is fsynced before the rename that makes it reachable. A manifest is different (N8):
    `commit_dataset` publishes it with `os.replace` and the tool fsyncs it right after, before its `done` record. A
    power loss in that window can leave an empty manifest that `Lake.index()` reads: every lake reader (research
    included) then fails with a JSON error until `recover` redoes it or `rollback` deletes it. Fail closed, never
    silent; tested by emptying a written manifest.
  - The evidence recovery relies on is fsynced before the `begin` record: `plan.json`, `inventory_pre.json`, the source
    evidence and the catalog backup. A missing or unreadable `inventory_pre.json` makes `recover` and `rollback` refuse
    cleanly with nothing changed (tested).
  - Directory fsync is unavailable on Windows, so recovery never trusts the journal alone: it re-checks every object by
    hash. An object of ours left torn by a power loss is *incomplete* (see the table above). `recover` redoes it and
    `rollback` deletes it; every other mismatch refuses.
- `recover` finishes an interrupted apply from whatever the journal and the bytes show, and is idempotent. It re-copies
  a part from `source_part_abs` in the plan, i.e. from `data_c1_check` (N9): that folder must be kept, unchanged,
  until `status` says COMPLETE. If it is missing, `recover` stops with a file error and nothing visible to readers
  changes, but it may leave an empty `.staging/d17-<op>/<uuid>/` and new journal records (an `intent`, or an
  announced deletion of an incomplete part of ours). A later `recover` or `rollback` removes the staging folder; the
  journal is append-only, and those records stay and are harmless. A changed file
  fails the SHA check. `rollback` does not need it, but needs the evidence folder (`--out`): keep it for as long
  as a rollback may be wanted.
- The source of truth is `status` (the journal), not the `COMPLETE.json` file: a crash between the two leaves
  CATALOG_DONE with `accepted: false`, and `recover` finishes it. This was probed by the fourth review in its scratch
  folder; there is no test for it in this repository. Simulated
  crashes are tested at each of the 8 injection points, on lakes with and without a catalog:
  - before part publication;
  - after the rename but before `done`;
  - after `done`;
  - between composites;
  - after the manifest write but before `done`;
  - before DATA_COMMITTED;
  - before and after the catalog.

  Power loss is simulated by zero-filling a renamed part and by emptying a written manifest, with a leftover
  `commit_dataset` temp file. Real power loss and OS caches are **not** tested.
- A catalog failure (for example the file is open) leaves DATA_COMMITTED: data is repaired, the repair is not accepted,
  `recover` retries only the catalog.
- **How to identify an incomplete promotion:** `d17_promote.py status --out <evidence>` shows a state other than
  COMPLETE; in the lake, a manifest carrying `d17_op_id` without a matching COMPLETE journal.

## Rollback
- Works from any state (partial, DATA_COMMITTED, COMPLETE, or an interrupted rollback) using the journal.
- **All preconditions are checked before the first deletion:**
  - every present object passes its ownership proof;
  - no other manifest in `imports/` references an owned part (a later import may reuse the content-addressed file), and
    an unreadable foreign manifest refuses;
  - nothing under `<data>/runs/` mentions a composite id;
  - every file in `inventory_pre.json` is unchanged;
  - if a catalog existed, it is either the pre-operation one or provably the rebuild that includes the composites, and
    the backup matches its pre-operation SHA.
- Deletion order:
  1. owned objects, composites first (the repair disappears atomically per symbol);
  2. `commit_dataset` temp files inside composite directories the operation created;
  3. parts;
  4. directories it created, only if empty;
  5. its staging;
  6. only if a catalog existed and changed, a restore of that catalog.

  Each deletion of an owned object and the catalog restore is journaled first (`intent_delete`,
  `intent_restore_catalog`); a rerun after a crash completes it.

  Three cleanup steps are not journaled, and are idempotent:
  - the removal of `commit_dataset` temp files (`.manifest.json.*.tmp`, only inside a composite directory the operation
    created);
  - the operation's own `.staging/d17-<op>/`;
  - `rmdir` of empty directories.

  It ends by re-checking that every pre-existing file is unchanged.
- It refuses, without changing anything, if any precondition fails: shared part, pinned run, changed pre-existing
  file, externally changed catalog, tampered or foreign owned path. Regression tests cover:
  - a foreign file inside a directory the operation created, which is kept along with the directory;
  - a foreign file of another import with the same content at the part path;
  - a part or composite without intent;
  - another `d17_op_id` or `dataset_id`;
  - an edit or emptying after `done`;
  - a path recorded as pre-existing;
  - an unreadable foreign manifest;
  - an incomplete part that another manifest references.

  Mutation run in scratch copies: 30 mutants of these checks, the lock, postconditions, fsync order, the evidence
  handling and the N7 journal repair, on the current files: 30 of 30 killed. Log, with their SHA-256:
  [`M2-D17-evidence/mutation_log.txt`](M2-D17-evidence/mutation_log.txt) (the previous version's run:
  [`mutation_log_v3.txt`](M2-D17-evidence/mutation_log_v3.txt)).

  The reviewers' own runs found mutants that are not killed:
  - redundant final re-checks;
  - the copy SHA check;
  - the pinned-only postcondition read;
  - the catalog-path checks of limitation 6;
  - removing an fsync call, the inventory type check, or the `--canonical` realpath check (fourth review);
  - removing `apply`'s pre-check that no target object exists yet (X14), and accepting COMPLETE without a
    `catalog_done` record (X16) (fourth review).

  Those are defence in depth, catalog-only, or untestable without real power loss; no test claims them (limitation
  12).

## Concurrency
`apply`, `recover` and `rollback` hold the app's `InstanceLock` on `<data>/tsclaude.lock` (the same contract as
`api/server.py`: exclusive, non-blocking, released by the OS if the process dies) and refuse while the app, the paper
session or another D-17 operation holds it. **No other `tsclaude` command takes that lock** (only `tsclaude app` does,
`api/server.py:100`). That includes `data import`, `backtest run`, `data catalog` and `data verify`, so none of them
may run on `data\` during the operation (operator rule). A racing import that changes values is detected by the
postconditions and blocks acceptance (tested), but it is not prevented. `plan` and `status` are read-only and take no
lock.

## Reader and catalog consequences after COMPLETE
- **Research path (M4):** `LakeReader` deduplicates by content hash and by minute; identical rows merge (`_same` ignores
  `ingested_at_ms`/`source`). Unpinned and composite-pinned reads return each minute once, gap closed, no conflict.
  Code outside `store/` cannot use duckdb or the lake internals (`test_arc_import_guard.py`), so the research path cannot
  reach the catalog.
- **Catalog views (exploration only, non-canonical):** the real lake has no catalog and the operation creates none.
  If one is built later (`tsclaude data catalog`), its views list every indexed part file. The `bars_1m` view would
  then show 43,199 XRP and 44,639 SOL minutes twice (old and new part), the first overlap in canonical `bars_1m`.
  Ad-hoc SQL over `catalog.duckdb` must deduplicate by `(symbol, open_time_ms)` or filter by dataset.
- **Pins for M4:** use the composite ids explicitly (`cf1f67e9…`, `3e2f33e9…`). An unpinned run records the old,
  defective id as well (deterministic, but confusing); a run pinned only to the old id reproduces the old gap and
  reports it as missing.
- The composite carries no `qa_summary` (stated in the manifest); QA for the replaced month is the source dataset's
  QA v2 (in the evidence). A full-range QA would read the sealed 2026 range and is not proposed.
- **The composite manifest's inherited `gaps` list is not authoritative for the repaired minute (confirmed on the real
  apply, 2026-11-06).** It is carried over from the superseded dataset and is not recomputed
  (`qa_summary_note: "not recomputed for the composite; see supersedes and the source dataset QA"`); on the real lake
  both new manifests still list the just-repaired minute as a gap even though the embedded part now has it. `LakeReader`
  is unaffected — it recomputes `missing` from the actual parts, not from this field (verified: 0 missing on the
  repaired minute, both unpinned and composite-pinned). **Any M4 code must therefore use `LakeReader` with an explicit
  composite dataset pin for these two months, never read a composite manifest's `gaps` field as ground truth.** The
  genuine SOL gap (2023-02-14 16:39, C-2) is unaffected and correctly remains a gap in both the manifest and the reader.

## Owner commands (NORMAL; NOT run; each needs the stated approval)
Preconditions (operator preflight, checked just before `apply`, in PowerShell after
`$main='C:\Users\felip\Projects\trading-system-claude'`):
1. The app and the M3 paper session are stopped. The lock enforces this only if the app uses this same `data\` as its
   data folder; confirm that before stopping it (not verified by any review, which could not touch the session).
2. No other `tsclaude` command (import, backtest, catalog, verify) and no other D-17 process is running. This must
   print nothing:
   `Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'tsclaude|d17_promote|backtest' } | Select-Object ProcessId, CommandLine`
   (run it from a shell whose own command line does not contain those words).
3. `Test-Path "$main\data\catalog.duckdb"` prints `False` (otherwise the catalog path of limitation 6 applies; stop
   and ask).
4. `--canonical` is exactly `"$main\data"`, copied, not typed (limitation 8).
5. `--out` is a new folder outside `data\`, kept permanently; `data_c1_check` is not touched until COMPLETE (N9).
6. The machine is on mains power (or a UPS) for the few seconds of the operation.
```powershell
# 1) read-only plan, writes only the evidence folder (no approval needed)
cd C:\Users\felip\Projects\tsc-m2-closure
$main='C:\Users\felip\Projects\trading-system-claude'; $src='C:\Users\felip\Projects\data_c1_check'
uv run --frozen python -I tests\owner_checks\d17_promote.py plan --canonical "$main\data" --source $src --out C:\Users\felip\Projects\d17_evidence_plan
# 2) ONLY after written D-17 approval: apply (binds the C-1 evidence by SHA-256)
uv run --frozen python -I tests\owner_checks\d17_promote.py apply --canonical "$main\data" --source $src --out C:\Users\felip\Projects\d17_evidence_apply --approval-ref "D-17 owner approval <date>" --bind "$src\c1_XRPUSDT_2025-04.json" --bind "$src\c1_SOLUSDT_2022-12.json" --bind "$src\verify.json"
uv run --frozen python -I tests\owner_checks\d17_promote.py status --out C:\Users\felip\Projects\d17_evidence_apply   # must say COMPLETE
# if not COMPLETE: recover (same approval) or rollback (separate decision)
uv run --frozen python -I tests\owner_checks\d17_promote.py recover --canonical "$main\data" --out C:\Users\felip\Projects\d17_evidence_apply
```
Expected: `{"state": "COMPLETE", "accepted": true}`. Do **not** use `c1_verify` on the canonical lake afterwards: it
reads every Parquet file of the month folder, so the old and new parts together would be reported as duplicate rows.
The tool's own postconditions read through `LakeReader` (unpinned and composite-pinned) instead.

## Remaining limitations
1. No atomic visibility across the two symbols (see above); accepted state is enforced by the journal only.
2. Only `tsclaude app` takes the instance lock; excluding every other `tsclaude` command is an operator rule.
3. Windows has no directory fsync, so a power loss may lose a rename or a manifest replace that happened after its
   intent. Recovery treats a missing object as not done and redoes it.
   - A torn object of ours is classified *incomplete* and redone, or deleted by rollback.
   - Any state the tool cannot prove is its own refuses, both in `recover` and `rollback`. The owner must then
     inspect it by hand, and nothing is deleted.
   - Power loss is only simulated (zero-filled or empty files); OS-level behaviour is not tested.
4. Tests are synthetic (two symbols, small months, one sealed part); the real lake was only read by `plan`.
5. The tool was written by the implementing agent. Five independent reviews ran; the fourth confirmed N1-N6 and gave
   a CONDITIONAL PASS on N7. A fifth, narrow review of the N7 fix and these doc changes found no code or
   canonical-write blocker and asked for six text corrections, applied in this version. The review reports are kept in
   the session scratch folder, not in this repository.
6. Catalog path (not used on the real lake, which has no catalog): an import racing in before the forward rebuild ends
   up in the rebuilt catalog. The operation still reaches COMPLETE, and a later rollback then refuses to restore that
   catalog (fail closed, manual decision).
7. A second power loss that tears the journal's `torn_sealed` marker itself makes `jread` refuse as corrupt (fail
   closed, manual repair of the journal).
8. A mistyped `--canonical` makes `InstanceLock` create that folder and a `tsclaude.lock` file before the tool refuses.
   If it points at a subfolder of the real lake (`data\lake` or `data\imports`), the stray `tsclaude.lock` lands inside
   the canonical tree. Readers are unaffected (dataset ids and pre-existing entries do not change), but the file must
   be removed by hand. Precondition 4 avoids it.
9. N8: an empty manifest left by a power loss blocks every lake reader until `recover` or `rollback` (fail closed).
10. N9: `recover` needs `data_c1_check`; `rollback` needs the evidence folder. Both must be kept.
11. N10: a non-UTF-8 tail in the journal raises a decode error instead of a clean refusal; nothing is changed (fail
    closed).
12. Some guards are defence in depth with no test that kills their removal (fsync calls, the inventory type check, the
    `--canonical` realpath check, `apply`'s target-exists pre-check X14, COMPLETE without `catalog_done` X16); the
    fourth review listed them. Their behaviour was checked by inspection and probes.

## Decisions needed
1. **Done 2026-11-06:** written D-17 authorization for exactly these two additive repairs (B01–B12 untouched) —
   given, and executed; see the "Status" line above.
2. Acceptance of limitations 1-12 and of the operator preconditions above (now historical: they describe the apply
   that already happened).
3. Composite ids as the M4 canonical pins for XRP and SOL (the other three symbols keep theirs) — recorded as a
   recommendation in the 2026-11-06 devlog; still needs the owner's or M4's explicit confirmation when M4 starts.
