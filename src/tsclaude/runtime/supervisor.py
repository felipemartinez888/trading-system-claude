"""Monitoring supervisor: starts and stops live market data for the app (M1-T2).

Owns one `MarketDataService` per app process (created on the first start, so a missing or invalid
`configs/*.yaml` surfaces as a *start failure*, never as a crash at launch). Contract with the
service (see `marketdata/service.py`):

- `start(session_id=...)` may be called again after `stop()`; a failed start cleans itself up and
  re-raises — the supervisor propagates the error so monitoring stays STOPPED with the reason;
- `stop()` closes every subscription, so the supervisor subscribes again **before each start**
  (before, so no bar closed right after start is missed) and closes that subscription itself if
  the start fails;
- `snapshot()` stays readable after `stop()` (values turn STALE) until `close()`.

All event-store writes (market data and demo signals) go through the app's single `EventStore`.
Everything here runs on the server's event loop; `stream_summary` is a plain attribute refreshed
on the loop so that threads (sync endpoints, heartbeat) can read it without touching the service.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tsclaude.contracts.events import Event
from tsclaude.contracts.market import Market, StreamState
from tsclaude.contracts.provenance import Provenance, Sourced
from tsclaude.contracts.timeutil import WallClock
from tsclaude.marketdata.bars import BarClosed
from tsclaude.marketdata.models import MarketSnapshot
from tsclaude.signals.demo import DEFAULT_THRESHOLD, DemoDetector, DemoDetectorConfig
from tsclaude.store.eventstore import EventStore

log = logging.getLogger(__name__)

NOT_STARTED = "monitoring has not been started in this app session"
STREAM_GROUP_MARKETS: dict[str, Market] = {"spot": Market.SPOT, "futures": Market.USDM}


class WeightLimiterLike(Protocol):
    @property
    def windows(self) -> dict[str, tuple[int, int | None]]: ...


class RestLike(Protocol):
    @property
    def weights(self) -> WeightLimiterLike: ...


class SubscriptionLike(Protocol):
    def close(self) -> None: ...

    async def get(self, timeout: float | None = None) -> BarClosed | Event | None: ...


class MarketDataLike(Protocol):
    """The subset of `MarketDataService` the supervisor uses (a fake implements it in tests)."""

    @property
    def running(self) -> bool: ...

    async def start(self, session_id: str | None = None) -> None: ...

    async def stop(self) -> None: ...

    def close(self) -> None: ...

    def snapshot(self) -> MarketSnapshot: ...

    def subscribe(self, maxsize: int = 10_000) -> SubscriptionLike: ...

    def rest(self, market: Market) -> RestLike: ...


type ServiceFactory = Callable[[EventStore, WallClock], MarketDataLike]


def config_dir(env: dict[str, str] | None = None) -> Path:
    """`TSC_CONFIG_DIR`, else `./configs`, else the repository's `configs/` (source checkout)."""
    source = dict(os.environ) if env is None else env
    if "TSC_CONFIG_DIR" in source:
        return Path(source["TSC_CONFIG_DIR"])
    local = Path("configs")
    if local.is_dir():
        return local
    return Path(__file__).resolve().parents[3] / "configs"


def default_service_factory(directory: Path | None = None) -> ServiceFactory:
    """Real Binance service from `universe.yaml` and `data_quality.yaml` (read at first start)."""

    def factory(store: EventStore, clock: WallClock) -> MarketDataLike:
        from tsclaude.marketdata.config import load_config
        from tsclaude.marketdata.service import MarketDataService

        base = directory if directory is not None else config_dir()
        config = load_config(base / "universe.yaml", base / "data_quality.yaml")
        service: MarketDataLike = MarketDataService(config, store, clock)
        return service

    return factory


def demo_config_from_env(env: dict[str, str] | None = None) -> DemoDetectorConfig:
    """`TSC_DEMO_DETECTOR=off` disables it; `TSC_DEMO_THRESHOLD=0.003` sets the body threshold."""
    source = dict(os.environ) if env is None else env
    enabled = source.get("TSC_DEMO_DETECTOR", "on").strip().lower() not in {"off", "0", "false"}
    threshold = float(source.get("TSC_DEMO_THRESHOLD", str(DEFAULT_THRESHOLD)))
    return DemoDetectorConfig(enabled=enabled, threshold=threshold)


