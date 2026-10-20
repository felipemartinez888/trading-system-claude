"""M0 spike: can this machine reach the Binance public endpoints the design depends on?

Run on the owner's Windows PC (and anywhere else we want to check):

    uv run python scripts/spikes/binance_check.py

It only uses public, unauthenticated market-data endpoints. It writes a JSON report to
`evidence/spikes/` (commit it as M0 evidence) and prints a summary.

Checks:
  REST   spot api.binance.com and data-api.binance.vision (time, exchangeInfo, klines)
  REST   USD-M futures fapi.binance.com (time, exchangeInfo, klines, premiumIndex, fundingRate,
         markPriceKlines)
  ARCH   data.binance.vision bucket listing for spot and USD-M futures data types
  WS     spot stream.binance.com:9443 and data-stream.binance.vision kline_1m
  WS     futures fstream.binance.com/market (kline_1m) and /public (bookTicker)
  CLOCK  local clock offset vs Binance server time
  LOOP   which asyncio event loop is in use (Windows Proactor check)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import socket
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import websockets

TIMEOUT = 15.0
ARCHIVE_LIST = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"

REST_CHECKS: list[tuple[str, str]] = [
    ("spot_time", "https://api.binance.com/api/v3/time"),
    ("spot_exchange_info", "https://api.binance.com/api/v3/exchangeInfo?symbol=BTCUSDT"),
    ("spot_klines", "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=3"),
    ("spot_data_api_time", "https://data-api.binance.vision/api/v3/time"),
    ("fut_time", "https://fapi.binance.com/fapi/v1/time"),
    ("fut_exchange_info", "https://fapi.binance.com/fapi/v1/exchangeInfo"),
    ("fut_klines", "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1m&limit=3"),
    ("fut_premium_index", "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"),
    ("fut_funding_rate", "https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=3"),
    (
        "fut_mark_price_klines",
        "https://fapi.binance.com/fapi/v1/markPriceKlines?symbol=BTCUSDT&interval=1m&limit=3",
    ),
]

ARCHIVE_PREFIXES = [
    "data/spot/monthly/",
    "data/spot/daily/",
    "data/futures/um/monthly/",
    "data/futures/um/daily/",
    "data/spot/monthly/klines/BTCUSDT/1m/",
    "data/futures/um/monthly/klines/BTCUSDT/1m/",
]

WS_CHECKS: list[tuple[str, str]] = [
    ("ws_spot_stream", "wss://stream.binance.com:9443/ws/btcusdt@kline_1m"),
    ("ws_spot_data_stream", "wss://data-stream.binance.vision/ws/btcusdt@kline_1m"),
    ("ws_fut_market_kline", "wss://fstream.binance.com/market/ws/btcusdt@kline_1m"),
    ("ws_fut_public_bookticker", "wss://fstream.binance.com/public/ws/btcusdt@bookTicker"),
]


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


async def _timed(name: str, fn: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        detail = await fn()
        ok = bool(detail.pop("ok", True))
        return {"name": name, "ok": ok, "ms": round((time.perf_counter() - start) * 1000), **detail}
    except Exception as exc:  # report every failure, never abort the whole spike
        return {
            "name": name,
            "ok": False,
            "ms": round((time.perf_counter() - start) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }


async def check_rest(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    resp = await client.get(url)
    weight_headers = {k: v for k, v in resp.headers.items() if k.lower().startswith("x-mbx-used")}
    body: Any = None
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:200]
    summary: Any
    if isinstance(body, list):
        summary = {"items": len(body), "first": body[0] if body else None}
    elif isinstance(body, dict) and "symbols" in body:
        summary = {"symbols": len(body["symbols"]), "rateLimits": body.get("rateLimits")}
    else:
        summary = body
    return {
        "ok": resp.status_code == 200,
        "status": resp.status_code,
        "weight_headers": weight_headers,
        "summary": summary,
    }


async def check_archive(client: httpx.AsyncClient, prefix: str) -> dict[str, Any]:
    resp = await client.get(ARCHIVE_LIST, params={"delimiter": "/", "prefix": prefix})
    if resp.status_code != 200:
        return {"ok": False, "status": resp.status_code}
    root = ET.fromstring(resp.text)  # public S3 listing XML
    ns = {"s3": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    tag = "s3:" if ns else ""
    prefixes = [
        p.text
        for p in root.findall(f".//{tag}CommonPrefixes/{tag}Prefix", ns)
        if p.text is not None
    ]
    keys = [k.text for k in root.findall(f".//{tag}Contents/{tag}Key", ns) if k.text is not None]
    return {
        "ok": True,
        "status": 200,
        "sub_prefixes": [p[len(prefix) :] for p in prefixes][:60],
        "files_sample": sorted(keys)[:3] + sorted(keys)[-3:],
        "file_count_first_page": len(keys),
    }


async def check_ws(url: str, wait_s: float) -> dict[str, Any]:
    first_latency_ms: float | None = None
    messages = 0
    sample: Any = None
    start = time.perf_counter()
    async with websockets.connect(url, open_timeout=TIMEOUT, ping_interval=None) as ws:
        deadline = start + wait_s
        while time.perf_counter() < deadline:
            try:
                remaining = max(0.1, deadline - time.perf_counter())
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except TimeoutError:
                break
            messages += 1
            if first_latency_ms is None:
                first_latency_ms = round((time.perf_counter() - start) * 1000)
                sample = json.loads(raw)
    return {
        "ok": messages > 0,
        "messages": messages,
        "first_message_ms": first_latency_ms,
        "sample": sample,
    }


async def check_clock(client: httpx.AsyncClient) -> dict[str, Any]:
    offsets = []
    for _ in range(5):
        t0 = _now_ms()
        resp = await client.get("https://api.binance.com/api/v3/time")
        t1 = _now_ms()
        server = int(resp.json()["serverTime"])
        offsets.append(server - (t0 + t1) / 2)
    offsets.sort()
    median = offsets[len(offsets) // 2]
    return {"ok": abs(median) <= 1000, "median_offset_ms": round(median, 1), "samples": offsets}


async def main_async(ws_seconds: float) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for name, url in REST_CHECKS:
            results.append(await _timed(name, lambda u=url: check_rest(client, u)))
        for prefix in ARCHIVE_PREFIXES:
            results.append(
                await _timed(f"archive:{prefix}", lambda p=prefix: check_archive(client, p))
            )
        results.append(await _timed("clock_offset", lambda: check_clock(client)))
    ws_results = await asyncio.gather(
        *(_timed(name, lambda u=url: check_ws(u, ws_seconds)) for name, url in WS_CHECKS)
    )
    results.extend(ws_results)
    loop = asyncio.get_running_loop()
    return {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "event_loop": type(loop).__name__,
        "httpx": httpx.__version__,
        "websockets": websockets.__version__,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--ws-seconds", type=float, default=20.0)
    parser.add_argument("--out-dir", type=Path, default=Path("evidence/spikes"))
    args = parser.parse_args()
    report = asyncio.run(main_async(args.ws_seconds))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out_dir / f"binance_check_{platform.system().lower()}_{stamp}.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    width = max(len(r["name"]) for r in report["results"])
    for r in report["results"]:
        mark = "OK  " if r["ok"] else "FAIL"
        extra = r.get("error") or r.get("status") or r.get("messages") or r.get("median_offset_ms")
        print(f"{mark} {r['name']:<{width}} {r['ms']:>6} ms  {extra}")
    print(f"\nevent loop: {report['event_loop']}  ·  report: {out}")
    failed = [r["name"] for r in report["results"] if not r["ok"]]
    if failed:
        print(f"{len(failed)} check(s) failed: {', '.join(failed)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
