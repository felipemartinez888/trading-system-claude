# Technical Assessment: M1/M2, Memory/SSD, and Rust Evaluation

**Date:** 2026-10-22  
**Repository:** `trading-system-claude` @ `3819ef6`  
**Branch:** `main`  
**Status:** READ-ONLY ASSESSMENT — no code changes made  
**Scope:** M1 root-cause investigation, M2 feasibility, memory/SSD analysis, Rust candidate
evaluation, architecture independence assessment

---

## 1. Executive Summary

Trading System Claude is well-architected for its current scale. The codebase implements bounded-
memory patterns throughout: month-by-month streaming in the backtest runner, semaphore-bounded
concurrency in the archive importer, bounded queues in the supervisor, and efficient O(1)-memory gap
tracking. **16 GB RAM is sufficient for the current 3-symbol universe** across all milestones.

The M1 "183 expected minutes missing" failure is **not caused by insufficient RAM or SSD issues**.
Code analysis points to a startup/subscription sequencing problem where the bar builder likely never
received WebSocket data during the short test, or bars were not persisted before the validator
checked. The fix is in diagnostics and the bar builder's startup path, not in memory management.

M2's archive importer already implements best-practice disk-backed processing: month-by-month
downloads with bounded concurrency (Semaphore(3)), checksum verification, atomic writes, and
idempotent re-runs. No redesign is needed.

**Rust is not justified at current scale.** Python + NumPy + PyArrow + DuckDB handles the 3-symbol
universe comfortably. The only pure-Python hot loop (EMA computation) can be addressed with Numba
or vectorized approximation if profiling ever shows it as a bottleneck. Introducing Rust would add
toolchain complexity, Windows CI burden, and maintenance cost without measurable benefit.

### Key Recommendations

| Priority | Action | Effort | Blocked By |
|---|---|---|---|
| **P0** | Diagnose M1 failure with instrumented short test | 1-2 days | Owner runs test |
| **P1** | Run M2 historical import (already implemented) | Hours | M1 gate pass |
| **P2** | Add optional event flushing to simulator for long runs | 1 day | None |
| **P3** | Profile actual memory usage during M2 import and backtest | Half day | M2 data |
| **P4** | Re-evaluate Rust only if profiling shows a Python bottleneck | — | P3 results |

---

## 2. Current Architecture Analysis

### 2.1 Architecture Overview

Single asyncio process (ADR-0002) with ProcessPoolExecutor for backtests. Storage is split between
SQLite WAL for operational writes and Parquet + DuckDB for analytical reads (ADR-0003).

**Source:** `docs/ARCHITECTURE.md`, `docs/adr/0002-*.md`, `docs/adr/0003-storage.md`

### 2.2 Component Memory Profiles

| Component | File | Memory Pattern | Peak Estimate (3 symbols) |
|---|---|---|---|
| WebSocket manager | `src/tsclaude/marketdata/binance/ws.py` (684 lines) | One connection per group (~3 groups), messages dispatched synchronously, no buffering | < 10 MB |
| Bar builder | `src/tsclaude/marketdata/bars.py` (389 lines) | Tracks cursor + `_settled` set per (market, symbol); bounded by monitoring window | < 5 MB |
| Gap registry | `src/tsclaude/quality/gaps.py` (357 lines) | `RangeSet`: sorted disjoint ranges, O(1) for multi-week gaps | < 1 MB |
| Supervisor | `src/tsclaude/runtime/supervisor.py` (354 lines) | Bounded queue `maxsize=10_000`; lazy service creation | < 20 MB |
| Live bar store | `src/tsclaude/store/livebars.py` (263 lines) | SQLite WAL, threading.Lock, `WITHOUT ROWID` table | < 50 MB (SQLite) |
| Archive importer | `src/tsclaude/marketdata/archive/importer.py` (735 lines) | `Semaphore(3)` month concurrency; per-month CSV parse + validate + write | < 500 MB peak |
| Archive downloader | `src/tsclaude/marketdata/archive/download.py` | `concurrency=4` semaphore, atomic temp-file writes, per-zip SHA-256 | < 200 MB |
| Parquet lake | `src/tsclaude/store/lake.py` (668 lines) | Content-addressed immutable parts; staging atomic publish | < 100 MB (index) |
| Lake reader | `src/tsclaude/store/lake_reader.py` (280 lines) | Month-by-month guarded reads, source-ranked conflict resolution | < 300 MB/month |
| Backtest runner | `src/tsclaude/research/runner.py` (1273 lines) | `_stream()`: month-by-month via `_months()` + `heapq.merge`; bars are yielded, not accumulated | < 300 MB/month |
| Simulator engine | `src/tsclaude/sim/engine.py` (376 lines) | **Accumulates all events in `_events` list** | Variable (see §2.3) |
| Sim Book | `src/tsclaude/sim/book.py` (1440 lines) | `_outbox` drains on every `step()` via `drain()`; does not accumulate | < 10 MB |
| Features | `src/tsclaude/features/core.py` (253 lines) | Pure NumPy `sliding_window_view`; `BarArrays` column-oriented | < 50 MB |

