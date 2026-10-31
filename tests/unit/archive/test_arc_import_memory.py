"""Bounded-memory properties of the archive importer (AP-1, plan tests T3 and T4).

The importer processes each UTC month inside its own task (fetch, merge, validate, stage) and keeps
only small summaries. These tests state that as measurable properties and run the frozen
pre-change pipeline (`legacy_importer`) through the same probes to show they can fail.
"""

from __future__ import annotations

import asyncio
import gc
import tracemalloc
import weakref
from pathlib import Path
from typing import Any

import pytest
from arc_helpers import MIN, JsonLinesCodec, YieldingArchive, edge_lines, ms
from legacy_importer import LegacyImporter

from tsclaude.contracts.market import Bar, Market
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.marketdata.archive.importer import Importer, ImportRequest
from tsclaude.marketdata.archive.paths import ArchiveDataType, ArchiveFile, Period
from tsclaude.store.lake import Lake, LakeKind
from tsclaude.store.lake_writer import LakeWriter

NOW = ms(2026, 10, 20, 12, 30)
SYM = "BTCUSDT"


def _file(label: str) -> ArchiveFile:
    return ArchiveFile(Market.SPOT, ArchiveDataType.KLINES, SYM, "1m", Period.MONTHLY, label)


def _labels(first_year: int, first_month: int, n: int) -> list[str]:
    out = []
    y, m = first_year, first_month
    for _ in range(n):
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _archive(labels: list[str], edge: int) -> YieldingArchive:
    archive = YieldingArchive()
    for label in labels:
        f = _file(label)
        archive.add(f, edge_lines(f, edge))
    return archive


def _request(labels: list[str]) -> ImportRequest:
    y, m = (int(x) for x in labels[-1].split("-"))
    end = ms(y + 1, 1) if m == 12 else ms(y, m + 1)
    first_y, first_m = (int(x) for x in labels[0].split("-"))
    return ImportRequest(Market.SPOT, LakeKind.BARS_1M, SYM, ms(first_y, first_m), end)


def _importer(
    cls: type[Importer], tmp_path: Path, archive: YieldingArchive, concurrency: int
) -> Importer:
    lake = Lake(tmp_path / "data", JsonLinesCodec())
    return cls(
        LakeWriter(lake.data_dir, lake.codec),
        archive,
        None,
        ManualClock(NOW),
        month_concurrency=concurrency,
    )


class _Tracked(list):  # type: ignore[type-arg]
    """A list that supports weak references (plain lists do not)."""


class _Alive:
    """Counts how many tracked containers are alive at once."""

    def __init__(self) -> None:
        self.alive = 0
        self.peak = 0

    def track(self, container: list[Any]) -> _Tracked:
        tracked = _Tracked(container)
        self.alive += 1
        self.peak = max(self.peak, self.alive)
        weakref.finalize(tracked, self._died).atexit = False
        return tracked

    def _died(self) -> None:
        self.alive -= 1


def _live_bars() -> int:
    gc.collect()
    return sum(1 for o in gc.get_objects() if type(o) is Bar)


def _instrument(monkeypatch: pytest.MonkeyPatch) -> tuple[_Alive, _Alive, dict[str, Any]]:
    """Track parsed-file row lists, merged-month row lists, and live `Bar`s at cross-check time."""
    parsed, merged = _Alive(), _Alive()
    # Bars left over from other tests in the same process are not ours: count relative to now.
    seen: dict[str, Any] = {"bars_alive": [], "bars_passed": [], "baseline": _live_bars()}
    orig_parse, orig_merge, orig_cross = (
        Importer._parse_file,
        Importer._merge,
        Importer._cross_checks,
    )

    def parse(self: Importer, *a: Any, **k: Any) -> Any:
        chunk = orig_parse(self, *a, **k)
        chunk.rows = parsed.track(chunk.rows)
        return chunk

    def merge(self: Importer, *a: Any, **k: Any) -> Any:
        return merged.track(orig_merge(self, *a, **k))

    async def cross(self: Importer, req: Any, label: str, bars: list[Bar], sources: Any) -> Any:
        seen["bars_alive"].append(_live_bars() - seen["baseline"])
        seen["bars_passed"].append(len(bars))
        return await orig_cross(self, req, label, bars, sources)

    monkeypatch.setattr(Importer, "_parse_file", parse)
    monkeypatch.setattr(Importer, "_merge", merge)
    monkeypatch.setattr(Importer, "_cross_checks", cross)
    return parsed, merged, seen


EDGE = 300  # minutes at each end of a month -> 600 rows per month
MONTHS = 12
CONCURRENCY = 2


