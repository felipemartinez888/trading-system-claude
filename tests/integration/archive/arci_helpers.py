"""Integration helpers: a MockTransport-backed fake of data.binance.vision + Binance REST."""

from __future__ import annotations

import importlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_UNIT = Path(__file__).resolve().parents[2] / "unit" / "archive"
if str(_UNIT) not in sys.path:
    sys.path.insert(0, str(_UNIT))

from arc_helpers import (  # noqa: E402
    DAY,
    MIN,
    FakeArchive,
    JsonLinesCodec,
    kline_values,
    ms,
)

__all__ = ["DAY", "MIN", "FakeArchive", "JsonLinesCodec", "ms", "require"]


def require(module: str) -> ModuleType:
    """Skip locally when a library is missing; in CI (`CI` set) a missing library fails."""
    if os.environ.get("CI"):
        return importlib.import_module(module)
    return pytest.importorskip(module)


def exchange_info(market: str, symbols: list[dict[str, Any]]) -> dict[str, Any]:
    """exchangeInfo payload in Binance's documented shape (trimmed to the fields used)."""
    limit = 6000 if market == "spot" else 2400
    return {
        "timezone": "UTC",
        "serverTime": ms(2026, 10, 20, 12),
        "rateLimits": [
            {
                "rateLimitType": "REQUEST_WEIGHT",
                "interval": "MINUTE",
                "intervalNum": 1,
                "limit": limit,
            }
        ],
        "exchangeFilters": [],
        "symbols": [{"filters": [], **s} for s in symbols],
    }


def kline_rows(start: int, end: int, step: int, limit: int) -> list[list[Any]]:
    rows: list[list[Any]] = []
    t = start - start % step + (step if start % step else 0)
    while t <= end and len(rows) < limit:
        v = kline_values(t, step)
        rows.append(
            [
                v[0],
                f"{v[1]:.8f}",
                f"{v[2]:.8f}",
                f"{v[3]:.8f}",
                f"{v[4]:.8f}",
                f"{v[5]:.8f}",
                v[6],
                f"{v[7]:.8f}",
                v[8],
                f"{v[9]:.8f}",
                f"{v[10]:.8f}",
                "0",
            ]
        )
        t += step
    return rows


_STEPS = {"1m": MIN, "1d": DAY, "15m": 15 * MIN, "1h": 60 * MIN}


def make_handler(
    archive: FakeArchive,
    spot_info: dict[str, Any],
    usdm_info: dict[str, Any],
    now_ms: int,
    log: list[str] | None = None,
) -> Callable[[Any], Any]:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if log is not None:
            log.append(f"{url.host}{url.path}")
        if url.host == "data.binance.vision":
            rel = url.path.lstrip("/")
            if rel.endswith(".CHECKSUM"):
                entry = archive.files.get(rel.removesuffix(".CHECKSUM"))
                return httpx.Response(200, text=entry[1]) if entry else httpx.Response(404)
            entry = archive.files.get(rel)
            return httpx.Response(200, content=entry[0]) if entry else httpx.Response(404)
        path = url.path
        params = dict(url.params)
        headers = {"X-MBX-USED-WEIGHT-1M": "5"}
        if path == "/api/v3/exchangeInfo":
            return httpx.Response(200, json=spot_info, headers=headers)
        if path == "/fapi/v1/exchangeInfo":
            return httpx.Response(200, json=usdm_info, headers=headers)
        if path in ("/api/v3/klines", "/fapi/v1/klines", "/fapi/v1/markPriceKlines"):
            step = _STEPS[params["interval"]]
            rows = kline_rows(
                int(params["startTime"]),
                min(int(params["endTime"]), now_ms),
                step,
                int(params["limit"]),
            )
            return httpx.Response(200, json=rows, headers=headers)
        if path == "/fapi/v1/fundingRate":
            start = int(params.get("startTime", 0))
            end = int(params.get("endTime", now_ms))
            t = start - start % (8 * 3_600_000) + 8 * 3_600_000
            out = []
            while t <= end and len(out) < int(params["limit"]):
                out.append(
                    {
                        "symbol": params["symbol"],
                        "fundingRate": "0.00010000",
                        "fundingTime": t,
                        "markPrice": "100.00000000",
                    }
                )
                t += 8 * 3_600_000
            return httpx.Response(200, json=out, headers=headers)
        return httpx.Response(404, json={"code": -1, "msg": "not found"})

    return handler


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