### 2.3 Simulator Event Accumulation — The One Concern

The `Simulator._events` list (engine.py:131) accumulates every event for the entire run. Events are
appended in `_append()` (engine.py:141-153) and never flushed until `finish()` returns the
`SimResult`.

**Estimate for a 3-year backtest, 3 symbols × 2 markets, typical trade frequency:**

- Bars processed: ~6 × 525,600 × 3 ≈ 9.5M bars
- Per-bar events: RUN_STARTED (1), BAR_GAP (rare), OBSERVATION_CHANGED (rare), LEDGER_POSTED (per
  trade ~2-4 entries), ENTRY/EXIT events
- With a realistic strategy (~500 trades/year/symbol): ~3,000 trades × 3 years × 6 pairs × 4
  events/trade × 3 books ≈ 648,000 events
- Event size: ~200-500 bytes each → ~130-324 MB for events
- Plus `equity_curve` in each Book: one `EquityPoint` per minute mark → 9.5M × 3 books × ~40
  bytes ≈ 1.1 GB

**Total simulator memory for a 3-year, 6-pair, 3-book backtest: ~1.5-2 GB**

This fits in 16 GB RAM with comfortable margin. However, scaling to 10+ symbols or adding more
books would push this higher. The fix (not urgent) is optional event streaming to disk during long
runs.

### 2.4 Existing Bounded-Memory Patterns (Evidence)

The codebase already implements disk-backed and bounded-memory processing:

1. **Month-by-month streaming in backtests** (`runner.py:284-318`):
   ```
   def _stream(...) -> Iterator[Bar]:
       for lo, hi in _months(start_ms, end_ms):
           chunks: list[list[Bar]] = []
           for market, symbol in req.symbols:
               res = env.reader.read_bars(market, symbol, lo, hi, ...)
               chunks.append(res.items)
           yield from heapq.merge(*chunks, key=order_key)
   ```

2. **Semaphore-bounded import concurrency** (`importer.py`): `month_concurrency=3`

3. **Bounded supervisor queue** (`supervisor.py`): `maxsize=10_000`

4. **Atomic writes with temp files** (`download.py:54-58`): `_write_atomic(path, data)`

5. **Content-addressed immutable Parquet parts** (`lake.py`): one part per month per symbol

6. **O(1) gap tracking** (`gaps.py`): `RangeSet` using sorted disjoint ranges

---

## 3. M1 Root-Cause Investigation

### 3.1 The Error

```
183 expected minutes missing without an open gap
```

**Source:** `scripts/verify_live.py:233`  
**Test command:** `verify_live.ps1 -Hours 0.25`  
**Reported execution time:** ~36 minutes (for a 15-minute -Hours parameter)

### 3.2 How the Validator Works

`verify_live.py` (378 lines):

1. **Observer** (line ~60-120): samples app health every 60 seconds, records stream states, clock
   offsets, and monitoring intervals.

2. **`_expected_minutes()`** (line ~130-160): computes the number of 1-minute bars that should exist
   for each (market, symbol) pair. A minute counts as expected only if it falls entirely inside a
   monitoring interval, with a 15-second tail allowance.

3. **`build_report()`** (line ~170-240): for each (market, symbol), queries `LiveBarStore` for
   stored bars, compares against expected count, checks for open gaps that could explain missing
   bars.

