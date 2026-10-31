# M2-E2: zero-trade classification and the narrow QA open exception

> **Committed locally** on `task/m2-e2` (base `dc97551`), with owner approval. Nothing here is a claim of
> integration, formal M2 acceptance, statistical validation or hardware stability.

## Status
- **Authorization:** owner, 2026-10-30 (D-11, D-13, ADR-0015 safeguards approved as governing policy). The owner
  reviewed the final report and approved one local commit.
- **Not done, and not authorized:** O-5, O-6, O-7, M4 backtests, repair or promotion of historical data, paper
  migration or reset, merges, pushes, any access to the 2026 holdout.

| Stage | State |
|---|---|
| Code implemented | Yes, committed locally on `task/m2-e2` |
| Tests passed | Yes: 14 of 14 targeted batches on the final code. Earlier stops stay unexplained |
| Independently reviewed | Yes: code, provenance, causality and safety, and a final review of the last fixes |
| Locally integrated, formally accepted, statistically validated | No |

## What changed
- **`marketdata/archive/qa.py`:** QA report version 2. Z1, Z2, A and missing classification, the
  `leading_zero_trade_open` rule with its evidence, and holdout redaction of the new fields.
- **`marketdata/archive/importer.py`:** takes the interruption registry, classifies per month in bounded memory,
  and attaches evidence from the month's own archive.
- **`marketdata/archive/csvparse.py`:** `open_time_lines` for line-number evidence.
- **`marketdata/datacli.py`:** `--interruptions`, default `configs/exchange_interruptions.yaml`. A broken file, or
  a named file that is missing, refuses before any fetch.
- **`quality/interruptions.py`, `configs/exchange_interruptions.yaml`:** documentation only. The registry entry is
  unchanged. The comment edit changes the file's SHA-256 from `44801445...` to `f40c5fe5...`, which is what new run
  manifests will record. No pin of the old value was found in docs, tests, source or configs.
- **Reports:** `qa_report_version: 2` at the top level and in the summary; a report without the key is version 1.
  `qa_report.json` gains `exchange_interruptions`. The manifest's `qa_summary` gains the new keys for new datasets
  only. Dataset ids and the write-once path are unchanged, so existing reports stay as they are.

## The five real cases
From read-only Spot archives. Fixture `tests/fixtures/archive/e2_open_exception.json`, SHA-256
`4e41b590af2714b0acd0dedc2de24dbbae2e3fc79bfb92cfd37603c33a53da72`, extractor rerun byte-identical. All k=1, trades 0.

| Case | Filler | Native open | 1m line | 15m line |
|---|---|---|---|---|
| AVAX 2025-12-07 03:45 | 13.48 | 13.49 | 8866 | 592 |
| AVAX 2025-12-29 23:15 | 12.42 | 12.41 | 41716 | 2782 |
| AVAX 2025-12-30 18:15 | 12.55 | 12.56 | 42856 | 2858 |
| HBAR 2024-01-25 18:30 | 0.0712 | 0.0711 | 35671 | 2379 |
| HBAR 2025-12-14 03:45 | 0.12399 | 0.124 | 18946 | 1264 |

## Test evidence
### Final regression on the final code
**14 of 14 batches passed, each with exit 0: 296 passed, 1 skipped; ruff and mypy clean.** The owner ran the
remaining batches locally with the bounded runner. Each batch ran in its own process, one at a time, with its own
log and exit code. No test outside these batches imports the changed modules. The full repository test suite was
not run.

| Batch | Result |
|---|---|
| E-2 test file | 115 passed, on the retry after the crash recorded below |
| `test_arc_cli.py` | 1 passed |
| `test_arc_holdout_reader.py` | 21 passed, 1 skipped ("symlinks not permitted here") |
| `test_arc_import.py` | 28 passed |
| `test_arc_import_guard.py` | 3 passed |
| `test_arc_import_integrity.py` | 13 passed |
| `test_arc_import_memory.py` | 5 passed, including `test_zero_trade_month_starts_keep_the_bars_bounded` |
| `test_arc_paths_parse.py` | 16 passed |
| `test_arc_universe.py` | 7 passed |
| `tests/integration/archive` | 9 passed |
| Registry and gap tests | 78 passed |
| ruff check | `All checks passed!` |
| ruff format | `129 files already formatted` |
| mypy | `Success: no issues found in 116 source files` |

### Earlier attempts, kept as history
These stops happened during the work and stay on record. Only the last row was on the final code.

| Batch | Result |
|---|---|
| `tests/unit/archive` in one process, attempt 1 | **Failed:** 1 failed, 93 passed, 1 skipped. `SchemaError: Uncaught PydanticUseDefault exception` while building a `Bar` in `_bar_row`, in `test_every_manifest_records_the_importing_code`. That test alone passed 3 of 3 afterwards |
| Same batch, attempt 2 | **Crashed:** exit `0xC0000005` (access violation) after about 70 tests, in `Bar(...)` inside `legacy_bar_from_row`, in `test_scaling_probe_detects_the_old_pipeline` |
| `test_arc_import.py` | **Crashed:** exit `0xC0000005` after 4 tests, in `arc_helpers._fmt`, while `test_checksum_mismatch_is_hard_error_and...` built its fixture, before any importer code ran |
| E-2 test file after the first review fix, attempt 1 | **Stopped:** exit 127 from the shell wrapper, no summary and no fault message, after 111 of 113 tests. The retry passed: 113 passed |
| E-2 test file after the audit fix, attempt 1 | **Crashed:** exit `0xC0000005` after 89 tests, during garbage collection in `csvparse._ts`. The retry passed: 115 passed |

