"""Binance payload parsers -> internal typed messages (no network libraries).

Formats follow Binance's documented payloads (Spot "WebSocket Streams" and "Market Data
endpoints"; USD-M Futures "Websocket Market Streams" and "Market Data" REST). Prices and
quantities arrive as decimal strings and are parsed to float64 (research-vector convention).

Every malformed payload raises `MalformedMessage`; callers count and log it and keep going.
Timestamps are normalized to epoch milliseconds: Binance Spot can emit microseconds
(`timeUnit=MICROSECOND`, and archive files from 2025), so values in the microsecond range are
divided by 1000.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tsclaude.contracts.market import Interval, Market
from tsclaude.marketdata.types import (
    BookTickerUpdate,
    ControlResponse,
    ExchangeInfo,
    FundingRate,
    KlineUpdate,
    MarkPriceUpdate,
    ParsedMessage,
    PremiumIndex,
    PriceKline,
    RateLimit,
    RestKline,
    SymbolInfo,
)

# Epoch ms for year ~5138 is 1e14; epoch microseconds for 2001+ exceed 1e15. Anything at or
# above 1e14 is therefore microseconds; at or above 1e17 it is not a timestamp we understand.
_MICROS_THRESHOLD = 10**14
_MAX_TIMESTAMP = 10**17
# ASCII only: str.isdigit()/float() also accept other Unicode digits ("²", "١٢").
_INT_TEXT = re.compile(r"-?[0-9]{1,19}")
_FLOAT_TEXT = re.compile(r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]{1,3})?")
_MAX_NUMBER_TEXT = 64


class MalformedMessage(ValueError):
    """A payload that does not match the documented format. `reason` is short and loggable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Envelope:
    """A parsed WebSocket frame. `stream` is set in combined-stream mode."""

    stream: str | None
    message: ParsedMessage


# ---------------------------------------------------------------------------- primitives
def _req(obj: Mapping[str, Any], key: str) -> Any:
    if key not in obj:
        raise MalformedMessage(f"missing field {key!r}")
    return obj[key]


def _float(obj: Mapping[str, Any], key: str) -> float:
    return _to_float(_req(obj, key), key)


def _to_float(raw: Any, key: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, str | int | float):
        raise MalformedMessage(f"field {key!r} is not numeric")
    if isinstance(raw, str) and (len(raw) > _MAX_NUMBER_TEXT or _FLOAT_TEXT.fullmatch(raw) is None):
        raise MalformedMessage(f"field {key!r} is not numeric")
    try:
        value = float(raw)
    except (ValueError, OverflowError) as exc:
        raise MalformedMessage(f"field {key!r} is not numeric") from exc
    if not math.isfinite(value):
        raise MalformedMessage(f"field {key!r} is not finite")
    return value


def _int(obj: Mapping[str, Any], key: str) -> int:
    return _to_int(_req(obj, key), key)


def _to_int(raw: Any, key: str) -> int:
    if isinstance(raw, bool):
        raise MalformedMessage(f"field {key!r} is not an integer")
    if isinstance(raw, int):
        if abs(raw) >= 2**63:
            raise MalformedMessage(f"field {key!r} is out of range")
        return raw
    if isinstance(raw, str) and _INT_TEXT.fullmatch(raw) is not None:
        return int(raw)
    raise MalformedMessage(f"field {key!r} is not an integer")


def to_epoch_ms(raw: Any, key: str = "timestamp") -> int:
    """Normalize a Binance timestamp (ms or µs) to epoch milliseconds."""
    value = _to_int(raw, key)
    if value < 0 or value >= _MAX_TIMESTAMP:
        raise MalformedMessage(f"field {key!r} is out of range")
    if value >= _MICROS_THRESHOLD:
        return value // 1000
    return value


def _ts(obj: Mapping[str, Any], key: str) -> int:
    return to_epoch_ms(_req(obj, key), key)


def _str(obj: Mapping[str, Any], key: str) -> str:
    raw = _req(obj, key)
    if not isinstance(raw, str) or not raw:
        raise MalformedMessage(f"field {key!r} is not a non-empty string")
    return raw


def _opt_str(obj: Mapping[str, Any], key: str) -> str | None:
    return _str(obj, key) if key in obj else None


def _bool(obj: Mapping[str, Any], key: str) -> bool:
    raw = _req(obj, key)
    if not isinstance(raw, bool):
        raise MalformedMessage(f"field {key!r} is not a boolean")
    return raw


