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
its subscription; any dropped update stops paper (`STOPPED_OVERFLOW`). Before each poll the
consumer offers every update already queued (bounded by `drain_max`; a bounded iteration defers its
poll), so a backlog minute is never released with only its first bar (M3-PB.3).

Observation (P-1; L-02 as the owner clarified it for paper). The service is gated: a bar counts as
observed only if it is a live WebSocket bar that opens at or after the observation anchor, the
first minute open after the run's first poll in exchange time, bounded with the market-data clock
round, the drift since it on the monotonic clock and the wall clock's movement against it
(`observation_bound`). Until trustworthy clock evidence gives the anchor nothing is observed, and
the reason is published (`observation`) and raised as a problem.
Fresh round before the anchor (NB-1 option B, owner decision D-21). The anchor is evaluated only
right after a server-clock round that this run requested succeeded (`resync_clock`, the
market-data service's `resync_clock`), from a snapshot taken after it, and only if the snapshot's
round (the requested one, or a periodic one that finished after it) is at most
`anchor_round_max_age_ms` old on both clocks; an older round never gives the anchor. The request
runs in the background (the consumer keeps polling); one request at a time, bounded by
`resync_timeout_s`, retried after `resync_retry_s` doubling up to `resync_retry_max_s` while it
fails or its round gives no anchor. Without a resync hook, while a request is pending or failed,
or when the round is not fresh enough, nothing is observed (fail closed: resting SL/TP still
resolve, no entry fills or is created). This shortens the interval from the round to the anchor
from up to the round's maximum age (15 min; rounds run every 5 min) to about the round's own
duration plus a poll; it does **not** remove NB-1: a wall step and a compensating monotonic
suspend inside that interval, an anomaly shared by both clocks (a VM pause or migration, a common
slew) or a clock outside the 500 ppm assumption can still give an anchor in a minute that had
started, by an amount that grows with the size of the anomaly (no fixed bound).
Bars before it (the downtime backfill, minutes queued during the startup wait, the minute a crash
interrupted, the partly observed first minute) are processed unobserved, as downtime is
(SIMULATION §7): resting SL/TP resolve over them, no entry fills on them and no signal is computed.
`bracket_reference` is built at the first observed bar (`DeferredReference`).

Known limitations, deliberately kept: restore equivalence across a restart is **not** guaranteed,
and not every divergence is visible. An instrument the live clock released a minute without is a
gap the session passed (the journal records the close, `minute_closed`), not downtime. A bar that
is missing after the journal's last closed minute and that no backfill can supply keeps the service
in `AWAITING_BACKFILL` until the operator records a new session; this includes a minute in which
no instrument had a bar (no journal entry records its close), an unclosed last minute, and journals
written before the close mark existed (seed 13073 behaves this way on them). Release time bounds
execution, and a restart changes the release time of re-delivered bars, so fills in any book, the
baseline included, can differ from an uninterrupted run while paper shows `RUNNING` and labels
values `SIMULATED` (seed 465923499). A journal that cannot be restored shows `UNAVAILABLE`.

Values are simulated (`Provenance.SIMULATED`); nothing here places orders anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
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
from tsclaude.runtime.session_evidence import capture_session_end
from tsclaude.sim.clock import order_key
from tsclaude.sim.engine import SimView
from tsclaude.sim.live import DEFAULT_SETTLE_MS
from tsclaude.sim.paper import (
    ClockEvidence,
    Intent,
    LiveStrategy,
    ObservationVerdict,
    PaperConfig,
    PaperTradingService,
    ServiceState,
    observation_bound,
)
from tsclaude.store.eventstore import EventStore
from tsclaude.strategies.live_adapter import ReferenceLiveStrategy

log = logging.getLogger(__name__)

PAPER_CONFIG_VERSION = "paper-config-v1"
AUDIT_FILE = "paper_sessions.jsonl"
MAX_FILLS = 200
MAX_TRADES = 200  # closed trades kept per book in the snapshot (B-1)
MAX_REJECTIONS = 200  # request-level rejections kept in the snapshot (B-2)
MAX_NOTE = 500
_SESSION_ID = re.compile(r"^paper[-0-9A-Za-z]{0,60}$")
_REQUEST_ID = re.compile(r"^[0-9A-Za-z_-]{8,64}$")
FILL_TYPE = "sim_order_filled"
REQUEST_REJECTED_TYPE = "sim_request_rejected"

LIMITATIONS = (
    "Paper trading is simulated (non-production); no order is ever sent to an exchange.",
    "Restart equivalence is not guaranteed. A bar missing after the journal's last closed minute "
    "that no backfill can supply keeps paper AWAITING_BACKFILL (also a minute with no bar at all, "
    "and journals written before the minute-close mark: seed 13073); after a restart fills in any "
    "book, the baseline included, can differ from an uninterrupted run while paper shows RUNNING "
    "/ SIMULATED, because release time bounds execution (seed 465923499); a journal that cannot "
    "be restored shows UNAVAILABLE. M3 acceptance is not claimed.",
    "Each monitoring start restores the active paper session again, also after a failure in the "
    "previous run; a new session is only ever created by the operator.",
    "Instrument filters come from configs/backtest.yaml and are unverified placeholders.",
    "Each monitoring start observes from the first whole minute after its first poll (exchange "
    "time, bounded with the server-clock round); earlier minutes, the downtime and the minutes "
    "queued during the startup wait included, are processed unobserved (no entry fills or is "
    "created on them). The anchor is fixed only from a server-clock round requested by this run "
    "right before it and found fresh at the poll; while that round is pending, failed or too old, "
    "nothing is observed (retried with backoff). The bound assumes that one and the same local "
    "clock (wall or monotonic) runs within 500 ppm of exchange time over the whole interval from "
    "the round to the poll, without switching clocks midway (an external assumption the runtime "
    "cannot verify), and that the exchange stamps server time while the request is in flight. A "
    "net wall-clock step, suspend or fast slew detected against the monotonic clock blocks "
    "observation until a new round. Not detected (residual risk NB-1): a wall step followed by a "
    "roughly compensating monotonic suspend (or the reverse) inside that interval, an anomaly "
    "shared by both clocks (a VM pause or migration, a common slew), or a clock outside 500 ppm; "
    "each can count a minute that had already started as observed, by an amount that grows with "
    "the anomaly (no fixed bound).",
    "strategy bracket_reference (if selected) is REFERENCE / NOT VALIDATED: a pipeline strategy, "
    "not claimed profitable, paper only; its signals are not research results. It needs about 7.8 "
    "days of unbroken 1m history per instrument before its first decision. It is built at the "
    "first observed minute and warmed up from the live bar store (the 9-day startup backfill and "
    "the unobserved minutes of the start included); after that any minute it does not see "
    "(missing, or REST-recovered/backfilled: processed unobserved) restarts that ~7.8-day warm-up.",
)


#: "none": no strategy (benchmark book only, the default); "bracket_reference": the
#: REFERENCE / NOT VALIDATED pipeline strategy, paper only (MVP-3, strategies/live_adapter.py)
STRATEGIES = frozenset({"none", "bracket_reference"})


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
ACTIVE_STATES = frozenset({PaperState.STARTING, PaperState.RUNNING, PaperState.AWAITING_BACKFILL})


class SubscriptionLike(Protocol):
    def close(self) -> None: ...

    async def get(self, timeout: float | None = None) -> BarClosed | Event | None: ...

    def get_nowait(self) -> BarClosed | Event | None:
        """The oldest queued update without awaiting, or None (empty, or the close sentinel is
        next: it stays queued for `get`)."""
        ...


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
    #: NB-1 option B (D-21): the anchor needs a round this run requested, at most this old at the
    #: poll on both clocks (the round itself takes a few round trips; the next poll follows within
    #: `poll_s`)
    anchor_round_max_age_ms: int = 5_000
    #: longest wait for one requested round (then it counts as failed)
    resync_timeout_s: float = 30.0
    #: wait before the next request after a failed one (or one whose round gave no anchor),
    #: doubling per consecutive failure up to `resync_retry_max_s`
    resync_retry_s: float = 15.0
    resync_retry_max_s: float = 240.0
    #: M3-PB.3: most updates taken without awaiting (`get_nowait`) per consumer iteration; when
    #: reached, that iteration does not poll (older updates may still be queued)
    drain_max: int = 1000
    source: str = "defaults"

    def __post_init__(self) -> None:
        if not _SESSION_ID.match(self.session_id):
            raise ValueError(f"invalid paper session_id {self.session_id!r}")
        if self.strategy not in STRATEGIES:
            raise ValueError(
                f"paper strategy {self.strategy!r} is not available; known: {sorted(STRATEGIES)}"
            )
        if self.settle_ms < 0 or self.poll_s <= 0 or self.silence_s <= self.poll_s:
            raise ValueError("settle_ms must be >= 0, poll_s > 0 and silence_s > poll_s")
        if self.backfill_wait_s <= 0:
            raise ValueError("backfill_wait_s must be > 0")
        if self.anchor_round_max_age_ms <= 0:
            raise ValueError("anchor_round_max_age_ms must be > 0")
        if not 0 < self.resync_retry_s <= self.resync_retry_max_s or self.resync_timeout_s <= 0:
            raise ValueError(
                "resync_timeout_s must be > 0 and 0 < resync_retry_s <= resync_retry_max_s"
            )
        if self.starting_equity <= 0:
            raise ValueError("starting_equity must be positive")
        if isinstance(self.drain_max, bool) or self.drain_max < 1:
            raise ValueError("drain_max must be > 0")

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
            "anchor_round_max_age_ms",
            "resync_timeout_s",
            "resync_retry_s",
            "resync_retry_max_s",
            "drain_max",
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
            anchor_round_max_age_ms=int(raw.get("anchor_round_max_age_ms", 5_000)),
            resync_timeout_s=float(raw.get("resync_timeout_s", 30.0)),
            resync_retry_s=float(raw.get("resync_retry_s", 15.0)),
            resync_retry_max_s=float(raw.get("resync_retry_max_s", 240.0)),
            drain_max=int(raw.get("drain_max", 1000)),
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


class AuditLogAmbiguous(Exception):
    """The audit log does not establish the active paper session unambiguously (fail closed)."""


@dataclass
class LogState:
    """What `paper_sessions.jsonl` establishes.

    `active` is None when the log is ambiguous (`reason` says why). `transitions` are the session
    changes in order: ``{"line", "ts_ms", "previous", "active"}``. `repairable_line` is set when the
    only defect is a complete final record without its newline (MVP-2 readers already used it)."""

    active: str | None
    transitions: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None
    repairable_line: int | None = None
    warnings: list[str] = field(default_factory=list)
    request_records: list[dict[str, Any]] = field(default_factory=list)


class SessionLog:
    """Append-only JSON-lines record of operator-created paper sessions (`paper_sessions.jsonl`).

    It is an audit aid, not the source of truth: the paper sessions themselves live in the
    hash-chained event store, and a new session never touches an old one. Bytes are never
    rewritten or removed. Reading fails closed:

    - a complete line that is not a valid record is accepted only if the **next** line is an
      operator-written quarantine record naming it (`{"kind": "quarantine", "line": n, "sha256":
      <hex of the line's exact bytes, without the line feed but including any CR>,
      "active_session_id": ..., "ts_ms": <wall time of the decision>, "note": ...}`); the
      quarantine then sets the active session explicitly;
    - an unterminated final line that parses as a session record is *repairable* (every earlier
      reader already treated it as active): the runtime may terminate it, append a `repair`
      record, and only after proving journal ownership (`PaperRuntime._resolve_session`);
    - an unterminated final line that does not parse is **ambiguous**: it may be a session switch
      that was used and later damaged, so nothing is inferred and paper stays UNAVAILABLE until
      the operator appends a quarantine record (documented in the MVP-2.1 devlog).
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def inspect(self, default: str) -> LogState:
        """Classify the log. Raises `OSError` when it cannot be read."""
        if not self.path.exists():
            return LogState(active=default)
        data = self.path.read_bytes()
        lines = data.split(b"\n")
        unterminated = lines.pop()  # b"" when the file ends with a newline
        state = LogState(active=default)
        active = default
        pending_invalid: tuple[int, bytes] | None = None
        for n, raw in enumerate(lines, 1):
            if not raw.strip():
                continue
            rec = _parse(raw)
            if pending_invalid is not None:
                bad_n, bad_raw = pending_invalid
                if rec is None or not _is_quarantine(rec, bad_n, bad_raw):
                    return _ambiguous(state, f"line {bad_n}: invalid audit record, not quarantined")
                quarantined = str(rec["active_session_id"])
                state.transitions.append(
                    {
                        "line": n,
                        "ts_ms": rec.get("ts_ms"),
                        "store_seq": rec.get("store_seq"),
                        "previous": active,
                        "active": quarantined,
                    }
                )
                state.warnings.append(f"line {bad_n} quarantined by the operator (line {n})")
                active = quarantined
                pending_invalid = None
                continue
            if rec is None:
                pending_invalid = (n, raw)
                continue
            kind = rec.get("kind")
            if kind == "repair":
                state.warnings.append(f"line {rec.get('line')} was repaired (line {n})")
                continue
            if kind == "quarantine":
                return _ambiguous(state, f"line {n}: quarantine record without an invalid line")
            if not _SESSION_ID.match(str(rec.get("new_session_id", ""))):
                pending_invalid = (n, raw)
                continue
            state.transitions.append(
                {
                    "line": n,
                    "ts_ms": rec.get("ts_ms"),
                    "store_seq": rec.get("store_seq"),
                    "previous": active,
                    "active": str(rec["new_session_id"]),
                }
            )
            state.request_records.append(rec)
            active = str(rec["new_session_id"])
        if pending_invalid is not None:
            return _ambiguous(state, f"line {pending_invalid[0]}: invalid audit record")
        if unterminated and not unterminated.strip():  # stray whitespace, no record in it
            state.repairable_line = len(lines) + 1
            state.active = active
            return state
        if unterminated.strip():
            n = len(lines) + 1
            rec = _parse(unterminated)
            if rec is None or not _SESSION_ID.match(str(rec.get("new_session_id", ""))):
                return _ambiguous(
                    state,
                    f"line {n}: incomplete final audit record (it may be a session switch that "
                    "was used and later damaged); explicit operator recovery is required",
                )
            state.transitions.append(
                {
                    "line": n,
                    "ts_ms": rec.get("ts_ms"),
                    "store_seq": rec.get("store_seq"),
                    "previous": active,
                    "active": str(rec["new_session_id"]),
                }
            )
            state.request_records.append(rec)
            active = str(rec["new_session_id"])
            state.repairable_line = n
        state.active = active
        return state

    def append(self, record: dict[str, Any]) -> None:
        """Append one record (fsynced). Refuses a file whose last line is unterminated."""
        if self.path.exists() and self.path.stat().st_size:
            with self.path.open("rb") as fh:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    raise AuditLogAmbiguous("the audit log ends with an unterminated record")
        self._write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")

    def repair_unterminated(self, line: int, now_ms: int) -> None:
        """Terminate a complete-but-unterminated final record and record the repair."""
        repair = {
            "v": 1,
            "kind": "repair",
            "line": line,
            "ts_ms": now_ms,
            "ts": to_iso(now_ms),
            "note": "terminated a complete final record that lacked its newline",
        }
        self._write("\n" + json.dumps(repair, sort_keys=True) + "\n")

    def _write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())


def _parse(raw: bytes) -> dict[str, Any] | None:
    try:
        rec = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):  # bad UTF-8, bad JSON, absurd numbers or nesting
        return None
    return rec if isinstance(rec, dict) else None


def _is_quarantine(rec: dict[str, Any] | None, line: int, raw: bytes) -> bool:
    return (
        rec is not None
        and rec.get("kind") == "quarantine"
        and rec.get("line") == line
        and rec.get("sha256") == hashlib.sha256(raw).hexdigest()
        and isinstance(rec.get("ts_ms"), int)
        and bool(str(rec.get("note", "")).strip())
        and bool(_SESSION_ID.match(str(rec.get("active_session_id", ""))))
    )


def _ambiguous(state: LogState, reason: str) -> LogState:
    state.active = None
    state.reason = reason
    return state


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


# ---------------------------------------------------------------------- strategy
class DeferredReference:
    """`bracket_reference`, built at its first call: the first observed bar (P-1).

    The engine calls a live strategy only on observed bars, and the bars that open before the
    observation anchor are unobserved, so a strategy built at the start would never see the minutes
    between the start and the anchor and its history would break (a ~7.8-day warm-up again). Built
    here it trades from the first observed minute (`trade_from_ms`) and is warmed up with every
    closed bar of the live bar store before it, those minutes included: the market-data service
    stores a closed bar before it publishes it, so the bars of the minute before were stored when
    they were released. A failed build raises, which the service records as a strategy error (no
    intents for that bar, fail closed); the next observed bar tries again."""

    def __init__(self, build: Callable[[int], ReferenceLiveStrategy]) -> None:
        self._build = build
        self.adapter: ReferenceLiveStrategy | None = None

    def on_bar(self, bar: Bar, view: SimView) -> Sequence[Intent]:
        if self.adapter is None:
            self.adapter = self._build(bar.open_time_ms)
        return self.adapter.on_bar(bar, view)


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
        self._data_dir = audit_path.parent
        #: path of the session-end evidence written by the last stop, or why none was
        self.session_end_evidence: str | None = None
        self._bars = bars
        self._strategy_factory = strategy_factory
        self.on_problem: Callable[[str, str], None] | None = None
        self.on_change: Callable[[], None] | None = None
        self.state = PaperState.IDLE if settings.enabled else PaperState.DISABLED
        self.reason: str | None = None if settings.enabled else "paper trading is off"
        self.service: PaperTradingService | None = None
        self.session_id: str | None = None
        self.observing_since_ms: int | None = None  # first poll of this run (Part B hook)
        #: the latest attempt to fix the observation anchor of this run (P-1)
        self.observation: dict[str, Any] | None = None
        #: why the last attempt gave no anchor (a problem is raised only when it changes)
        self._observation_code: str | None = None
        self.backfill: dict[str, Any] | None = None
        self.dropped_updates = 0
        #: M3-PB.3 consumer counters of this run: updates taken by the drain (`get_nowait`), the
        #: largest drain of one iteration, iterations whose poll was deferred by `drain_max`
        self._drained_updates = 0
        self._largest_drain = 0
        self._drain_deferrals = 0
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._sub: SubscriptionLike | None = None
        self._snapshot: Callable[[], MarketSnapshot | None] = lambda: None
        self._market_running: Callable[[], bool] = lambda: False
        #: NB-1 option B (D-21): the market-data hook that takes a fresh clock round, and this
        #: run's request state (one request at a time, backoff after a failure)
        self._resync: Callable[[], Awaitable[bool]] | None = None
        self._resync_task: asyncio.Task[tuple[bool, str | None]] | None = None
        self._resync_attempts = 0
        self._resync_failures = 0  # consecutive
        self._resync_next_at: float | None = None  # loop time; None: request at the next poll
        self._resync_last_error: str | None = None
        #: (snapshot, published_at_ms): one tuple, replaced in a single assignment (H2)
        self._published: tuple[dict[str, Any], int | None] = ({}, None)
        self.opened_at_ms: int | None = None
        self._released_at_open = 0  # LiveClock releases counted when this run opened (H1)
        self.previous_run: dict[str, Any] | None = None  # kept across a stop (H3)
        self.audit_warnings: list[str] = []
        self.reference: ReferenceLiveStrategy | None = None
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
        resync_clock: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        """Open or restore the paper session and start consuming. Never raises: any failure
        leaves the state `UNAVAILABLE` with the reason, and the subscription closed.

        `ready` (the market-data service's `wait_backfill`) is awaited in the background task,
        bounded by `backfill_wait_s`, before the session is opened and the risk tracker seeded, so
        neither reads a bar store that the startup backfill is still filling.

        `resync_clock` (the market-data service's `resync_clock`) takes the fresh server-clock
        round the observation anchor needs (D-21); without it this run never observes."""
        if not self.enabled:
            subscription.close()
            return
        if self._task is not None and not self._task.done():
            subscription.close()
            self._problem("Paper start refused", "paper is already running")
            return
        self.service = None
        self.observing_since_ms = None
        self.observation = None
        self._observation_code = None
        self.opened_at_ms = None
        self.backfill = None
        self.dropped_updates = 0
        self._drained_updates = 0
        self._largest_drain = 0
        self._drain_deferrals = 0
        self._snapshot = snapshot
        self._market_running = market_running
        self._resync = resync_clock
        self._reset_resync()
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
        self.session_end_evidence = None
        try:
            config = self._config_factory()
            session_id = self._resolve_session()
            tracker = self._seeded_tracker(config)
            strategy = self._build_strategy(config)
            service = PaperTradingService.open(
                config,
                self._store,
                session_id=session_id,
                tracker=tracker,
                settle_ms=self.settings.settle_ms,
                strategy=strategy,
                observation_gated=True,
            )
        except Exception as exc:
            log.exception("paper session could not be opened")
            self._fail_open(f"paper session could not be opened: {type(exc).__name__}: {exc}")
            return False
        self.service = service
        self.session_id = session_id
        self.opened_at_ms = self._clock.now_ms()
        self._released_at_open = service.clock.stats.released
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
        await self._cancel_resync()
        if self._sub is not None:
            self._sub.close()
            self._sub = None
        if self.state in (PaperState.DISABLED, PaperState.IDLE):  # nothing ran since the last stop
            self._publish()
            return
        service = self.service
        self._capture_session_end(service)
        self.previous_run = {
            "state": str(self.state),
            "reason": self.reason,
            "awaiting_missing": dict(service.awaiting_missing) if service is not None else {},
            "stopped_at_ms": self._clock.now_ms(),
        }
        if self.state in (PaperState.RUNNING, PaperState.STARTING):
            self._set(PaperState.IDLE, "monitoring stopped")
        elif self.state is PaperState.AWAITING_BACKFILL:
            self._set(
                PaperState.IDLE,
                f"monitoring stopped; the previous run was AWAITING_BACKFILL: {self.reason}",
            )
        else:  # a failure state stays as it is: a stop never erases it
            self._publish()

    def _capture_session_end(self, service: PaperTradingService | None) -> None:
        """Write the live ledger hashes for the replay gate (`session_evidence`); never raises."""
        if service is None or self.session_id is None:
            return
        try:
            path = capture_session_end(
                self._data_dir,
                config=self._config_factory(),
                session_id=self.session_id,
                ledger_hashes=service.ledger_hashes(),
                fingerprint=service.session.fingerprint(),
                batches=service.journal.batches(),
                state=str(self.state),
                written_at_ms=self._clock.now_ms(),
            )
            self.session_end_evidence = str(path)
        except Exception as exc:
            self.session_end_evidence = f"not written: {type(exc).__name__}: {exc}"
            self._problem("Session-end evidence not written", self.session_end_evidence)
            log.exception("session-end evidence could not be written")

    # ------------------------------------------------------------------ consumer
    async def _run(self) -> None:
        """Consume until stopped or halted. Each iteration awaits one update (or `poll_s`), then
        offers every update already queued (`_drain`, at most `drain_max`) before it snapshots,
        anchors and polls once, so a poll decides on every bar that had arrived by then (M3-PB.3:
        one update per poll released a backlog minute past its deadline with only its first bar
        and made the rest of it late). When the bound is reached older updates may still be
        queued: that iteration neither snapshots, anchors nor polls (no minute is released
        incomplete because of the bound); it records a deferral, yields and continues. The stop,
        fault, overflow and silence checks run every iteration."""
        service, sub = self.service, self._sub
        if service is None or sub is None:  # start() sets both before creating the task
            return
        loop = asyncio.get_running_loop()
        last_update = loop.time()  # monotonic: independent of the (possibly manual) wall clock
        drain_max = self.settings.drain_max
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
                # Deterministic: MarketDataService.stop() clears `running` before it closes the
                # subscriptions, so a None from a closed subscription always comes with
                # market_running() == False. A close by anyone else is caught by the watchdog.
                if update is None and not self._market_running():
                    self._halt(
                        PaperState.SUBSCRIPTION_LOST,
                        "market data stopped while paper was consuming (subscription closed)",
                    )
                    return
                if update is not None:
                    last_update = loop.time()
                    service.on_update(update)
                # Updates already queued (also after a timeout). Stops at the close sentinel,
                # which stays queued: the next get() returns None and is decided above. A stream
                # failure raised here propagates (CONSUMER_DIED), as from get().
                drained = self._drain(service, sub, drain_max)
                if drained:
                    last_update = loop.time()
                if self._overflowed(sub):
                    return
                if (
                    update is None
                    and not drained
                    and loop.time() - last_update > self.settings.silence_s
                ):
                    self._halt(
                        PaperState.SUBSCRIPTION_LOST,
                        f"no market-data update for more than {self.settings.silence_s:g} s",
                    )
                    return
                if drained >= drain_max:
                    self._drain_deferrals += 1
                    log.info(
                        "paper poll deferred: %d queued update(s) drained (drain_max %d); older "
                        "updates may still be queued",
                        drained,
                        drain_max,
                        extra={"drained": drained, "deferrals": self._drain_deferrals},
                    )
                    await asyncio.sleep(0)
                    continue
                snap = self._snapshot()
                if snap is not None:
                    service.on_snapshot(snap)
                now = self._clock.now_ms()
                if self.observing_since_ms is None:
                    self.observing_since_ms = now
                if service.observing_from_ms is None:
                    self._anchor_step(service, snap, now)
                service.poll(now)
                if service.state is ServiceState.AWAITING_BACKFILL and self.backfill is None:
                    self._attempt_backfill(service, now)
                self._sync_state(service)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("paper consumer died")
            self._halt(PaperState.CONSUMER_DIED, f"{type(exc).__name__}: {exc}")

    def _drain(self, service: PaperTradingService, sub: SubscriptionLike, limit: int) -> int:
        """Offer up to `limit` already queued updates, in queue order, without awaiting."""
        n = 0
        while n < limit:
            queued = sub.get_nowait()
            if queued is None:  # empty, or the close sentinel is next (left for get())
                break
            service.on_update(queued)
            n += 1
        self._drained_updates += n
        self._largest_drain = max(self._largest_drain, n)
        return n

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

    # ------------------------------------------------------------------ fresh round (D-21)
    def _reset_resync(self) -> None:
        self._resync_task = None
        self._resync_attempts = 0
        self._resync_failures = 0
        self._resync_next_at = None
        self._resync_last_error = None

    async def _cancel_resync(self) -> None:
        task, self._resync_task = self._resync_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _request_round(self) -> tuple[bool, str | None]:
        """One fresh server-clock round through the hook: (True, None) or (False, why). Never
        raises except on cancellation."""
        resync = self._resync
        if resync is None:
            return False, "no resync hook"
        try:
            ok = await asyncio.wait_for(resync(), timeout=self.settings.resync_timeout_s)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return False, f"no server-clock round within {self.settings.resync_timeout_s:g} s"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if ok is not True:
            return False, "the server-clock round produced no sample"
        return True, None

    def _resync_state(self) -> dict[str, Any]:
        retry_in = None
        if self._resync_task is None and self._resync_next_at is not None:
            retry_in = max(0.0, round(self._resync_next_at - asyncio.get_running_loop().time(), 1))
        return {
            "available": self._resync is not None,
            "pending": self._resync_task is not None,
            "attempts": self._resync_attempts,
            "consecutive_failures": self._resync_failures,
            "last_error": self._resync_last_error,
            "retry_in_s": retry_in,
        }

    def _anchor_step(
        self, service: PaperTradingService, snap: MarketSnapshot | None, now: int
    ) -> None:
        """One poll of the anchor gate (NB-1 option B, D-21), while this run has no anchor.

        No hook: blocked (`clock_resync_unavailable`). Otherwise the gate requests one fresh
        server-clock round in the background (the consumer keeps polling; blocked as
        `clock_resync_pending`, no problem raised for that transient state). When it finished:
        on failure blocked (`clock_resync_failed`); on success the anchor is evaluated once, from
        a snapshot taken now, so after that round, by `_establish_observation`, which also
        requires the round to be at most `anchor_round_max_age_ms` old on both clocks. Any outcome
        without an anchor waits `resync_retry_s`, doubling per consecutive failure up to
        `resync_retry_max_s`, before the next request; the last blocked reason stays published.
        Bars meanwhile are processed unobserved (fail closed)."""
        if self._resync is None:
            self._record_observation(
                service,
                ObservationVerdict(
                    None,
                    "clock_resync_unavailable",
                    "no fresh server-clock round can be requested (market data has no resync)",
                ),
                now,
            )
            return
        loop = asyncio.get_running_loop()
        task = self._resync_task
        if task is None:
            if self._resync_next_at is not None and loop.time() < self._resync_next_at:
                if self.observation is not None:  # keep the reason, refresh the retry state
                    self.observation = {**self.observation, "resync": self._resync_state()}
                return
            self._resync_attempts += 1
            self._resync_task = asyncio.create_task(
                self._request_round(), name="paper-clock-resync"
            )
            self._record_pending(now)
            return
        if not task.done():
            self._record_pending(now)
            return
        self._resync_task = None
        ok: bool
        error: str | None
        if task.cancelled():
            ok, error = False, "the server-clock round was cancelled"
        else:
            ok, error = task.result()
        if ok:
            self._establish_observation(service, self._snapshot(), self._clock.now_ms())
            if service.observing_from_ms is not None:
                self._resync_failures = 0
                self._resync_last_error = None
                self.observation = {**(self.observation or {}), "resync": self._resync_state()}
                return
            error = (self.observation or {}).get("problem") or "no anchor"
        self._resync_failures += 1
        self._resync_last_error = error
        exponent = min(self._resync_failures - 1, 16)
        delay = min(self.settings.resync_retry_s * 2**exponent, self.settings.resync_retry_max_s)
        self._resync_next_at = loop.time() + delay
        if not ok:
            self._record_observation(
                service,
                ObservationVerdict(
                    None, "clock_resync_failed", f"fresh server-clock round failed: {error}"
                ),
                now,
            )
        else:  # blocked by the verdict, already recorded: refresh the retry state
            self.observation = {**(self.observation or {}), "resync": self._resync_state()}

    def _record_pending(self, now: int) -> None:
        """Published, but no problem is raised and the last blocked code is kept (transient)."""
        self.observation = {
            "state": "blocked",
            "attempted_at_ms": now,
            "observing_from_ms": None,
            "problem": "waiting for a fresh server-clock round before fixing the anchor",
            "code": "clock_resync_pending",
            "resync": self._resync_state(),
        }

    # ------------------------------------------------------------------ observation
    def _establish_observation(
        self,
        service: PaperTradingService,
        snap: MarketSnapshot | None,
        now: int,
        *,
        bound_only: bool = False,
    ) -> None:
        """Fix the observation anchor at this poll (P-1, L-02), or fail closed until a later one.

        The anchor is the first minute open after the exchange-time bound of the snapshot instant
        (`observation_bound`: the clock round's offset, largest round trip, drift since the round
        on the monotonic clock, and the wall clock's movement against it during the round). With no
        trustworthy evidence (no snapshot; an offset, round trip or monotonic evidence that is not
        LIVE or comes from another round; or a verdict without an anchor) no anchor is set: the
        service processes every bar unobserved (resting exits resolve; no entry fills or is
        created), the reason is published and raised as a problem when it changes, and a later
        poll tries again.

        D-21: a round older than `anchor_round_max_age_ms` (on either clock) at the snapshot gives
        no anchor (`clock_round_not_fresh`); the runtime calls this only right after a fresh round
        it requested (`_anchor_step`). `bound_only` skips that freshness rule, for tests of the
        bound alone."""
        verdict = self._observation_verdict(service, snap)
        if verdict.anchor_ms is not None and not bound_only:
            age = verdict.details.get("round_age_ms")
            limit = self.settings.anchor_round_max_age_ms
            if age is None or age > limit:
                verdict = ObservationVerdict(
                    None,
                    "clock_round_not_fresh",
                    f"server clock round {age} ms old at the poll; the anchor needs a fresh "
                    f"round at most {limit} ms old",
                    details=dict(verdict.details),
                )
        self._record_observation(service, verdict, now)

    def _record_observation(
        self, service: PaperTradingService, verdict: ObservationVerdict, now: int
    ) -> None:
        info: dict[str, Any] = {
            "state": "active" if verdict.anchor_ms is not None else "blocked",
            "attempted_at_ms": now,
            "observing_from_ms": verdict.anchor_ms,
            "problem": verdict.problem,
            "code": verdict.code,
            **verdict.details,
            "resync": self._resync_state(),
        }
        self.observation = info
        if verdict.anchor_ms is None:
            if verdict.code != self._observation_code:
                self._problem("Paper observation blocked", verdict.problem or "unknown")
            self._observation_code = verdict.code
            return
        self._observation_code = None
        service.observe_from(verdict.anchor_ms)

    def _observation_verdict(
        self, service: PaperTradingService, snap: MarketSnapshot | None
    ) -> ObservationVerdict:
        if snap is None:
            return ObservationVerdict(None, "no_snapshot", "no market-data snapshot")
        off, rtt, cont = snap.clock_offset_ms, snap.clock_max_rtt_ms, snap.clock_continuity
        if off.provenance is not Provenance.LIVE or off.value is None or off.as_of_ms is None:
            return ObservationVerdict(None, "clock_offset_unknown", "server clock offset unknown")
        if rtt.provenance is not Provenance.LIVE or rtt.value is None:
            return ObservationVerdict(None, "clock_rtt_unknown", "server clock round trip unknown")
        if rtt.as_of_ms != off.as_of_ms:
            return ObservationVerdict(
                None,
                "clock_rtt_other_round",
                "server clock round trip comes from another round than the offset",
            )
        if cont.provenance is not Provenance.LIVE or cont.value is None:
            return ObservationVerdict(
                None,
                "clock_continuity_unknown",
                f"no monotonic clock evidence ({cont.reason or cont.provenance})",
            )
        if cont.as_of_ms != off.as_of_ms:
            return ObservationVerdict(
                None,
                "clock_continuity_other_round",
                "monotonic clock evidence comes from another round than the offset",
            )
        c = cont.value
        evidence = ClockEvidence(
            round_at_ms=off.as_of_ms,
            offset_ms=off.value,
            max_rtt_ms=rtt.value,
            min_rtt_ms=c.min_rtt_ms,
            samples=c.samples,
            round_span_ms=c.round_span_ms,
            round_step_ms=c.round_step_ms,
            elapsed_ms=c.elapsed_ms,
            wall_elapsed_ms=c.wall_elapsed_ms,
        )
        risk = service.config.risk
        return observation_bound(
            evidence,
            max_age_ms=risk.clock_offset_max_age_ms,
            skew_max_ms=risk.clock_skew_max_ms,
        )

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

    def _build_strategy(self, config: PaperConfig) -> LiveStrategy | None:
        """The configured live strategy (tests may inject one through `strategy_factory`).

        `bracket_reference` is built at the first observed bar (`DeferredReference`, P-1)."""
        self.reference = None
        if self._strategy_factory is not None:
            return self._strategy_factory()
        if self.settings.strategy == "none":
            return None
        return DeferredReference(lambda start: self._reference(config, start))

    def _reference(self, config: PaperConfig, start: int) -> ReferenceLiveStrategy:
        """`bracket_reference` trading from the minute `start` on (`trade_from_ms`), its history
        warmed up causally from closed bars of the live bar store that closed before that minute
        (the market-data startup backfill completed before the session opened)."""
        adapter = ReferenceLiveStrategy.create(
            config.cost_model, [s.key for s in config.instruments], trade_from_ms=start
        )
        if self._bars is not None:
            span = (adapter.strategy.window_bars + 2) * adapter.strategy.interval.ms
            begin = start - start % adapter.strategy.interval.ms - span
            # one instrument at a time (histories are per instrument): peak memory is one
            # instrument's ~11k bars, not the whole universe's
            for spec in config.instruments:
                adapter.warm_up(self._bars(spec.market, spec.symbol, begin, start))
        self.reference = adapter
        return adapter

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
            state = self._log.inspect(self.settings.session_id)  # read only: nothing written yet
        except OSError as exc:
            raise PaperActionRefused(423, f"paper session audit log: {exc}") from exc
        if state.active is None:
            raise PaperActionRefused(423, f"paper session audit log: {state.reason}")
        for rec in state.request_records:
            if rec.get("request_id") == request_id:
                if rec.get("note") != note:
                    raise PaperActionRefused(409, "request_id was already used with another note")
                return {**rec, "created": False}
        consuming = self._task is not None and not self._task.done()
        if self.state in ACTIVE_STATES or consuming:
            raise PaperActionRefused(
                409,
                f"paper is {self.state}; stop monitoring first: a new session can only replace "
                "a stopped or failed one",
            )
        try:  # ownership proof (and a proven repair) only for a request that will be recorded
            self._resolve_session()
            state = self._log.inspect(self.settings.session_id)
        except (AuditLogAmbiguous, OSError) as exc:
            raise PaperActionRefused(423, f"paper session audit log: {exc}") from exc
        now = self._clock.now_ms()
        previous = str(state.active)
        stamp = to_iso(now).replace("-", "").replace(":", "").replace(".", "")[:15] + "Z"
        known = {str(t["active"]) for t in state.transitions} | {self.settings.session_id}
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
            "store_seq": self._store.head[0],  # journal position of the switch (ownership proof)
            "state_at_request": str(self.state),
            "reason_at_request": self.reason,
            "note": note,
        }
        try:
            self._log.append(record)
        except (AuditLogAmbiguous, OSError) as exc:
            raise PaperActionRefused(423, f"paper session audit log: {exc}") from exc
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

    def _resolve_session(self) -> str:
        """The active paper session, proven unambiguous; raises `AuditLogAmbiguous` otherwise.

        Proof: the audit log parses (or its only defect is a complete final record without its
        newline), and journal ownership agrees with it: the newest paper step in the store
        belongs to a session the log knows, and no superseded session has a paper step after the
        record that superseded it. Only then is a repairable final record terminated (append-only,
        with a `repair` record)."""
        state = self._log.inspect(self.settings.session_id)
        if state.active is None:
            raise AuditLogAmbiguous(state.reason or "audit log is ambiguous")
        problem = self._ownership_problem(state)
        if problem is not None:
            raise AuditLogAmbiguous(f"journal ownership ambiguous: {problem}")
        warnings = list(state.warnings)
        if state.repairable_line is not None:
            self._log.repair_unterminated(state.repairable_line, self._clock.now_ms())
            warnings.append(f"line {state.repairable_line}: missing newline repaired")
        self.audit_warnings = warnings
        return state.active

    def _ownership_problem(self, state: LogState) -> str | None:
        known = {self.settings.session_id} | {str(t["active"]) for t in state.transitions}
        newest = self._store.read(types=[EventType.PAPER_STEP], descending=True, limit=1)
        if newest and newest[0].aggregate_id not in known:
            return f"the journal has paper session {newest[0].aggregate_id!r} unknown to the log"
        # When was each session last switched away from? A session that is active again later
        # (or kept active by a quarantine) is only constrained up to that last switch-away.
        # The switch's store sequence (recorded since MVP-2.1) is exact: no step of the superseded
        # session may follow it in the journal. Older records only have wall time, compared with
        # the step's bar close (a weaker check, kept for MVP-2 logs).
        superseded_at: dict[str, tuple[int, int | None, int]] = {}
        for t in state.transitions:
            switched_at = t.get("ts_ms")
            if not isinstance(switched_at, int):
                return f"line {t['line']}: session record without an integer ts_ms"
            seq = t.get("store_seq")
            previous, active = str(t["previous"]), str(t["active"])
            if previous != active:
                superseded_at[previous] = (
                    switched_at,
                    seq if isinstance(seq, int) else None,
                    int(t["line"]),
                )
            superseded_at.pop(active, None)  # active again: its earlier end no longer binds
        for session, (switched_at, seq, line) in sorted(superseded_at.items()):
            last = self._last_step(session)
            if last is None:
                continue
            after = last.seq > seq if seq is not None else last.ts_ms > switched_at
            if after:
                return f"session {session!r} has paper steps after it was superseded (line {line})"
        return None

    def _last_step(self, session_id: str) -> Event | None:
        rows = self._store.read(
            types=[EventType.PAPER_STEP],
            aggregate=(AggregateType.SIM, session_id),
            descending=True,
            limit=1,
        )
        return rows[0] if rows else None

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
            log_state = self._log.inspect(self.settings.session_id)
            next_session = log_state.active
            audit = {
                "ok": log_state.active is not None,
                "reason": log_state.reason,
                "warnings": self.audit_warnings or log_state.warnings,
                "repairable_line": log_state.repairable_line,
            }
        except OSError as exc:  # never crash the app on an unreadable log (H4)
            next_session = None
            audit = {
                "ok": False,
                "reason": f"unreadable: {type(exc).__name__}: {exc}",
                "warnings": [],
                "repairable_line": None,
            }
        snap: dict[str, Any] = {
            "state": str(self.state),
            "reason": self.reason,
            "enabled": self.enabled,
            "settings_source": self.settings.source,
            "strategy": self.settings.strategy,
            "session_id": self.session_id,
            "next_session_id": next_session,
            "observing_since_ms": self.observing_since_ms,
            "observation": self.observation,
            "opened_at_ms": self.opened_at_ms,
            "settle_ms": self.settings.settle_ms,
            "values_as_of_ms": None,
            "live_bar_since_start": False,
            "previous_run": self.previous_run,
            "audit_log": audit,
            "dropped_updates": self.dropped_updates,
            "consumer": {
                "drain_max": self.settings.drain_max,
                "drained_updates": self._drained_updates,
                "largest_drain": self._largest_drain,
                "deferrals": self._drain_deferrals,
            },
            "backfill": self.backfill,
            "limitations": list(LIMITATIONS),
            "reference": self.reference.describe() if self.reference is not None else None,
            "service": None,
            "books": {},
            "fills": [],
            "trades": [],
            "request_rejections": [],
        }
        service = self.service
        if service is not None:
            last = service.session.last_minute_ms
            snap["values_as_of_ms"] = None if last is None else last + MS_PER_MINUTE - 1
            snap["live_bar_since_start"] = service.clock.stats.released > self._released_at_open
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
                    # entries / exits that waited for a candle with trades (ADR-0014 rule 4)
                    "no_trade_deferrals": book.no_trade_deferrals,
                }
                for name, book in service.session.sim.books.items()
            }
            # read-only views of what the simulator already recorded (no new simulation state);
            # `sim.events` returns a copy, so it is taken once and scanned once per publish
            fills: list[dict[str, Any]] = []
            rejected: list[dict[str, Any]] = []
            for e in service.session.sim.events:
                kind = e.get("type")
                if kind == FILL_TYPE:
                    fills.append(e)
                elif kind == REQUEST_REJECTED_TYPE:
                    rejected.append(e)
            snap["fills"] = [
                {"ts_ms": e["ts_ms"], "aggregate_id": e["aggregate_id"], **e["payload"]}
                for e in fills[-MAX_FILLS:]
            ]
            snap["request_rejections"] = [
                {"ts_ms": e["ts_ms"], "aggregate_id": e["aggregate_id"], **e["payload"]}
                for e in rejected[-MAX_REJECTIONS:]
            ]
            trades = [
                t.to_payload()
                for book in service.session.sim.books.values()
                for t in book.trades[-MAX_TRADES:]
            ]
            snap["trades"] = sorted(trades, key=lambda t: (t["exit_bar_open_ms"], t["book"]))
        self._published = (_frozen(snap), now)
        if self.on_change is not None:
            with contextlib.suppress(Exception):
                self.on_change()

    def published(self) -> tuple[dict[str, Any], int | None]:
        """The last published snapshot and when it was taken (safe from any thread)."""
        return self._published

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
    "AuditLogAmbiguous",
    "BarReader",
    "LogState",
    "PaperActionRefused",
    "PaperRuntime",
    "PaperSettings",
    "PaperState",
    "SessionLog",
    "build_paper_config",
    "load_paper_runtime",
    "validate_backfill",
]
