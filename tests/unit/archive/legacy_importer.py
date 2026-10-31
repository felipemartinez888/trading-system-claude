"""Frozen copy of the pre-AP-1 kline pipeline, kept only as a test reference.

`LegacyImporter` is the importer exactly as it behaved before the bounded-memory change: every
month's merged rows are held until all months are fetched, each row becomes a `Bar` twice
(`_bar_row`, then `_bar_from_row`), and validation/staging run after the gather. The equivalence
tests assert the current `Importer` produces identical, content-addressed output; the memory tests
use it to show the retention/scaling assertions can fail (they are not vacuous).

M2-E2: the reference also classifies zero-volume candles, over all bars of the request at once
(`classify_zero_trade` without `defer_first`), so the equivalence tests check the bounded
per-month classification and its month-boundary join against the whole-sequence result.
"""

from __future__ import annotations

import asyncio
from array import array
from typing import Any

from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.marketdata.archive import paths
from tsclaude.marketdata.archive.importer import (
    _KIND_SPEC,
    Importer,
    ImportRequest,
    ImportResult,
    PlanItem,
    _Chunk,
)
from tsclaude.marketdata.archive.qa import (
    QACollector,
    build_report,
    classify_zero_trade,
    merge_runs,
    zero_volume_runs,
)
from tsclaude.store.lake_writer import LakeKind, PartInfo, Row, Staging


def legacy_bar_from_row(market: Market, symbol: str, r: Row) -> Bar:
    return Bar(
        market=market,
        symbol=symbol,
        interval=Interval.M1,
        open_time_ms=r["open_time_ms"],
        close_time_ms=r["close_time_ms"],
        open=r["open"],
        high=r["high"],
        low=r["low"],
        close=r["close"],
        volume=r["volume"],
        quote_volume=r["quote_volume"],
        trades=r["trades"],
        taker_buy_volume=r["taker_buy_volume"],
        taker_buy_quote_volume=r["taker_buy_quote_volume"],
        source=BarSource(r["source"]),
        ingested_at_ms=r["ingested_at_ms"],
    )


class LegacyImporter(Importer):
    async def _run_klines(self, req: ImportRequest, staging: Staging) -> ImportResult:
        data_type, interval, step = _KIND_SPEC[req.kind]
        assert data_type is not None
        collector = QACollector()
        sources: list[dict[str, Any]] = []
        months: dict[str, list[PlanItem]] = {}
        for item in self._plan(req):
            months.setdefault(paths.ms_to_date(item.start_ms).strftime("%Y-%m"), []).append(item)

        async def one_month(label: str) -> tuple[str, list[Any], list[dict[str, Any]]]:
            async with self._month_sem:
                chunks: list[_Chunk] = []
                for item in months[label]:
                    chunks.extend(
                        await self._resolve(req, data_type, interval, step, item, collector)
                    )
                rows = self._merge(chunks, req, collector)
                return label, rows, [c.record for c in chunks]

        results = await asyncio.gather(*(one_month(m) for m in sorted(months)))
        parts: list[PartInfo] = []
        times = array("q")
        zero_runs: list[tuple[int, int]] = []
        bars_by_month: dict[str, list[Bar]] = {}
        all_bars: list[Bar] = []
        sample = self._sample_months(req) if req.kind is LakeKind.BARS_1M else set()
        ingested = self._clock.now_ms()
        for label, raw_rows, records in sorted(results, key=lambda r: r[0]):
            sources.extend(records)
            valid, _bars = self._validate(req, raw_rows, step, ingested, collector)
            if not valid:
                continue
            times.extend(int(r["open_time_ms"]) for r in valid)
            if req.kind is LakeKind.BARS_1M:
                bars = [legacy_bar_from_row(req.market, req.symbol, r) for r in valid]
                zero_runs.extend(zero_volume_runs(bars, min_run=1))
                all_bars.extend(bars)
                if label in sample:
                    bars_by_month[label] = bars
            parts.extend(staging.write_parts(req.market, req.kind, req.symbol, valid))
        checks: list[dict[str, Any]] = []
        for label in sorted(bars_by_month):
            checks.extend(await self._cross_checks(req, label, bars_by_month[label], sources))
        registered = self._registry.for_market(req.market)
        runs = [r for r in merge_runs(zero_runs) if (r[1] - r[0]) // MS_PER_MINUTE >= 3]
        qa = build_report(
            bars_times=list(times),
            start_ms=req.start_ms,
            end_ms=self._effective_end(req, step),
            step_ms=step,
            collector=collector,
            zero_runs=runs if req.kind is LakeKind.BARS_1M else (),
            cross_checks=checks,
            zero_trade=(
                classify_zero_trade(all_bars, registered) if req.kind is LakeKind.BARS_1M else None
            ),
            interruptions=registered,
            registry=self._registry.manifest() if req.kind is LakeKind.BARS_1M else None,
        )
        return self._commit(req, interval, staging, parts, sources, qa)
