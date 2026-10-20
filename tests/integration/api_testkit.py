"""Shared helpers for the API integration tests (imported by conftest and test modules)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tsclaude.api.context import AppContext
from tsclaude.contracts.events import canonical_json
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.runtime.sessions import SessionManager
from tsclaude.store.eventstore import EventStore

T0 = 1_760_000_000_000
PORT = 8765
BASE_URL = f"http://127.0.0.1:{PORT}"
WS_URL = f"ws://127.0.0.1:{PORT}/ws/live"

TOKEN = "test-token-" + "x" * 32


class ContextFactory:
    """Builds AppContexts over one temp db and closes their stores at teardown."""

    def __init__(self, db_path: Path, clock: ManualClock) -> None:
        self.db_path = db_path
        self.clock = clock
        self._stores: list[EventStore] = []

    def __call__(self, *, start: bool = True, heartbeat_interval_s: float = 5.0) -> AppContext:
        store = EventStore(self.db_path)
        self._stores.append(store)
        ctx = AppContext(
            store=store,
            clock=self.clock,
            app_version="test",
            port=PORT,
            heartbeat_interval_s=heartbeat_interval_s,
            token=TOKEN,
        )
        if start:
            ctx.start()
        return ctx

    def close_all(self) -> None:
        for store in self._stores:
            store.close()


def tamper(db_path: Path, seq: int = 1) -> None:
    """Rewrite an event's payload behind the store's back (as an attacker or a bug would).

    The payload stays canonical JSON and keeps its keys, so the only detectable problem is the
    hash mismatch (and the app can still read the event).
    """
    conn = sqlite3.connect(db_path)
    (payload_text,) = conn.execute("SELECT payload FROM events WHERE seq=?", (seq,)).fetchone()
    payload = json.loads(payload_text)
    payload["tampered"] = True
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("UPDATE events SET payload=? WHERE seq=?", (canonical_json(payload), seq))
    conn.commit()
    conn.close()


def make_tampered_db(db_path: Path, clock: ManualClock) -> None:
    """A previous clean session whose first event was later modified."""
    with EventStore(db_path) as store:
        mgr = SessionManager(store, clock, app_version="old")
        mgr.open()
        clock.advance(1_000)
        mgr.close("user")
    tamper(db_path, seq=1)
    clock.advance(60_000)
