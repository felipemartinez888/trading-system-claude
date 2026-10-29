# M2 decisions P-1 … P-9: final technical recommendations, C-1 diagnosis and remediation plan

> **Decided 2026-10-28:** the owner's decisions are recorded as D-09 … D-17 in `docs/DECISIONS_PENDING.md`.
> The status lines below describe the state when these recommendations were written.

- **Status:** recommendations only (2026-10-28), after the documentation commit `ebefbc1`. Nothing is accepted,
  implemented, re-imported or promoted.
- **Unchanged:** ADR-0013, ADR-0014 and ADR-0015 stay **Proposed**; the grade-X contract; simulator behaviour;
  general missing-data checks; raw candles.
- **Explicitly not accepted or promoted:** B03, B10 and B12.
- **Evidence:** read-only checks of the cached source archives, manifests and QA reports, plus a replay of every
  committed version of the archive CSV parser. The scripts are in the session scratchpad;
  `scripts/owner_checks/o2_canonical_row_audit.py` is the owner-run sweep.

## 1. Measurements behind the recommendations
**Zero-trade minutes in source archives** (2026 holdout excluded):

| Symbol and period | Minutes | Zero-trade minutes | Z1-shaped (flat at previous close, all volumes 0) | 15m buckets starting with a zero-trade minute |
|---|---:|---:|---:|---:|
| BTC 2022–2025 (cached archives) | 2,103,760 | 72 | 72 (71 + the truncated 12:39 candle) | 5 |
| BTC 2022–2025, **excluding 2023-03-24 11:28–14:00** | 2,103,688 | **0** | 0 | 0 |
| BTC 2024 | 527,040 | 0 | 0 | 0 |
| AVAX 2024–2025 | 1,052,640 | 590 (0.056 %) | 590 | 19 (0.027 %) |
| HBAR 2024–2025 | 1,052,640 | 6,757 (0.64 %) | 6,757 | 336 (0.48 %) |

Every zero-trade minute observed is Z1-shaped; there is no class A anywhere.

**The 2023-03-24 signature** is identical in all 5 canonical symbols: Z1 run 11:28–12:38 (71 minutes),
truncated candle at 12:39 (line 33880), absent 12:40–13:59, trading again at 14:00. External reports confirm a
spot matching-engine halt, cancel-only until about 13:40 UTC, and resumption at 14:00 UTC.

## 2. Do the 71 zero-trade minutes belong inside the interruption? (P-2)
**Recommendation: yes. Admit [11:28, 14:00).**

**Evidence:**
1. In four years of BTC history (2022–2025), **every** zero-trade minute lies in this window; outside it there
   are none. For a market as liquid as BTCUSDT a minute without a single trade does not happen in normal
   operation. 71 in a row, starting **in the same minute in all five symbols**, is not "quiet trading".
2. The run ends in the truncated 12:39 candle and then the absent minutes. It is one continuous event, not two.
3. The external reports confirm the halt and the 14:00 resumption. They give no precise start time, so the start
   comes from the data, as ADR-0013's admission rule requires (`from` = the first minute of the market-wide
   zero-trade or absent run).

**What the choice changes**, and what it does not:

| Aspect | Full interval [11:28, 14:00) | Absent part only [12:39, 14:00) |
|---|---|---|
| Fills during 11:28–12:38 | none (ADR-0013) | none, **if P-4 is approved** (Z1 no-fill); otherwise fills at a frozen price nobody traded at |
| Exposed positions | closed at the 14:00 open (L-11), flagged | held through the Z1 minutes, then closed at the 14:00 open, flagged |
| Integrity accounting | 152 minutes reported as exchange downtime; not missing | 81 minutes downtime; 71 Z1 minutes counted as ordinary bars |
| Raw data | unchanged | unchanged |

Under P-4 the difference in execution is essentially none: nobody traded, so nothing fills in either case. The
full interval labels the event truthfully and keeps the 71 minutes out of "normal market" statistics, such as
volatility features computed over a frozen price.

**Remaining evidence to collect (optional):** USD-M behaviour in the same window, once USD-M data exists. A spot
halt does not imply a futures halt, so USD-M gets its own registry entry or none.

## 3. Do the outage treatment and the no-fill rule keep execution realistic and statistics honest? (P-1, P-4)
**Execution realism:**
- **The simulator today fills orders on zero-trade candles** at the filler price, which equals the previous
  close. No counterparty existed at that price: that is a fictitious fill, and it is optimistic for limit or stop
  entries that "touch" a frozen price.
- **The ADR-0014 rule** moves any such entry or exit to the next candle with trades, evaluated with the existing
  gap-fill rules (the worse of open and level). This is never more favourable than reality: a stop that would
  have been hit during a gap fills at the gapped open, as on a real exchange.
