"""Paper-trading runtime: runs `PaperTradingService` next to live market data (MVP-2).

The supervisor gives the runtime its **own** market-data subscription at every monitoring start.
The runtime opens (or restores) the paper session from its hash-chained journal, feeds it closed
bars and market snapshots, polls it, and publishes an immutable status snapshot that API threads
read without touching live objects.

Fail closed (CLAUDE.md rule 4). Paper decisions stop immediately, and the state says why, when:
- the session cannot be opened or restored (`UNAVAILABLE`);
- the service faults on a step or a journal write (`FAULTED`);
- downtime after the journal is unresolved (`AWAITING_BACKFILL`); one all-or-nothing backfill
  attempt from the live bar store is made per monitoring start, after validating every bar;
- the subscription dropped an update (`STOPPED_OVERFLOW`: a lost bar would otherwise turn into a
  silent feed gap), was closed while monitoring runs (`SUBSCRIPTION_LOST`), or the consumer
  raised (`CONSUMER_DIED`).
Within a monitoring run nothing resumes paper after it stopped. The next monitoring start (manual
or unattended) restores the active session from its verified journal again; a failure then shows
again. A new paper session is an explicit, token-protected operator action with a note
(`request_new_session`); old sessions stay in the hash chain.

At start the runtime waits for the market-data service's own startup backfill (`wait_backfill`,
bounded by `backfill_wait_s`) before it opens the session, seeds the risk tracker or attempts its
own backfill, so it never reads a half-filled bar store. Updates arriving meanwhile stay queued in
its subscription; any dropped update stops paper (`STOPPED_OVERFLOW`).

Known limitations, deliberately kept: restore equivalence across a restart is **not** guaranteed,
and not every divergence is visible. Seed 13073 (a bar after the journal's last key that never
arrives) keeps the service in `AWAITING_BACKFILL`. Seed 465923499 (release-time dependence) lets the
shadow book's fills differ from an uninterrupted run while paper shows `RUNNING` and labels values
`SIMULATED`. A journal that cannot be restored shows `UNAVAILABLE`. The per-minute journal-close
design is not implemented.

Values are simulated (`Provenance.SIMULATED`); nothing here places orders anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from tsclaude.contracts.events import AggregateType, Event, EventType
from tsclaude.contracts.market import Bar, Interval, Market
from tsclaude.contracts.provenance import Provenance, Sourced
from tsclaude.contracts.timeutil import MS_PER_MINUTE, WallClock, to_iso
from tsclaude.marketdata.bars import BarClosed
from tsclaude.marketdata.models import MarketSnapshot
from tsclaude.risk.market import MarketStateTracker
from tsclaude.sim.clock import order_key
from tsclaude.sim.live import DEFAULT_SETTLE_MS
from tsclaude.sim.paper import LiveStrategy, PaperConfig, PaperTradingService, ServiceState
from tsclaude.store.eventstore import EventStore

log = logging.getLogger(__name__)

PAPER_CONFIG_VERSION = "paper-config-v1"
AUDIT_FILE = "paper_sessions.jsonl"
MAX_FILLS = 200
MAX_NOTE = 500
_SESSION_ID = re.compile(r"^paper[-0-9A-Za-z]{0,60}$")
_REQUEST_ID = re.compile(r"^[0-9A-Za-z_-]{8,64}$")
FILL_TYPE = "sim_order_filled"

LIMITATIONS = (
    "Paper trading is simulated (non-production); no order is ever sent to an exchange.",
    "Restart equivalence is not guaranteed. A bar that never arrives after a restart keeps paper "
    "AWAITING_BACKFILL (seed 13073); after a restart the shadow book can differ from an "
    "uninterrupted run while paper shows RUNNING / SIMULATED (seed 465923499); a journal that "
    "cannot be restored shows UNAVAILABLE. M3 acceptance is not claimed.",
    "Each monitoring start restores the active paper session again, also after a failure in the "
    "previous run; a new session is only ever created by the operator.",
    "Instrument filters come from configs/backtest.yaml and are unverified placeholders.",
)


class PaperState(StrEnum):
    DISABLED = "DISABLED"  # configs/paper.yaml: enabled: false (the default)
    IDLE = "IDLE"  # enabled; monitoring is not running
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    AWAITING_BACKFILL = "AWAITING_BACKFILL"
    FAULTED = "FAULTED"
    UNAVAILABLE = "UNAVAILABLE"
    STOPPED_OVERFLOW = "STOPPED_OVERFLOW"
    SUBSCRIPTION_LOST = "SUBSCRIPTION_LOST"
    CONSUMER_DIED = "CONSUMER_DIED"


#: states in which paper decisions are being made (a new session is refused)
ACTIVE_STATES = frozenset({PaperState.STARTING, PaperState.RUNNING})


class SubscriptionLike(Protocol):
    def close(self) -> None: ...

    async def get(self, timeout: float | None = None) -> BarClosed | Event | None: ...


type BarReader = Callable[[Market, str, int, int], Sequence[Bar]]


# ---------------------------------------------------------------------- settings
@dataclass(frozen=True)
class PaperSettings:
    enabled: bool = False
    session_id: str = "paper"
    starting_equity: Decimal = Decimal(10_000)
    settle_ms: int = DEFAULT_SETTLE_MS
    poll_s: float = 1.0
    strategy: str = "none"
    backfill_from_live_bars: bool = True
    #: no update at all for this long while market data runs -> SUBSCRIPTION_LOST (the real
    #: subscription returns None both on timeout and when closed, so silence is the only signal)
    silence_s: float = 180.0
    #: longest wait for the market-data startup backfill before opening (then UNAVAILABLE)
    backfill_wait_s: float = 900.0
    source: str = "defaults"

    def __post_init__(self) -> None:
        if not _SESSION_ID.match(self.session_id):
            raise ValueError(f"invalid paper session_id {self.session_id!r}")
        if self.strategy != "none":
            # MVP-2: only "none"; the reference strategy is wired by MVP-3 (fail closed)
            raise ValueError(f"paper strategy {self.strategy!r} is not available in this build")
        if self.settle_ms < 0 or self.poll_s <= 0 or self.silence_s <= self.poll_s:
            raise ValueError("settle_ms must be >= 0, poll_s > 0 and silence_s > poll_s")
        if self.backfill_wait_s <= 0:
            raise ValueError("backfill_wait_s must be > 0")
        if self.starting_equity <= 0:
            raise ValueError("starting_equity must be positive")

    @classmethod
    def load(cls, path: Path) -> PaperSettings:
        """`configs/paper.yaml`; a missing file means paper trading is off."""
        if not path.exists():
            return cls(source=f"{path} not found (paper trading off)")
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: expected a mapping")
        known = {
            "version",
            "enabled",
            "session_id",
            "starting_equity",
            "settle_ms",
            "poll_s",
            "strategy",
            "backfill_from_live_bars",
            "silence_s",
            "backfill_wait_s",
        }
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(f"{path}: unknown keys {unknown}")
        if raw.get("version", PAPER_CONFIG_VERSION) != PAPER_CONFIG_VERSION:
            raise ValueError(f"{path}: unsupported version {raw.get('version')!r}")
        enabled = raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError(f"{path}: enabled must be true or false")
        return cls(
            enabled=enabled,
            session_id=str(raw.get("session_id", "paper")),
            starting_equity=Decimal(str(raw.get("starting_equity", "10000"))),
            settle_ms=int(raw.get("settle_ms", DEFAULT_SETTLE_MS)),
            poll_s=float(raw.get("poll_s", 1.0)),
            strategy=str(raw.get("strategy", "none")),
            backfill_from_live_bars=bool(raw.get("backfill_from_live_bars", True)),
            silence_s=float(raw.get("silence_s", 180.0)),
            backfill_wait_s=float(raw.get("backfill_wait_s", 900.0)),
            source=str(path),
        )


def build_paper_config(settings: PaperSettings, base: Path) -> PaperConfig:
    """Paper books for the live universe: instruments from `backtest.yaml`, risk from
    `risk.yaml`, pairs from `universe.yaml`. Any missing file or spec raises (fail closed)."""
    from tsclaude.marketdata.config import load_config
    from tsclaude.research.runner import BacktestConfig
    from tsclaude.risk.config import load_risk_config

    universe = load_config(base / "universe.yaml", base / "data_quality.yaml").universe
    backtest = BacktestConfig.load(base / "backtest.yaml")
    specs = tuple(backtest.instrument(m, s) for m, s in universe.pairs())
    return PaperConfig(
        instruments=specs,
        starting_equity=settings.starting_equity,
        risk=load_risk_config(base / "risk.yaml"),
        decision_latency_ms=backtest.decision_latency_ms,
    )


# ---------------------------------------------------------------------- audit log
class PaperActionRefused(Exception):
    """An operator action was refused; `code` maps to an HTTP status."""

    def __init__(self, code: int, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class SessionLog:
    """Append-only JSON-lines record of operator-created paper sessions (`paper_sessions.jsonl`).

    It is an audit aid, not the source of truth: the paper sessions themselves live in the
    hash-chained event store, and a new session never touches an old one. A malformed log fails
    closed (the runtime refuses to start rather than guess the active session).
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{self.path}:{n}: malformed audit record") from exc
            if not isinstance(rec, dict) or not _SESSION_ID.match(
                str(rec.get("new_session_id", ""))
            ):
                raise ValueError(f"{self.path}:{n}: invalid audit record")
            out.append(rec)
        return out

    def active(self, default: str) -> str:
        records = self.records()
        return str(records[-1]["new_session_id"]) if records else default

    def append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())


