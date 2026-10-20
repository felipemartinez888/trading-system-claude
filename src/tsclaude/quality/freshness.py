"""Per-stream health state machine (DATA_ARCHITECTURE §3).

States (`contracts.market.StreamState`):

    CONNECTING --first message--> HEALTHY --budget exceeded--> STALE --fresh message--> HEALTHY
         any state --connection lost--> DISCONNECTED --connection open--> RECOVERING
    RECOVERING --message received--> HEALTHY

  Every transition *into* HEALTHY (from CONNECTING, STALE or RECOVERING) also requires the repair
  check to pass. If it does not yet, the qualifying message is remembered and `tick()` completes
  the transition once repair is done (for STALE, only while that fresh message is still within
  the budget).

- "Fresh" depends on the stream: for 1m klines it is a *closed* bar (budget counted since the last
  closed bar, or since the stream became healthy if none has closed yet); for bookTicker and
  markPrice it is any message.
- Repair is decided by an injected predicate (for klines: the bar builder is caught up and the
  symbol has no pending gap). Streams without history (book, mark) are repaired on first message.
- Every transition emits one `STREAM_STATE_CHANGED` event. Anything other than HEALTHY must block
  new entries (fail closed); that decision belongs to the risk engine, which reads `state()`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from tsclaude.contracts.events import AggregateType, EventType, NewEvent
from tsclaude.contracts.market import StreamState
from tsclaude.marketdata.types import EventSink, StreamKey, StreamKind

log = logging.getLogger(__name__)

type RepairCheck = Callable[[StreamKey], bool]
type BudgetFor = Callable[[StreamKind], int]


@dataclass
class StreamHealth:
    key: StreamKey
    state: StreamState
    since_ms: int  # time of the last transition
    budget_ms: int
    last_message_ms: int | None = None
    last_fresh_ms: int | None = None
    healthy_since_ms: int | None = None
    opened_at_ms: int | None = None
    message_since_open: bool = False
    pending_healthy: bool = False  # qualifying message seen, waiting for the repair check
    transitions: int = 0
    disconnects: int = 0


class FreshnessMonitor:
    def __init__(
        self,
        streams: Iterable[StreamKey],
        budget_ms: BudgetFor,
        emit: EventSink,
        now_ms: Callable[[], int],
        repaired: RepairCheck | None = None,
    ) -> None:
        self._emit = emit
        self._now = now_ms
        self._repaired: RepairCheck = repaired if repaired is not None else (lambda _k: True)
        now = now_ms()
        self._streams: dict[StreamKey, StreamHealth] = {
            k: StreamHealth(
                key=k, state=StreamState.CONNECTING, since_ms=now, budget_ms=budget_ms(k.kind)
            )
            for k in streams
        }

    # ------------------------------------------------------------------ queries
    def state(self, key: StreamKey) -> StreamState:
        return self._streams[key].state

    def health(self, key: StreamKey) -> StreamHealth:
        return self._streams[key]

    def all(self) -> list[StreamHealth]:
        return list(self._streams.values())

    def keys(self) -> list[StreamKey]:
        return list(self._streams)

    def __contains__(self, key: object) -> bool:
        return key in self._streams

    # ------------------------------------------------------------------ inputs
    def on_open(self, key: StreamKey) -> None:
        """The connection carrying `key` is open (subscribed). No data proven yet."""
        h = self._streams[key]
        h.opened_at_ms = self._now()
        h.message_since_open = False
        if h.state is StreamState.DISCONNECTED:
            self._transition(h, StreamState.RECOVERING, "reconnected")

    def on_close(self, key: StreamKey, reason: str) -> None:
        h = self._streams[key]
        h.opened_at_ms = None
        h.message_since_open = False
        if h.state is not StreamState.DISCONNECTED:
            h.disconnects += 1
            self._transition(h, StreamState.DISCONNECTED, reason)

    def on_message(self, key: StreamKey, *, fresh: bool = True) -> None:
        h = self._streams[key]
        now = self._now()
        h.last_message_ms = now
        h.message_since_open = True
        if fresh:
            h.last_fresh_ms = now
        if h.state is StreamState.CONNECTING:
            self._try_healthy(h, "first_message")
        elif h.state is StreamState.STALE and fresh:
            self._try_healthy(h, "fresh_message")
        elif h.state is StreamState.RECOVERING:
            self._try_healthy(h, "recovered")

    def tick(self) -> None:
        """Evaluate time-based transitions. Call about once per second."""
        now = self._now()
        for h in self._streams.values():
            if h.state is StreamState.HEALTHY:
                ref = self._reference_ms(h)
                if now - ref > h.budget_ms:
                    self._transition(h, StreamState.STALE, "freshness_budget_exceeded")
            elif not h.pending_healthy:
                continue
            elif h.state is StreamState.STALE:
                if h.last_fresh_ms is None or now - h.last_fresh_ms > h.budget_ms:
                    h.pending_healthy = False  # that fresh message is itself stale now
                else:
                    self._try_healthy(h, "fresh_message")
            elif h.state is StreamState.CONNECTING:
                self._try_healthy(h, "first_message")
            elif h.state is StreamState.RECOVERING:
                self._try_healthy(h, "recovered")

    # ------------------------------------------------------------------ internals
    def _reference_ms(self, h: StreamHealth) -> int:
        candidates = [v for v in (h.last_fresh_ms, h.healthy_since_ms) if v is not None]
        return max(candidates) if candidates else h.since_ms

    def _try_healthy(self, h: StreamHealth, reason: str) -> None:
        if self._repaired(h.key):
            self._transition(h, StreamState.HEALTHY, reason)
        else:
            h.pending_healthy = True

    def _transition(self, h: StreamHealth, to: StreamState, reason: str) -> None:
        now = self._now()
        before = h.state
        h.pending_healthy = False
        h.state = to
        h.since_ms = now
        h.transitions += 1
        if to is StreamState.HEALTHY:
            h.healthy_since_ms = now
        log.info(
            "stream state changed",
            extra={"stream": h.key.id, "from": str(before), "to": str(to), "reason": reason},
        )
        self._emit(
            NewEvent(
                type=EventType.STREAM_STATE_CHANGED,
                aggregate_type=AggregateType.STREAM,
                aggregate_id=h.key.id,
                ts_ms=now,
                payload={
                    "stream": h.key.id,
                    "market": str(h.key.market),
                    "symbol": h.key.symbol,
                    "kind": str(h.key.kind),
                    "from": str(before),
                    "to": str(to),
                    "reason": reason,
                    "budget_ms": h.budget_ms,
                    "last_message_ms": h.last_message_ms,
                    "last_fresh_ms": h.last_fresh_ms,
                },
            )
        )
