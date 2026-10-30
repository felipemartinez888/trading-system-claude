# M2-E1b: observed-versus-absent split and registry governance

- **Authorization:** owner message "OWNER AUTHORIZATION — E-1b POLICY COMPLETION" (2026-10-29). It closes the
  implementation gaps of D-10A and D-09A, recorded at the E-1 final review.
- **Base:** E-1 commit `4e88ba0` on `task/m2-e1`, in an isolated git worktree. E-1b was reviewed by the owner
  ("E-1b FINAL REVIEW — LOCAL COMMIT APPROVED") and committed locally on top of `4e88ba0`; not merged or pushed.
- **Optional scope not taken:** D-12A's waiting-time and deferred-versus-other metrics. The owner judged the
  existing per-trade `deferred_no_trade` flags sufficient for now; the proposal stays in the
  [E-1 devlog](2026-10-28-m2-e1-no-fill-and-interruptions.md#owner-decisions-of-2026-10-29-and-proposed-e-1b).
- **Not touched:**
  - canonical data, evidence, the 2026 holdout and research results;
  - the simulator (the freeze still covers the whole interval) and paper;
  - `contracts/`.

  No historical backtest was run; every number below comes from synthetic fixtures.

## D-10A: only the documented absence period is downtime
- **New field:** each registry entry has a required `absent_from` (minute-aligned, `from ≤ absent_from < to`).
  For the 2023-03-24 halt it is `12:39`:
  - candles 11:28–12:38 were published (zero-trade fillers);
  - 12:39–13:59 are absent (81 minutes, including the truncated 12:39 line).
- **Runner** (`split_missing`): only `[absent_from, to)` is eligible downtime. A minute missing in
  `[from, absent_from)` should have been published, so it counts as **missing data**. That grades X under the
  unchanged `max_missing_share: 0.0`, with the usual "data gaps" reason.
- **Kept as is:**
  - the interval `[11:28, 14:00)`;
  - the simulator freeze over the whole interval, fillers included (paper/backtest parity and execution
    semantics unchanged);
  - observed zero-trade candles, still counted in `zero_trade_minutes_*`;
  - the interval ids listed in range or warm-up for the whole interval.

  No candle is synthesized or removed.
- **Version:** `RUNNER_VERSION = "runner-v3"`. Integrity accounting under a registry changes, so runs are
  distinguishable.

## D-09A: registry governance (schema 2)
`configs/exchange_interruptions.yaml` is now `schema: 2`. The loader (`quality/interruptions.py`) refuses the
whole registry, so the CLI exits 5 and no run starts, when:
- **`approved_at`** is missing or not an ISO date `YYYY-MM-DD`. A YAML-parsed date is accepted; a datetime,
  number or other format is refused.
- **`absent_from`** is missing, not minute-aligned, or outside `[from, to)`.
- **The 24-hour limit** is exceeded: an interval longer than 24 hours (`to − from > 24 h`; exactly 24 h is
  allowed) has no `exceptional_override`.
- **The override is incomplete:** it must be a mapping with non-blank `approved_by`, `reason` and `reference`,
  and an ISO-date `approved_at`.
- **An override is attached to an interval of 24 hours or less.** It is reserved for the exception it documents.
- **Fields are unknown:** at the top level, in an entry (so a typo such as `absent_form` can never be silently
  ignored) or in the override.
- **Fields have the wrong type** (review B-1): `id` and `approved_by` must be non-blank strings (`id` without
  surrounding spaces), `signature` a non-empty mapping, `external_sources` a non-empty list of non-blank
  strings, and `schema` an integer (`2.0`, `"2"` and `true` are refused). A merely truthy value is never enough.
- **A key is duplicated** in the YAML (review N-1). PyYAML would silently keep the last value, while a reader of
  the file sees the first. The loader uses a strict `SafeLoader` subclass that refuses duplicates.
- **A timestamp has no time part** (review N-2): `from`, `to` and `absent_from` must look like
  `YYYY-MM-DDTHH:MM[:SS]Z`.
- **Anything already refused in E-1:** a wrong schema, a duplicate id, an unknown market, a non-exchange-wide
  scope, missing evidence fields, a timestamp not ending in `Z`, an unaligned minute, `to ≤ from`, or overlap
  within a market.

**Reproducibility:**
- the file's SHA-256 goes into the manifest, as before;
- the manifest's `exchange_interruptions.entries` now records every policy field (`absent_from_ms`,
  `approved_at`, `exceptional_override`), so even a registry built in code without a file hash changes the
  manifest hash when any policy field changes.