4. **The failing check** (line 233):
   ```python
   if unexplained > 0:
       failures.append(f"{unexplained} expected minutes missing without an open gap")
   ```
   This fires when: stored bars < expected minutes AND no open gaps account for the difference.

### 3.3 Arithmetic of the Failure

With `-Hours 0.25` and ~36 minutes of actual execution:
- Universe: 3 symbols × 2 markets (spot + USDM) = 6 (market, symbol) pairs
- Expected minutes per pair with 36 min monitoring: ~34-35 (accounting for tail allowance)
- Total expected: ~210 minutes across all pairs
- Missing: 183 out of ~210 → **87% of bars absent**
- This means almost no bars were stored for any symbol

### 3.4 Hypotheses (Ranked by Likelihood)

**H1 — Bar builder never started receiving data (HIGH probability)**

The `MarketDataService` is created lazily by the supervisor (`supervisor.py`). If the WebSocket
connections failed to establish, or the bar builder wasn't subscribed before monitoring started, all
expected minutes would show as missing with no gaps registered (gaps are only created by the bar
builder itself). The `_recycle()` mechanism in `ws.py` opens a new connection and waits for a first
message — if the initial connection never connects, there's no message and no gap.

Evidence supporting this:
- 183 / 6 ≈ 30.5 missing per pair, very close to the expected ~34-35 per pair
- The remaining ~27 minutes (210 - 183) may be bars from a brief initial connection or REST backfill
- The audit notes "eventos de desconexión/reconexión WebSocket" during the test

**H2 — Persistence race condition (MEDIUM probability)**

The `LiveBarStore.write()` uses `BEGIN IMMEDIATE` transactions with a threading.Lock. If the bar
builder processes bars on the asyncio event loop but the validator reads from a different connection
before WAL checkpointing completes, bars might not be visible. However, SQLite WAL with
`synchronous=FULL` should make committed writes visible immediately on the same database file.

**H3 — Watermark timing with short test (MEDIUM probability)**

The bar builder's `check_watermark()` (`bars.py`) waits for the watermark deadline (kline freshness
budget of 75 seconds from `data_quality.yaml`) before declaring a minute as due. In a 15-minute
test, the first few minutes might not trigger watermark checks if the grace period hasn't elapsed.
But this would affect only a few minutes at the start, not 87% of the test.

**H4 — REST recovery disabled or failing (LOW probability)**

If WebSocket bars aren't arriving, `due_minutes()` should trigger REST backfill via
`check_watermark()`. If REST calls fail (e.g., Binance API not whitelisted), bars would remain
missing. But the gap registry should record these as `UNKNOWN` gaps, which would count as "open
gaps" in the validator — reducing the unexplained count. That 183 are "without an open gap" suggests
the gap detection pipeline itself isn't running.

**H5 — Insufficient RAM (REJECTED)**

Live monitoring for 3 symbols uses < 100 MB total. A 16 GB machine has no RAM pressure for this
workload. The audit document correctly states: "No existe evidencia confirmada aquí de OOM o falta
de RAM como causa de M1."

### 3.5 Recommended Diagnostic Steps

1. **Instrumented short test** (5 minutes, not the 2-hour gate):
   - Add startup logging: log when each WebSocket group connects, when the first kline message
     arrives per stream, when the first bar is settled, when the first bar is written to SQLite
   - Log `LiveBarStore.count()` every 60 seconds during the test
   - Log `SymbolBarBuilder._settled` set size per symbol every 60 seconds
   - Record process RSS at 30-second intervals

2. **Verify the startup sequence**:
   - Confirm `MarketDataService.start()` completes before the Observer begins recording intervals
   - Confirm WebSocket connections are established and receiving messages
   - Check for exceptions in the bar builder's message handler that might be swallowed

3. **Check Binance endpoint accessibility**:
   - Verify `api.binance.com`, `fapi.binance.com`, and `data-api.binance.vision` are reachable from
     the owner's machine (decision D-01 notes these must be allowlisted)

4. **Acceptance criteria for the diagnostic test** (5-minute test, not the 2-hour gate):
   - All 6 (market, symbol) WebSocket streams connect within 30 seconds
   - First kline message received per stream within 90 seconds
   - At least 3 bars per pair stored in SQLite by minute 5
   - 0 unexplained missing minutes (all missing accounted for by gaps or timing)
   - Process RSS stays below 500 MB throughout