def _obj(raw: Any, what: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MalformedMessage(f"{what} is not a JSON object")
    return raw


def _interval(raw: str) -> Interval:
    try:
        return Interval(raw)
    except ValueError as exc:
        raise MalformedMessage(f"unsupported interval {raw!r}") from exc


def loads(raw: str | bytes) -> Any:
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return json.loads(text)
    except (ValueError, RecursionError) as exc:
        # JSONDecodeError and UnicodeDecodeError are ValueErrors; so is Python's int-digit limit
        # (> 4300 digits). Deeply nested arrays raise RecursionError.
        raise MalformedMessage("not valid JSON") from exc


# ---------------------------------------------------------------------------- WebSocket
def parse_ws_frame(raw: str | bytes, market: Market) -> Envelope:
    """Parse one WebSocket text frame (raw or combined-stream mode).

    Raises only `MalformedMessage`: any other error while interpreting an untrusted payload is
    converted, so callers have a single, countable failure type.
    """
    try:
        return _parse_ws_frame(raw, market)
    except MalformedMessage:
        raise
    except Exception as exc:
        raise MalformedMessage(f"unparseable frame ({type(exc).__name__})") from exc


def _parse_ws_frame(raw: str | bytes, market: Market) -> Envelope:
    data = _obj(loads(raw), "frame")
    if "stream" in data and "data" in data:
        stream = data["stream"]
        if not isinstance(stream, str):
            raise MalformedMessage("combined frame 'stream' is not a string")
        return Envelope(stream, parse_ws_payload(_obj(data["data"], "data"), market))
    return Envelope(None, parse_ws_payload(data, market))


def parse_ws_payload(data: Mapping[str, Any], market: Market) -> ParsedMessage:
    if "id" in data and ("result" in data or "code" in data or "error" in data):
        return _control(data)
    event = data.get("e")
    if event == "kline":
        return parse_kline_event(data, market)
    if event == "markPriceUpdate":
        if market is not Market.USDM:
            raise MalformedMessage("markPriceUpdate on a non-futures connection")
        return parse_mark_price_event(data)
    if event == "bookTicker" or (event is None and {"u", "s", "b", "B", "a", "A"} <= data.keys()):
        return parse_book_ticker(data, market)
    raise MalformedMessage(f"unsupported event type {event!r}")


def _control(data: Mapping[str, Any]) -> ControlResponse:
    ident = data.get("id")
    if ident is not None and (isinstance(ident, bool) or not isinstance(ident, int | str)):
        raise MalformedMessage("control response id has an invalid type")
    error: object | None = data.get("error")
    if error is None and "code" in data:
        error = {"code": data.get("code"), "msg": data.get("msg")}
    return ControlResponse(id=ident, result=data.get("result"), error=error)


def parse_kline_event(data: Mapping[str, Any], market: Market) -> KlineUpdate:
    k = _obj(_req(data, "k"), "field 'k'")
    symbol = _str(k, "s")
    outer = data.get("s")
    if outer is not None and outer != symbol:
        raise MalformedMessage("kline symbol does not match event symbol")
    return KlineUpdate(
        market=market,
        symbol=symbol,
        interval=_interval(_str(k, "i")),
        event_time_ms=_ts(data, "E"),
        open_time_ms=_ts(k, "t"),
        close_time_ms=_ts(k, "T"),
        open=_float(k, "o"),
        high=_float(k, "h"),
        low=_float(k, "l"),
        close=_float(k, "c"),
        volume=_float(k, "v"),
        quote_volume=_float(k, "q"),
        trades=_int(k, "n"),
        taker_buy_volume=_float(k, "V"),
        taker_buy_quote_volume=_float(k, "Q"),
        is_closed=_bool(k, "x"),
    )


def parse_book_ticker(data: Mapping[str, Any], market: Market) -> BookTickerUpdate:
    return BookTickerUpdate(
        market=market,
        symbol=_str(data, "s"),
        update_id=_int(data, "u"),
        bid_price=_float(data, "b"),
        bid_qty=_float(data, "B"),
        ask_price=_float(data, "a"),
        ask_qty=_float(data, "A"),
        event_time_ms=_ts(data, "E") if "E" in data else None,
        transaction_time_ms=_ts(data, "T") if "T" in data else None,
    )


def parse_mark_price_event(data: Mapping[str, Any]) -> MarkPriceUpdate:
    rate_raw = _req(data, "r")
    funding = None if rate_raw == "" else _to_float(rate_raw, "r")
    next_raw = data.get("T")
    next_funding = None if next_raw in (None, 0, "", "0") else to_epoch_ms(next_raw, "T")
    return MarkPriceUpdate(
        market=Market.USDM,
        symbol=_str(data, "s"),
        event_time_ms=_ts(data, "E"),
        mark_price=_float(data, "p"),
        index_price=_float(data, "i"),
        estimated_settle_price=_float(data, "P"),
        funding_rate=funding,
        next_funding_time_ms=next_funding,
    )


# ---------------------------------------------------------------------------- REST
def parse_server_time(payload: Any) -> int:
    return _ts(_obj(payload, "time response"), "serverTime")


def _rows(payload: Any, what: str) -> Sequence[Sequence[Any]]:
    if not isinstance(payload, list):
        raise MalformedMessage(f"{what} response is not a JSON array")
    for row in payload:
        if not isinstance(row, list) or len(row) < 11:
            raise MalformedMessage(f"{what} row is not an array of >= 11 columns")
    return payload


def parse_rest_klines(payload: Any) -> list[RestKline]:
    """GET /api/v3/klines and /fapi/v1/klines: arrays of 12 columns (the last is 'ignore')."""
    out: list[RestKline] = []
    for row in _rows(payload, "klines"):
        out.append(
            RestKline(
                open_time_ms=to_epoch_ms(row[0], "open time"),
                open=_to_float(row[1], "open"),
                high=_to_float(row[2], "high"),
                low=_to_float(row[3], "low"),
                close=_to_float(row[4], "close"),
                volume=_to_float(row[5], "volume"),
                close_time_ms=to_epoch_ms(row[6], "close time"),
                quote_volume=_to_float(row[7], "quote volume"),
                trades=_to_int(row[8], "trades"),
                taker_buy_volume=_to_float(row[9], "taker buy base volume"),
                taker_buy_quote_volume=_to_float(row[10], "taker buy quote volume"),
            )
        )
    return out


def parse_price_klines(payload: Any) -> list[PriceKline]:
    """GET /fapi/v1/markPriceKlines: same layout; volume/trade columns are documented 'ignore'."""
    out: list[PriceKline] = []
    for row in _rows(payload, "markPriceKlines"):
        out.append(
            PriceKline(
                open_time_ms=to_epoch_ms(row[0], "open time"),
                open=_to_float(row[1], "open"),
                high=_to_float(row[2], "high"),
                low=_to_float(row[3], "low"),
                close=_to_float(row[4], "close"),
                close_time_ms=to_epoch_ms(row[6], "close time"),
            )
        )
    return out


def parse_premium_index(payload: Any) -> PremiumIndex:
    data = _obj(payload, "premiumIndex response")
    return PremiumIndex(
        symbol=_str(data, "symbol"),
        mark_price=_float(data, "markPrice"),
        index_price=_float(data, "indexPrice"),
        estimated_settle_price=_float(data, "estimatedSettlePrice"),
        last_funding_rate=_float(data, "lastFundingRate"),
        interest_rate=_float(data, "interestRate"),
        next_funding_time_ms=_ts(data, "nextFundingTime"),
        time_ms=_ts(data, "time"),
    )


def parse_funding_rates(payload: Any) -> list[FundingRate]:
    if not isinstance(payload, list):
        raise MalformedMessage("fundingRate response is not a JSON array")
    out: list[FundingRate] = []
    for item in payload:
        row = _obj(item, "fundingRate row")
        mark_raw = row.get("markPrice")
        out.append(
            FundingRate(
                symbol=_str(row, "symbol"),
                funding_rate=_float(row, "fundingRate"),
                funding_time_ms=_ts(row, "fundingTime"),
                mark_price=None if mark_raw in (None, "") else _to_float(mark_raw, "markPrice"),
            )
        )
    return out


def _filter_value(filters: Sequence[Mapping[str, Any]], ftype: str, key: str) -> str | None:
    for f in filters:
        if f.get("filterType") == ftype and key in f:
            return str(f[key])
    return None


def parse_exchange_info(market: Market, payload: Any) -> ExchangeInfo:
    data = _obj(payload, "exchangeInfo response")
    limits_raw = _req(data, "rateLimits")
    if not isinstance(limits_raw, list):
        raise MalformedMessage("rateLimits is not an array")
    limits: list[RateLimit] = []
    for item in limits_raw:
        rl = _obj(item, "rateLimits entry")
        interval = _str(rl, "interval")
        if interval not in ("SECOND", "MINUTE", "HOUR", "DAY"):
            raise MalformedMessage(f"unknown rate-limit interval {interval!r}")
        limits.append(
            RateLimit(
                rate_limit_type=_str(rl, "rateLimitType"),
                interval=interval,
                interval_num=_int(rl, "intervalNum"),
                limit=_int(rl, "limit"),
            )
        )
    symbols_raw = _req(data, "symbols")
    if not isinstance(symbols_raw, list):
        raise MalformedMessage("symbols is not an array")
    symbols: dict[str, SymbolInfo] = {}
    for item in symbols_raw:
        s = _obj(item, "symbol entry")
        filters_raw = s.get("filters", [])
        if not isinstance(filters_raw, list):
            raise MalformedMessage("filters is not an array")
        filters = tuple(_obj(f, "filter") for f in filters_raw)
        min_notional = _filter_value(filters, "NOTIONAL", "minNotional")
        if min_notional is None:
            min_notional = _filter_value(filters, "MIN_NOTIONAL", "minNotional")
        if min_notional is None:
            min_notional = _filter_value(filters, "MIN_NOTIONAL", "notional")  # USD-M naming
        info = SymbolInfo(
            symbol=_str(s, "symbol"),
            status=_str(s, "status"),
            base_asset=_str(s, "baseAsset"),
            quote_asset=_str(s, "quoteAsset"),
            tick_size=_filter_value(filters, "PRICE_FILTER", "tickSize"),
            step_size=_filter_value(filters, "LOT_SIZE", "stepSize"),
            min_notional=min_notional,
            filters=filters,
            contract_type=_opt_str(s, "contractType"),
            onboard_date_ms=to_epoch_ms(s["onboardDate"], "onboardDate")
            if "onboardDate" in s
            else None,
        )
        symbols[info.symbol] = info
    server_time = data.get("serverTime")
    return ExchangeInfo(
        market=market,
        server_time_ms=None if server_time is None else to_epoch_ms(server_time, "serverTime"),
        rate_limits=tuple(limits),
        symbols=symbols,
    )