**Repository entry:** the 2023 entry gains `absent_from: "2023-03-24T12:39:00Z"` and `approved_at: "2026-10-28"`
(the D-10 approval date). Its interval is 152 minutes, well under 24 hours.

## Pinned values updated
| Pin | New value | Why it changed |
|---|---|---|
| `GOLDEN_MANIFEST` | `519558a8…` | `runner-v3` and the empty registry's `schema: 2`; reverting exactly those two values gives the previous `f77a88bb…` |
| `GOLDEN_METRICS` | `e32098e5…` | only the embedded `manifest_hash`; restoring the previous hash gives the previous `ef8abc59…` |
| `GOLDEN_TRADES` | unchanged `2dff9ce6…` | — |

## Tests
`tests/unit/backtest/test_bt_exchange_interruptions.py` now collects 60 tests and
`tests/unit/sim/test_e1_no_trade_and_outage.py` 18. New or changed for E-1b:
- **Missing observations:**
  - a gap whose first 10 minutes precede `absent_from` gives downtime 20, missing 10, grade X ("data gaps: 10
    missing");
  - the 2023 shape (fillers, then absence) gives downtime 20, zero-trade minutes 10, missing 0, not X, and the
    same bars read as without a registry;
  - in the repository registry, `split_missing(11:30–11:40)` gives `(0, 10, [])` and `(12:39–14:00)` gives 81
    downtime minutes.
- **Simulator interval:** `simulator_intervals()` still starts at 11:28 (the whole interval). A behavioural sim
  test (review M1) feeds a schema-2 entry `[minute 3, minute 6)` with `absent_from` at minute 5 to the
  simulator: the stop is not hit inside the observed part, and the exit is at minute 6 as `EXCHANGE_DOWNTIME`.
- **Boundaries:**
  - `absent_from` before `from`, equal to `to`, or unaligned fails;
  - exactly 24 h passes, 24 h + 1 min fails, and the same with a complete override passes.
- **Malformed approvals:** `approved_at` missing, `28/10/2026`, `2026-13-01`, `2026-W44-3`, a datetime, `false`
  or an integer fails; an unquoted YAML date is accepted.
- **Overrides:** each of the four fields missing, empty or blank fails; an invalid override `approved_at` fails;
  a non-mapping fails; an override on an interval of 24 h or less fails; unknown override, entry or top-level
  fields fail; schema 1 and a non-integer schema fail.
- **Typed fields and YAML:** blank, boolean, numeric or list `approved_by`; blank or padded `id`; a string,
  blank or non-string `external_sources`; a boolean or list `signature`; a date-only timestamp; a duplicate
  `absent_from` key in the repository file all fail.
- **Non-applicable markets:** another market's entry never applies (existing tests); overlap across markets is
  allowed.
- **Hash:** changing `approved_at` or `absent_from` changes the manifest entries; the manifest records every
  override field, and a different override `reason` changes it; two files differing only in `approved_at` have
  different SHA-256.
- **CLI:** the malformed-registry refusal now uses schema 2.

## Test evidence
All runs are on the worktree after the review fixes, before the commit (main venv, worktree `src` on `PYTHONPATH`,
focused chunks only, 2026-10-29).

| Chunk | Result |
|---|---|
| `tests/unit/backtest` + `tests/unit/sim/test_e1_no_trade_and_outage.py` | 121 passed |
| sim determinism, paper session/restart/seeded, and the MVP-2/2.1/3/4 paper integration and API tests (parity) | 235 passed |
| `tests/unit/research` + `tests/unit/risk` (`-m "not live"`) | 212 passed |
| `ruff check --no-cache`, `ruff format --check` | clean (211 files) |
| `mypy src` (scratch cache) | clean (115 source files) |

The golden pins were verified as explained above (by the implementer and independently by the reviewer).
No full suite was run (WHEA instability); the frontend was not touched by E-1b.

### Mutation sensitivity
Each safeguard was removed in a scratch copy of `src`, and the two focused files above were run against it. All
18 mutants were **killed**.

| Mutant | Killed by |
|---|---|
| split ignores `absent_from` (D-10A) | `test_missing_minutes_before_absent_from_are_genuine_missing_data` |
| simulator freezes only the absence period | `test_the_repository_registry_holds_exactly_the_approved_2023_halt` |
| `absent_from` range not checked | invalid-registry case `absent_from` |
| `approved_at` not validated | invalid-registry case `approved_at` |
| 24 h limit removed | `test_the_24_hour_limit_is_inclusive_and_longer_intervals_fail_closed` |
| 24 h limit exclusive (off by one) | same |
| override `reference` not required | override cases |
| unknown entry fields accepted | invalid-registry case `unknown field` |
| unknown override fields accepted | `test_unknown_override_or_registry_fields_fail_closed` |
| unknown top-level fields accepted | same |
| manifest omits `approved_at` | `test_every_policy_field_is_reflected_in_the_manifest_and_the_file_hash` |
| `approved_at` accepts a datetime (review M3) | invalid-registry case `approved_at` (datetime) |
| override `approved_at` not validated (review M4) | `test_an_incomplete_exceptional_override_fails_closed[approved_at]` |
| manifest records the override as a bool (review M5) | `test_every_policy_field_is_reflected_in_the_manifest_and_the_file_hash` |
| `approved_by` only truthy (review B-1) | invalid-registry case `approved_by` |
| duplicate YAML keys accepted (review N-1) | `test_a_duplicate_yaml_key_fails_closed` |
| date-only timestamp accepted (review N-2) | invalid-registry case `timestamp` |
| override allowed on a short interval (review N-3) | invalid-registry case `only for intervals longer` |

## Review
An independent causal/safety review (fresh context) read the E-1b diff.
- **Causality: PASS.**
  - No decision uses later data.
  - The simulator still freezes the whole `[from, to)`, so no fill is possible on the published fillers.
  - The observed/absent split changes only integrity accounting, never execution.
  - Paper/backtest parity is unchanged (the simulator and paper are untouched).
- **Governance: CONDITIONAL**, with every condition now fixed:
  - **B-1 (blocking):** evidence and approval fields were checked as merely truthy. They are now typed (see D-09A
    above).
  - **N-1:** duplicate YAML keys were silently accepted. Now refused.
  - **N-2:** the timestamp check accepted a date without a time. Now refused.
  - **N-3:** an override was accepted on a short interval. Now refused.
  - **M1, M3, M4, M5 (missing tests):** the behavioural sim test, the datetime `approved_at`, the override
    `approved_at` and the manifest's override content are now covered, each with a killed mutant.
- **N-4, deferred on purpose:** a deferred-versus-other breakdown and a separate count of observed zero-trade
  minutes inside a registered interval. The observed fillers are already counted in `zero_trade_minutes_*`, and
  the owner judged the existing per-trade flags sufficient for this stage.

## Commit approval requirements
The owner approved one local commit on 2026-10-30 ("E-1b FINAL REVIEW — LOCAL COMMIT APPROVED"). The
requirements it was made against:
- **Base:** `task/m2-e1` at `4e88ba0` (E-1), in the `tsc-e1` worktree. One local commit on top; no amend of
  `4e88ba0`, no merge, no push.
- **Stage exactly these paths:**
  - `configs/exchange_interruptions.yaml`
  - `src/tsclaude/quality/interruptions.py`
  - `src/tsclaude/research/runner.py`
  - `tests/unit/backtest/test_bt_exchange_interruptions.py`
  - `tests/unit/backtest/test_bt_cli.py`
  - `tests/unit/backtest/test_bt_runner.py`
  - `tests/unit/sim/test_e1_no_trade_and_outage.py`
  - `docs/DECISIONS_PENDING.md`, `docs/ROADMAP.md`, `docs/adr/0013-exchange-wide-interruptions.md`
  - `docs/devlog/2026-10-28-m2-e1-no-fill-and-interruptions.md`, this devlog, `docs/devlog/metrics.csv`
  - optionally `docs/tasks/M2-E1-E3-integration-plan.md` (a plan only, marked NOT EXECUTED)
- **Never staged:** `docs/tasks/M2-E1-checkpoint.md` (session note) and any data, evidence or cache.
- **Pre-commit checks:** `git diff --cached --stat` lists only the paths above; the focused backtest/sim chunk,
  ruff, format and mypy pass again on the staged tree.
- **Proposed message:** `feat(quality): registry schema 2 — absent_from split and approval governance (M2-E1b)`.
- **Known consequence:** `runner-v3` and registry schema 2 change the manifest hash of every new run. Older run
  manifests stay valid records of their own version; nothing is recomputed.
