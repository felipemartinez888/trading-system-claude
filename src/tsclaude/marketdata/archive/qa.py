"""Data-quality report for one imported dataset (M2-T1 deliverable 5).

Reported, never repaired: missing minutes (interior gaps, plus uncovered head/tail of the request),
duplicates (identical and conflicting), rows rejected by validation (OHLC violations, malformed
rows), zero-volume runs, and a cross-check of derived 15m/1h bars against Binance's native klines.

QA report version 2 (M2-E2; ADR-0014 rule 2, ADR-0015) adds, without changing any stored or
derived bar:
- the classification of every zero-volume 1m candle: Z1 genuine no-trade, Z2 zero-trade filler
  inside a registered exchange interruption, A anomalous (listed individually);
- the split of every missing minute of the request (interior gaps, uncovered head and tail) into
  registered downtime (only the documented absence period `[absent_from, to)` of an approved
  registry entry for the dataset's market) and unexpected missing minutes, which keep failing
  (`classification_ok` false);
- the single explained cross-check difference `leading_zero_trade_open`, with its evidence.
The exchange-interruption registry is read here for that classification only: it exempts nothing
from the cross-check, changes no execution and is never used by live or paper.
Reports written before version 2 carry no `qa_report_version` and keep their original meaning.
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
from tsclaude.quality.interruptions import ExchangeInterruption
from tsclaude.store.holdout import HOLDOUT_END_MS, HOLDOUT_START_MS

SAMPLE = 20
ZERO_VOLUME_MIN_RUN = 3
VOLUME_REL_TOL = 1e-9
VOLUME_ABS_TOL = 1e-8

#: version 2 (M2-E2): zero-volume classes, missing-minute split, explained cross-check differences
QA_REPORT_VERSION = 2
#: the only explained cross-check difference (ADR-0015)
LEADING_ZERO_TRADE_OPEN = "leading_zero_trade_open"
LONG_ZERO_TRADE_RUN = 30  # minutes; a Z1 run this long is flagged for review (ADR-0014 rule 2)
LISTED_LIMIT = 500  # class A rows and explained mismatches listed individually, up to this many

Z1, Z2, A = "Z1", "Z2", "A"
#: class A reasons (ADR-0014): no class is ever inferred from an unseen candle
A_PREVIOUS_MISSING = "previous_minute_missing"  # incl. the first candle of the data
A_FLAT_NOT_PREVIOUS_CLOSE = "flat_price_differs_from_previous_close"
A_NOT_FLAT = "zero_trades_not_flat"
A_VOLUME_WITH_ZERO_TRADES = "volume_with_zero_trades"
A_ZERO_VOLUME_WITH_TRADES = "zero_volume_with_trades"


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


# ---------------------------------------------------------------- zero-volume classes (v2)
def is_z1_shape(b: Bar) -> bool:
    """No trade, every volume field 0, O=H=L=C. Whether it is Z1 also needs the previous close."""
    return (
        b.trades == 0
        and b.volume == 0
        and b.quote_volume == 0
        and b.taker_buy_volume == 0
        and b.taker_buy_quote_volume == 0
        and b.open == b.high == b.low == b.close
    )


def _registered(intervals: Sequence[ExchangeInterruption], t: int) -> ExchangeInterruption | None:
    for e in intervals:
        if e.contains(t):
            return e
    return None


#: (open time, close) of the candle before; all a zero-volume classification needs from it
Previous = tuple[int, float]


def previous_of(b: Bar) -> Previous:
    return (b.open_time_ms, b.close)


def classify_candle(
    b: Bar, prev: Previous | None, intervals: Sequence[ExchangeInterruption]
) -> tuple[str, str | None] | None:
    """The class of one zero-volume candle (`trades == 0` or `volume == 0`), or None for an
    ordinary candle. `prev` is the (open time, close) of the candle before, or None if it is not
    available (missing, or outside the data): then the filler cannot be checked, so the class is
    A. Only a Z1-shaped candle's class depends on `prev`."""
    if b.trades != 0 and b.volume != 0:
        return None
    if b.trades != 0:
        return A, A_ZERO_VOLUME_WITH_TRADES
    if not is_z1_shape(b):
        flat = b.open == b.high == b.low == b.close
        return A, (A_VOLUME_WITH_ZERO_TRADES if flat else A_NOT_FLAT)
    if prev is None or prev[0] != b.open_time_ms - MS_PER_MINUTE:
        return A, A_PREVIOUS_MISSING
    if b.open != prev[1]:
        return A, A_FLAT_NOT_PREVIOUS_CLOSE
    return (Z2, None) if _registered(intervals, b.open_time_ms) else (Z1, None)


