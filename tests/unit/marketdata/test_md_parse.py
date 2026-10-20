from __future__ import annotations

import json

import pytest
from md_helpers import load_fixture, raw_fixture

from tsclaude.contracts.market import Interval, Market
from tsclaude.marketdata.binance.parse import (
    MalformedMessage,
    parse_exchange_info,
    parse_funding_rates,
    parse_premium_index,
    parse_price_klines,
    parse_rest_klines,
    parse_server_time,
    parse_ws_frame,
    to_epoch_ms,
)
from tsclaude.marketdata.types import (
    BookTickerUpdate,
    ControlResponse,
    KlineUpdate,
    MarkPriceUpdate,
)


def test_spot_closed_kline() -> None:
    env = parse_ws_frame(raw_fixture("ws_spot_kline_closed.json"), Market.SPOT)
    assert env.stream is None
    k = env.message
    assert isinstance(k, KlineUpdate)
    assert (k.market, k.symbol, k.interval) == (Market.SPOT, "BTCUSDT", Interval.M1)
    assert k.is_closed is True
    assert k.open_time_ms == 1_792_411_200_000
    assert k.close_time_ms == 1_792_411_259_999
    assert k.event_time_ms == 1_792_411_260_005
    assert (k.open, k.high, k.low, k.close) == (67250.01, 67275.0, 67240.1, 67261.5)
    assert k.volume == pytest.approx(12.3456)
    assert k.trades == 1200
    assert k.taker_buy_volume == pytest.approx(6.5432)


def test_spot_partial_kline() -> None:
    k = parse_ws_frame(raw_fixture("ws_spot_kline_partial.json"), Market.SPOT).message
    assert isinstance(k, KlineUpdate)
    assert k.is_closed is False


def test_usdm_combined_kline() -> None:
    env = parse_ws_frame(raw_fixture("ws_usdm_kline_combined.json"), Market.USDM)
    assert env.stream == "btcusdt@kline_1m"
    assert isinstance(env.message, KlineUpdate)
    assert env.message.market is Market.USDM
    assert env.message.close == pytest.approx(67233.4)


def test_spot_book_ticker_has_no_event_time() -> None:
    b = parse_ws_frame(raw_fixture("ws_spot_book_ticker.json"), Market.SPOT).message
    assert isinstance(b, BookTickerUpdate)
    assert b.event_time_ms is None
    assert b.transaction_time_ms is None
    assert b.update_id == 77001234567
    assert b.bid_price < b.ask_price


def test_usdm_book_ticker() -> None:
    b = parse_ws_frame(raw_fixture("ws_usdm_book_ticker.json"), Market.USDM).message
    assert isinstance(b, BookTickerUpdate)
    assert b.event_time_ms == 1_792_411_261_001
    assert b.transaction_time_ms == 1_792_411_260_999


def test_mark_price_update() -> None:
    m = parse_ws_frame(raw_fixture("ws_usdm_mark_price.json"), Market.USDM).message
    assert isinstance(m, MarkPriceUpdate)
    assert m.mark_price == pytest.approx(67234.12)
    assert m.funding_rate == pytest.approx(0.0001)
    assert m.next_funding_time_ms == 1_792_425_600_000


def test_mark_price_empty_funding_for_non_perpetual() -> None:
    data = load_fixture("ws_usdm_mark_price.json")
    data["r"], data["T"] = "", 0
    m = parse_ws_frame(json.dumps(data), Market.USDM).message
    assert isinstance(m, MarkPriceUpdate)
    assert m.funding_rate is None
    assert m.next_funding_time_ms is None


def test_mark_price_rejected_on_spot() -> None:
    with pytest.raises(MalformedMessage):
        parse_ws_frame(raw_fixture("ws_usdm_mark_price.json"), Market.SPOT)


def test_control_responses() -> None:
    ok = parse_ws_frame(raw_fixture("ws_subscribe_response.json"), Market.SPOT).message
    assert isinstance(ok, ControlResponse)
    assert ok.id == 1
    assert ok.error is None
    err = parse_ws_frame(raw_fixture("ws_error_response.json"), Market.SPOT).message
    assert isinstance(err, ControlResponse)
    assert err.error is not None


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        b"\xff\xfe",
        "[]",
        '{"e": "trade"}',
        '{"e": "kline", "E": 1}',
        '{"stream": "x", "data": []}',
    ],
)
def test_malformed_frames(raw: str | bytes) -> None:
    with pytest.raises(MalformedMessage):
        parse_ws_frame(raw, Market.SPOT)


