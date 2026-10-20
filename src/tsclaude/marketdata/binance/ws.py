"""WebSocket connection manager for Binance public market streams.

Connection groups (DATA_ARCHITECTURE §1):
- spot: `wss://stream.binance.com:9443` (fallback `wss://data-stream.binance.vision`):
  `<symbol>@kline_1m`, `<symbol>@bookTicker`;
- USD-M `/market` at `wss://fstream.binance.com/market`: `<symbol>@kline_1m`,
  `<symbol>@markPrice@1s`;
- USD-M `/public` at `wss://fstream.binance.com/public`: `<symbol>@bookTicker`.
Each group uses combined-stream mode (`<base>/stream?streams=a/b/c`), so subscribing needs no
control messages; a group larger than `max_streams_per_connection` is split.

Per connection:
- reconnect with exponential back-off + full jitter, capped; back-off resets after the connection
  has been stable for `stable_after_s`; after `fallback_after_failures` consecutive failures the
  next base URL is tried;
- every connection attempt (all connections share one budget per IP) passes a sliding-window
  limiter far below Binance's 300 per 5 min;
- planned recycle at `recycle_after_s` (default 23 h): open a new connection, wait for its first
  message, then close the old one. Messages from both arrive during the overlap; the bar builder
  absorbs duplicate closed bars. If the new connection does not deliver in time, the old one is
  kept and the recycle retried after `recycle_retry_s`;
- outgoing control messages (`subscribe`/`unsubscribe`/`list_subscriptions`) pass a per-connection
  limiter (<= 4/s spot, <= 8/s futures). Protocol pings are answered by the `websockets` library;
  its own keepalive pings (one per `ping_interval_s`) are not counted by our limiter;
- every frame is parsed here; malformed frames and handler errors are counted, logged and skipped,
  never allowed to end the read loop.

The `websockets` library is imported only by the default connector, so this module and its logic
are testable with an injected in-memory connector.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import WallClock
from tsclaude.marketdata.binance.limits import Backoff, Sleep, SlidingWindowLimiter
from tsclaude.marketdata.binance.parse import Envelope, MalformedMessage, parse_ws_frame
from tsclaude.marketdata.config import EndpointsConfig, UniverseConfig, WsConfig
from tsclaude.marketdata.types import (
    BookTickerUpdate,
    ControlResponse,
    KlineUpdate,
    MarkPriceUpdate,
    ParsedMessage,
    StreamKey,
    StreamKind,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------- connector seam
class WsConnection(Protocol):
    """The subset of `websockets.asyncio.client.ClientConnection` the manager uses."""

    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...

    async def close(self) -> None: ...


type WsConnect = Callable[[str], Awaitable[WsConnection]]


def default_connector(config: WsConfig) -> WsConnect:
    """Connector on the `websockets` asyncio client (imported lazily)."""

    async def connect(url: str) -> WsConnection:
        from websockets.asyncio.client import connect as ws_connect

        conn = await ws_connect(
            url,
            open_timeout=config.open_timeout_s,
            ping_interval=config.ping_interval_s,
            ping_timeout=config.ping_timeout_s,
            close_timeout=5,
            max_size=2**22,
        )
        return conn

    return connect


# ---------------------------------------------------------------------------- stream naming
_KIND_SUFFIX = {
    StreamKind.KLINE_1M: "kline_1m",
    StreamKind.BOOK_TICKER: "bookTicker",
    StreamKind.MARK_PRICE: "markPrice@1s",
}


def stream_name(key: StreamKey) -> str:
    return f"{key.symbol.lower()}@{_KIND_SUFFIX[key.kind]}"


def key_for(message: ParsedMessage) -> StreamKey | None:
    """Which stream a parsed message belongs to (None for control responses)."""
    if isinstance(message, KlineUpdate):
        return StreamKey(message.market, message.symbol, StreamKind.KLINE_1M)
    if isinstance(message, BookTickerUpdate):
        return StreamKey(message.market, message.symbol, StreamKind.BOOK_TICKER)
    if isinstance(message, MarkPriceUpdate):
        return StreamKey(message.market, message.symbol, StreamKind.MARK_PRICE)
    return None


def combined_url(base: str, streams: Sequence[StreamKey]) -> str:
    return f"{base.rstrip('/')}/stream?streams={'/'.join(stream_name(k) for k in streams)}"


@dataclass(frozen=True)
class ConnectionGroup:
    name: str  # e.g. "spot", "usdm-market", "usdm-public" (+ "#n" when split)
    market: Market
    urls: tuple[str, ...]
    streams: tuple[StreamKey, ...]
    control_msgs_per_s: int


def plan_groups(
    universe: UniverseConfig, endpoints: EndpointsConfig, ws: WsConfig
) -> list[ConnectionGroup]:
    spot = tuple(
        StreamKey(Market.SPOT, s, kind)
        for s in universe.spot
        for kind in (StreamKind.KLINE_1M, StreamKind.BOOK_TICKER)
    )
    fut_market = tuple(
        StreamKey(Market.USDM, s, kind)
        for s in universe.usdm
        for kind in (StreamKind.KLINE_1M, StreamKind.MARK_PRICE)
    )
    fut_public = tuple(StreamKey(Market.USDM, s, StreamKind.BOOK_TICKER) for s in universe.usdm)
    plan = [
        ("spot", Market.SPOT, endpoints.spot_ws, spot, ws.control_msgs_per_s_spot),
        (
            "usdm-market",
            Market.USDM,
            endpoints.usdm_ws_market,
            fut_market,
            ws.control_msgs_per_s_futures,
        ),
        (
            "usdm-public",
            Market.USDM,
            endpoints.usdm_ws_public,
            fut_public,
            ws.control_msgs_per_s_futures,
        ),
    ]
    groups: list[ConnectionGroup] = []
    size = ws.max_streams_per_connection
    for name, market, urls, streams, rate in plan:
        chunks = [streams[i : i + size] for i in range(0, len(streams), size)]
        for n, chunk in enumerate(chunks):
            label = name if len(chunks) == 1 else f"{name}#{n + 1}"
            groups.append(ConnectionGroup(label, market, urls, tuple(chunk), rate))
    return groups


# ---------------------------------------------------------------------------- one connection
type MessageHandler = Callable[[ParsedMessage], None]
type StreamsCallback = Callable[[tuple[StreamKey, ...], str], None]


@dataclass
class ConnectionStats:
    group: str
    url: str | None = None
    connected: bool = False
    connected_since_ms: int | None = None
    connect_attempts: int = 0
    connect_failures: int = 0
    reconnects: int = 0
    recycles: int = 0
    recycle_failures: int = 0
    crashes: int = 0  # connection task died unexpectedly and was restarted by the manager
    fallbacks: int = 0
    messages: int = 0
    malformed: int = 0
    handler_errors: int = 0
    control_sent: int = 0
    control_errors: int = 0
    last_message_ms: dict[StreamKey, int] = field(default_factory=dict)
    last_error: str | None = None


class _Live:
    """A connected socket and its reader task."""

    _ids = itertools.count(1)

    def __init__(self, conn: WsConnection, url: str) -> None:
        self.id = next(self._ids)
        self.conn = conn
        self.url = url
        self.first_message = asyncio.Event()
        self.reader: asyncio.Task[str] | None = None
        self.opened_at = asyncio.get_running_loop().time()


class StreamConnection:
    """Keeps one connection group connected until `stop()`."""

    def __init__(
        self,
        group: ConnectionGroup,
        handler: MessageHandler,
        config: WsConfig,
        clock: WallClock,
        connect: WsConnect,
        connect_budget: SlidingWindowLimiter,
        *,
        on_open: StreamsCallback | None = None,
        on_close: StreamsCallback | None = None,
        sleep: Sleep = asyncio.sleep,
        rng: Callable[[float, float], float] | None = None,
    ) -> None:
        self.group = group
        self._handler = handler
        self._cfg = config
        self._clock = clock
        self._connect = connect
        self._budget = connect_budget
        self._on_open = on_open
        self._on_close = on_close
        self._sleep = sleep
        self._backoff = Backoff(
            config.backoff_base_s,
            config.backoff_cap_s,
            rng if rng is not None else random.SystemRandom().uniform,
        )
        self._control = SlidingWindowLimiter(group.control_msgs_per_s, 1_000, clock, sleep)
        self._url_index = 0
        self._consecutive_failures = 0
        self._stopping = asyncio.Event()
        self._active: _Live | None = None
        self._control_ids = itertools.count(1)
        self.stats = ConnectionStats(group=group.name)

    # ------------------------------------------------------------------ public
    @property
    def url(self) -> str:
        return combined_url(self.group.urls[self._url_index], self.group.streams)

    async def stop(self) -> None:
        self._stopping.set()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    async def wait_or_stop(self, seconds: float) -> bool:
        """Sleep up to `seconds`; True if stop was requested meanwhile."""
        return await self._wait_or_stop(seconds)

    async def send_control(self, method: str, params: list[str] | None = None) -> int:
        """Send a control message (rate-limited). Returns its id."""
        live = self._active
        if live is None:
            raise ConnectionError("not connected")
        await self._control.acquire()
        ident = next(self._control_ids)
        body: dict[str, object] = {"method": method, "id": ident}
        if params is not None:
            body["params"] = params
        await live.conn.send(json.dumps(body))
        self.stats.control_sent += 1
        return ident

    async def subscribe(self, keys: Sequence[StreamKey]) -> int:
        return await self.send_control("SUBSCRIBE", [stream_name(k) for k in keys])

    async def unsubscribe(self, keys: Sequence[StreamKey]) -> int:
        return await self.send_control("UNSUBSCRIBE", [stream_name(k) for k in keys])

    async def list_subscriptions(self) -> int:
        return await self.send_control("LIST_SUBSCRIPTIONS")

    async def run(self) -> None:
        """Connect, serve, reconnect, recycle — until `stop()`.

        An unexpected exception propagates (after the streams are reported closed with reason
        `internal_error: ...`) so that `WsManager` can log it and restart the connection.
        """
        reason = "stopped"
        try:
            while not self._stopping.is_set():
                live = await self._open_with_retry()
                if live is None:
                    break
                self._activate(live)
                reason = await self._serve()
                current = self._active
                if current is not None:
                    await self._dispose(current)
                self._deactivate(reason)
                # Jittered pause before reconnecting: <= backoff_base_s after a stable connection,
                # growing if connections keep dropping right after opening (flapping).
                if await self._wait_or_stop(self._backoff.next_delay()):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = f"internal_error: {type(exc).__name__}: {exc}"[:200]
            self.stats.last_error = reason
            raise
        finally:
            await self._close_active(reason)

    # ------------------------------------------------------------------ internals
    async def _wait_or_stop(self, seconds: float) -> bool:
        """Sleep up to `seconds`; True if stop was requested meanwhile."""
        if seconds <= 0:
            return self._stopping.is_set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        return self._stopping.is_set()

    async def _attempt(self) -> _Live | None:
        """One budgeted connection attempt; starts the reader on success."""
        wait = self._budget.wait_ms()
        if wait > 0:
            log.warning(
                "connection-attempt budget exhausted; waiting",
                extra={"group": self.group.name, "wait_ms": wait},
            )
        await self._budget.acquire()
        url = self.url
        self.stats.connect_attempts += 1
        try:
            conn = await self._connect(url)
        except Exception as exc:
            self.stats.connect_failures += 1
            self.stats.last_error = repr(exc)
            log.warning(
                "websocket connect failed",
                extra={"group": self.group.name, "url": url, "error": repr(exc)},
            )
            return None
        live = _Live(conn, url)
        live.reader = asyncio.create_task(self._read(live), name=f"ws-read-{self.group.name}")
        return live

    async def _open_with_retry(self) -> _Live | None:
        while not self._stopping.is_set():
            live = await self._attempt()
            if live is not None:
                return live
            self._consecutive_failures += 1
            if (
                len(self.group.urls) > 1
                and self._consecutive_failures % self._cfg.fallback_after_failures == 0
            ):
                self._url_index = (self._url_index + 1) % len(self.group.urls)
                self.stats.fallbacks += 1
                log.warning(
                    "switching websocket endpoint",
                    extra={"group": self.group.name, "url": self.group.urls[self._url_index]},
                )
            delay = self._backoff.next_delay()
            if await self._wait_or_stop(delay):
                return None
        return None

    def _activate(self, live: _Live) -> None:
        reconnect = self.stats.connected_since_ms is not None
        self._active = live
        self.stats.connected = True
        self.stats.url = live.url
        self.stats.connected_since_ms = self._clock.now_ms()
        if reconnect:
            self.stats.reconnects += 1
        log.info("websocket connected", extra={"group": self.group.name, "url": live.url})
        self._notify(self._on_open, "connected")

    def _deactivate(self, reason: str) -> None:
        self.stats.connected = False
        self._active = None
        self._notify(self._on_close, reason)

    def _notify(self, callback: StreamsCallback | None, reason: str) -> None:
        """Run a health callback; its failure is logged and counted, never fatal."""
        if callback is None:
            return
        try:
            callback(self.group.streams, reason)
        except Exception:
            self.stats.handler_errors += 1
            log.exception("stream callback failed", extra={"group": self.group.name})

    async def _serve(self) -> str:
        """Serve the active connection until it ends; perform planned recycles. Returns reason."""
        loop = asyncio.get_running_loop()
        live = self._active
        if live is None or live.reader is None:  # pragma: no cover - set by _activate
            return "no_connection"
        backoff_reset = False
        recycle_at = live.opened_at + self._cfg.recycle_after_s
        stop_task = asyncio.create_task(self._stopping.wait())
        try:
            while True:
                live = self._active
                if live is None or live.reader is None:  # pragma: no cover - defensive
                    return "no_connection"
                now = loop.time()
                wake = recycle_at
                if not backoff_reset:
                    stable_at = live.opened_at + self._cfg.stable_after_s
                    if now >= stable_at and live.first_message.is_set():
                        self._backoff.reset()
                        self._consecutive_failures = 0
                        backoff_reset = True
                    elif now < stable_at:
                        wake = min(wake, stable_at)
                    else:  # open long enough but silent: re-check shortly
                        wake = min(wake, now + 1.0)
                done, _ = await asyncio.wait(
                    {live.reader, stop_task},
                    timeout=max(0.0, wake - now),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done:
                    return "stopped"
                if live.reader in done:
                    return live.reader.result()
                if loop.time() >= recycle_at:
                    if await self._recycle():
                        backoff_reset = True
                        recycle_at = loop.time() + self._cfg.recycle_after_s
                    else:
                        recycle_at = loop.time() + self._cfg.recycle_retry_s
        finally:
            stop_task.cancel()

    async def _recycle(self) -> bool:
        """Open a new connection, wait for its first message, then close the old one.

        The wait races against `stop()` and against the new connection dropping. Whatever ends
        the recycle early — timeout, stop, drop, or cancellation of this task — the new
        connection is disposed of unless it has already replaced the old one.
        """
        old = self._active
        if old is None:
            return False
        log.info("planned websocket recycle", extra={"group": self.group.name})
        new: _Live | None = None
        try:
            new = await self._attempt()
            if new is None:
                self.stats.recycle_failures += 1
                return False
            outcome = await self._await_first_message(new)
            if outcome != "message":
                if outcome != "stopped":
                    self.stats.recycle_failures += 1
                    log.warning(
                        "recycle aborted; keeping the old connection",
                        extra={"group": self.group.name, "outcome": outcome},
                    )
                await self._dispose(new)
                return False
            self._active = new
            self.stats.url = new.url
            self.stats.recycles += 1
            self.stats.connected_since_ms = self._clock.now_ms()
            await self._dispose(old)
            log.info("recycle complete", extra={"group": self.group.name, "url": new.url})
            return True
        except BaseException:
            if new is not None and self._active is not new:
                await self._dispose(new)
            raise

    async def _await_first_message(self, new: _Live) -> str:
        """'message', 'stopped', 'dropped' or 'timeout' — whichever happens first."""
        if new.first_message.is_set():
            return "message"
        msg = asyncio.create_task(new.first_message.wait())
        stop = asyncio.create_task(self._stopping.wait())
        waiters: set[asyncio.Future[Any]] = {msg, stop}
        if new.reader is not None:
            waiters.add(new.reader)
        try:
            await asyncio.wait(
                waiters,
                timeout=self._cfg.first_message_timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            msg.cancel()
            stop.cancel()
            await asyncio.gather(msg, stop, return_exceptions=True)
        if self._stopping.is_set():
            return "stopped"
        if new.reader is not None and new.reader.done():
            return "dropped"
        return "message" if new.first_message.is_set() else "timeout"

    async def _dispose(self, live: _Live) -> None:
        if live.reader is not None and not live.reader.done():
            live.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await live.reader
        with contextlib.suppress(Exception):
            await live.conn.close()

    async def _close_active(self, reason: str) -> None:
        live = self._active
        if live is not None:
            await self._dispose(live)
            self._deactivate(reason)

    async def _read(self, live: _Live) -> str:
        """Read frames until the connection ends. Never raises; returns the close reason."""
        while True:
            try:
                raw = await live.conn.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = f"connection_lost: {type(exc).__name__}: {exc}"[:200]
                self.stats.last_error = reason
                log.warning("websocket closed", extra={"group": self.group.name, "reason": reason})
                return reason
            try:
                self._dispatch(raw)
            except Exception:  # last line of defence: one frame must never end the loop
                self.stats.handler_errors += 1
                log.exception("websocket frame dispatch failed", extra={"group": self.group.name})
            live.first_message.set()

    def _dispatch(self, raw: str | bytes) -> None:
        self.stats.messages += 1
        try:
            env: Envelope = parse_ws_frame(raw, self.group.market)
        except Exception as exc:  # MalformedMessage, or an unexpected parser failure
            self.stats.malformed += 1
            reason = exc.reason if isinstance(exc, MalformedMessage) else repr(exc)
            log.error(
                "malformed websocket message skipped",
                extra={
                    "group": self.group.name,
                    "reason": reason,
                    "sample": (raw[:200] if isinstance(raw, str) else repr(raw[:200])),
                },
            )
            return
        message = env.message
        if isinstance(message, ControlResponse):
            if message.error is not None:
                self.stats.control_errors += 1
                log.error(
                    "control message rejected",
                    extra={"group": self.group.name, "error": str(message.error)},
                )
            return
        key = key_for(message)
        if key is not None:
            self.stats.last_message_ms[key] = self._clock.now_ms()
        try:
            self._handler(message)
        except Exception:
            self.stats.handler_errors += 1
            log.exception("message handler failed", extra={"group": self.group.name})


# ---------------------------------------------------------------------------- manager
class WsManager:
    """Owns all connection groups and the shared per-IP connection-attempt budget."""

    def __init__(
        self,
        groups: Sequence[ConnectionGroup],
        handler: MessageHandler,
        config: WsConfig,
        clock: WallClock,
        connect: WsConnect | None = None,
        *,
        on_open: StreamsCallback | None = None,
        on_close: StreamsCallback | None = None,
        sleep: Sleep = asyncio.sleep,
        rng: Callable[[float, float], float] | None = None,
    ) -> None:
        self._budget = SlidingWindowLimiter(
            config.connect_budget, int(config.connect_budget_window_s * 1000), clock, sleep
        )
        self._config = config
        self._rng = rng
        connector = connect if connect is not None else default_connector(config)
        self.connections = [
            StreamConnection(
                g,
                handler,
                config,
                clock,
                connector,
                self._budget,
                on_open=on_open,
                on_close=on_close,
                sleep=sleep,
                rng=rng,
            )
            for g in groups
        ]
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def running(self) -> bool:
        return any(not t.done() for t in self._tasks)

    def start(self) -> None:
        if self._tasks:
            raise RuntimeError("already started")
        self._tasks = [
            asyncio.create_task(self._supervise(c), name=f"ws-{c.group.name}")
            for c in self.connections
        ]

    async def _supervise(self, conn: StreamConnection) -> None:
        """Keep `conn.run()` alive until stop: log, count and restart it if it ever dies."""
        backoff = Backoff(
            self._config.backoff_base_s,
            self._config.backoff_cap_s,
            self._rng if self._rng is not None else random.SystemRandom().uniform,
        )
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            try:
                await conn.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                conn.stats.crashes += 1
                conn.stats.last_error = f"internal_error: {type(exc).__name__}: {exc}"[:200]
                log.exception(
                    "websocket connection task crashed; restarting",
                    extra={"group": conn.group.name, "crashes": conn.stats.crashes},
                )
            else:
                if conn.stopping:
                    return
                conn.stats.crashes += 1
                log.error(
                    "websocket connection task ended without stop; restarting",
                    extra={"group": conn.group.name},
                )
            if loop.time() - started >= self._config.supervisor_stable_after_s:
                backoff.reset()  # one crash after a long healthy run restarts promptly
            if conn.stopping or await conn.wait_or_stop(backoff.next_delay()):
                return

    async def stop(self, timeout_s: float = 5.0) -> None:
        """Ask every connection to stop; cancel those still busy after `timeout_s`."""
        for c in self.connections:
            await c.stop()
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=timeout_s)
            for t in pending:
                t.cancel()
            results = await asyncio.gather(*self._tasks, return_exceptions=True)
            for task, result in zip(self._tasks, results, strict=True):
                if isinstance(result, Exception):
                    log.error(
                        "websocket task failed during stop",
                        extra={"task": task.get_name(), "error": repr(result)},
                    )
        self._tasks = []

    def stats(self) -> list[ConnectionStats]:
        return [c.stats for c in self.connections]