- **During a registered outage** no order can execute, because the exchange accepted none. At resumption,
  exposed positions close at the first traded open (aligned with live rule L-11), and that open includes the
  repricing over the outage. This is the conservative assumption; it does not pretend a stop could have been
  honoured mid-halt.
- **Entries scheduled inside an outage** execute at the first traded open if still valid; `valid_until` is never
  extended, so no extra time is granted.

**Statistical honesty:**
- **No trades are deleted or excluded.** Affected trades stay in every statistic, so there is no survivorship
  bias. They are flagged (`spans_exchange_downtime`, counted into the existing `spans_gap` share).
- **Above the existing `flagged_share_cap` (5 %) the grade is capped at C**, so a result that depends on outage
  trades can never look credible. For the 2023 outage the exposure is tiny: 152 minutes in about 1.05 million per
  two years (0.014 %).
- **General missing-data checks are not relaxed.**
  - `max_missing_share` stays 0.0.
  - Only minutes in an owner-approved, source-proven, market-wide registry entry are reported as downtime instead
    of missing.
  - The C-1 rows and the single SOL 2023-02-14 gap can never qualify, because each affects a single symbol.
- **Size of the P-4 effect:**
  - **BTC:** none outside the outage.
  - **AVAX:** small (0.056 % of minutes).
  - **HBAR:** material (0.64 % of minutes; 0.48 % of 15m buckets start with a zero-trade minute).

  The rule therefore removes optimism mainly where liquidity is thin, which is where it matters. Results that
  change under P-4 must be re-run with the new simulator version; no local backtest runs exist today.
- **Parity:** backtest and paper share the simulator, so P-4 applies to both. P-1 without P-4 would make the
  backtest refuse fills that paper would accept, which is why the two must be approved and implemented together.

## 4. C-1 diagnosis: canonical rows missing although present in the source
**Facts:**
- Canonical XRP 2025-04 records **43,199** source rows, and canonical SOL 2022-12 records **44,639**. The cached
  archives with the **same SHA-256** contain 43,200 and 44,640 data lines.
- The missing minutes (XRP 2025-04-03 17:24, line 3925; SOL 2022-12-21 15:23, line 29724) are ordinary traded
  candles. QA reported them only as gaps; there was no malformed or rejected record.
- **Every committed version of the CSV parser reads all rows from these exact bytes.** Six commits were
  replayed: `38a9510`, `ba32cd2`, `f17a0f5`, `55d6e4a`, `f679750` and `7d615a4`. In the importer of the
  canonical era (`f679750`), the recorded `rows` is simply the parser's output length.
- The canonical imports were created 2026-10-24 00:05–00:16 UTC (2026-10-23 18:05–18:16 local). The latest
  commit at that time was `49823ae` (main, containing `f679750`); the state of the working tree is not
  recorded.
- Spot-import **native-crash isolation diagnostics** started 18:28 local, minutes later
  (`evidence/diagnostics/spot_crash_isolation.*`), during the open WHEA hardware investigation.
- No log of the canonical import exists (`evidence/data/full_import/` is empty). The manifests record only
  `tsclaude 0.1.0`, with no commit or dirty flag.
- The AP-1 import of 2026-10-27 (`7be73eb`), on the same archive, recorded 43,200 rows (B08).

**Diagnosis:** the loss is **not reproducible with any committed code**. The two hypotheses consistent with the
evidence:
- **H1, transient fault during the 2026-10-24 run** (memory or CPU error under the known WHEA instability). The
  archive is verified by SHA-256 on disk, but decompression and parsing in RAM are not checked. This is the
  leading hypothesis, given the timing.
- **H2, uncommitted code in the working tree at that time.** It cannot be ruled out, because the manifest does
  not record the code state.

**Consequences:**
- **Lost rows are detectable; altered values are not.** A silent row loss shows up as a count mismatch (O-2). A
  silently altered price that still passes validation would not. The canonical datasets from 2026-10-24 are
  therefore **verified only where an independent re-import matches part for part**:
  - BTC 2022-01 → 2024-12;
  - ETH, SOL, DOGE 2024-01 → 2025-12;
  - XRP 2024-01 → 2025-12 except 2025-04.
- **Not yet independently verified:**
  - BTC 2025;
  - ETH, SOL, DOGE, XRP 2022–2023;
  - XRP 2025-04 (known defective);
  - all 2026 holdout parts.
- **Importer weakness** (a code finding for later): the importer does not cross-check its parsed row count
  against an independent count of data lines. It does not record the code commit or dirty state in manifests,
  and it does not re-read parts after writing them. Any one of these would have caught or explained C-1.