@dataclass(frozen=True)
class StreamGroupSummary:
    """Status-bar summary of one stream group ("spot" or "futures")."""

    healthy: int
    total: int
    worst: StreamState | None

    def label(self) -> str:
        if self.total == 0:
            return "no streams configured"
        if self.healthy == self.total:
            return f"HEALTHY {self.healthy}/{self.total}"
        return f"{self.worst} ({self.healthy}/{self.total} healthy)"


_SEVERITY = {
    StreamState.HEALTHY: 0,
    StreamState.CONNECTING: 1,
    StreamState.RECOVERING: 2,
    StreamState.STALE: 3,
    StreamState.DISCONNECTED: 4,
}


def summarize_streams(snapshot: MarketSnapshot | None, reason: str) -> dict[str, Sourced[str]]:
    """Aggregate stream states per group; provenance is the streams' own (SYSTEM or STALE)."""
    if snapshot is None:
        return {name: Sourced[str].unavailable(reason) for name in STREAM_GROUP_MARKETS}
    out: dict[str, Sourced[str]] = {}
    for name, market in STREAM_GROUP_MARKETS.items():
        streams = [s for s in snapshot.streams if s.market is market]
        if not streams:
            out[name] = Sourced[str].unavailable(f"no {name} streams in the snapshot")
            continue
        states = [s.state for s in streams]
        healthy = sum(1 for st in states if st.value is StreamState.HEALTHY)
        known = [st.value for st in states if st.value is not None]
        worst = max(known, key=lambda st: _SEVERITY[st]) if known else None
        summary = StreamGroupSummary(healthy, len(streams), worst)
        stale = [st for st in states if st.provenance is Provenance.STALE]
        if stale or not snapshot.running:
            as_of = min((st.as_of_ms for st in stale if st.as_of_ms is not None), default=None)
            out[name] = Sourced[str](
                value=summary.label(),
                provenance=Provenance.STALE,
                as_of_ms=as_of if as_of is not None else snapshot.as_of_ms,
                reason="monitoring stopped" if not snapshot.running else None,
            )
        else:
            out[name] = Sourced[str](
                value=summary.label(), provenance=Provenance.SYSTEM, as_of_ms=snapshot.as_of_ms
            )
    return out


class StartFailed(RuntimeError):
    pass