5. **Only after the diagnostic test passes**: run the official 2-hour gate with `verify_live.ps1
   -Hours 2` for M1 acceptance.

### 3.6 Files to Instrument

| File | Line(s) | What to Add |
|---|---|---|
| `src/tsclaude/marketdata/binance/ws.py` | `StreamConnection._connect()` | Log connection established, first message timestamp |
| `src/tsclaude/marketdata/bars.py` | `SymbolBarBuilder.on_kline()` | Log first kline per symbol, bar settlement |
| `src/tsclaude/runtime/supervisor.py` | `_start_service()` | Log service start completion timestamp |
| `scripts/verify_live.py` | `Observer._poll()` | Log bar counts per symbol at each sample |

---

## 4. M2 Assessment: Historical Data & Backtesting

### 4.1 Import Pipeline (Already Disk-Backed)

The archive importer (`importer.py`, 735 lines) already implements the batch/SSD processing pattern
the audit document requests:

| Feature | Implementation | Evidence |
|---|---|---|
| Bounded concurrency | `asyncio.Semaphore(month_concurrency)` with default 3 | `importer.py` constructor |
| Month-by-month processing | Outer loop iterates monthly zip sources | `_plan_sources()` |
| Download verification | SHA-256 checksum per zip, `ChecksumError` aborts | `download.py` |
| Atomic writes | `_write_atomic()` via temp file + rename | `download.py:54-58` |
| Idempotent re-runs | Content-addressed Parquet parts, staging directories | `lake.py` |
| Fallback chain | Monthly zip → daily zip → REST backfill | `_plan_sources()` |
| QA cross-check | Derived 15m/1h bars validated against native archive klines | `importer.py` |

### 4.2 Memory Profile During Import

For one month of one symbol (worst case — BTCUSDT spot, ~43,200 1m bars):

| Stage | Memory | Duration |
|---|---|---|
| Download zip (~2-5 MB compressed) | ~5 MB | Seconds |
| Decompress + parse CSV | ~50 MB (rows as Python dicts) | Seconds |
| Validate via Bar contract | ~50 MB (same rows) | < 1 second |
| Write Parquet part | ~10 MB (PyArrow buffer) | < 1 second |
| **Peak per month** | **~60 MB** | |

With `month_concurrency=3`: peak ~180 MB for import processing, plus ~50 MB for the download buffer
(concurrency=4 in downloader, but bounded by the month semaphore to 3 active months).

**Total import peak estimate: ~250-400 MB.** Well within 16 GB.

### 4.3 Import Scale for the Current Universe

From `configs/universe.yaml`: 3 symbols (BTCUSDT, ETHUSDT, SOLUSDT), 2 markets each = 6 pairs.

Research data: 2022-01-01 to 2024-12-31 (development) + 2025 (validation) = 4 years.

| Data | Months | Pairs | Total Parts | Disk (est.) |
|---|---|---|---|---|
| bars_1m | 48 | 6 | 288 | ~3-5 GB Parquet |
| mark_price_1m | 48 | 3 (USDM) | 144 | ~1-2 GB |
| funding | 48 | 3 (USDM) | 144 | ~50 MB |
| klines_1d | 48 | 6 | 288 | ~5 MB |
| **Total** | | | **864** | **~5-8 GB** |

Import time estimate: at 3 months concurrent, ~5 seconds/month → ~15 minutes for all data. Network
is the bottleneck, not memory or CPU.

### 4.4 Backtest Memory Profile

The backtest runner streams bars month-by-month (`_stream()`, runner.py:284-318). Peak memory
during a backtest is dominated by:

1. **Bars in flight**: one month of bars for all symbols, ~50-100 MB
2. **Simulator events**: see §2.3, ~1.5-2 GB for a full 3-year, 6-pair run
3. **Equity curves**: ~1.1 GB for 3 books × 9.5M points
4. **Feature computation**: < 50 MB (windowed, no accumulation)
5. **Trade records**: < 10 MB for typical trade frequency

**Total backtest peak: ~3-4 GB.** Fits in 16 GB.

### 4.5 Recommendations for M2