def _bar_values(b: Bar) -> dict[str, Any]:
    return {
        "open": b.open,
        "high": b.high,
        "low": b.low,
        "close": b.close,
        "volume": b.volume,
        "quote_volume": b.quote_volume,
        "taker_buy_volume": b.taker_buy_volume,
        "taker_buy_quote_volume": b.taker_buy_quote_volume,
        "trades": b.trades,
    }


@dataclass
class ZeroTradeClasses:
    """Zero-volume classes of a stretch of 1m bars (one month of the importer, or all of it).

    Small by construction: counts, `[from, to)` ranges and at most `LISTED_LIMIT` class A rows,
    never the bars themselves. To join stretches it keeps the (open time, close) of the last bar
    and, only if the first bar is Z1-shaped (its class depends on the stretch before), that bar."""

    z1_minutes: int = 0
    z2_minutes: int = 0
    anomaly_count: int = 0
    z1_ranges: list[tuple[int, int]] = field(default_factory=list)
    z2_ranges: list[tuple[int, int]] = field(default_factory=list)
    #: observed candles inside a documented absence period `[absent_from, to)`: the data
    #: contradicts the registry, so they are a finding, never downtime
    absence_observed: list[tuple[int, int]] = field(default_factory=list)
    anomalies: list[dict[str, Any]] = field(default_factory=list)  # class A rows, time order
    #: a Z1-shaped first bar, left unclassified: its previous candle lies outside this stretch
    first: Bar | None = None
    last: Previous | None = None

    def add(
        self, b: Bar, cls: tuple[str, str | None] | None, intervals: Sequence[ExchangeInterruption]
    ) -> None:
        t = b.open_time_ms
        entry = _registered(intervals, t)
        if entry is not None and t >= entry.absent_from_ms:
            self.absence_observed.append((t, t + MS_PER_MINUTE))
        if cls is None:
            return
        kind, reason = cls
        if kind == Z1:
            self.z1_minutes += 1
            self.z1_ranges.append((t, t + MS_PER_MINUTE))
        elif kind == Z2:
            self.z2_minutes += 1
            self.z2_ranges.append((t, t + MS_PER_MINUTE))
        else:
            self.anomaly_count += 1
            if len(self.anomalies) < LISTED_LIMIT:
                self.anomalies.append(
                    {"open_time_ms": t, "time": to_iso(t), "reason": reason, "row": _bar_values(b)}
                )


def classify_zero_trade(
    bars: Sequence[Bar],
    intervals: Sequence[ExchangeInterruption] = (),
    *,
    defer_first: bool = False,
) -> ZeroTradeClasses:
    """Classify every zero-volume candle of time-ordered 1m bars of one market. With
    `defer_first`, a Z1-shaped first bar is kept unclassified (`first`) for `join_zero_trade`,
    because its previous candle belongs to the stretch before."""
    out = ZeroTradeClasses()
    prev: Previous | None = None
    for i, b in enumerate(bars):
        if i == 0 and defer_first and is_z1_shape(b):
            out.first = b
        else:
            out.add(b, classify_candle(b, prev, intervals), intervals)
        prev = previous_of(b)
    out.last = prev
    _merge_ranges(out)
    return out


def _merge_ranges(c: ZeroTradeClasses) -> None:
    c.z1_ranges = merge_runs(c.z1_ranges)
    c.z2_ranges = merge_runs(c.z2_ranges)
    c.absence_observed = merge_runs(c.absence_observed)


