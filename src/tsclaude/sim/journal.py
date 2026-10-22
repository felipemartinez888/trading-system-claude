"""Paper journal: the durable, append-only record of a paper-trading session.

A session is a sequence of **batches**, appended atomically (all or nothing). Each batch is one
input entry (type `STEP_TYPE`: a released bar, a funding application or a manual reset, plus the
strategy intents and risk views the step consumed) followed by every simulator event and signal
lifecycle event that step produced. Restoring a session replays the inputs through the same code
and checks that it reproduces the recorded outputs exactly (`sim.paper`).

Implementations:
- `MemoryJournal` (tests; optional fault injection);
- `EventStoreJournal`: the hash-chained `EventStore` (ADR-0003), the production journal. It checks
  at construction that `contracts.events` defines every type it writes (fails closed with
  `JournalUnavailable` otherwise).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from tsclaude.contracts.events import AggregateType, EventType, NewEvent, canonical_json
from tsclaude.sim.book import SimEventType
from tsclaude.store.eventstore import EventStore

STEP_TYPE = "paper_step"
SIM_AGGREGATE = "sim"
SESSION_AGGREGATE_ID = "paper"


@dataclass(frozen=True, slots=True)
class JournalEntry:
    type: str
    aggregate_type: str
    aggregate_id: str
    ts_ms: int
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "ts_ms": self.ts_ms,
            "payload": self.payload,
        }

    def canonical(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_event(cls, event: NewEvent) -> JournalEntry:
        return cls(
            str(event.type),
            str(event.aggregate_type),
            event.aggregate_id,
            event.ts_ms,
            dict(event.payload),
        )


type Batch = list[JournalEntry]


class JournalError(RuntimeError):
    """The journal could not durably record a batch (the session must stop: fail closed)."""


class JournalUnavailable(JournalError):
    """The journal cannot be used at all (e.g. missing contract event types)."""


class Journal(Protocol):
    def append(self, batch: Sequence[JournalEntry]) -> None:
        """Append one batch atomically, or raise `JournalError` (nothing appended)."""
        ...

    def batches(self) -> list[Batch]:
        """Every batch in append order."""
        ...


@dataclass
class MemoryJournal:
    """In-memory journal. `fail_when(batch_index, batch)` returning True injects a write failure."""

    fail_when: Callable[[int, Sequence[JournalEntry]], bool] | None = None
    _batches: list[Batch] = field(default_factory=list)

    def append(self, batch: Sequence[JournalEntry]) -> None:
        if not batch or batch[0].type != STEP_TYPE:
            raise JournalError("a batch starts with its input entry")
        if self.fail_when is not None and self.fail_when(len(self._batches), batch):
            raise JournalError("injected journal failure")
        # canonical round trip: what is stored is exactly JSON (no Decimals, no NaN)
        self._batches.append([JournalEntry(**_roundtrip(e.to_dict())) for e in batch])

    def batches(self) -> list[Batch]:
        return [list(b) for b in self._batches]


def _roundtrip(d: dict[str, Any]) -> dict[str, Any]:
    import json

    out: dict[str, Any] = json.loads(canonical_json(d))
    return out


# ---------------------------------------------------------------- event store
def required_event_types() -> list[str]:
    return [STEP_TYPE, *(str(t) for t in SimEventType)]


def missing_contract_types() -> list[str]:
    """Journal types the contract does not define yet (empty = EventStoreJournal usable)."""
    known = {str(t) for t in EventType}
    missing = [t for t in required_event_types() if t not in known]
    if SIM_AGGREGATE not in {str(a) for a in AggregateType}:
        missing.append(f"aggregate_type:{SIM_AGGREGATE}")
    return missing


class EventStoreJournal:
    """Journal on the hash-chained event store. Batches are consecutive events: the input entry
    carries ``batch_size`` so a reader can regroup them without any other index.

    One store can hold several sessions: input entries carry the session id as aggregate id and a
    journal only reads its own session. `batches` verifies the whole hash chain first and refuses
    a broken one (`JournalError`), so a restore never builds on edited history.
    """

    def __init__(self, store: EventStore, session_id: str = SESSION_AGGREGATE_ID) -> None:
        if not session_id:
            raise ValueError("session_id is required")
        self.session_id = session_id
        missing = missing_contract_types()
        if missing:
            raise JournalUnavailable(
                "contracts.events lacks the paper journal types: " + ", ".join(missing)
            )
        self.store = store

    def append(self, batch: Sequence[JournalEntry]) -> None:
        if not batch or batch[0].type != STEP_TYPE:
            raise JournalError("a batch starts with its input entry")
        head = batch[0]
        if head.aggregate_id != self.session_id:
            raise JournalError(f"batch of session {head.aggregate_id!r} in {self.session_id!r}")
        first = NewEvent(
            type=EventType(head.type),
            aggregate_type=AggregateType(head.aggregate_type),
            aggregate_id=head.aggregate_id,
            ts_ms=head.ts_ms,
            payload={**head.payload, "batch_size": len(batch)},
        )
        rest = [
            NewEvent(
                type=EventType(e.type),
                aggregate_type=AggregateType(e.aggregate_type),
                aggregate_id=e.aggregate_id,
                ts_ms=e.ts_ms,
                payload=e.payload,
            )
            for e in batch[1:]
        ]
        try:
            self.store.append_many([first, *rest])
        except Exception as exc:
            raise JournalError(f"event store append failed: {type(exc).__name__}") from exc

    def batches(self) -> list[Batch]:
        report = self.store.verify()
        if not report.ok:
            raise JournalError(
                f"event store hash chain is broken at seq {report.first_bad_seq}: {report.reason}"
            )
        out: list[Batch] = []
        heads = self.store.read(
            types=[EventType(STEP_TYPE)], aggregate=(AggregateType(SIM_AGGREGATE), self.session_id)
        )
        for head in heads:
            size = int(head.payload["batch_size"])
            payload = {k: v for k, v in head.payload.items() if k != "batch_size"}
            entries = [
                JournalEntry(
                    str(head.type),
                    str(head.aggregate_type),
                    head.aggregate_id,
                    head.ts_ms,
                    payload,
                )
            ]
            for e in self.store.read(after_seq=head.seq, limit=size - 1):
                entries.append(
                    JournalEntry(
                        str(e.type),
                        str(e.aggregate_type),
                        e.aggregate_id,
                        e.ts_ms,
                        dict(e.payload),
                    )
                )
            if len(entries) != size:
                raise JournalError(f"batch at seq {head.seq} is truncated")
            out.append(entries)
        return out


def flatten(batches: Iterable[Sequence[JournalEntry]]) -> list[JournalEntry]:
    return [e for b in batches for e in b]