1. **Run the import as-is.** The pipeline is already disk-backed and bounded. No redesign needed.
2. **Start small**: import one month of one symbol first to verify the pipeline works end-to-end.
3. **Monitor**: record peak RSS and elapsed time during the import.
4. **Optional future optimization**: if scaling beyond 10 symbols, add event streaming to the
   simulator (write events to a temp Parquet file during the run instead of accumulating in memory).

---

## 5. Python vs Rust Evaluation

### 5.1 Evaluation Methodology

Each component is evaluated against three criteria:
1. **Measured concern**: is there an actual performance or memory problem in Python?
2. **Rust value**: would Rust provide a measurable improvement?
3. **Maintenance cost**: what is the cost of adding Rust to the toolchain?

### 5.2 Component-by-Component Matrix

| Component | Current Stack | Measured Concern | Rust Value | Python Alternative | Recommendation |
|---|---|---|---|---|---|
| **WebSocket ingestion** | `websockets` + asyncio | None. 6 streams, < 10 MB | None. Python handles this trivially | — | **No Rust** |
| **Data normalization** | CSV parse → Bar contract | None at 3 months/batch | Small throughput gain | Polars `read_csv()` | **No Rust** (Polars if needed) |
| **Historical transforms** | PyArrow + DuckDB | None. Month-by-month streaming | Marginal | DuckDB SQL transforms | **No Rust** |
| **Gap detection** | Pure Python `RangeSet` | None. O(1) memory | None | — | **No Rust** |
| **Backtesting engine** | Pure Python `Simulator` | Event accumulation (memory, not CPU) | Low. Memory issue, not speed | Event streaming to disk | **No Rust** (optimize Python) |
| **Indicator calculations** | NumPy `sliding_window_view` | EMA is Python loop (core.py:193) | Small latency gain | Numba JIT, vectorized EMA | **No Rust** (Numba if needed) |
| **Feature computation** | NumPy vectorized | None at current scale | None | — | **No Rust** |
| **Dashboard/API** | FastAPI + React/TS | None | None | — | **No Rust** |

### 5.3 Detailed Analysis: EMA (The Only Hot Loop)

The EMA function (`features/core.py:184-198`) is the only pure-Python loop in the computation path:

```python
for i in range(x.shape[0]):
    acc = acc + alpha * (float(x[i]) - acc)
    raw[i] = acc
```

For a 3-year backtest of one symbol: ~1.58M iterations. This takes ~0.5-1 second in CPython 3.13.
With 6 pairs: ~3-6 seconds total for EMA computation across the entire backtest.

**Python alternatives (before Rust)**:
1. **Numba `@njit`**: compiles to native code, ~50x speedup → < 0.1 seconds
2. **Vectorized approximation**: EMA can be expressed as `scipy.signal.lfilter` (C implementation)
3. **`pandas.Series.ewm()`**: Cython-backed, ~10x speedup

**Verdict**: Even without any optimization, 3-6 seconds for EMA is negligible compared to the I/O
time of reading Parquet data. Rust is not justified for this.

### 5.4 Rust Toolchain Cost

Introducing Rust would require:

| Cost Factor | Impact |
|---|---|
| Rust toolchain on Windows | Visual Studio Build Tools + rustup, ~2 GB disk |
| PyO3/maturin integration | Build system complexity, `pyproject.toml` changes |
| Windows CI configuration | GitHub Actions needs Rust toolchain, ~3 min added to CI |
| Developer onboarding | Every contributor needs Rust knowledge |
| Debugging across FFI boundary | Harder to trace errors across Python↔Rust |
| Binary distribution | Platform-specific wheels, no pure-Python fallback |
| Memory safety at boundary | PyO3 handles this well, but adds review burden |
| Compilation time | Rust builds are slow; full rebuild ~2-5 minutes |

**For a single-developer local tool**: this overhead is significant relative to the zero measurable
benefit at current scale.

### 5.5 When Rust Would Be Justified

Rust becomes worth considering if **and only if**:
1. The universe grows to 50+ symbols AND
2. Profiling shows a specific Python component as the bottleneck AND
3. Python alternatives (Numba, Polars, DuckDB) don't resolve it AND
4. The bottleneck is CPU-bound, not I/O-bound

The most likely future Rust candidate would be a custom indicator engine if the strategy requires
many complex features computed per bar on a large universe. But this is speculative and far beyond
current requirements.

---