## 5. C-1 targeted remediation plan (nothing executed; each step needs owner approval)
1. **O-2 sweep: DONE** (owner, 2026-10-28, 13.9 s, `evidence/owner_checks/o2_canonical_rows_20261028T014419Z.*`).
   240 files; exactly the 2 known missing rows (XRP 2025-04-03 17:24, SOL 2022-12-21 15:23); 5 files OK with the
   known 2023-03 malformed line; no other count-level defect outside the holdout.
2. **Independent parity for the unverified months.** Owner-run, isolated, AP-1 importer worktree `7be73eb`,
   output to new folders under `data_batches/`, never into `data/`:
   - `c1_btc_2025` (BTC 2025-01 → 2025-12);
   - `c1_eth_sol_doge_xrp_2022_2023` (one batch per symbol: 4 × 24 months);
   - XRP 2025-04 is already covered by B08.
   - Then compare part content hashes with canonical, read-only, with the same check used for B01–B08.
   - Cost: about 108 months, at the ~25–80 s per 12–24 months the B-batches took; minutes, not hours.
   - Hardware monitoring is on, as in B01–B12.
3. **Holdout parts (2026-01 → 2026-09):** verify them **only through the sealed path and the holdout guard**
   (L-07). Proposal: the owner runs the sealed re-import into an isolated sealed root and compares content
   hashes, with counts and hashes only, never prices. This needs explicit owner approval, because it touches the
   holdout procedure.
4. **Repair:**
   - Every canonical month that fails parity is replaced with its independently verified part, through a
     promotion procedure (P-9). It records the old and new content hashes and keeps the old manifest; there are
     no hand edits and no silent overwrite.
   - New dataset ids follow automatically from the content hashes.
   - Backtests (none exist yet) would pin the new ids.
5. **Prevention** (code tasks, separate approval):
   - record the git commit and dirty flag in every manifest's `tool`;
   - fail an import if the parsed rows differ from the data-line count of a verified archive;
   - re-read and re-hash written parts before commit;
   - keep an import log under `evidence/`.
6. **Hardware:** keep imports under WHEA monitoring until the AP-4 hardware investigation closes.

## 6. Final recommendations, one decision each
| ID | Decision | Recommendation | Required before implementation |
|---|---|---|---|
| **P-1** | ADR-0013: exchange-interruption registry and backtest treatment | **Approve, together with P-4** | A registry file and its manifest hash; a canary that an unregistered gap still grades X; golden tests for close-at-first-traded-open |
| **P-2** | Admit `binance-spot-2023-03-24` = **[11:28, 14:00)** UTC, spot only | **Approve the full interval** (section 2) | P-1; USD-M decided separately when its data exists |
| **P-3** | ADR-0014 classification (Z1, Z2, M, A) and QA reporting | **Approve** | QA implementation; a re-run on cached archives (owner) |
| **P-4** | No fills on zero-trade candles (simulator, version bump) | **Approve, together with P-1** | Verify that the live kline stream emits closed zero-trade candles; golden tests; re-run any recorded backtests |
| **P-5** | ADR-0015: narrow `leading_zero_trade_open` exception | **Approve** | Five fixtures; mutation tests; fail closed at month boundaries; explained count visible |
| **P-6** | RESEARCH_PROTOCOL §6 clarification (reference-only X = no validated evidence; contract unchanged) | **Approve** the text as proposed | None (documentation) |
| **P-7** | B01–B12 acceptance | **Do not accept anything automatically.** B04–B09 and B11 can be accepted on the existing evidence whenever you choose (B08 as a batch is clean). **B03:** after P-1/P-2. **B10:** O-3 passed (2026-10-28); now after P-5 plus a QA re-run. **B12:** after P-3/P-5 plus a QA re-run | Your sign-off per batch |
| **P-8** | C-1 is an M2 blocker; targeted parity and repair per section 5 | **Approve** | O-2 result; the parity batches; the holdout procedure approved separately |
| **P-9** | Promotion of `data_batches/` into canonical | **Defer** until P-7 and P-8. Then promote only verified parts through a logged, non-overwriting procedure | A promotion procedure (owner-approved) |

**Suggested order:**
1. ~~O-2 and O-3 (owner, minutes)~~: done 2026-10-28, both PASS.
2. P-6 (text only).
3. P-1 + P-2 + P-4 as one implementation brief.
4. P-3 + P-5 as one QA brief.
5. The C-1 parity batches.
6. P-7 decisions.
7. P-9.

M4 research starts only after steps 3–6.

## 7. Side observation (no action taken)
Some batch archive caches contain raw 2026 monthly archives; for example `data_batches/b02_spot_btc_2024/
archive_cache/.../BTCUSDT-1m-2026-09.zip` was seen while listing files. The holdout seal (L-07) protects the
lake roots, not plain archive caches. None of these files were opened. Worth a decision on whether raw holdout
archives should live only under the sealed path.
