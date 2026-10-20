"""Market-data push on `/ws/live`: at most one `delta` message per interval (default 1 s).

Every tick (on the event loop):
1. refresh the supervisor's cached stream summary; if it changed, publish a `status` message
   (the status bar's Spot/Futures indicators);
2. diff the market rows against what was last sent and collect the changed rows;
3. collect new notification events since the last tick (from the event store) and connection
   crashes (counter increases, which are not events) as transient notifications;
4. publish **one** `delta` message if anything changed: `{"markets": [...], "notifications": [...],
   "health_changed": bool}`.

New WebSocket clients first receive the full status; the dashboard fetches `/api/markets` itself and
then merges deltas, so a client that connects between two ticks misses nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from tsclaude.api.context import AppContext
from tsclaude.api.market_schemas import NotificationOut
from tsclaude.api.market_views import market_rows, notifications_after
from tsclaude.contracts.provenance import Provenance
from tsclaude.contracts.timeutil import to_iso

log = logging.getLogger(__name__)

PUSH_INTERVAL_S = 1.0


class MarketPusher:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context
        self._sent_rows: dict[str, str] = {}
        self._sent_streams = ""
        self._summary = ""
        self._last_seq, _ = context.store.head
        self._crashes: dict[str, int] = {}
        self.messages_sent = 0

    def tick(self) -> dict[str, Any] | None:
        """Compute one delta (or None). Must run on the event loop."""
        ctx = self._ctx
        sup = ctx.supervisor
        if sup is None:
            return None
        summary = json.dumps(
            {k: v.model_dump(mode="json") for k, v in sup.refresh().items()}, sort_keys=True
        )
        if summary != self._summary:
            self._summary = summary
            ctx.publish_status()
        snapshot = sup.snapshot()
        changed_rows: list[dict[str, Any]] = []
        health_changed = False
        transient: list[NotificationOut] = []
        if snapshot is not None:
            for row in market_rows(snapshot, ctx.bar_store()):
                data = row.model_dump(mode="json")
                key = f"{row.market}:{row.symbol}"
                text = json.dumps(data, sort_keys=True)
                if self._sent_rows.get(key) != text:
                    self._sent_rows[key] = text
                    changed_rows.append(data)
            streams = json.dumps(
                [
                    (s.stream, str(s.state.value), str(s.state.provenance), s.disconnects)
                    for s in snapshot.streams
                ]
                + [
                    (c.group, c.connected, c.reconnects, c.recycles, c.crashes, c.malformed)
                    for c in snapshot.connections
                ],
                default=str,
            )
            if streams != self._sent_streams:
                self._sent_streams = streams
                health_changed = True
            for c in snapshot.connections:
                before = self._crashes.get(c.group, 0)
                if c.crashes > before:
                    note = NotificationOut(
                        id=f"crash-{c.group}-{c.crashes}",
                        seq=0,
                        ts=to_iso(snapshot.as_of_ms),
                        kind="connection_crashed",
                        severity="error",
                        title=f"WebSocket connection task crashed: {c.group}",
                        detail=c.last_error or "restarted by the supervisor",
                        market=None,
                        symbol=None,
                        provenance=Provenance.SYSTEM,
                    )
                    transient.append(note)
                    ctx.transient_notifications.append(note)
                self._crashes[c.group] = c.crashes
        new = notifications_after(ctx.store, self._last_seq)
        head, _ = ctx.store.head
        if new:
            self._last_seq = max(n.seq for n in new)
        else:
            self._last_seq = head
        notes = [n.model_dump(mode="json") for n in [*new, *transient]]
        if not changed_rows and not notes and not health_changed:
            return None
        return {
            "type": "delta",
            "data": {
                "as_of": to_iso(ctx.clock.now_ms()),
                "markets": changed_rows,
                "notifications": notes,
                "health_changed": health_changed,
            },
        }

    async def run(self, interval_s: float = PUSH_INTERVAL_S) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                message = self.tick()
            except Exception:  # the push channel must not die; the UI also polls
                log.exception("market push tick failed")
                continue
            if message is not None:
                self._ctx.hub.publish(message)
                self.messages_sent += 1