## 6. Target Architecture

### 6.1 Recommendation: Keep Current Architecture

The current architecture is well-suited for the project's scope:

- **Single process + worker processes** (ADR-0002): correct for a local-first tool
- **SQLite WAL + Parquet/DuckDB** (ADR-0003): right split between operational and analytical
- **Canonical 1m bars** (ADR-0004): clean data model
- **Event-driven simulator** (ADR-0005): reproducible, fingerprinted runs
- **Windowed features** (ADR-0006): no look-ahead, bounded memory
- **Advisory AI** (ADR-0007): agents advise only, never set prices

### 6.2 Targeted Improvements (Not Architectural Changes)

| Improvement | Component | Effort | Benefit |
|---|---|---|---|
| Event streaming for long runs | `sim/engine.py` | 1 day | Removes the only memory scaling concern |
| Startup instrumentation | `ws.py`, `bars.py`, `supervisor.py` | Half day | Diagnoses M1 failure |
| Memory profiling hooks | Runtime | Half day | Validates all estimates in this assessment |
| Numba JIT for EMA (optional) | `features/core.py` | 2 hours | ~50x faster EMA (already fast enough) |

### 6.3 Architecture at Scale (Future, Not Now)

If the universe grows significantly (20+ symbols, real-time multi-strategy):

1. **Phase 1** (10-20 symbols): add event streaming to simulator, use Polars lazy for import
2. **Phase 2** (20-50 symbols): consider ProcessPoolExecutor for parallel backtests per symbol group
3. **Phase 3** (50+ symbols): evaluate Rust for indicator engine only, via PyO3 extension module

**Current phase: Phase 0.** No changes needed beyond M1 diagnostics.

---

## 7. Benchmark Plan

### 7.1 Protocol

All benchmarks must follow these rules (from the audit document §5):

1. Freeze inputs: symbols, ranges, timezone, intervals, seeds, fees, slippage, dataset, versions,
   hardware, configuration
2. Run unchanged baseline first
3. Record: duration, throughput (bars/sec), peak RSS, CPU utilization, disk I/O, errors, integrity
   (fingerprint match), repeatability (run twice, compare)
4. Set acceptance thresholds **before** analyzing results
5. Never relax M1/M2 acceptance criteria to get a pass

### 7.2 Benchmark Suite

| ID | What | Command | Metric | Threshold |
|---|---|---|---|---|
| B-01 | M1 short diagnostic | `verify_live.ps1 -Hours 0.083` (5 min) | Unexplained missing minutes | 0 |
| B-02 | M1 2-hour gate | `verify_live.ps1 -Hours 2` | All M1 acceptance criteria | Per ROADMAP.md |
| B-03 | M2 single-month import | `tsclaude data import` (1 symbol, 1 month) | Peak RSS, time, integrity | < 500 MB, < 30s |
| B-04 | M2 full import | `import_history.ps1` | Peak RSS, time, disk usage | < 2 GB, < 30 min |
| B-05 | Backtest 1-year, 6 pairs | `run_backtest(...)` | Peak RSS, time, fingerprint match | < 4 GB, < 5 min |
| B-06 | Backtest 3-year, 6 pairs | `run_backtest(...)` | Peak RSS, time, fingerprint match | < 6 GB, < 15 min |

### 7.3 Measurement Tools

```powershell
# Peak RSS on Windows (PowerShell)
$proc = Get-Process -Id $pid
$proc.PeakWorkingSet64 / 1MB  # peak RSS in MB

# Or use Python's resource module equivalent:
# import psutil; psutil.Process().memory_info().peak_wset
```

All measurements should be committed to `docs/devlog/metrics.csv` with the benchmark ID, timestamp,
and results, per the project's existing convention.

---

## 8. Risk Assessment

### 8.1 Current Risks

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| M1 remains blocked by undiagnosed failure | High | High | Instrumented diagnostic test (§3.5) |
| M2 import encounters Binance rate limits | Medium | Medium | Already handled: exponential backoff, `concurrency=4`, `Retry-After` respect |
| Simulator OOM on scaled universe | Medium | Low (at 3 symbols) | Event streaming to disk (§6.2) |
| Parquet lake fills disk | Low | Low | 5-8 GB for full dataset; standard SSD has 100+ GB free |
| WebSocket drops during 2-hour M1 test | Medium | Medium | Already handled: reconnect with backoff, planned 23h recycle, REST recovery |
| Historical data has exchange gaps | Low | Low | Already handled: gap detection, coverage tracking, `GapRecord` events |