def test_at_most_month_concurrency_months_of_raw_rows_are_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3: raw/merged rows of at most `month_concurrency` months exist at once, and only the two
    cross-check sample months keep `Bar`s until the end."""
    labels = _labels(2024, 1, MONTHS)
    parsed, merged, seen = _instrument(monkeypatch)
    res = asyncio.run(
        _importer(Importer, tmp_path, _archive(labels, EDGE), CONCURRENCY).run(_request(labels))
    )
    assert res.rows == MONTHS * 2 * EDGE
    assert 1 <= parsed.peak <= CONCURRENCY
    assert 1 <= merged.peak <= CONCURRENCY
    assert parsed.alive == merged.alive == 0  # every month's rows are gone once the run returns
    # two sample months (first and last) are cross-checked; no other month's Bars survive
    month_rows = 2 * EDGE
    assert seen["bars_passed"] == [month_rows, month_rows]
    assert seen["bars_alive"] == [2 * month_rows] * 2  # exactly the two sample months, nothing else
    print(f"T3 new: parsed peak={parsed.peak} merged peak={merged.peak} bars={seen['bars_alive']}")


def _z1_month_starts(labels: list[str], edge: int) -> YieldingArchive:
    """Sparse months whose first minute is a zero-trade filler at the previous month's last close:
    the only shape the bounded classification must retain until the months are joined."""
    archive = YieldingArchive()
    previous_close = 100.0
    for label in labels:
        f = _file(label)
        lines = edge_lines(f, edge)
        first = [f.start_ms, *[previous_close] * 4, 0.0, f.start_ms + MIN - 1, 0.0, 0, 0.0, 0.0, 0]
        lines[0] = ",".join(str(v) for v in first)
        archive.add(f, lines)
        previous_close = float(lines[-1].split(",")[4])
    return archive


def test_zero_trade_month_starts_keep_the_bars_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2-E2: one retained first bar per month at most, on top of the two sample months; the
    classification across every month boundary is still complete."""
    labels = _labels(2024, 1, MONTHS)
    parsed, merged, seen = _instrument(monkeypatch)
    res = asyncio.run(
        _importer(Importer, tmp_path, _z1_month_starts(labels, EDGE), CONCURRENCY).run(
            _request(labels)
        )
    )
    assert res.rows == MONTHS * 2 * EDGE
    assert 1 <= parsed.peak <= CONCURRENCY
    assert 1 <= merged.peak <= CONCURRENCY
    month_rows = 2 * EDGE
    assert seen["bars_passed"] == [month_rows, month_rows]
    assert all(alive <= 2 * month_rows + MONTHS for alive in seen["bars_alive"])
    summary = res.qa["summary"]
    assert summary["zero_trade_z1_minutes"] == MONTHS - 1  # the very first one has no predecessor
    assert summary["zero_trade_a_rows"] == 1
    assert res.qa["zero_trade_classification"]["a_rows_listed"][0]["reason"] == (
        "previous_minute_missing"
    )


def test_retention_probe_detects_the_old_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The T3 probe is not vacuous: the pre-change pipeline holds every month's merged rows."""
    labels = _labels(2024, 1, MONTHS)
    _parsed, merged, _seen = _instrument(monkeypatch)
    asyncio.run(
        _importer(LegacyImporter, tmp_path, _archive(labels, EDGE), CONCURRENCY).run(
            _request(labels)
        )
    )
    assert merged.peak == MONTHS  # all months retained at once, regardless of the semaphore
    print(f"T3 old: merged peak={merged.peak}")


def _traced_peak(cls: type[Importer], tmp_path: Path, n_months: int, rows_edge: int) -> int:
    labels = _labels(2024, 1, n_months)
    archive = _archive(labels, rows_edge)  # built before tracing starts
    importer = _importer(cls, tmp_path, archive, CONCURRENCY)
    req = _request(labels)
    gc.collect()
    tracemalloc.start()
    try:
        asyncio.run(importer.run(req))
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


TRACE_EDGE = 2000  # 4,000 rows per month


def test_peak_memory_does_not_grow_with_request_length(tmp_path: Path) -> None:
    """T4: tracemalloc peak is flat in the request length. The plan's 6-vs-2-month shape (ratio
    < 1.6) holds, but it does not separate the pipelines (the pre-change one measures about 1.5
    there: the Bars of the two sample months are a fixed cost), so the discriminating check is
    12 vs 2 months with a stricter bound: new about 1.0, pre-change about 2.0."""
    peaks = {n: _traced_peak(Importer, tmp_path / str(n), n, TRACE_EDGE) for n in (2, 6, 12)}
    mb = {n: round(p / 1e6, 1) for n, p in peaks.items()}
    r6, r12 = peaks[6] / peaks[2], peaks[12] / peaks[2]
    print(f"T4 new: peak MB by months {mb}, x{r6:.2f} (6/2), x{r12:.2f} (12/2)")
    assert r6 < 1.6
    assert r12 < 1.3


def test_scaling_probe_detects_the_old_pipeline(tmp_path: Path) -> None:
    """The T4 probe is not vacuous: the pre-change pipeline fails the 12-vs-2 check above (its
    ratio is about 2.0, and it also exceeds the plan's 1.6 at 12 vs 2)."""
    short = _traced_peak(LegacyImporter, tmp_path / "s", 2, TRACE_EDGE)
    long = _traced_peak(LegacyImporter, tmp_path / "l", 12, TRACE_EDGE)
    ratio = long / short
    print(f"T4 old: 2 months={short / 1e6:.1f} MB, 12 months={long / 1e6:.1f} MB, x{ratio:.2f}")
    assert ratio > 1.6
