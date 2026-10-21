"""Signal lifecycle (SIGNALS_AND_AGENTS §1): WATCH -> CONFIRMED -> OPEN -> CLOSED, or VETOED /
EXPIRED / INVALIDATED, as append-only events; the current state is a projection.

- `SignalLifecycle.create` emits ``SIGNAL_CREATED`` (the immutable `SignalRecord`);
  `transition` emits ``SIGNAL_TRANSITIONED`` with ``{signal_id, from, to, reason, book, outcome}``
  and refuses every transition the contract does not allow (`contracts.signals.can_transition`).
- Both return `contracts.events.NewEvent`s and apply them to the projection; `apply` replays stored
  events into a fresh projection, and is the only way a status changes.
- The paper book that drives OPEN / CLOSED is ``baseline`` (`PAPER_STATUS_BOOK`). Its simulator
  events map to transitions in `transitions_from_sim` (risk rejections -> VETOED, expiry ->
  EXPIRED, fill -> OPEN, close -> CLOSED with the outcome).
- **Shadow outcome.** Vetoed signals keep a hypothetical outcome: the ``shadow`` book takes every
  confirmed signal without the risk gate, and its closed trade for a signal is attached to the
  projection (`shadow_outcome`). It is derived from simulator events (recomputed when a paper
  session is restored by replaying its journal); it is not a lifecycle transition.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from tsclaude.contracts.events import AggregateType, Event, EventType, NewEvent
from tsclaude.contracts.signals import SignalRecord, SignalStatus, can_transition

PAPER_STATUS_BOOK = "baseline"
SHADOW_BOOK = "shadow"

#: outcome fields copied from a closed simulator trade
OUTCOME_FIELDS = (
    "exit_reason",
    "r_multiple",
    "net_pnl",
    "gross_pnl",
    "fees",
    "funding",
    "mae_r",
    "mfe_r",
    "ambiguous_bar",
    "spans_gap",
    "entry_price",
    "exit_price",
    "entry_bar_open_ms",
    "exit_bar_open_ms",
)


class LifecycleError(ValueError):
    """Unknown signal, duplicate creation or a transition the contract does not allow."""


@dataclass
class SignalState:
    signal_id: str
    record: dict[str, Any]
    status: SignalStatus
    created_ms: int
    updated_ms: int
    reason: str | None = None
    outcome: dict[str, Any] | None = None
    shadow_outcome: dict[str, Any] | None = None
    history: list[tuple[str, str, int, str | None]] = field(default_factory=list)

    @property
    def market(self) -> str:
        return str(self.record["market"])

    @property
    def symbol(self) -> str:
        return str(self.record["symbol"])

    @property
    def valid_until_ms(self) -> int:
        return int(self.record["entry_rule"]["valid_until_ms"])

    @property
    def terminal(self) -> bool:
        return self.status in (
            SignalStatus.VETOED,
            SignalStatus.CLOSED,
            SignalStatus.EXPIRED,
            SignalStatus.INVALIDATED,
        )


def outcome_of(trade_payload: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: trade_payload.get(k) for k in OUTCOME_FIELDS}
    out["resolved_from_backfill"] = bool(trade_payload.get("resolved_from_backfill", False))
    return out


class SignalLifecycle:
    def __init__(self) -> None:
        self._signals: dict[str, SignalState] = {}

    # ------------------------------------------------------------ views
    def get(self, signal_id: str) -> SignalState | None:
        return self._signals.get(signal_id)

    def status(self, signal_id: str) -> SignalStatus | None:
        s = self._signals.get(signal_id)
        return None if s is None else s.status

    def signals(self) -> list[SignalState]:
        return [self._signals[k] for k in sorted(self._signals)]

    def active(self, *statuses: SignalStatus) -> list[SignalState]:
        wanted = set(statuses) or {SignalStatus.WATCH, SignalStatus.CONFIRMED, SignalStatus.OPEN}
        return [s for s in self.signals() if s.status in wanted]

    def shadow_outcome(self, signal_id: str) -> dict[str, Any] | None:
        s = self._signals.get(signal_id)
        return None if s is None else s.shadow_outcome

    # ------------------------------------------------------------ producing events
    def create(self, record: SignalRecord, ts_ms: int) -> NewEvent:
        if record.signal_id in self._signals:
            raise LifecycleError(f"signal {record.signal_id} already exists")
        event = NewEvent(
            type=EventType.SIGNAL_CREATED,
            aggregate_type=AggregateType.SIGNAL,
            aggregate_id=record.signal_id,
            ts_ms=ts_ms,
            payload=record.model_dump(mode="json"),
        )
        self.apply(event)
        return event

    def transition(
        self,
        signal_id: str,
        target: SignalStatus,
        ts_ms: int,
        reason: str | None = None,
        *,
        book: str | None = None,
        outcome: Mapping[str, Any] | None = None,
    ) -> NewEvent:
        state = self._signals.get(signal_id)
        if state is None:
            raise LifecycleError(f"unknown signal {signal_id}")
        if not can_transition(state.status, target):
            raise LifecycleError(f"{signal_id}: {state.status} -> {target} is not allowed")
        event = NewEvent(
            type=EventType.SIGNAL_TRANSITIONED,
            aggregate_type=AggregateType.SIGNAL,
            aggregate_id=signal_id,
            ts_ms=ts_ms,
            payload={
                "signal_id": signal_id,
                "from": str(state.status),
                "to": str(target),
                "reason": reason,
                "book": book,
                "outcome": None if outcome is None else dict(outcome),
            },
        )
        self.apply(event)
        return event

    def try_transition(
        self,
        signal_id: str,
        target: SignalStatus,
        ts_ms: int,
        reason: str | None = None,
        **kw: Any,
    ) -> NewEvent | None:
        """`transition` if the signal exists and the move is allowed, else None (no event)."""
        state = self._signals.get(signal_id)
        if state is None or not can_transition(state.status, target):
            return None
        return self.transition(signal_id, target, ts_ms, reason, **kw)

    def expire_due(
        self, ts_ms: int, *, market: str, symbol: str, downtime: bool = False
    ) -> list[NewEvent]:
        """WATCH signals of (market, symbol) whose validity window has elapsed by `ts_ms`.

        CONFIRMED signals expire through their paper order (`transitions_from_sim`), so the
        lifecycle and the book can never disagree about whether an entry was still possible.
        """
        out: list[NewEvent] = []
        for s in self.signals():
            if s.status is not SignalStatus.WATCH or (s.market, s.symbol) != (market, symbol):
                continue
            if ts_ms >= s.valid_until_ms:
                reason = "downtime" if downtime else "validity_elapsed"
                out.append(self.transition(s.signal_id, SignalStatus.EXPIRED, ts_ms, reason))
        return out

    # ------------------------------------------------------------ projection
    def apply(self, event: NewEvent | Event) -> None:
        if event.type is EventType.SIGNAL_CREATED:
            p = event.payload
            sid = str(p["signal_id"])
            if sid in self._signals:
                raise LifecycleError(f"signal {sid} already exists")
            stage = SignalStatus(p["stage"])
            self._signals[sid] = SignalState(
                signal_id=sid,
                record=dict(p),
                status=stage,
                created_ms=event.ts_ms,
                updated_ms=event.ts_ms,
                history=[("", str(stage), event.ts_ms, "created")],
            )
        elif event.type is EventType.SIGNAL_TRANSITIONED:
            p = event.payload
            sid = str(p["signal_id"])
            state = self._signals.get(sid)
            if state is None:
                raise LifecycleError(f"transition for unknown signal {sid}")
            source, target = SignalStatus(p["from"]), SignalStatus(p["to"])
            if source is not state.status or not can_transition(source, target):
                raise LifecycleError(f"{sid}: stored transition {source} -> {target} is invalid")
            state.status = target
            state.updated_ms = event.ts_ms
            state.reason = p.get("reason")
            if p.get("outcome") is not None:
                state.outcome = dict(p["outcome"])
            state.history.append((str(source), str(target), event.ts_ms, state.reason))

    @classmethod
    def rebuild(cls, events: Iterable[NewEvent | Event]) -> SignalLifecycle:
        lc = cls()
        for e in events:
            if e.aggregate_type is AggregateType.SIGNAL:
                lc.apply(e)
        return lc

    # ------------------------------------------------------------ from simulator events
    def transitions_from_sim(self, sim_events: Iterable[Mapping[str, Any]]) -> list[NewEvent]:
        """Lifecycle transitions implied by one step's simulator events (deterministic order)."""
        out: list[NewEvent] = []
        for e in sim_events:
            kind, ts, p = e["type"], int(e["ts_ms"]), e["payload"]
            book = p.get("book")
            if book == SHADOW_BOOK and kind == "sim_position_closed":
                state = self._signals.get(str(p.get("request_id")))
                if state is not None and state.shadow_outcome is None:
                    state.shadow_outcome = outcome_of(p)
                continue
            if book != PAPER_STATUS_BOOK:
                continue
            sid = _signal_of(kind, p)
            if sid is None or sid not in self._signals:
                continue
            event: NewEvent | None = None
            if kind in ("sim_request_rejected", "sim_order_rejected"):
                event = self.try_transition(
                    sid, SignalStatus.VETOED, ts, str(p.get("reason")), book=book
                )
            elif kind == "sim_order_expired":
                why = "downtime" if str(p.get("reason", "")).endswith(":downtime") else "not_filled"
                event = self.try_transition(sid, SignalStatus.EXPIRED, ts, why, book=book)
            elif kind == "sim_order_cancelled":
                reason = str(p.get("reason"))
                if reason.startswith("book_halted"):
                    event = self.try_transition(sid, SignalStatus.VETOED, ts, reason, book=book)
                elif reason == "end_of_data":
                    event = self.try_transition(sid, SignalStatus.EXPIRED, ts, reason, book=book)
                # exit_signal: already INVALIDATED by the intent that requested it
            elif kind == "sim_order_filled" and p.get("role") == "entry":
                event = self.try_transition(sid, SignalStatus.OPEN, ts, "entry_filled", book=book)
            elif kind == "sim_position_closed":
                event = self.try_transition(
                    sid,
                    SignalStatus.CLOSED,
                    ts,
                    str(p.get("exit_reason")),
                    book=book,
                    outcome=outcome_of(p),
                )
            if event is not None:
                out.append(event)
        return out


def _entry_signal(order_id: object, book: str) -> str | None:
    """Signal id of an entry order id ``<book>:<request_id>:entry`` (None for other orders)."""
    text = str(order_id)
    prefix, suffix = f"{book}:", ":entry"
    if text.startswith(prefix) and text.endswith(suffix) and len(text) > len(prefix + suffix):
        return text[len(prefix) : -len(suffix)]
    return None


def _signal_of(kind: str, p: Mapping[str, Any]) -> str | None:
    if kind in ("sim_request_rejected", "sim_position_closed"):
        rid = p.get("request_id")
        return None if rid is None else str(rid)
    if kind in (
        "sim_order_rejected",
        "sim_order_expired",
        "sim_order_cancelled",
        "sim_order_filled",
    ):
        return _entry_signal(p.get("order_id"), str(p.get("book")))
    return None
