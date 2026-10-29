# Devlog — 2026-10-28 — M2-E3: importer integrity safeguards (C-1 prevention)

- **Authorization:** owner, 2026-10-28: E-3 only, in an isolated worktree, uncommitted, while O-4 runs.
- **Worktree:** `C:\Users\felip\Projects\tsc-e3`, branch `task/m2-e3`, from `ebefbc1`.
  - The owner's main tree (`feat/mvp-4-dashboard`, with uncommitted E-0/E-4(a)) and the running O-4 capture
    were not touched.
- **Not done, and not authorized:** E-1, E-2, imports, live calls, canonical repair (D-16) and promotion
  (D-17). The import log from the brief was **omitted**, because evidence folders must not be modified.
- **Status:** uncommitted, pending owner review. No merge or push.

## What changed
| File | Change |
|---|---|
| `src/tsclaude/marketdata/archive/provenance.py` (new) | `code_provenance()`: the tsclaude version, the package path, the **git commit and dirty state of the directory the package was loaded from** (not the process working directory, so imports that run another worktree's code are attributed correctly), and the Python version. Unknown values are `None`. Cached per process |
| `src/tsclaude/marketdata/archive/importer.py` | (1) Per archive file, `source_data_lines()` re-decompresses the **verified** zip independently and counts non-blank data lines (header excluded); the count must equal parsed + malformed rows, else `ArchiveImportError`. (2) Per month, `_check_month_accounting`: parsed = kept + identical duplicates + differing duplicates + outside the request; kept = valid + rejected; staged rows = valid. (3) The manifest gains `code_provenance`, and each archive source record gains `source_data_lines`. The `tool` field is unchanged |
| `src/tsclaude/store/lake.py` | `Staging.commit` calls `verify_staged()` before publishing: file hash, row count and content hash of each staged part, re-read. `publish` re-reads each final file (`_check_part_content`), including an existing part reused by content hash. Any mismatch raises `LakeError`; no manifest is written |
| `tests/unit/archive/test_arc_import_integrity.py` (new) | 13 synthetic tests (below) |

**Unchanged:** dataset ids (content hashes), QA policy and thresholds, csvparse, holdout sealing (sealed parts are
re-read only inside `store`, like `verify`), `contracts/`, the simulator, research, data and evidence.

## Tests (focused; worktree code via `PYTHONPATH=<worktree>/src`)
- **New (13, all pass; the 13th is the review B-1 fake-header case):**
  - **C-1 shape:** a row lost after parsing (line 3925, like XRP 2025-04) makes the import fail with both counts,
    and nothing is committed.
  - **Lost newline:** a newline lost in the first decompression (two records merged, parsed as one valid row)
    makes the import fail.
  - **Merge and staging:** a row lost while merging, and a row lost while staging, both fail with the exact
    identity.
  - **Malformed rows and headers:** a truncated candle plus a header pass, with `source_data_lines` = rows + 1
    and the header excluded.
  - **Line counting:** blank, comma-only and header lines are handled.
  - **Staged part:** a staged part changed after writing is never published.
  - **Faulty read path:** a codec that reads back fewer rows makes the publish fail.
  - **Corrupted existing part:** an existing canonical-like part with different content is not silently
    adopted.
  - **Provenance:** the manifest's provenance equals `git rev-parse HEAD` of the package directory; git missing
    and non-checkout cases give `None`.
- **Mutation check:** disabling the file reconciliation, the month accounting, `verify_staged` and the final
  re-read makes **7 tests fail**; restored, all 12 pass.
- **Regression:**
  - before the new tests: `tests/unit/archive` + `tests/integration/archive`, 89 passed, 1 skipped (symlinks not
    permitted on this machine);
  - after them: integrity, import, import-memory and lake e2e, 49 passed.
- **Lint and types:** `ruff check` and `ruff format --check` clean on `src` and `tests`; `mypy src` shows no
  issues in 115 files.

## Review
**Independent code review: changes required, 1 blocker, fixed; re-check approved.**
- **B-1:** the "independent" line count trusted the parser's header flag. A parser that dropped the first row and
  called it a header would have committed 44,639 rows silently (reproduced by the reviewer).
  - **Fix:** `source_data_lines` detects the header itself (`csvparse.KNOWN_HEADERS`), and the import fails if
    that disagrees with the parser.
  - **Regression test:** `test_a_first_row_dropped_as_a_fake_header_fails_the_import`. It fails when the check is
    disabled.
- **Non-blocking items fixed:**
  - the line count is streamed (`zf.open`);
  - the docstring explains that disagreements can only fail, never pass silently;
  - HEAD is trusted only if the package `__init__.py` is tracked;
  - untracked files under the package are counted;
  - `git --no-optional-locks`, so the owner's git index is never locked;
  - provenance is resolved in `Importer.__init__`, outside the event loop;
  - library versions (pyarrow, duckdb, httpx) are recorded;
  - the publish re-read covers only pre-existing files (staged parts are re-read in `verify_staged`), and the
    error carries a repair hint;
  - `git_dirty` is cross-checked in tests against `git status`, the test skips explicitly without git, and the
    corrupted-part test asserts that no manifest exists.
- **Accepted as is:**
  - `git_dirty` covers tracked files; untracked package files are a separate field;
  - ignored files are not counted;
  - the provenance cache lasts the process lifetime.
- **Final focused results:**
  - integrity: 13 passed;
  - import, guard and lake e2e: 49 passed;
  - memory and CLI: 5 passed;
  - ruff and mypy clean.

## Resumption checkpoint (if a session ends before review is complete)
1. The worktree `C:\Users\felip\Projects\tsc-e3` holds all E-3 changes, uncommitted (`git -C ../tsc-e3 status`).
   Pre-change copies of `importer.py` and `lake.py`:
   `C:\Users\felip\AppData\Local\Temp\claude\C--Users-felip-Projects-trading-system-claude\bak\e3\`.
2. Re-run (low CPU):
   `cd ../tsc-e3; PYTHONPATH=<abs>/src ../trading-system-claude/.venv/Scripts/python.exe -m pytest tests/unit/archive/test_arc_import_integrity.py -q -p no:cacheprovider`.
3. Remaining: apply the review findings; then the owner reviews. A local commit needs owner approval. Merging
   `task/m2-e3` into the MVP branch line is a separate approval.
4. The owner's main tree (`feat/mvp-4-dashboard`) has the uncommitted E-0/E-4(a) work (O-4 tool, 24 tests,
   docs). It is independent of this worktree.
