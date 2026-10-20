from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tsclaude.contracts.market import Market
from tsclaude.marketdata.binance.ws import combined_url, plan_groups, stream_name
from tsclaude.marketdata.config import MarketDataConfig, WsConfig, load_config
from tsclaude.marketdata.types import StreamKey, StreamKind

REPO = Path(__file__).resolve().parents[3]
UNIVERSE = {
    "version": 0,
    "snapshot_date": "2026-10-19",
    "rule": "test",
    "spot": ["BTCUSDT", "ETHUSDT"],
    "usdm": ["BTCUSDT"],
}


def test_defaults_and_pairs() -> None:
    cfg = MarketDataConfig.from_mapping(UNIVERSE)
    assert cfg.universe.pairs() == [
        (Market.SPOT, "BTCUSDT"),
        (Market.SPOT, "ETHUSDT"),
        (Market.USDM, "BTCUSDT"),
    ]
    q = cfg.quality
    assert q.watermark.grace_s == 5
    assert q.ws.recycle_after_s == 23 * 3600
    assert q.backfill.lookback_days == 3
    assert q.endpoints.usdm_ws_market == ("wss://fstream.binance.com/market",)
    assert q.endpoints.usdm_ws_public == ("wss://fstream.binance.com/public",)


@pytest.mark.parametrize(
    "quality",
    [
        {"freshness": {"kline_1m_s": 0}},
        {"freshness": {"typo_s": 3}},
        {"ws": {"recycle_after_s": 86400}},
        {"ws": {"control_msgs_per_s_spot": 5}},
        {"ws": {"control_msgs_per_s_futures": 10}},
        {"ws": {"backoff_base_s": 10, "backoff_cap_s": 5}},
        {"rest": {"weight_budget_fraction": 1.5}},
        {"backfill": {"lookback_days": 10, "max_backfill_days": 5}},
    ],
)
def test_invalid_quality_rejected(quality: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MarketDataConfig.from_mapping(UNIVERSE, quality)


@pytest.mark.parametrize("symbols", [["btcusdt"], ["BTC-USDT"], ["BTCUSDT", "BTCUSDT"]])
def test_invalid_symbols_rejected(symbols: list[str]) -> None:
    with pytest.raises(ValidationError):
        MarketDataConfig.from_mapping({**UNIVERSE, "spot": symbols})


def test_repo_yaml_files_load() -> None:
    pytest.importorskip("yaml")
    cfg = load_config(REPO / "configs" / "universe.yaml", REPO / "configs" / "data_quality.yaml")
    assert cfg.universe.spot == ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    assert cfg.universe.usdm == ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    assert cfg.quality.freshness.mark_price_s == 5
    assert cfg.quality.ws.connect_budget <= 300


def test_group_plan_and_urls() -> None:
    cfg = MarketDataConfig.from_mapping(UNIVERSE)
    groups = plan_groups(cfg.universe, cfg.quality.endpoints, cfg.quality.ws)
    assert [g.name for g in groups] == ["spot", "usdm-market", "usdm-public"]
    spot, fmarket, fpublic = groups
    assert spot.control_msgs_per_s == 4
    assert fmarket.control_msgs_per_s == 8
    assert {k.kind for k in fmarket.streams} == {StreamKind.KLINE_1M, StreamKind.MARK_PRICE}
    assert {k.kind for k in fpublic.streams} == {StreamKind.BOOK_TICKER}
    assert combined_url(fmarket.urls[0], fmarket.streams) == (
        "wss://fstream.binance.com/market/stream?streams=btcusdt@kline_1m/btcusdt@markPrice@1s"
    )
    assert combined_url(spot.urls[0], spot.streams[:2]) == (
        "wss://stream.binance.com:9443/stream?streams=btcusdt@kline_1m/btcusdt@bookTicker"
    )
    assert stream_name(StreamKey(Market.SPOT, "SOLUSDT", StreamKind.BOOK_TICKER)) == (
        "solusdt@bookTicker"
    )


def test_groups_split_by_max_streams() -> None:
    cfg = MarketDataConfig.from_mapping(UNIVERSE, {"ws": {"max_streams_per_connection": 3}})
    groups = plan_groups(
        cfg.universe, cfg.quality.endpoints, WsConfig(max_streams_per_connection=3)
    )
    assert [g.name for g in groups] == ["spot#1", "spot#2", "usdm-market", "usdm-public"]
    assert [len(g.streams) for g in groups] == [3, 1, 2, 1]