@pytest.mark.parametrize(
    ("field", "value"),
    [("o", "abc"), ("x", "true"), ("n", 1.5), ("c", "NaN"), ("i", "7m"), ("t", -1), ("v", None)],
)
def test_malformed_kline_fields(field: str, value: object) -> None:
    data = load_fixture("ws_spot_kline_closed.json")
    data["k"][field] = value
    with pytest.raises(MalformedMessage):
        parse_ws_frame(json.dumps(data), Market.SPOT)


def test_symbol_mismatch_rejected() -> None:
    data = load_fixture("ws_spot_kline_closed.json")
    data["s"] = "ETHUSDT"
    with pytest.raises(MalformedMessage, match="symbol"):
        parse_ws_frame(json.dumps(data), Market.SPOT)


def test_timestamps_normalized_from_microseconds() -> None:
    assert to_epoch_ms(1_792_411_200_000) == 1_792_411_200_000
    assert to_epoch_ms(1_792_411_200_000_123) == 1_792_411_200_000
    data = load_fixture("ws_spot_kline_closed.json")
    data["E"] *= 1000
    data["k"]["t"] *= 1000
    data["k"]["T"] = data["k"]["T"] * 1000 + 999
    k = parse_ws_frame(json.dumps(data), Market.SPOT).message
    assert isinstance(k, KlineUpdate)
    assert (k.open_time_ms, k.close_time_ms) == (1_792_411_200_000, 1_792_411_259_999)
    with pytest.raises(MalformedMessage):
        to_epoch_ms(10**18)


def test_rest_klines_and_time() -> None:
    rows = parse_rest_klines(load_fixture("rest_spot_klines.json"))
    assert [r.open_time_ms for r in rows] == [1_792_411_200_000, 1_792_411_260_000]
    assert rows[0].close_time_ms == 1_792_411_259_999
    assert rows[1].trades == 980
    assert parse_server_time(load_fixture("rest_time.json")) == 1_792_411_265_000
    with pytest.raises(MalformedMessage):
        parse_rest_klines([[1, "2"]])
    with pytest.raises(MalformedMessage):
        parse_rest_klines({"code": -1121, "msg": "Invalid symbol."})


def test_rest_futures_payloads() -> None:
    pi = parse_premium_index(load_fixture("rest_usdm_premium_index.json"))
    assert pi.last_funding_rate == pytest.approx(0.0001)
    assert pi.next_funding_time_ms == 1_792_425_600_000
    fr = parse_funding_rates(load_fixture("rest_usdm_funding_rate.json"))
    assert [f.funding_rate for f in fr] == [pytest.approx(0.0001), pytest.approx(-0.000025)]
    mk = parse_price_klines(load_fixture("rest_usdm_mark_price_klines.json"))
    assert mk[0].close == pytest.approx(67234.12)


def test_exchange_info_spot() -> None:
    info = parse_exchange_info(Market.SPOT, load_fixture("rest_spot_exchange_info.json"))
    weight = [r for r in info.rate_limits if r.rate_limit_type == "REQUEST_WEIGHT"]
    assert len(weight) == 1
    assert (weight[0].limit, weight[0].window_ms, weight[0].header_suffix) == (6000, 60_000, "1M")
    btc = info.symbols["BTCUSDT"]
    assert (btc.status, btc.tick_size, btc.step_size, btc.min_notional) == (
        "TRADING",
        "0.01000000",
        "0.00001000",
        "5.00000000",
    )
    assert info.symbols["ETHUSDT"].status == "BREAK"


def test_exchange_info_usdm_min_notional_naming() -> None:
    info = parse_exchange_info(Market.USDM, load_fixture("rest_usdm_exchange_info.json"))
    assert info.symbols["BTCUSDT"].min_notional == "100"
    assert info.symbols["BTCUSDT"].tick_size == "0.10"
    assert [r.limit for r in info.rate_limits if r.rate_limit_type == "REQUEST_WEIGHT"] == [2400]
