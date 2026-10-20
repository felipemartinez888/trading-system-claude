"""Data-quality report for one imported dataset (M2-T1 deliverable 5).

Reported, never repaired: missing minutes (interior gaps, plus uncovered head/tail of the request),
duplicates (identical and conflicting), rows rejected by validation (OHLC violations, malformed
rows), zero-volume runs, and a cross-check of derived 15m/1h bars against Binance's native klines.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from tsclaude.contracts.market import Bar, Interval
from tsclaude.contracts.timeutil import MS_PER_MINUTE, to_iso
from tsclaude.marketdata.aggregate import aggregate
from tsclaude.marketdata.types import RestKline

SAMPLE = 20
ZERO_VOLUME_MIN_RUN = 3
VOLUME_REL_TOL = 1e-9
VOLUME_ABS_TOL = 1e-8


@dataclass
class QACollector:
    """Facts gathered while parsing and validating; turned into a report by `build_report`."""

    duplicates_identical: int = 0
    duplicate_conflicts: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)  # validation failures
    malformed: list[dict[str, Any]] = field(default_factory=list)  # unparseable rows
    unit_notes: list[str] = field(default_factory=list)
    unavailable: list[dict[str, Any]] = field(default_factory=list)  # archive files not found


def _ranges(
    times: Sequence[int], start_ms: int, end_ms: int, step: int
) -> tuple[list[tuple[int, int]], tuple[int, int] | None, tuple[int, int] | None]:
    """(interior gaps, uncovered head, uncovered tail) of sorted unique times in [start, end)."""
    if not times:
        return [], (start_ms, end_ms) if end_ms > start_ms else None, None
    gaps: list[tuple[int, int]] = []
    for a, b in pairwise(times):
        if b - a > step:
            gaps.append((a + step, b))
    head = (start_ms, times[0]) if times[0] > start_ms else None
    tail = (times[-1] + step, end_ms) if times[-1] + step < end_ms else None
    return gaps, head, tail


def _range_dict(lo: int, hi: int, step: int) -> dict[str, Any]:
    return {
        "from_ms": lo,
        "to_ms": hi,
        "from": to_iso(lo),
        "to": to_iso(hi),
        "count": (hi - lo) // step,
    }


def zero_volume_runs(
    bars: Sequence[Bar], min_run: int = ZERO_VOLUME_MIN_RUN
) -> list[tuple[int, int]]:
    """Runs of >= min_run consecutive zero-volume 1m bars, as [from, to) ranges."""
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    run_len = 0
    prev: int | None = None
    for b in bars:
        extends = (
            b.volume == 0
            and run_start is not None
            and prev is not None
            and b.open_time_ms == prev + MS_PER_MINUTE
        )
        if extends:
            run_len += 1
        else:
            if run_start is not None and prev is not None and run_len >= min_run:
                runs.append((run_start, prev + MS_PER_MINUTE))
            run_start, run_len = (b.open_time_ms, 1) if b.volume == 0 else (None, 0)
        prev = b.open_time_ms
    if run_start is not None and prev is not None and run_len >= min_run:
        runs.append((run_start, prev + MS_PER_MINUTE))
    return runs


def merge_runs(runs: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Join runs that touch (e.g. a run split at a month boundary)."""
    out: list[tuple[int, int]] = []
    for lo, hi in sorted(runs):
        if out and out[-1][1] == lo:
            out[-1] = (out[-1][0], hi)
        else:
            out.append((lo, hi))
    return out


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=VOLUME_REL_TOL, abs_tol=VOLUME_ABS_TOL)


def cross_check(
    bars: Sequence[Bar], native: Sequence[RestKline], interval: Interval
) -> dict[str, Any]:
    """Compare derived bars with native Binance klines of the same interval."""
    derived, skipped = aggregate(bars, interval)
    by_open = {k.open_time_ms: k for k in native}
    compared = 0
    mismatches: list[dict[str, Any]] = []
    for d in derived:
        n = by_open.get(d.open_time_ms)
        if n is None:
            continue
        compared += 1
        diffs = {}
        for f in ("open", "high", "low", "close"):
            if getattr(d, f) != getattr(n, f):
                diffs[f] = [getattr(d, f), getattr(n, f)]
        for f in ("volume", "quote_volume", "taker_buy_volume", "taker_buy_quote_volume"):
            if not _close(getattr(d, f), getattr(n, f)):
                diffs[f] = [getattr(d, f), getattr(n, f)]
        if d.trades != n.trades:
            diffs["trades"] = [d.trades, n.trades]
        if diffs:
            mismatches.append(
                {"open_time_ms": d.open_time_ms, "open": to_iso(d.open_time_ms), "diffs": diffs}
            )
    native_without_derived = len(set(by_open) - {d.open_time_ms for d in derived})
    return {
        "interval": str(interval),
        "native_bars": len(native),
        "derived_complete": len(derived),
        "derived_incomplete_skipped": skipped,
        "compared": compared,
        "native_without_complete_derived": native_without_derived,
        "mismatches": len(mismatches),
        "mismatch_samples": mismatches[:SAMPLE],
        "ok": compared > 0 and not mismatches,
    }


def build_report(
    *,
    bars_times: Sequence[int],
    start_ms: int,
    end_ms: int,
    step_ms: int,
    collector: QACollector,
    zero_runs: Sequence[tuple[int, int]] = (),
    cross_checks: Sequence[dict[str, Any]] = (),
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gaps, head, tail = _ranges(bars_times, start_ms, end_ms, step_ms)
    gap_count = sum((hi - lo) // step_ms for lo, hi in gaps)
    summary = {
        "rows": len(bars_times),
        "missing_interior": gap_count,
        "gap_ranges": len(gaps),
        "uncovered_head": 0 if head is None else (head[1] - head[0]) // step_ms,
        "uncovered_tail": 0 if tail is None else (tail[1] - tail[0]) // step_ms,
        "duplicates_identical": collector.duplicates_identical,
        "duplicate_conflicts": len(collector.duplicate_conflicts),
        "rejected_rows": len(collector.rejected),
        "malformed_rows": len(collector.malformed),
        "zero_volume_runs": len(zero_runs),
        "zero_volume_minutes": sum((hi - lo) // MS_PER_MINUTE for lo, hi in zero_runs),
        "unavailable_files": len(collector.unavailable),
        "cross_checks_ok": all(c.get("ok") for c in cross_checks) if cross_checks else None,
    }
    return {
        "summary": summary,
        "gaps": [_range_dict(lo, hi, step_ms) for lo, hi in gaps],
        "uncovered_head": None if head is None else _range_dict(*head, step_ms),
        "uncovered_tail": None if tail is None else _range_dict(*tail, step_ms),
        "duplicate_conflicts": collector.duplicate_conflicts[:SAMPLE],
        "rejected_rows": collector.rejected[:SAMPLE],
        "malformed_rows": collector.malformed[:SAMPLE],
        "zero_volume_runs": [_range_dict(lo, hi, MS_PER_MINUTE) for lo, hi in zero_runs[:SAMPLE]],
        "unit_notes": collector.unit_notes,
        "unavailable_files": collector.unavailable,
        "cross_checks": list(cross_checks),
        **(extra or {}),
        "notes": [
            "gaps are reported, never interpolated",
            "uncovered head/tail can be before listing / after delisting or not yet published",
        ],
    }