class MonitoringSupervisor:
    def __init__(
        self,
        store: EventStore,
        clock: WallClock,
        *,
        service_factory: ServiceFactory | None = None,
        demo: DemoDetectorConfig | None = None,
        on_problem: Callable[[str, str], None] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._factory = service_factory or default_service_factory()
        self._service: MarketDataLike | None = None
        self._consumer: asyncio.Task[None] | None = None
        self._session_id: str | None = None
        self._closed = False
        self.detector = DemoDetector(
            demo or DemoDetectorConfig(),
            store.append,
            self._symbol_health,
            clock.now_ms,
        )
        # (title, detail) callback for problems the UI must see (e.g. the consumer died)
        self.on_problem = on_problem
        self.consumer_failures = 0
        self.stream_summary: dict[str, Sourced[str]] = summarize_streams(None, NOT_STARTED)
        self.changes = 0  # bumped on every update; the push loop sends deltas when it moves

    # ------------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return self._service is not None and self._service.running

    @property
    def service(self) -> MarketDataLike | None:
        return self._service

    async def start(self, session_id: str) -> None:
        """Start ingestion; raises `StartFailed` (with the reason) if anything goes wrong."""
        if self._closed:
            raise StartFailed("the supervisor was closed")
        if self.running:
            raise StartFailed("market data is already running")
        if self._service is None:
            try:
                self._service = self._factory(self._store, self._clock)
            except Exception as exc:
                log.exception("market data service could not be created")
                raise StartFailed(f"configuration error: {type(exc).__name__}: {exc}") from exc
        service = self._service
        subscription = service.subscribe()  # before start: closed by stop(), so one per run
        try:
            await service.start(session_id=session_id)
        except asyncio.CancelledError:
            subscription.close()
            raise
        except Exception as exc:
            subscription.close()
            self.refresh()
            raise StartFailed(f"market data failed to start: {type(exc).__name__}: {exc}") from exc
        self._session_id = session_id
        self._consumer = asyncio.create_task(self._consume(subscription), name="md-consumer")
        self._consumer.add_done_callback(self._consumer_done)
        self.refresh()

    async def stop(self) -> None:
        service = self._service
        try:
            if service is not None and service.running:
                await service.stop()  # closes the subscription -> the consumer ends
        finally:
            if self._consumer is not None:
                self._consumer.cancel()
                # A consumer that already died was reported by `_consumer_done`; stopping must not
                # fail because of it.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._consumer
                self._consumer = None
            self.refresh()

    async def close(self) -> None:
        if self._closed:
            return
        try:
            await self.stop()
        finally:
            self._closed = True
            if self._service is not None:
                try:
                    self._service.close()
                except Exception:
                    log.exception("closing the market data service failed")

    # ------------------------------------------------------------------ reads
    def snapshot(self) -> MarketSnapshot | None:
        if self._service is None or self._closed:
            return None
        return self._service.snapshot()

    def refresh(self) -> dict[str, Sourced[str]]:
        """Recompute the cached stream summary (call on the event loop)."""
        try:
            snap = self.snapshot()
        except Exception:
            log.exception("market data snapshot failed")
            self.stream_summary = summarize_streams(None, "market data snapshot failed")
            return self.stream_summary
        self.stream_summary = summarize_streams(snap, NOT_STARTED)
        return self.stream_summary

    def rest_usage(self) -> dict[str, dict[str, tuple[int, int | None]]] | None:
        service = self._service
        if service is None:
            return None
        out: dict[str, dict[str, tuple[int, int | None]]] = {}
        for market in (Market.SPOT, Market.USDM):
            try:
                out[str(market)] = dict(service.rest(market).weights.windows)
            except (KeyError, AttributeError, RuntimeError):
                continue
        return out or None

    # ------------------------------------------------------------------ internals
    def _consumer_done(self, task: asyncio.Task[None]) -> None:
        """The consumer ends normally when stop() closes the subscription; anything else is a
        failure that silently disables the demo detector, so it is logged and surfaced."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        self.consumer_failures += 1
        log.error(
            "market-data consumer died; demo detector stopped until monitoring restarts",
            exc_info=exc,
        )
        if self.on_problem is not None:
            self.on_problem(
                "Market-data consumer stopped",
                f"{type(exc).__name__}: {exc}. Demo detector disabled until monitoring restarts.",
            )

    def _symbol_health(self, market: Market, symbol: str) -> tuple[bool, str | None]:
        """Every stream of `symbol` in *both* markets HEALTHY while the service runs (fail
        closed): a perp-only or spot-only view of a move is not enough for a WATCH."""
        if not self.running:
            return False, "market data is not running"
        try:
            snap = self.snapshot()
        except Exception as exc:
            return False, f"snapshot failed: {type(exc).__name__}"
        if snap is None:
            return False, "no snapshot"
        streams = [s for s in snap.streams if s.symbol == symbol]
        if not any(s.market is market for s in streams):
            return False, "no streams for this symbol"
        bad = [
            f"{s.market}:{s.kind}={s.state.value}"
            for s in streams
            if s.state.value is not StreamState.HEALTHY
            or s.state.provenance is not Provenance.SYSTEM
        ]
        if bad:
            return False, "not HEALTHY: " + ", ".join(bad)
        return True, None

    async def _consume(self, subscription: SubscriptionLike) -> None:
        while True:
            update = await subscription.get()
            if update is None:  # closed by stop()
                return
            self.changes += 1
            if isinstance(update, BarClosed):
                try:
                    self.detector.on_bar(update.bar, session_id=self._session_id)
                except Exception:  # the detector must never break ingestion
                    log.exception("demo detector failed on a bar")
