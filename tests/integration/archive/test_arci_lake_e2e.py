"""End to end via `datacli` + MockTransport: import, Parquet lake, catalog, verify, universe."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from arci_helpers import (
    DAY,
    FakeArchive,
    JsonLinesCodec,
    exchange_info,
    make_handler,
    ms,
    read_json,
    require,
)

httpx = require("httpx")

from tsclaude.contracts.market import Market  # noqa: E402
from tsclaude.contracts.timeutil import ManualClock  # noqa: E402
from tsclaude.marketdata.archive.paths import (  # noqa: E402
    ArchiveDataType,
    ArchiveFile,
    Period,
)
from tsclaude.marketdata.datacli import open_env, run_import, run_universe  # noqa: E402
from tsclaude.marketdata.universe import UniverseRule  # noqa: E402
from tsclaude.store.holdout import HoldoutGuard, SealedUnlocks  # noqa: E402
from tsclaude.store.lake import Lake, LakeKind  # noqa: E402
from tsclaude.store.lake_reader import LakeReader  # noqa: E402

NOW = ms(2026, 10, 20, 12, 30)
SPOT_SYMBOLS = [
    {"symbol": s, "status": "TRADING", "baseAsset": s.removesuffix("USDT"), "quoteAsset": "USDT"}
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")
]
USDM_SYMBOLS = [
    {**s, "contractType": "PERPETUAL", "onboardDate": ms(2020, 1, 1), "pair": s["symbol"]}
    for s in SPOT_SYMBOLS
]


def _codec(name: str) -> Any:
    if name == "parquet":
        require("pyarrow")
        from tsclaude.store.parquet_codec import ParquetCodec

        return ParquetCodec()
    return JsonLinesCodec()


def _klines(
    archive: FakeArchive,
    market: Market,
    symbol: str,
    label: str,
    interval: str = "1m",
    step: int = 60_000,
) -> None:
    period = Period.MONTHLY if len(label) == 7 else Period.DAILY
    archive.add_klines(
        ArchiveFile(market, ArchiveDataType.KLINES, symbol, interval, period, label),
        step=step,
        unit="us" if market is Market.SPOT and label >= "2025" else "ms",
    )


@pytest.mark.parametrize("codec_name", ["jsonl", "parquet"])
def test_import_verify_catalog(tmp_path: Path, codec_name: str) -> None:
    codec = _codec(codec_name)
    archive = FakeArchive()
    for label in ("2024-12", "2025-01"):
        _klines(archive, Market.SPOT, "BTCUSDT", label)
        _klines(archive, Market.SPOT, "BTCUSDT", label, "15m", 15 * 60_000)
        _klines(archive, Market.SPOT, "BTCUSDT", label, "1h", 3_600_000)
    handler = make_handler(
        archive, exchange_info("spot", SPOT_SYMBOLS), exchange_info("usdm", USDM_SYMBOLS), NOW
    )

    async def main() -> dict[str, Any]:
        env = await open_env(
            tmp_path / "data",
            clock=ManualClock(NOW),
            codec=codec,
            http_transport=httpx.MockTransport(handler),
        )
        try:
            return await run_import(env, Market.SPOT, ["BTCUSDT"], ms(2024, 12), ms(2025, 2))
        finally:
            await env.close()

    result = asyncio.run(main())
    assert result["ok"], result["failures"]
    (imp,) = result["imports"]
    assert imp["rows"] == 62 * 1440
    assert imp["qa"]["missing_interior"] == 0
    lake = Lake(tmp_path / "data", codec)
    dsid = imp["dataset_id"]
    report = lake.verify(dsid, raw_cache_dir=tmp_path / "data" / "archive_cache")
    assert report["ok"], report["errors"]
    assert report["sources_checked"] == 6  # 2 data files + 4 QA reference files
    manifest = read_json(tmp_path / "data" / "imports" / dsid / "manifest.json")
    assert [s["timestamp_unit"] for s in manifest["sources"] if s["role"] == "data"] == ["ms", "us"]
    reader = LakeReader(lake, HoldoutGuard(SealedUnlocks()))
    bars = reader.read_bars(
        Market.SPOT, "BTCUSDT", ms(2024, 12, 31, 23, 58), ms(2025, 1, 1, 0, 2), purpose="research"
    ).items
    assert [b.open_time_ms for b in bars] == [
        ms(2024, 12, 31, 23, 58) + i * 60_000 for i in range(4)
    ]
    if codec_name == "parquet":
        duckdb = require("duckdb")
        assert result["catalog"] is not None
        con = duckdb.connect(result["catalog"], read_only=True)
        try:
            assert con.execute("SELECT count(*) FROM bars_1m").fetchone() == (62 * 1440,)
            assert con.execute("SELECT count(*) FROM datasets").fetchone() == (1,)
            months = con.execute("SELECT DISTINCT month FROM bars_1m ORDER BY 1").fetchall()
            assert [m[0] for m in months] == [1, 12]
        finally:
            con.close()


def test_parquet_and_jsonl_give_the_same_dataset_id(tmp_path: Path) -> None:
    require("pyarrow")
    from tsclaude.store.parquet_codec import ParquetCodec

    rows = [
        {
            "funding_time_ms": ms(2025, 1) + i * 8 * 3_600_000,
            "funding_rate": 0.0001 * i,
            "mark_price": None if i == 1 else 100.5,
            "source": "rest_backfill",
            "ingested_at_ms": i,
        }
        for i in range(3)
    ]
    ids = []
    for name, codec in (("pq", ParquetCodec()), ("js", JsonLinesCodec())):
        lake = Lake(tmp_path / name, codec)
        parts = lake.write_parts(Market.USDM, LakeKind.FUNDING, "BTCUSDT", rows)
        ids.append(lake.commit_dataset(parts, {}, None)[0])
        back = lake.read_part(parts[0])
        assert back[1]["mark_price"] is None
        assert back[2]["funding_rate"] == rows[2]["funding_rate"]
        assert lake.verify(ids[-1])["ok"]
    assert ids[0] == ids[1]  # content addressing is independent of the file encoder


def test_universe_snapshot_end_to_end(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    archive = FakeArchive()
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        for market in (Market.SPOT, Market.USDM):
            for label in ("2026-07", "2026-08", "2026-09"):
                _klines(archive, market, symbol, label, "1d", DAY)
        if symbol != "SOLUSDT":  # SOL spot has no daily klines around the 24-month cutoff
            _klines(archive, Market.SPOT, symbol, "2024-10", "1d", DAY)
    handler = make_handler(
        archive, exchange_info("spot", SPOT_SYMBOLS), exchange_info("usdm", USDM_SYMBOLS), NOW
    )
    rule = UniverseRule(top_n=1, min_volume_days=85)

    async def main() -> dict[str, Any]:
        env = await open_env(
            tmp_path / "data",
            clock=ManualClock(NOW),
            codec=JsonLinesCodec(),
            http_transport=httpx.MockTransport(handler),
        )
        try:
            return await run_universe(env, date(2026, 10, 19), rule, tmp_path / "configs")
        finally:
            await env.close()

    result = asyncio.run(main())
    assert result["ok"], result
    assert result["candidates"] == 3
    assert result["eligible"] == 2
    assert len(result["symbols"]) == 1
    import yaml

    doc = yaml.safe_load(Path(result["snapshot"]).read_text(encoding="utf-8"))
    assert doc["snapshot_date"] == "2026-10-19"
    assert doc["symbols"] == result["symbols"]
    assert {e["symbol"]: e["reason"] for e in doc["excluded"]}["SOLUSDT"].startswith("spot not")
    assert len(doc["inputs"]["dataset_ids"]) == 8  # 3 symbols x 2 markets + 2 listing probes
    assert set(doc["inputs"]["exchange_info"]) == {"spot", "usdm"}
    assert all(len(v["sha256"]) == 64 for v in doc["inputs"]["exchange_info"].values())
    assert doc["selected"][0]["spot_volume_days"] == 90
