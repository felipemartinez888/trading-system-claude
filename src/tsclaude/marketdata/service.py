"""`MarketDataService`: the facade the runtime supervisor (M1-T2) starts and stops.

`start()`:
1. rebuild the gap registry from persisted gap events;
2. read `exchangeInfo` for spot and USD-M (seeds REST weight limiters, symbol status);
3. sample the server clock (median of N) and fix `watch_from` = the current forming minute by
   server time; one bar builder per (market, symbol) owns every minute from there on;
4. start the WebSocket groups, then — concurrently — startup backfill of every minute before
   `watch_from` (so there is neither overlap nor a hole between backfill and live);
5. start periodic loops: freshness tick, watermark check + bounded gap retries, clock sampling.

`stop()` closes everything; stream states go to DISCONNECTED with reason `stopped`.

Every produced event is appended to the event store first and then published to subscribers
(`subscribe()`), together with `BarClosed` notifications. A slow subscriber cannot block ingestion:
its queue is bounded and overflow is counted on the subscription (the data itself is durable in
`state.db` and the event log).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tsclaude.contracts.events import Event, EventType, NewEvent
from tsclaude.contracts.market import Bar, BarSource, Market, StreamState
from tsclaude.contracts.provenance import Provenance, Sourced
from tsclaude.contracts.timeutil import MS_PER_DAY, WallClock
from tsclaude.marketdata.backfill import (
    Backfiller,
    BackfillResult,
    BackfillSettings,
    current_minute_open,
)
from tsclaude.marketdata.bars import BarBuilder, BarClosed, SymbolBarBuilder
from tsclaude.marketdata.binance.ws import WsConnect, WsManager, plan_groups
from tsclaude.marketdata.config import MarketDataConfig
from tsclaude.marketdata.models import (
    BookTop,
    ConnectionStatus,
    Funding,
    MarketSnapshot,
    PartialCandle,
    StreamStatus,
    SymbolSnapshot,
)
from tsclaude.marketdata.types import (
    BookTickerUpdate,
    KlineUpdate,
    MarkPriceUpdate,
    ParsedMessage,
    RestKline,
    StreamKey,
    StreamKind,
)
from tsclaude.quality.clock import ClockMonitor
from tsclaude.quality.freshness import FreshnessMonitor
from tsclaude.quality.gaps import GapCause, GapRegistry
from tsclaude.runtime.sessions import observation_intervals
from tsclaude.store.eventstore import EventStore
from tsclaude.store.livebars import LiveBarStore

if TYPE_CHECKING:
    import httpx

    from tsclaude.marketdata.binance.rest import BinanceRest

log = logging.getLogger(__name__)

PUBLISHED_EVENTS = frozenset(
    {
        EventType.STREAM_STATE_CHANGED,
        EventType.GAP_DETECTED,
        EventType.GAP_RESOLVED,
        EventType.BAR_LATE,
        EventType.BAR_CONFLICT,
        EventType.CLOCK_SKEW_CHANGED,
        EventType.BACKFILL_COMPLETED,
    }
)

type Update = BarClosed | Event


class Subscription:
    """Async iterator of updates (closed bars and market-data events) for one consumer."""

    def __init__(self, owner: MarketDataService, maxsize: int) -> None:
        self._owner = owner
        self._queue: asyncio.Queue[Update | None] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def _put(self, item: Update | None) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1  # durable copies remain in state.db / the event log

    def close(self) -> None:
        self._owner._subscribers.discard(self)
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)

    def __aiter__(self) -> AsyncIterator[Update]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Update]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def get(self, timeout: float | None = None) -> Update | None:
        """Next update, or None if the subscription was closed (or timed out)."""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return None


@dataclass
class _Mark:
    update: MarkPriceUpdate
    received_at_ms: int


@dataclass
class _FundingState:
    rate: float
    next_funding_time_ms: int | None
    source: str
    as_of_ms: int


class MarketDataService:
    def __init__(
        self,
        config: MarketDataConfig,
        store: EventStore,
        clock: WallClock,
        http_transport: httpx.AsyncBaseTransport | None = None,
        ws_connect: WsConnect | None = None,
        *,
        session_id: str | None = None,
        bars: LiveBarStore | None = None,
        rng: Callable[[float, float], float] | None = None,
    ) -> None:
        self._cfg = config
        self._store = store
        self._clock = clock
        self._transport = http_transport
        self._ws_connect = ws_connect
        self._session_id = session_id
        self._own_bars = bars is None
        self._bars = bars if bars is not None else LiveBarStore(store.path)
        self._rng = rng
        self._subscribers: set[Subscription] = set()
        self.gaps = GapRegistry(self._emit, clock.now_ms)
        self.builder = BarBuilder()
        self._rest: dict[Market, BinanceRest] = {}
        self._ws: WsManager | None = None
        self._clock_monitor: ClockMonitor | None = None
        self.freshness: FreshnessMonitor | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._books: dict[tuple[Market, str], BookTop] = {}
        self._marks: dict[str, _Mark] = {}
        self._funding: dict[str, _FundingState] = {}
        self._status: dict[tuple[Market, str], str] = {}
        self._watch_from: int | None = None
        self.backfill_results: list[BackfillResult] = []
        self._backfill_done = asyncio.Event()
        self._running = False
        self._last_gap_retry_ms = 0

    # ------------------------------------------------------------------ events
    def _emit(self, event: NewEvent) -> Event:
        stored = self._store.append(event)
        if stored.type in PUBLISHED_EVENTS:
            for sub in list(self._subscribers):
                sub._put(stored)
        return stored

    def _on_bar(self, closed: BarClosed) -> None:
        for sub in list(self._subscribers):
            sub._put(closed)

    def subscribe(self, maxsize: int = 10_000) -> Subscription:
        sub = Subscription(self, maxsize)
        self._subscribers.add(sub)
        return sub

    # ------------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return self._running

    @property
    def watch_from_ms(self) -> int | None:
        return self._watch_from

    def rest(self, market: Market) -> BinanceRest:
        return self._rest[market]

    async def start(self) -> None:
        if self._running:
            raise RuntimeError("market data service already running")
        from tsclaude.marketdata.binance.rest import BinanceRest  # needs httpx

        q = self._cfg.quality
        universe = self._cfg.universe
        self.gaps.load(self._store.read(types=[EventType.GAP_DETECTED, EventType.GAP_RESOLVED]))
        self._rest = {
            Market.SPOT: BinanceRest(
                Market.SPOT,
                q.endpoints.spot_rest,
                self._clock,
                q.rest,
                transport=self._transport,
            ),
            Market.USDM: BinanceRest(
                Market.USDM,
                q.endpoints.usdm_rest,
                self._clock,
                q.rest,
                transport=self._transport,
            ),
        }
        for market in (Market.SPOT, Market.USDM):
            symbols = universe.symbols(market)
            if not symbols:
                continue
            try:
                info = await self._rest[market].load_exchange_info(symbols)
                for s in symbols:
                    sym = info.symbols.get(s)
                    self._status[(market, s)] = sym.status if sym else "NOT_LISTED"
            except Exception as exc:  # fail closed: status stays unknown
                log.error("exchangeInfo failed", extra={"market": str(market), "error": repr(exc)})

        self._clock_monitor = ClockMonitor(
            self._clock,
            self._rest[Market.SPOT].server_time,
            self._emit,
            samples_per_round=q.clock.samples_per_round,
            threshold_ms=q.clock.skew_threshold_ms,
        )
        await self._clock_monitor.sample_round()
        server_now = self._clock_monitor.server_now_ms()
        watch_from = current_minute_open(server_now)
        self._watch_from = watch_from

        keys: list[StreamKey] = []
        for market, symbol in universe.pairs():
            self.builder.add(
                SymbolBarBuilder(
                    market=market,
                    symbol=symbol,
                    watch_from_ms=watch_from,
                    store=self._bars,
                    gaps=self.gaps,
                    emit=self._emit,
                    now_ms=self._clock.now_ms,
                    server_now_ms=self._clock_monitor.server_now_ms,
                    session_id=self._session_id,
                    grace_ms=int(q.watermark.grace_s * 1000),
                    max_gap_attempts=q.watermark.gap_retry_max_attempts,
                    on_bar=self._on_bar,
                    cause_for=self._cause_for,
                )
            )
            kinds = [StreamKind.KLINE_1M, StreamKind.BOOK_TICKER]
            if market is Market.USDM:
                kinds.append(StreamKind.MARK_PRICE)
            keys.extend(StreamKey(market, symbol, k) for k in kinds)
        self.freshness = FreshnessMonitor(
            keys, q.freshness.budget_ms, self._emit, self._clock.now_ms, self._repaired
        )
        for symbol in universe.usdm:
            await self._load_premium_index(symbol)

        groups = plan_groups(universe, q.endpoints, q.ws)
        self._ws = WsManager(
            groups,
            self._on_message,
            q.ws,
            self._clock,
            self._ws_connect,
            on_open=self._on_streams_open,
            on_close=self._on_streams_close,
            rng=self._rng,
        )
        self._running = True
        self._ws.start()
        self._tasks = [
            asyncio.create_task(self._backfill(watch_from), name="md-backfill"),
            asyncio.create_task(
                self._loop(q.freshness.tick_interval_s, self._tick_freshness), name="md-freshness"
            ),
            asyncio.create_task(
                self._loop(q.watermark.check_interval_s, self.check_watermarks), name="md-watermark"
            ),
            asyncio.create_task(
                self._loop(q.clock.sample_interval_s, self._sample_clock, initial_delay=True),
                name="md-clock",
            ),
        ]

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._ws is not None:
            await self._ws.stop()
        for rest in self._rest.values():
            await rest.aclose()
        if self._own_bars:
            self._bars.close()
        for sub in list(self._subscribers):
            sub.close()

    async def wait_backfill(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._backfill_done.wait(), timeout)

    # ------------------------------------------------------------------ periodic work
    async def _loop(
        self, interval_s: float, fn: Callable[[], Any], *, initial_delay: bool = False
    ) -> None:
        if initial_delay:
            await asyncio.sleep(interval_s)
        while True:
            try:
                result = fn()
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:  # periodic work must never die silently
                log.exception("periodic market-data task failed")
            await asyncio.sleep(interval_s)

    def _tick_freshness(self) -> None:
        if self.freshness is not None:
            self.freshness.tick()

    async def _sample_clock(self) -> None:
        if self._clock_monitor is not None:
            await self._clock_monitor.sample_round()

    async def check_watermarks(self) -> None:
        """One watermark pass over every builder, plus bounded gap retries when due."""
        retry_due = (
            self._clock.now_ms() - self._last_gap_retry_ms
            >= self._cfg.quality.watermark.gap_retry_interval_s * 1000
        )
        for b in self.builder.all():
            await b.check_watermark(self._fetch_klines)
            if retry_due:
                await b.retry_gaps(self._fetch_klines)
        if retry_due:
            self._last_gap_retry_ms = self._clock.now_ms()

    async def _fetch_klines(
        self, market: Market, symbol: str, start_ms: int, end_ms: int
    ) -> list[RestKline]:
        return await self._rest[market].klines(symbol, start_ms, end_ms)

    async def _backfill(self, end_ms: int) -> None:
        q = self._cfg.quality.backfill
        settings = BackfillSettings(
            lookback_ms=int(q.lookback_days * MS_PER_DAY),
            max_backfill_ms=int(q.max_backfill_days * MS_PER_DAY),
        )
        backfiller = Backfiller(
            self._bars, self.gaps, self._fetch_klines, self._emit, self._clock.now_ms, settings
        )
        intervals = observation_intervals(self._store)
        try:
            for market, symbol in self._cfg.universe.pairs():
                try:
                    res = await backfiller.run(market, symbol, end_ms, intervals)
                    self.backfill_results.append(res)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception(
                        "backfill failed", extra={"market": str(market), "symbol": symbol}
                    )
        finally:
            self._backfill_done.set()

    async def _load_premium_index(self, symbol: str) -> None:
        try:
            pi = await self._rest[Market.USDM].premium_index(symbol)
        except Exception as exc:
            log.warning("premiumIndex failed", extra={"symbol": symbol, "error": repr(exc)})
            return
        self._funding[symbol] = _FundingState(
            pi.last_funding_rate, pi.next_funding_time_ms, "rest_premium_index", pi.time_ms
        )

    # ------------------------------------------------------------------ stream callbacks
    def _cause_for(self, market: Market, symbol: str) -> GapCause:
        if self.freshness is None:
            return GapCause.UNKNOWN
        state = self.freshness.state(StreamKey(market, symbol, StreamKind.KLINE_1M))
        if state in (StreamState.DISCONNECTED, StreamState.RECOVERING):
            return GapCause.DISCONNECT
        return GapCause.UNKNOWN

    def _repaired(self, key: StreamKey) -> bool:
        if key.kind is not StreamKind.KLINE_1M:
            return True
        b = self.builder.get(key.market, key.symbol)
        return b is not None and b.caught_up()

    def _on_streams_open(self, keys: tuple[StreamKey, ...], _reason: str) -> None:
        if self.freshness is None:
            return
        for k in keys:
            self.freshness.on_open(k)

    def _on_streams_close(self, keys: tuple[StreamKey, ...], reason: str) -> None:
        if self.freshness is None:
            return
        for k in keys:
            self.freshness.on_close(k, reason)

    def _on_message(self, message: ParsedMessage) -> None:
        now = self._clock.now_ms()
        if isinstance(message, KlineUpdate):
            self.builder.on_kline(message)
            key = StreamKey(message.market, message.symbol, StreamKind.KLINE_1M)
            fresh = message.is_closed
        elif isinstance(message, BookTickerUpdate):
            current = self._books.get((message.market, message.symbol))
            if current is None or message.update_id >= current.update_id:
                self._books[(message.market, message.symbol)] = BookTop(
                    bid_price=message.bid_price,
                    bid_qty=message.bid_qty,
                    ask_price=message.ask_price,
                    ask_qty=message.ask_qty,
                    update_id=message.update_id,
                    received_at_ms=now,
                    event_time_ms=message.event_time_ms,
                )
            key = StreamKey(message.market, message.symbol, StreamKind.BOOK_TICKER)
            fresh = True
        elif isinstance(message, MarkPriceUpdate):
            prev = self._marks.get(message.symbol)
            if prev is None or message.event_time_ms >= prev.update.event_time_ms:
                self._marks[message.symbol] = _Mark(message, now)
                if message.funding_rate is not None:
                    self._funding[message.symbol] = _FundingState(
                        message.funding_rate,
                        message.next_funding_time_ms,
                        "ws_mark_price",
                        message.event_time_ms,
                    )
            key = StreamKey(Market.USDM, message.symbol, StreamKind.MARK_PRICE)
            fresh = True
        else:
            return
        if self.freshness is not None and key in self.freshness:
            self.freshness.on_message(key, fresh=fresh)

    # ------------------------------------------------------------------ snapshot
    def _stream_ok(self, key: StreamKey) -> bool:
        return (
            self._running
            and self.freshness is not None
            and key in self.freshness
            and self.freshness.state(key) is StreamState.HEALTHY
        )

    def _sourced_live[T](self, value: T, ok: bool, as_of_ms: int) -> Sourced[T]:
        prov = Provenance.LIVE if ok else Provenance.STALE
        return Sourced[T](value=value, provenance=prov, as_of_ms=as_of_ms)

    def _bar_sourced(self, market: Market, symbol: str) -> Sourced[Bar]:
        stored = self._bars.last(market, symbol)
        if stored is None:
            return Sourced[Bar].unavailable("no closed bar stored yet")
        bar = stored.bar
        ok = self._stream_ok(StreamKey(market, symbol, StreamKind.KLINE_1M))
        if not ok:
            prov = Provenance.STALE
        elif bar.source is BarSource.WS_LIVE:
            prov = Provenance.LIVE
        elif bar.source is BarSource.REST_RECOVERED:
            prov = Provenance.RECOVERED
        else:
            prov = Provenance.HISTORICAL
        return Sourced[Bar](value=bar, provenance=prov, as_of_ms=bar.close_time_ms)

    def _partial_sourced(self, market: Market, symbol: str) -> Sourced[PartialCandle]:
        b = self.builder.get(market, symbol)
        k = b.partial() if b is not None else None
        if k is None:
            return Sourced[PartialCandle].unavailable("no forming candle received")
        candle = PartialCandle(
            market=k.market,
            symbol=k.symbol,
            open_time_ms=k.open_time_ms,
            close_time_ms=k.close_time_ms,
            open=k.open,
            high=k.high,
            low=k.low,
            close=k.close,
            volume=k.volume,
            quote_volume=k.quote_volume,
            trades=k.trades,
            event_time_ms=k.event_time_ms,
        )
        ok = self._stream_ok(StreamKey(market, symbol, StreamKind.KLINE_1M))
        prov = Provenance.PARTIAL if ok else Provenance.STALE
        return Sourced[PartialCandle](value=candle, provenance=prov, as_of_ms=k.event_time_ms)

    def snapshot(self) -> MarketSnapshot:
        now = self._clock.now_ms()
        streams: list[StreamStatus] = []
        if self.freshness is not None:
            for h in self.freshness.all():
                state = (
                    Sourced[StreamState](value=h.state, provenance=Provenance.LIVE, as_of_ms=now)
                    if self._running
                    else Sourced[StreamState](
                        value=h.state, provenance=Provenance.STALE, as_of_ms=h.since_ms
                    )
                )
                streams.append(
                    StreamStatus(
                        stream=h.key.id,
                        market=h.key.market,
                        symbol=h.key.symbol,
                        kind=str(h.key.kind),
                        state=state,
                        since_ms=h.since_ms,
                        budget_ms=h.budget_ms,
                        last_message_ms=h.last_message_ms,
                        last_fresh_ms=h.last_fresh_ms,
                        disconnects=h.disconnects,
                    )
                )
        symbols: list[SymbolSnapshot] = []
        for market, symbol in self._cfg.universe.pairs():
            book = self._books.get((market, symbol))
            book_s = (
                self._sourced_live(
                    book,
                    self._stream_ok(StreamKey(market, symbol, StreamKind.BOOK_TICKER)),
                    book.received_at_ms,
                )
                if book is not None
                else Sourced[BookTop].unavailable("no bookTicker received")
            )
            if market is Market.USDM:
                mark = self._marks.get(symbol)
                mark_ok = self._stream_ok(StreamKey(market, symbol, StreamKind.MARK_PRICE))
                mark_s = (
                    self._sourced_live(mark.update.mark_price, mark_ok, mark.update.event_time_ms)
                    if mark is not None
                    else Sourced[float].unavailable("no markPrice received")
                )
                fs = self._funding.get(symbol)
                funding_s = (
                    self._sourced_live(
                        Funding(
                            rate=fs.rate,
                            next_funding_time_ms=fs.next_funding_time_ms,
                            source=fs.source,
                        ),
                        mark_ok,
                        fs.as_of_ms,
                    )
                    if fs is not None
                    else Sourced[Funding].unavailable("no funding data received")
                )
            else:
                mark_s = Sourced[float].unavailable("spot has no mark price")
                funding_s = Sourced[Funding].unavailable("spot has no funding")
            status = self._status.get((market, symbol))
            status_s = (
                Sourced[str](value=status, provenance=Provenance.LIVE, as_of_ms=now)
                if status is not None
                else Sourced[str].unavailable("exchangeInfo not loaded")
            )
            symbols.append(
                SymbolSnapshot(
                    market=market,
                    symbol=symbol,
                    last_closed_bar=self._bar_sourced(market, symbol),
                    partial_candle=self._partial_sourced(market, symbol),
                    best_bid_ask=book_s,
                    mark_price=mark_s,
                    funding=funding_s,
                    open_gap_minutes=sum(
                        g.missing_minutes for g in self.gaps.open_gaps(market, symbol)
                    ),
                    exchange_status=status_s,
                )
            )
        cm = self._clock_monitor
        offset = (
            Sourced[int](
                value=cm.offset_ms,
                provenance=Provenance.LIVE,
                as_of_ms=cm.last_round.at_ms if cm.last_round else now,
            )
            if cm is not None and cm.offset_ms is not None
            else Sourced[int].unavailable("server clock not sampled")
        )
        connections = [
            ConnectionStatus(
                group=s.group,
                url=s.url,
                connected=s.connected,
                connect_attempts=s.connect_attempts,
                reconnects=s.reconnects,
                recycles=s.recycles,
                malformed=s.malformed,
                messages=s.messages,
            )
            for s in (self._ws.stats() if self._ws is not None else [])
        ]
        return MarketSnapshot(
            as_of_ms=now,
            running=self._running,
            clock_offset_ms=offset,
            rest_banned={str(m): r.banned for m, r in self._rest.items()},
            streams=streams,
            symbols=symbols,
            connections=connections,
        )