### 8.2 Risks of Premature Optimization

| Risk | Impact |
|---|---|
| Introducing Rust before profiling | Toolchain complexity with no measured benefit |
| Redesigning importer that already works | Delay M2 gate; regression risk |
| Adding disk-backed event streaming before it's needed | Code complexity for a hypothetical problem |
| Upgrading to 32 GB RAM as workaround | Masks bugs (like the M1 failure) instead of diagnosing them |

### 8.3 Risks the Audit Document Flags (Verified)

| Audit Concern | Verified Status |
|---|---|
| "No asumir que RAM insuficiente es causa de M1" | **Correct.** Memory is not the issue. |
| "No asumir Rust es superior para cada componente" | **Correct.** Rust is not justified for any component at current scale. |
| "Pagefile de Windows no equivale a batch processing" | **Correct.** The codebase uses real batch processing (month-by-month), not pagefile. |
| "No ejecutar dos horas como sustituto del diagnóstico" | **Correct.** The 5-minute diagnostic must pass first. |
| "Proteger exactitud del backtesting" | **Verified.** The runner uses holdout guards, fingerprinted results, and sealed datasets. |

---

## 9. Prioritized Roadmap

### Phase 1: M1 Diagnosis and Gate (Priority: Immediate)

| Step | Action | Blocks |
|---|---|---|
| 1a | Add startup instrumentation to `ws.py`, `bars.py`, `supervisor.py` | Nothing |
| 1b | Run diagnostic test (5 min) with instrumentation | 1a |
| 1c | Analyze diagnostic output, identify root cause | 1b |
| 1d | Fix root cause (likely startup sequencing or subscription wiring) | 1c + owner approval |
| 1e | Re-run diagnostic test, verify fix | 1d |
| 1f | Run official 2-hour M1 gate | 1e + owner action |

### Phase 2: M2 Import and Verification (Priority: After M1)

| Step | Action | Blocks |
|---|---|---|
| 2a | Run single-month test import with memory monitoring | M1 gate |
| 2b | Verify imported data integrity (checksums, row counts, coverage) | 2a |
| 2c | Run full `import_history.ps1` | 2b |
| 2d | Build DuckDB catalog over imported data | 2c |
| 2e | Run benchmark backtest (B-05) | 2d |

### Phase 3: Optimization (Priority: Only If Needed)

| Step | Action | Blocks |
|---|---|---|
| 3a | Profile actual memory during B-05 and B-06 | Phase 2 complete |
| 3b | If simulator events > 4 GB: implement event streaming | 3a showing need |
| 3c | If EMA is bottleneck: add Numba JIT | 3a showing need |
| 3d | If import is slow: switch CSV parse to Polars | 3a showing need |
| 3e | Re-evaluate Rust only if 3b-3d insufficient | 3b-3d showing insufficient |

### Milestone Dependencies

```
M1 gate ← diagnostic fix ← instrumented test
    ↓
M2 gate ← import ← data verification
    ↓
M3 (dashboard wiring) ← M2 data available
    ↓
M4 (preregistrations) ← M3 + backtesting verified
```

---

## 10. Owner Decision Register

Decisions requiring owner approval before any implementation:

