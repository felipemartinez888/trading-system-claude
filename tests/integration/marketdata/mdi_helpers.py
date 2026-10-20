"""Shared helpers for market-data integration tests (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tsclaude.contracts.timeutil import ManualClock

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "binance"
T0 = 1_792_411_200_000  # 2026-10-19T12:00:00Z
MIN = 60_000


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def kline_row(open_ms: int, close: str = "100.5") -> list[Any]:
    """One REST kline row in Binance's documented 12-column array format."""
    return [
        open_ms,
        "100.0",
        "101.0",
        "99.0",
        close,
        "10.0",
        open_ms + MIN - 1,
        "1000.0",
        42,
        "5.0",
        "500.0",
        "0",
    ]


def ws_kline(
    open_ms: int, *, closed: bool = True, symbol: str = "BTCUSDT", close: str = "100.5"
) -> dict[str, Any]:
    """A kline event in Binance's documented WebSocket format (values synthetic)."""
    return {
        "e": "kline",
        "E": open_ms + MIN + 5 if closed else open_ms + 30_000,
        "s": symbol,
        "k": {
            "t": open_ms,
            "T": open_ms + MIN - 1,
            "s": symbol,
            "i": "1m",
            "f": 1,
            "L": 42,
            "o": "100.0",
            "c": close,
            "h": "101.0",
            "l": "99.0",
            "v": "10.0",
            "n": 42,
            "x": closed,
            "q": "1000.0",
            "V": "5.0",
            "Q": "500.0",
            "B": "0",
        },
    }


class FakeSleep:
    """Advances a ManualClock instead of waiting."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(max(1, round(seconds * 1000)))


class FakeConn:
    """In-memory stand-in for a websockets ClientConnection."""

    def __init__(self, url: str) -> None:
        import asyncio

        self.url = url
        self.inbox: asyncio.Queue[str | Exception] = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = False

    async def recv(self) -> str:
        item = await self.inbox.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True
        self.inbox.put_nowait(ConnectionError("closed by client"))

    def push(self, payload: object, stream: str | None = None) -> None:
        body = payload if stream is None else {"stream": stream, "data": payload}
        self.inbox.put_nowait(body if isinstance(body, str) else json.dumps(body))

    def drop(self) -> None:
        self.inbox.put_nowait(ConnectionError("server dropped the connection"))


class FakeExchange:
    """Connector handing out FakeConn objects; tests drive them directly."""

    def __init__(self) -> None:
        self.conns: list[FakeConn] = []

    async def connect(self, url: str) -> FakeConn:
        conn = FakeConn(url)
        self.conns.append(conn)
        return conn

    def latest(self, fragment: str) -> FakeConn:
        matches = [c for c in self.conns if fragment in c.url]
        if not matches:
            raise LookupError(fragment)
        return matches[-1]