def join_zero_trade(
    parts: Sequence[ZeroTradeClasses], intervals: Sequence[ExchangeInterruption] = ()
) -> ZeroTradeClasses:
    """Join consecutive stretches (in time order) classified with `defer_first`: each deferred
    first bar is classified against the last bar of the stretch before, if that is the minute
    immediately before it. The result equals classifying all bars at once."""
    out = ZeroTradeClasses()
    before: Previous | None = None
    for p in parts:
        if p.first is not None:
            out.add(p.first, classify_candle(p.first, before, intervals), intervals)
        out.z1_minutes += p.z1_minutes
        out.z2_minutes += p.z2_minutes
        out.anomaly_count += p.anomaly_count
        out.z1_ranges.extend(p.z1_ranges)
        out.z2_ranges.extend(p.z2_ranges)
        out.absence_observed.extend(p.absence_observed)
        out.anomalies.extend(p.anomalies)
        if p.last is not None:
            before = p.last
    out.anomalies = out.anomalies[:LISTED_LIMIT]
    out.last = before
    _merge_ranges(out)
    return out


def _minutes(ranges: Sequence[tuple[int, int]]) -> int:
    return sum((hi - lo) // MS_PER_MINUTE for lo, hi in ranges)


def zero_trade_section(c: ZeroTradeClasses) -> dict[str, Any]:
    runs = [r for r in c.z1_ranges if (r[1] - r[0]) // MS_PER_MINUTE >= ZERO_VOLUME_MIN_RUN]
    long_runs = [r for r in runs if (r[1] - r[0]) // MS_PER_MINUTE >= LONG_ZERO_TRADE_RUN]
    return {
        "definition": (
            "Z1: trades 0, every volume 0, O=H=L=C equal to the previous minute's close, outside "
            "any registered interruption; Z2: the same inside a registered interruption; A: any "
            "other candle with trades 0 or volume 0 (incl. one whose previous minute is missing)"
        ),
        "z1_minutes": c.z1_minutes,
        "z2_minutes": c.z2_minutes,
        "a_rows": c.anomaly_count,
        "z1_runs": len(runs),
        "z1_run_minutes": _minutes(runs),
        "z1_run_samples": [_range_dict(lo, hi, MS_PER_MINUTE) for lo, hi in runs[:SAMPLE]],
        "long_z1_runs": [_range_dict(lo, hi, MS_PER_MINUTE) for lo, hi in long_runs[:LISTED_LIMIT]],
        "long_z1_run_note": (
            f"Z1 runs of at least {LONG_ZERO_TRADE_RUN} minutes need review; a run seen in every "
            "symbol of the market is only a candidate for the registry (ADR-0013), never waived"
        ),
        "z2_ranges": [_range_dict(lo, hi, MS_PER_MINUTE) for lo, hi in c.z2_ranges[:LISTED_LIMIT]],
        "observed_in_documented_absence": {
            "minutes": _minutes(c.absence_observed),
            "ranges": [
                _range_dict(lo, hi, MS_PER_MINUTE) for lo, hi in c.absence_observed[:LISTED_LIMIT]
            ],
        },
        "a_rows_listed": redact_holdout(c.anomalies),
        "a_rows_truncated": c.anomaly_count > len(c.anomalies),
    }


def missing_section(
    gaps: Sequence[tuple[int, int]], intervals: Sequence[ExchangeInterruption]
) -> dict[str, Any]:
    """Missing ranges split into registered downtime (inside the documented absence period
    `[absent_from, to)` of an entry of this market) and unexpected missing minutes. A minute
    missing in `[from, absent_from)` was published by the exchange, so it is unexpected."""
    registered: list[dict[str, Any]] = []
    for lo, hi in gaps:
        for e in intervals:
            a, b = max(lo, e.absent_from_ms), min(hi, e.to_ms)
            if b > a:
                registered.append({**_range_dict(a, b, MS_PER_MINUTE), "registry_id": e.id})
    total = _minutes(gaps)
    inside = sum(int(r["count"]) for r in registered)
    return {
        "registered_downtime_minutes": inside,
        "unexpected_minutes": total - inside,
        "registered_downtime": registered,
        "note": (
            "only the documented absence period of an approved registry entry of this market is "
            "registered downtime; every other missing minute is unexpected and fails"
        ),
    }


# ---------------------------------------------------------------- cross-check
def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=VOLUME_REL_TOL, abs_tol=VOLUME_ABS_TOL)


def explain_open_difference(
    derived: Bar,
    native: RestKline,
    bars_by_open: dict[int, Bar],
    step_ms: int,
    intervals: Sequence[ExchangeInterruption] = (),
) -> dict[str, Any] | None:
    """ADR-0015 `leading_zero_trade_open`, for a bucket whose only differing field is `open`
    (high, low, close and trades exact, volumes within tolerance). Every condition must hold;
    otherwise None and the mismatch stays unexplained:
    - the candle before the bucket is available (fails closed at a month boundary);
    - the bucket's leading k >= 1 candles are Z1 (each flat at the close before it, outside any
      registered interruption);
    - the derived open equals the first 1m open, the filler, and the close before the bucket;
    - the native open equals the open of the first 1m candle with trades > 0;
    - all 1m candles of the bucket are present."""
    start = derived.open_time_ms
    prev = bars_by_open.get(start - MS_PER_MINUTE)
    if prev is None:
        return None
    minutes = [bars_by_open.get(start + i * MS_PER_MINUTE) for i in range(step_ms // MS_PER_MINUTE)]
    if any(b is None for b in minutes):
        return None
    k = 0
    before = previous_of(prev)
    first_traded: Bar | None = None
    for b in minutes:
        assert b is not None
        if b.trades > 0:
            first_traded = b
            break
        if classify_candle(b, before, intervals) != (Z1, None):
            return None
        k += 1
        before = previous_of(b)
    first = minutes[0]
    assert first is not None
    if k < 1 or first_traded is None:
        return None
    if not (derived.open == first.open == prev.close):
        return None
    if native.open != first_traded.open:
        return None
    return {
        "reason": LEADING_ZERO_TRADE_OPEN,
        "k": k,
        "filler": first.open,
        "first_traded_open": first_traded.open,
        "previous_minute": to_iso(prev.open_time_ms),
        "first_minute": to_iso(first.open_time_ms),
        "first_traded_minute": to_iso(first_traded.open_time_ms),
    }


def cross_check(
    bars: Sequence[Bar],
    native: Sequence[RestKline],
    interval: Interval,
    intervals: Sequence[ExchangeInterruption] = (),
) -> dict[str, Any]:
    """Compare derived bars with native Binance klines of the same interval. A mismatch is
    explained only by `explain_open_difference`; `ok` is true only with no unexplained one."""
    derived, skipped = aggregate(bars, interval)
    by_open = {k.open_time_ms: k for k in native}
    bars_by_open: dict[int, Bar] | None = None
    compared = 0
    mismatches: list[dict[str, Any]] = []
    explained: list[dict[str, Any]] = []
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
            explanation = None
            if set(diffs) == {"open"}:
                if bars_by_open is None:
                    bars_by_open = {b.open_time_ms: b for b in bars}
                explanation = explain_open_difference(d, n, bars_by_open, interval.ms, intervals)
            entry = {
                "open_time_ms": d.open_time_ms,
                "time": to_iso(d.open_time_ms),
                "diffs": diffs,
                "explanation": explanation,
            }
            mismatches.append(entry)
            if explanation is not None:
                explained.append(entry)
    native_without_derived = len(set(by_open) - {d.open_time_ms for d in derived})
    unexplained = len(mismatches) - len(explained)
    return {
        "interval": str(interval),
        "native_bars": len(native),
        "derived_complete": len(derived),
        "derived_incomplete_skipped": skipped,
        "compared": compared,
        "native_without_complete_derived": native_without_derived,
        "mismatches": len(mismatches),
        "mismatches_explained": len(explained),
        "mismatches_unexplained": unexplained,
        "explained_by_reason": {LEADING_ZERO_TRADE_OPEN: len(explained)} if explained else {},
        "mismatch_samples": mismatches[:SAMPLE],
        "explained_mismatches": explained[:LISTED_LIMIT],
        "ok": compared > 0 and unexplained == 0,
    }


_PRICE_BEARING = ("row", "kept", "dropped")
_EXPLANATION_PRICES = ("filler", "first_traded_open")


def _in_holdout(entry: dict[str, Any]) -> bool:
    t = entry.get("open_time_ms", entry.get("funding_time_ms"))
    return isinstance(t, int) and HOLDOUT_START_MS <= t < HOLDOUT_END_MS


def redact_holdout(entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Decision L-07 c: QA outputs carry no holdout prices -- counts and timestamps only."""
    out: list[dict[str, Any]] = []
    for e in entries:
        if not _in_holdout(e):
            out.append(e)
            continue
        r = {k: v for k, v in e.items() if k not in _PRICE_BEARING}
        if "diffs" in r:
            r["diffs"] = sorted(r["diffs"])  # field names only
        if isinstance(r.get("explanation"), dict):
            r["explanation"] = {
                k: v for k, v in r["explanation"].items() if k not in _EXPLANATION_PRICES
            }
        r["redacted"] = "holdout period: values withheld"
        out.append(r)
    return out


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
    zero_trade: ZeroTradeClasses | None = None,
    interruptions: Sequence[ExchangeInterruption] = (),
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The QA report (version `QA_REPORT_VERSION`). `zero_trade` is given for 1m bar imports only;
    the classification fields are None otherwise."""
    gaps, head, tail = _ranges(bars_times, start_ms, end_ms, step_ms)
    gap_count = sum((hi - lo) // step_ms for lo, hi in gaps)
    explained = sum(int(c.get("mismatches_explained", 0)) for c in cross_checks)
    unexplained = sum(int(c.get("mismatches_unexplained", 0)) for c in cross_checks)
    uncovered = [r for r in (head, tail) if r is not None]
    missing = (
        missing_section([*gaps, *uncovered], interruptions) if zero_trade is not None else None
    )
    explained_by_reason: dict[str, int] = {}
    for c in cross_checks:
        for reason, count in c.get("explained_by_reason", {}).items():
            explained_by_reason[reason] = explained_by_reason.get(reason, 0) + count
    findings: list[str] = []
    if zero_trade is not None and missing is not None:
        if not bars_times:
            findings.append("no rows imported")
        if missing["unexpected_minutes"]:
            findings.append(f"unexpected missing minutes: {missing['unexpected_minutes']}")
        if zero_trade.anomaly_count:
            findings.append(f"class A zero-volume candles: {zero_trade.anomaly_count}")
        observed = _minutes(zero_trade.absence_observed)
        if observed:
            findings.append(f"candles observed inside a documented absence period: {observed}")
    summary = {
        "qa_report_version": QA_REPORT_VERSION,
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
        "cross_check_mismatches_explained": explained,
        "cross_check_explained_by_reason": explained_by_reason,
        "cross_check_mismatches_unexplained": unexplained,
        "zero_trade_z1_minutes": None if zero_trade is None else zero_trade.z1_minutes,
        "zero_trade_z2_minutes": None if zero_trade is None else zero_trade.z2_minutes,
        "zero_trade_a_rows": None if zero_trade is None else zero_trade.anomaly_count,
        "missing_registered_downtime": None
        if missing is None
        else missing["registered_downtime_minutes"],
        "missing_unexpected": None if missing is None else missing["unexpected_minutes"],
        "classification_ok": None if zero_trade is None else not findings,
    }
    return {
        "qa_report_version": QA_REPORT_VERSION,
        "summary": summary,
        "gaps": [_range_dict(lo, hi, step_ms) for lo, hi in gaps],
        "uncovered_head": None if head is None else _range_dict(*head, step_ms),
        "uncovered_tail": None if tail is None else _range_dict(*tail, step_ms),
        "duplicate_conflicts": redact_holdout(collector.duplicate_conflicts[:SAMPLE]),
        "rejected_rows": redact_holdout(collector.rejected[:SAMPLE]),
        "malformed_rows": collector.malformed[:SAMPLE],
        "zero_volume_runs": [_range_dict(lo, hi, MS_PER_MINUTE) for lo, hi in zero_runs[:SAMPLE]],
        "unit_notes": collector.unit_notes,
        "unavailable_files": collector.unavailable,
        "cross_checks": [
            {
                **c,
                "mismatch_samples": redact_holdout(c.get("mismatch_samples", [])),
                **(
                    {"explained_mismatches": redact_holdout(c["explained_mismatches"])}
                    if "explained_mismatches" in c
                    else {}
                ),
            }
            for c in cross_checks
        ],
        "zero_trade_classification": None if zero_trade is None else zero_trade_section(zero_trade),
        "missing_classification": missing,
        "classification_findings": None if zero_trade is None else findings,
        "exchange_interruptions": registry,
        **(extra or {}),
        "notes": [
            "gaps are reported, never interpolated",
            "uncovered head/tail can be before listing / after delisting or not yet published",
            "the exchange-interruption registry is read for data-quality classification only",
        ],
    }