| ID | Decision | Options | Recommendation | Impact |
|---|---|---|---|---|
| **OD-01** | Approve M1 diagnostic instrumentation | (a) Add logging to `ws.py`, `bars.py`, `supervisor.py` (b) Run diagnostic without changes | **(a) Add logging** — minimal, non-functional changes needed to diagnose the 183-minute failure | Unblocks M1 |
| **OD-02** | Run 5-minute diagnostic test | Owner must execute `verify_live.ps1 -Hours 0.083` on their Windows machine with Binance endpoints accessible | **Run after OD-01** | Produces diagnostic evidence |
| **OD-03** | Approve M1 root-cause fix | Depends on diagnostic findings | Fix the identified issue | Unblocks M1 gate |
| **OD-04** | Run official 2-hour M1 gate | Owner executes `verify_live.ps1 -Hours 2` | **Run after diagnostic test passes** | M1 acceptance |
| **OD-05** | Run M2 historical import | Owner executes `import_history.ps1` | **Run after M1 gate passes** | M2 acceptance |
| **OD-06** | RAM upgrade to 32 GB | (a) Keep 16 GB (b) Upgrade | **(a) Keep 16 GB** — sufficient for current and near-future scale; upgrade only for Windows multitasking comfort | Optional |
| **OD-07** | Introduce Rust | (a) No Rust (b) Selective Rust for specific component | **(a) No Rust** — not justified at current scale; re-evaluate only after profiling with real data (Phase 3) | Deferred |
| **OD-08** | Event streaming for simulator | (a) Keep current (accumulate in memory) (b) Add optional disk streaming | **(a) Keep current** — sufficient for 3 symbols; implement (b) only when scaling beyond ~10 symbols | Low priority |

### Standing Approvals That Apply

From `docs/DECISIONS_PENDING.md`:
- **D-07**: Standing approval for non-destructive pushes of tested milestones
- **D-01**: Binance endpoints allowlisted (owner action pending)
- **D-08**: Commits authored as "Claude" with co-author trailer

---

## Appendix A: File Inventory

All files examined during this assessment (read-only, no modifications):

| File | Lines | Purpose |
|---|---|---|
| `docs/PLAN_OVERVIEW.md` | — | Project overview and milestones |
| `docs/ROADMAP.md` | — | Milestone status and acceptance criteria |
| `docs/ARCHITECTURE.md` | — | Architecture diagram and stack |
| `docs/DATA_ARCHITECTURE.md` | — | Data products, storage layout, watermarks |
| `docs/DECISIONS_PENDING.md` | 36 | Owner and lead decisions |
| `docs/adr/0003-storage.md` | — | Storage ADR |
| `docs/tasks/M1-T1.md` | — | M1 task brief |
| `scripts/verify_live.ps1` | — | M1 gate wrapper script |
| `scripts/verify_live.py` | 378 | M1 gate evidence generator |
| `scripts/import_history.ps1` | — | M2 import script |
| `configs/universe.yaml` | — | Symbol universe (3 symbols) |
| `configs/data_quality.yaml` | — | Freshness budgets, gap retry config |
| `pyproject.toml` | — | Dependencies, Python 3.13 |
| `src/tsclaude/marketdata/binance/ws.py` | 684 | WebSocket connection manager |
| `src/tsclaude/marketdata/bars.py` | 389 | Bar builder, watermark, gap detection |
| `src/tsclaude/marketdata/archive/importer.py` | 735 | Archive import pipeline |
| `src/tsclaude/marketdata/archive/download.py` | 80+ | Resumable downloader |
| `src/tsclaude/store/lake.py` | 668 | Parquet lake, content-addressed parts |
| `src/tsclaude/store/lake_reader.py` | 280 | Guarded research read API |
| `src/tsclaude/store/livebars.py` | 263 | SQLite live bar storage |
| `src/tsclaude/quality/gaps.py` | 357 | Gap registry, RangeSet |
| `src/tsclaude/runtime/supervisor.py` | 354 | Monitoring supervisor |
| `src/tsclaude/sim/engine.py` | 376 | Simulator loop |
| `src/tsclaude/sim/book.py` | 1440 | Position/trade management |
| `src/tsclaude/research/runner.py` | 1273 | Backtest runner |
| `src/tsclaude/features/core.py` | 253 | Windowed feature functions |

**Total source files:** 111  
**Total test files:** 83

## Appendix B: Mapping to Audit Document §8 Deliverables

The audit document requested 5 separate files. This single assessment covers all of them:

| Requested Deliverable | Covered In |
|---|---|
| `AUDIT_CLAUDE_M1_M2_MEMORY.md` | §2 (Architecture), §3.1-3.4 (M1), §4 (M2) |
| `M1_ROOT_CAUSE_AND_RETEST_PLAN.md` | §3 (entire section) |
| `M2_DISK_BACKED_FEASIBILITY.md` | §4 (entire section) |
| `RUST_CANDIDATE_MATRIX_CLAUDE.md` | §5 (entire section) |
| `OWNER_DECISIONS_CLAUDE.md` | §10 (Owner Decision Register) |