**The cause of these five stops is not established.** They happened at five unrelated places, and one is in a test
helper E-2 did not change, before any importer code ran. That points away from the E-2 logic but does not prove it.
A stopped run is not a pass. The final passes do not explain the stops, and they make no claim about hardware
stability.

## Mutation testing
85 mutants: **84 killed with a named failing test, 1 survived as equivalent.** The last five cover the two review
fixes. Four of them ran just before the zero-rows line was added, and the fifth, on that line, ran on the final code.
- **Equivalent:** `first==prev-close dropped`. A Z1 first candle with k≥1 already forces `first.open == prev.close`.
  The check stays as the ADR writes it.
- **Runs:** run 1 stopped by the machine after 70 mutants, with 59 named kills. Run 2a killed 7 of 7. Run 3 covered
  the rest under corrected accounting.
- **Correction of an earlier false statement:** the status "all 70 mutants run so far are killed" was wrong. The
  first runner counted any non-zero exit as a kill, and four entries had no named test. On rerun, two of them were
  false kills, and they exposed real test gaps. The corrected runner counts a kill only for exit 1 with a named
  failing test.
- **Real gaps found by survivors, all now covered:** redaction through `build_report`, the market filter through the
  importer, the registry in the importer's cross-check, native-line evidence, the long-run limit as a literal,
  evidence month, the role filter, and the 1m line.
- **Limit of this evidence:** the first 80 mutants ran on slightly earlier code. Source logic changed since then
  only by removing a redundant `sorted` in `join_zero_trade` and by the two fixes below, which add to the missing
  split and to the findings. No fault signature appears in the saved mutation logs, but run 1 kept only test names.

## Known deviations and limits
- `LONG_ZERO_TRADE_RUN` is a module constant, while ADR-0014 says "configurable".
- The class A list is capped at `LISTED_LIMIT` (500), with a truncation flag. Counts stay complete.
- Above 500 explained mismatches, the rest count as unexplained with no stated reason. This fails closed.
- The default registry path is relative to the working directory, so O-7 must run from the repository root or pass
  `--interruptions`. Without the file, Z2 fillers read as Z1, and `sha256: null` in the report shows it.
- `ok` and the exit code of `data import` ignore the QA result, and nothing in `src` or `scripts` reads
  `classification_ok` or `cross_checks_ok` today. A future gate must require `is True`, because `None` means
  "not classified".
- Long Z1 runs are in `qa_report.json` only, not in the summary or the findings.
- A symbol listed or delisted inside the request now fails `classification_ok`, as ADR-0013 section 6 reads. A later
  study must not filter its universe on this flag, because that would create survivorship bias.
- An import with `--to` set to today can record a few false unexpected minutes, because the clock is read after
  fetching. This fails closed, and historical runs are unaffected.
- The tests of the two last fixes call `build_report` directly. No test covers the zero-row or uncovered paths
  through the importer itself.

## Independent reviews
All reviews were read-only and ran no tests.

**Final review of the two last fixes: APPROVE WITH NOTES, no blocking findings.** It covered both fixes, ADR-0015,
market-specific downtime, provenance, report versioning, bounded memory and non-1m imports. Its two notes are the
last two items under "Known deviations and limits". It approved by reading, and the test results come from the
final regression above.

The first three reviews ran before those fixes:

**Code review: REQUEST CHANGES.**
- **Fixed:** `classification_ok` passed with uncovered head or tail minutes and with zero rows. `build_report` now
  counts them in the missing split, so ADR-0013 section 6 holds as written.
- **Fixed:** per-reason counts added to the summary; two tests that never reached the rule corrected; ADR line
  numbers pinned; the cross-check equality claimed by a docstring now asserted; O-7 instructions added to the brief.
- **Cleared:** its remaining blocker, the regression evidence, by the final regression above.

**Provenance review: PASS.** The extractor reran with exit 0 and reproduced the fixture byte for byte. Source hashes
and lines tie to the ADR, and the fixture holds nothing from 2026. Volumes were not hand-checked.

**Causality and safety audit: FAIL on one point, now fixed.**
- **Fixed:** an empty request range, or one wholly inside a documented absence, still reported `classification_ok`
  true. Zero rows is now always a finding.
- **Clean by reading:** look-ahead, registry containment, the narrow exception, holdout redaction, secrets and orders.
- **Note:** prices of sealed months are now compared in memory on the import path, including the December to January
  join. This bypasses no guard and adds no file read, and redacted class A rows keep only a price-derived category.

## Machine stops during this task
Kernel-Power 41 at 2026-10-30 19:10:07 local, with the mutation log stopping at 19:09:20 after 70 of 80 mutants.
Checked after the restart: branches, tag, fixture hash and uncommitted work all intact. The owner closed hardware
investigation as out of scope, so none was continued. Testing now follows the owner's bounded, sequential,
resumable policy.