# ---------------------------------------------------------------------- backfill validation
@dataclass
class BackfillCheck:
    bars: list[Bar] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def validate_backfill(
    rows: dict[tuple[Market, str], Sequence[Bar]],
    *,
    start_ms: int,
    end_ms: int,
    now_ms: int,
) -> BackfillCheck:
    """Identity, timeframe, timestamps and ordering of candidate backfill bars.

    Every bar must be the requested instrument's closed 1m bar with an aligned open in
    ``[start_ms, end_ms)`` and ``close = open + 1 min - 1``; keys must be unique. Any problem
    rejects the whole set (all or nothing). Completeness is checked by the caller against the
    service (`missing_before`)."""
    check = BackfillCheck()
    seen: set[tuple[int, str, str]] = set()
    for (market, symbol), bars in sorted(rows.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        for b in bars:
            where = f"{market}:{symbol}@{b.open_time_ms if isinstance(b, Bar) else '?'}"
            if not isinstance(b, Bar):
                check.problems.append(f"{where}: not a Bar")
                continue
            if (b.market, b.symbol) != (market, symbol):
                check.problems.append(f"{where}: identity {b.market}:{b.symbol} differs")
            if b.interval is not Interval.M1:
                check.problems.append(f"{where}: interval {b.interval} is not 1m")
            if b.open_time_ms % MS_PER_MINUTE or b.close_time_ms != (
                b.open_time_ms + MS_PER_MINUTE - 1
            ):
                check.problems.append(f"{where}: timestamps not a closed 1m bar")
            if not start_ms <= b.open_time_ms < end_ms:
                check.problems.append(f"{where}: outside [{start_ms}, {end_ms})")
            if b.close_time_ms >= now_ms:
                check.problems.append(f"{where}: not closed at {now_ms}")
            key = order_key(b)
            if key in seen:
                check.problems.append(f"{where}: duplicate bar")
            seen.add(key)
            check.bars.append(b)
    check.bars.sort(key=order_key)
    return check


# ---------------------------------------------------------------------- runtime
def _frozen(value: Any) -> Any:
    """A JSON-only deep copy (Decimals as strings): safe to hand to other threads."""
    return json.loads(json.dumps(value, default=str, sort_keys=True))


class PaperRuntime:
    def __init__(
        self,
        settings: PaperSettings,
        store: EventStore,
        clock: WallClock,
        *,
        config_factory: Callable[[], PaperConfig],
        audit_path: Path,
        bars: BarReader | None = None,
        strategy_factory: Callable[[], LiveStrategy | None] | None = None,
    ) -> None:
        self.settings = settings
        self._store = store
        self._clock = clock
        self._config_factory = config_factory
        self._log = SessionLog(audit_path)
        self._bars = bars
        self._strategy_factory = strategy_factory
        self.on_problem: Callable[[str, str], None] | None = None
        self.on_change: Callable[[], None] | None = None
        self.state = PaperState.IDLE if settings.enabled else PaperState.DISABLED
        self.reason: str | None = None if settings.enabled else "paper trading is off"
        self.service: PaperTradingService | None = None
        self.session_id: str | None = None
        self.observing_since_ms: int | None = None  # first poll of this run (Part B hook)
        self.backfill: dict[str, Any] | None = None
        self.dropped_updates = 0
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._sub: SubscriptionLike | None = None
        self._snapshot: Callable[[], MarketSnapshot | None] = lambda: None
        self._market_running: Callable[[], bool] = lambda: False
        self._published: dict[str, Any] = {}
        self._published_at_ms: int | None = None
        self._publish()

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    # ------------------------------------------------------------------ lifecycle
    async def start(
        self,
        subscription: SubscriptionLike,
        *,
        snapshot: Callable[[], MarketSnapshot | None],
        market_running: Callable[[], bool],
        ready: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Open or restore the paper session and start consuming. Never raises: any failure
        leaves the state `UNAVAILABLE` with the reason, and the subscription closed.

        `ready` (the market-data service's `wait_backfill`) is awaited in the background task,
        bounded by `backfill_wait_s`, before the session is opened and the risk tracker seeded, so
        neither reads a bar store that the startup backfill is still filling."""
        if not self.enabled:
            subscription.close()
            return
        if self._task is not None and not self._task.done():
            subscription.close()
            self._problem("Paper start refused", "paper is already running")
            return
        self.service = None
        self.observing_since_ms = None
        self.backfill = None
        self.dropped_updates = 0
        self._snapshot = snapshot
        self._market_running = market_running
        self._sub = subscription
        self._stop = asyncio.Event()
        if ready is None:
            self._set(PaperState.STARTING, None)
            if self._open():
                self._task = asyncio.create_task(self._run(), name="paper-consumer")
            return
        self._set(PaperState.STARTING, "waiting for the market-data startup backfill")
        self._task = asyncio.create_task(self._open_when_ready(ready), name="paper-consumer")

    async def _open_when_ready(self, ready: Callable[[], Awaitable[None]]) -> None:
        try:
            await asyncio.wait_for(ready(), timeout=self.settings.backfill_wait_s)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._fail_open(
                "the market-data startup backfill did not finish within "
                f"{self.settings.backfill_wait_s:g} s"
            )
            return
        except Exception as exc:
            self._fail_open(f"waiting for the market-data backfill failed: {type(exc).__name__}")
            return
        if self._stop.is_set():
            return
        if self._open():
            await self._run()

    def _open(self) -> bool:
        """Open or restore the session from its journal; False (UNAVAILABLE) on any failure."""
        try:
            config = self._config_factory()
            session_id = self._log.active(self.settings.session_id)
            tracker = self._seeded_tracker(config)
            strategy = self._strategy_factory() if self._strategy_factory is not None else None
            service = PaperTradingService.open(
                config,
                self._store,
                session_id=session_id,
                tracker=tracker,
                settle_ms=self.settings.settle_ms,
                strategy=strategy,
            )
        except Exception as exc:
            log.exception("paper session could not be opened")
            self._fail_open(f"paper session could not be opened: {type(exc).__name__}: {exc}")
            return False
        self.service = service
        self.session_id = session_id
        self._set(PaperState.RUNNING, None)
        return True

    def _fail_open(self, reason: str) -> None:
        if self._sub is not None:
            self._sub.close()
            self._sub = None
        self._set(PaperState.UNAVAILABLE, reason)
        self._problem("Paper trading unavailable", reason)

    async def stop(self) -> None:
        """Stop consuming (end of monitoring). The journal is already durable per batch."""
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self.settings.poll_s + 2.0)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._sub is not None:
            self._sub.close()
            self._sub = None
        if self.state in (PaperState.RUNNING, PaperState.AWAITING_BACKFILL, PaperState.STARTING):
            self._set(PaperState.IDLE, "monitoring stopped")
        else:
            self._publish()

    # ------------------------------------------------------------------ consumer
    async def _run(self) -> None:
        service, sub = self.service, self._sub
        if service is None or sub is None:  # start() sets both before creating the task
            return
        loop = asyncio.get_running_loop()
        last_update = loop.time()  # monotonic: independent of the (possibly manual) wall clock
        try:
            while not self._stop.is_set():
                if service.state is ServiceState.FAULTED:
                    self._halt(PaperState.FAULTED, service.fault or "paper service faulted")
                    return
                update = await sub.get(timeout=self.settings.poll_s)
                if self._stop.is_set():
                    return
                if self._overflowed(sub):
                    return
                if update is None and (getattr(sub, "closed", False) or not self._market_running()):
                    self._halt(
                        PaperState.SUBSCRIPTION_LOST,
                        "the market-data subscription closed while monitoring was expected to run",
                    )
                    return
                if update is None and loop.time() - last_update > self.settings.silence_s:
                    self._halt(
                        PaperState.SUBSCRIPTION_LOST,
                        f"no market-data update for more than {self.settings.silence_s:g} s",
                    )
                    return
                if update is not None:
                    last_update = loop.time()
                    service.on_update(update)
                snap = self._snapshot()
                if snap is not None:
                    service.on_snapshot(snap)
                now = self._clock.now_ms()
                if self.observing_since_ms is None:
                    self.observing_since_ms = now
                service.poll(now)
                if service.state is ServiceState.AWAITING_BACKFILL and self.backfill is None:
                    self._attempt_backfill(service, now)
                self._sync_state(service)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("paper consumer died")
            self._halt(PaperState.CONSUMER_DIED, f"{type(exc).__name__}: {exc}")

    def _overflowed(self, sub: SubscriptionLike) -> bool:
        dropped = int(getattr(sub, "dropped", 0))  # fresh subscription per run: any drop counts
        if dropped <= 0:
            return False
        self.dropped_updates = dropped
        self._halt(
            PaperState.STOPPED_OVERFLOW,
            f"{dropped} market-data update(s) were dropped by the paper subscription; "
            "paper decisions stopped (a lost bar would otherwise become a silent gap)",
        )
        return True

    def _sync_state(self, service: PaperTradingService) -> None:
        mapping = {
            ServiceState.RUNNING: PaperState.RUNNING,
            ServiceState.AWAITING_BACKFILL: PaperState.AWAITING_BACKFILL,
            ServiceState.FAULTED: PaperState.FAULTED,
        }
        state = mapping[service.state]
        reason = None
        if state is PaperState.AWAITING_BACKFILL:
            reason = "downtime after the journal is unresolved; waiting for a complete backfill"
        elif state is PaperState.FAULTED:
            reason = service.fault
        if state is not self.state or reason != self.reason:
            self._set(state, reason)
        else:
            self._publish()

    def _halt(self, state: PaperState, reason: str) -> None:
        self._set(state, reason)
        self._problem(f"Paper trading stopped: {state}", reason)

    # ------------------------------------------------------------------ backfill
    def _attempt_backfill(self, service: PaperTradingService, now: int) -> None:
        """One all-or-nothing attempt per monitoring start, from the live bar store only."""
        result: dict[str, Any] = {"attempted_at_ms": now, "ok": False, "processed": 0}
        self.backfill = result
        if not self.settings.backfill_from_live_bars or self._bars is None:
            result["problems"] = ["backfill from the live bar store is disabled"]
            return
        last = service.session.last_minute_ms
        first_live = service.clock.earliest_buffered()
        if last is None or first_live is None:
            result["problems"] = ["no journaled minute or no buffered live minute"]
            return
        rows: dict[tuple[Market, str], Sequence[Bar]] = {}
        try:
            for spec in service.config.instruments:
                rows[spec.key] = list(self._bars(spec.market, spec.symbol, last, first_live))
        except Exception as exc:
            result["problems"] = [f"live bar store read failed: {type(exc).__name__}: {exc}"]
            return
        check = validate_backfill(rows, start_ms=last, end_ms=first_live, now_ms=now)
        problems = list(check.problems)
        if not problems:
            missing = service.missing_before(first_live, check.bars)
            for key, minutes in sorted(missing.items()):
                problems.append(f"{key}: {len(minutes)} minute(s) missing from the backfill")
            if first_live > last + MS_PER_MINUTE and not any(
                b.open_time_ms == first_live - MS_PER_MINUTE for b in check.bars
            ):
                problems.append("no backfilled bar for the minute before the first live minute")
        if problems:
            result["problems"] = problems[:50]
            return
        result["processed"] = service.resume_after_downtime(check.bars)
        result["ok"] = service.state is not ServiceState.FAULTED
        result["problems"] = []

    def _seeded_tracker(self, config: PaperConfig) -> MarketStateTracker:
        """Risk tracker with the lookback seeded from the live bar store (observation only: the
        seeded bars are never processed by the books)."""
        risk = config.risk
        tracker = MarketStateTracker(
            median_window_ms=risk.spread.median_window_ms,
            min_samples=risk.spread.min_samples,
            max_offset_age_ms=risk.clock_offset_max_age_ms,
            lookback_minutes=risk.data_freshness.lookback_minutes,
        )
        if self._bars is None:
            return tracker
        now = self._clock.now_ms()
        start = now - now % MS_PER_MINUTE - risk.data_freshness.lookback_minutes * MS_PER_MINUTE
        seeded: list[Bar] = []
        for spec in config.instruments:
            seeded.extend(
                b for b in self._bars(spec.market, spec.symbol, start, now) if b.close_time_ms < now
            )
        for b in sorted(seeded, key=order_key):
            tracker.observe_bar(b)
        return tracker

    # ------------------------------------------------------------------ operator action
    def request_new_session(self, note: str, request_id: str) -> dict[str, Any]:
        """Record a new paper session to be opened at the next monitoring start.

        Idempotent by `request_id`; refused while paper is processing; never reuses an id that
        already has journal entries. Old sessions are left untouched in the hash chain."""
        note = note.strip()
        if not note or len(note) > MAX_NOTE:
            raise PaperActionRefused(422, f"a note of 1..{MAX_NOTE} characters is required")
        if not _REQUEST_ID.match(request_id):
            raise PaperActionRefused(422, "request_id must be 8-64 characters [0-9A-Za-z_-]")
        if not self.enabled:
            raise PaperActionRefused(409, "paper trading is disabled (configs/paper.yaml)")
        try:
            records = self._log.records()
        except ValueError as exc:
            raise PaperActionRefused(423, str(exc)) from exc
        for rec in records:
            if rec.get("request_id") == request_id:
                if rec.get("note") != note:
                    raise PaperActionRefused(409, "request_id was already used with another note")
                return {**rec, "created": False}
        if self.state in ACTIVE_STATES:
            raise PaperActionRefused(
                409,
                f"paper is {self.state}; a new session can only replace a stopped or failed one",
            )
        now = self._clock.now_ms()
        previous = records[-1]["new_session_id"] if records else self.settings.session_id
        stamp = to_iso(now).replace("-", "").replace(":", "").replace(".", "")[:15] + "Z"
        known = {str(r["new_session_id"]) for r in records} | {self.settings.session_id}
        candidate, n = f"paper-{stamp}", 1
        while candidate in known or self._has_journal(candidate):
            n += 1
            candidate = f"paper-{stamp}-{n}"
        record = {
            "v": 1,
            "ts_ms": now,
            "ts": to_iso(now),
            "request_id": request_id,
            "previous_session_id": previous,
            "new_session_id": candidate,
            "state_at_request": str(self.state),
            "reason_at_request": self.reason,
            "note": note,
        }
        self._log.append(record)
        log.warning(
            "new paper session recorded by operator",
            extra={"previous": previous, "new": candidate, "request_id": request_id},
        )
        self._problem(
            "New paper session recorded",
            f"{previous} -> {candidate} (opens at the next monitoring start): {note}",
        )
        self._publish()
        return {**record, "created": True}

    def _has_journal(self, session_id: str) -> bool:
        return bool(
            self._store.read(
                types=[EventType.PAPER_STEP],
                aggregate=(AggregateType.SIM, session_id),
                limit=1,
            )
        )

    # ------------------------------------------------------------------ published state
    def _set(self, state: PaperState, reason: str | None) -> None:
        self.state = state
        self.reason = reason
        self._publish()

    def _problem(self, title: str, detail: str) -> None:
        if self.on_problem is not None:
            try:
                self.on_problem(title, detail)
            except Exception:
                log.exception("paper problem callback failed")

    def _publish(self) -> None:
        """Build the immutable snapshot read by API threads (on the event loop only)."""
        now = self._clock.now_ms()
        try:
            next_session = self._log.active(self.settings.session_id)
        except ValueError as exc:
            next_session = f"unreadable audit log: {exc}"
        snap: dict[str, Any] = {
            "state": str(self.state),
            "reason": self.reason,
            "enabled": self.enabled,
            "settings_source": self.settings.source,
            "strategy": self.settings.strategy,
            "session_id": self.session_id,
            "next_session_id": next_session,
            "observing_since_ms": self.observing_since_ms,
            "dropped_updates": self.dropped_updates,
            "backfill": self.backfill,
            "limitations": list(LIMITATIONS),
            "service": None,
            "books": {},
            "fills": [],
        }
        service = self.service
        if service is not None:
            snap["service"] = service.status()
            snap["books"] = {
                name: {
                    "equity": book.equity(),
                    "cash": book.cash,
                    "unrealized_pnl": book.unrealized_pnl(),
                    "open_positions": book.open_positions,
                    "positions": {
                        f"{m}:{s}": qty for (m, s), qty in sorted(book.positions_snapshot().items())
                    },
                    "halted": book.halted,
                    "ledger_reconciled": book.ledger.reconcile().ok,
                }
                for name, book in service.session.sim.books.items()
            }
            fills = [e for e in service.session.sim.events if e.get("type") == FILL_TYPE]
            snap["fills"] = [
                {"ts_ms": e["ts_ms"], "aggregate_id": e["aggregate_id"], **e["payload"]}
                for e in fills[-MAX_FILLS:]
            ]
        self._published = _frozen(snap)
        self._published_at_ms = now
        if self.on_change is not None:
            with contextlib.suppress(Exception):
                self.on_change()

    def published(self) -> tuple[dict[str, Any], int | None]:
        """The last published snapshot and when it was taken (safe from any thread)."""
        return self._published, self._published_at_ms

    def summary(self) -> Sourced[str]:
        """One-line state for the status bar (`StatusResponse.paper`)."""
        snap, at = self.published()
        label = str(snap.get("state", self.state))
        if snap.get("reason"):
            label = f"{label}: {snap['reason']}"
        return Sourced[str](value=label, provenance=Provenance.SYSTEM, as_of_ms=at)


def load_paper_runtime(
    config_base: Path,
    data_dir: Path,
    store: EventStore,
    clock: WallClock,
    bars: BarReader | None,
) -> PaperRuntime:
    """The app's paper runtime from `configs/paper.yaml`. An invalid file never enables paper:
    the runtime is created disabled and shows `UNAVAILABLE` with the reason."""
    path = config_base / "paper.yaml"
    try:
        settings = PaperSettings.load(path)
        problem = None
    except Exception as exc:
        settings = PaperSettings(source=f"{path} invalid")
        problem = f"{path} is invalid: {type(exc).__name__}: {exc}"
    runtime = PaperRuntime(
        settings,
        store,
        clock,
        config_factory=lambda: build_paper_config(settings, config_base),
        audit_path=data_dir / AUDIT_FILE,
        bars=bars,
    )
    if problem is not None:
        runtime.state = PaperState.UNAVAILABLE
        runtime.reason = problem
        runtime._publish()
    return runtime


__all__ = [
    "ACTIVE_STATES",
    "AUDIT_FILE",
    "BarReader",
    "PaperActionRefused",
    "PaperRuntime",
    "PaperSettings",
    "PaperState",
    "SessionLog",
    "build_paper_config",
    "load_paper_runtime",
    "validate_backfill",
]
