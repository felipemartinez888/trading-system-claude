"""The only read API for research over the Parquet lake.

`read_bars(market, symbol, start_ms, end_ms, *, purpose, ...)` returns closed canonical 1m bars with
their `source`, after the holdout guard (store.holdout) has approved the range. The same guard
applies to mark-price and funding reads. Nothing is interpolated: missing minutes are simply absent
(and listed in `ReadResult.missing`).

Several parts may cover the same minute (overlapping imports). Identical rows are merged. Differing
rows are a **conflict**: the row from the higher-ranked source wins (archive > rest_backfill), ties
broken by the lexicographically smaller part content hash (deterministic), and every conflict is
reported in the result. `dataset_ids=` pins the read to specific datasets, which is how a backtest
manifest makes a run reproducible.

Parts of the sealed holdout (decision L-07 b) are opened only with the guard's approval.
`daily_quote_volumes` / `first_daily_open` serve volume-only facts for the research universe rule
and are guarded by the same holdout rule (L-08).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tsclaude.contracts.market import Bar, BarSource, Interval, Market
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.store.holdout import HoldoutApproval, HoldoutGuard, Purpose
from tsclaude.store.lake import TIME_KEY, IndexedPart, Lake, LakeKind, PartCodec, Row

_SOURCE_RANK = {
    str(BarSource.ARCHIVE): 0,
    str(BarSource.REST_BACKFILL): 1,
    str(BarSource.REST_RECOVERED): 2,
    str(BarSource.WS_LIVE): 3,
}
_MARKET_FIELDS = (
    "close_time_ms",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trades",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "funding_rate",
    "mark_price",
)


@dataclass
class ReadResult[T]:
    items: list[T]
    dataset_ids: tuple[str, ...]
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    missing: list[tuple[int, int]] = field(default_factory=list)  # 1m kinds only


class LakeReader:
    def __init__(self, lake: Lake, guard: HoldoutGuard) -> None:
        self._lake = lake
        self._guard = guard

    @classmethod
    def open(
        cls, data_dir: Path | str, guard: HoldoutGuard, codec: PartCodec | None = None
    ) -> LakeReader:
        return cls(Lake(data_dir, codec), guard)

    def refresh(self) -> None:
        self._lake.refresh()

    # ------------------------------------------------------------------ guarded research reads
    def read_bars(
        self,
        market: Market,
        symbol: str,
        start_ms: int,
        end_ms: int,
        *,
        purpose: Purpose | str,
        prereg_id: str | None = None,
        strategy_hash: str | None = None,
        dataset_ids: Collection[str] | None = None,
    ) -> ReadResult[Bar]:
        approval = self._guard.check(
            start_ms, end_ms, purpose=purpose, prereg_id=prereg_id, strategy_hash=strategy_hash
        )
        rows, used, conflicts = self._read(
            market, LakeKind.BARS_1M, symbol, start_ms, end_ms, dataset_ids, approval
        )
        bars = [_bar(market, symbol, r) for r in rows]
        return ReadResult(bars, used, conflicts, _missing(rows, start_ms, end_ms))

    def read_mark_price_1m(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
        *,
        purpose: Purpose | str,
        prereg_id: str | None = None,
        strategy_hash: str | None = None,
        dataset_ids: Collection[str] | None = None,
    ) -> ReadResult[Row]:
        approval = self._guard.check(
            start_ms, end_ms, purpose=purpose, prereg_id=prereg_id, strategy_hash=strategy_hash
        )
        rows, used, conflicts = self._read(
            Market.USDM, LakeKind.MARK_PRICE_1M, symbol, start_ms, end_ms, dataset_ids, approval
        )
        return ReadResult(rows, used, conflicts, _missing(rows, start_ms, end_ms))

    def read_funding(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
        *,
        purpose: Purpose | str,
        prereg_id: str | None = None,
        strategy_hash: str | None = None,
        dataset_ids: Collection[str] | None = None,
    ) -> ReadResult[Row]:
        approval = self._guard.check(
            start_ms, end_ms, purpose=purpose, prereg_id=prereg_id, strategy_hash=strategy_hash
        )
        rows, used, conflicts = self._read(
            Market.USDM, LakeKind.FUNDING, symbol, start_ms, end_ms, dataset_ids, approval
        )
        return ReadResult(rows, used, conflicts)

    # ------------------------------------------------------------------ unguarded, volume only
    def daily_quote_volumes(
        self, market: Market, symbol: str, start_ms: int, end_ms: int
    ) -> dict[int, float]:
        """UTC day open time -> quote volume (native daily klines). No prices are returned.

        Guarded like research reads (L-08): a range touching the holdout is refused.
        """
        approval = self._guard.check(start_ms, end_ms, purpose=Purpose.RESEARCH)
        rows, _, _ = self._read(
            market, LakeKind.KLINES_1D, symbol, start_ms, end_ms, None, approval
        )
        return {int(r["open_time_ms"]): float(r["quote_volume"]) for r in rows}

    def first_daily_open(
        self, market: Market, symbol: str, start_ms: int, end_ms: int
    ) -> int | None:
        approval = self._guard.check(start_ms, end_ms, purpose=Purpose.RESEARCH)
        rows, _, _ = self._read(
            market, LakeKind.KLINES_1D, symbol, start_ms, end_ms, None, approval
        )
        return int(rows[0]["open_time_ms"]) if rows else None

    # ------------------------------------------------------------------ internals
    def _parts(
        self,
        market: Market,
        kind: LakeKind,
        symbol: str,
        start_ms: int,
        end_ms: int,
        dataset_ids: Collection[str] | None,
    ) -> list[IndexedPart]:
        wanted = set(dataset_ids) if dataset_ids is not None else None
        out = []
        for ip in self._lake.index():
            p = ip.part
            if p.market != str(market) or p.kind != str(kind) or p.symbol != symbol:
                continue
            if wanted is not None and ip.dataset_id not in wanted:
                continue
            if p.last_time_ms < start_ms or p.first_time_ms >= end_ms:
                continue
            out.append(ip)
        return out

    def _read(
        self,
        market: Market,
        kind: LakeKind,
        symbol: str,
        start_ms: int,
        end_ms: int,
        dataset_ids: Collection[str] | None,
        approval: HoldoutApproval,
    ) -> tuple[list[Row], tuple[str, ...], list[dict[str, Any]]]:
        key = TIME_KEY[kind]
        chosen: dict[int, tuple[tuple[int, str], Row]] = {}
        conflicts: list[dict[str, Any]] = []
        used: set[str] = set()
        seen_parts: set[str] = set()
        for ip in self._parts(market, kind, symbol, start_ms, end_ms, dataset_ids):
            if ip.part.content_hash in seen_parts:
                used.add(ip.dataset_id)
                continue
            seen_parts.add(ip.part.content_hash)
            used.add(ip.dataset_id)
            rows_of_part = (
                self._lake.read_sealed_part(
                    ip.part,
                    self._guard,
                    prereg_id=approval.prereg_id,
                    strategy_hash=approval.strategy_hash,
                )
                if ip.part.sealed
                else self._lake.read_part(ip.part)
            )
            for row in rows_of_part:
                t = int(row[key])
                if not start_ms <= t < end_ms:
                    continue
                rank = (_SOURCE_RANK.get(str(row.get("source")), 9), ip.part.content_hash)
                current = chosen.get(t)
                if current is None:
                    chosen[t] = (rank, row)
                    continue
                if _same(current[1], row):
                    continue
                winner = min(current, (rank, row), key=lambda x: x[0])
                conflicts.append(
                    {
                        "time_ms": t,
                        "kept_source": winner[1].get("source"),
                        "a": _public(current[1]),
                        "b": _public(row),
                    }
                )
                chosen[t] = winner
        rows = [chosen[t][1] for t in sorted(chosen)]
        return rows, tuple(sorted(used)), conflicts


def _same(a: Row, b: Row) -> bool:
    return all(a.get(f) == b.get(f) for f in _MARKET_FIELDS)


def _public(row: Row) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k != "ingested_at_ms"}


def _bar(market: Market, symbol: str, r: Row) -> Bar:
    return Bar(
        market=market,
        symbol=symbol,
        interval=Interval.M1,
        open_time_ms=int(r["open_time_ms"]),
        close_time_ms=int(r["close_time_ms"]),
        open=float(r["open"]),
        high=float(r["high"]),
        low=float(r["low"]),
        close=float(r["close"]),
        volume=float(r["volume"]),
        quote_volume=float(r["quote_volume"]),
        trades=int(r["trades"]),
        taker_buy_volume=float(r["taker_buy_volume"]),
        taker_buy_quote_volume=float(r["taker_buy_quote_volume"]),
        source=BarSource(str(r["source"])),
        ingested_at_ms=int(r["ingested_at_ms"]),
    )


def _missing(rows: Sequence[Row], start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """Missing 1m open times in [start, end) as ranges (rows are sorted and unique)."""
    lo = start_ms + (-start_ms) % MS_PER_MINUTE
    hi = end_ms - end_ms % MS_PER_MINUTE if end_ms % MS_PER_MINUTE else end_ms
    out: list[tuple[int, int]] = []
    cursor = lo
    for r in rows:
        t = int(r["open_time_ms"])
        if t > cursor:
            out.append((cursor, min(t, hi)))
        cursor = max(cursor, t + MS_PER_MINUTE)
    if cursor < hi:
        out.append((cursor, hi))
    return [(a, b) for a, b in out if b > a]
