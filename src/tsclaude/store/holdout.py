"""Holdout guard (ADR-0009, RESEARCH_PROTOCOL §2, decision D-05).

The sealed holdout is 2026-01-01 → 2026-09-30 UTC, i.e. `[HOLDOUT_START_MS, HOLDOUT_END_MS)`.

- A `research` read whose range touches the holdout is refused unless a `holdout_unlocked` event
  exists for exactly that (`prereg_id`, `strategy_hash`).
- An unlock happens **once per preregistration**: a second unlock for the same `prereg_id` (with any
  strategy hash) is refused, so the holdout cannot be re-opened for a tweaked strategy.
- Reads ending at or before the holdout start (warm-up) are served normally; nothing inside the
  holdout is served as warm-up.
- Unknown purposes are refused (fail closed); new purposes need an explicit decision.

The durable unlock record is a chained event (`EventType.HOLDOUT_UNLOCKED`). That event type is a
pending contract change (Lead-owned `contracts/`); until it exists `EventStoreUnlocks` treats the
holdout as locked and refuses to unlock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from tsclaude.contracts.events import AggregateType, EventType, NewEvent
from tsclaude.contracts.timeutil import to_iso
from tsclaude.store.eventstore import EventStore

log = logging.getLogger(__name__)

HOLDOUT_START_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z
HOLDOUT_END_MS = 1_790_812_800_000  # 2026-10-01T00:00:00Z (exclusive; last day 2026-09-30)


class Purpose(StrEnum):
    RESEARCH = "research"


class HoldoutError(PermissionError):
    pass


class HoldoutLocked(HoldoutError):
    """A research read touched the sealed holdout without a matching unlock."""


class HoldoutAlreadyUnlocked(HoldoutError):
    """The preregistration already used its single unlock."""


class HoldoutContractMissing(HoldoutError):
    """`EventType.HOLDOUT_UNLOCKED` is not in the contracts yet: unlocking is impossible."""


@dataclass(frozen=True, slots=True)
class UnlockRecord:
    prereg_id: str
    strategy_hash: str
    unlocked_at_ms: int
    reason: str


class UnlockRegistry(Protocol):
    def find(self, prereg_id: str) -> UnlockRecord | None: ...

    def unlock(
        self, prereg_id: str, strategy_hash: str, *, reason: str, now_ms: int
    ) -> UnlockRecord: ...


def _unlock_event_type() -> EventType | None:
    return getattr(EventType, "HOLDOUT_UNLOCKED", None)


def _research_aggregate() -> AggregateType:
    # Requested as AggregateType.RESEARCH; RUNTIME until the Lead adds it.
    agg: AggregateType = getattr(AggregateType, "RESEARCH", AggregateType.RUNTIME)
    return agg


class EventStoreUnlocks:
    """Unlocks as chained events in the event store (durable, auditable, append-only)."""

    def __init__(self, store: EventStore) -> None:
        self._store = store

    @staticmethod
    def aggregate_id(prereg_id: str) -> str:
        return f"prereg:{prereg_id}"

    def find(self, prereg_id: str) -> UnlockRecord | None:
        etype = _unlock_event_type()
        if etype is None:
            return None  # fail closed: no unlock can exist
        events = self._store.read(
            types=[etype], aggregate=(_research_aggregate(), self.aggregate_id(prereg_id))
        )
        if not events:
            return None
        p = events[0].payload
        return UnlockRecord(
            str(p["prereg_id"]), str(p["strategy_hash"]), int(p["unlocked_at_ms"]), str(p["reason"])
        )

    def unlock(
        self, prereg_id: str, strategy_hash: str, *, reason: str, now_ms: int
    ) -> UnlockRecord:
        etype = _unlock_event_type()
        if etype is None:
            raise HoldoutContractMissing(
                "EventType.HOLDOUT_UNLOCKED is not defined in contracts; the holdout stays sealed"
            )
        existing = self.find(prereg_id)
        if existing is not None:
            raise HoldoutAlreadyUnlocked(
                f"preregistration {prereg_id} already unlocked the holdout at "
                f"{to_iso(existing.unlocked_at_ms)} for strategy {existing.strategy_hash}"
            )
        payload: dict[str, Any] = {
            "prereg_id": prereg_id,
            "strategy_hash": strategy_hash,
            "unlocked_at_ms": now_ms,
            "reason": reason,
            "holdout_from_ms": HOLDOUT_START_MS,
            "holdout_to_ms": HOLDOUT_END_MS,
        }
        self._store.append(
            NewEvent(
                type=etype,
                aggregate_type=_research_aggregate(),
                aggregate_id=self.aggregate_id(prereg_id),
                ts_ms=now_ms,
                payload=payload,
            )
        )
        log.warning("holdout unlocked", extra={"prereg_id": prereg_id, "strategy": strategy_hash})
        return UnlockRecord(prereg_id, strategy_hash, now_ms, reason)


def touches_holdout(start_ms: int, end_ms: int) -> bool:
    return start_ms < HOLDOUT_END_MS and end_ms > HOLDOUT_START_MS


class HoldoutGuard:
    def __init__(self, unlocks: UnlockRegistry) -> None:
        self._unlocks = unlocks

    def check(
        self,
        start_ms: int,
        end_ms: int,
        *,
        purpose: Purpose | str,
        prereg_id: str | None = None,
        strategy_hash: str | None = None,
    ) -> None:
        """Raise unless the read [start_ms, end_ms) is allowed for `purpose`."""
        try:
            purpose = Purpose(purpose)
        except ValueError:
            raise HoldoutError(f"unknown read purpose {purpose!r}; refused (fail closed)") from None
        if end_ms <= start_ms or not touches_holdout(start_ms, end_ms):
            return
        if not prereg_id or not strategy_hash:
            raise HoldoutLocked(
                f"[{to_iso(start_ms)}, {to_iso(end_ms)}) touches the sealed holdout "
                f"[{to_iso(HOLDOUT_START_MS)}, {to_iso(HOLDOUT_END_MS)}); research reads there "
                "need prereg_id and strategy_hash with a recorded unlock"
            )
        record = self._unlocks.find(prereg_id)
        if record is None:
            raise HoldoutLocked(f"no holdout unlock recorded for preregistration {prereg_id}")
        if record.strategy_hash != strategy_hash:
            raise HoldoutLocked(
                f"the holdout unlock of {prereg_id} is for strategy {record.strategy_hash}, "
                f"not {strategy_hash}"
            )

    def unlock(
        self, prereg_id: str, strategy_hash: str, *, reason: str, now_ms: int
    ) -> UnlockRecord:
        if not prereg_id or not strategy_hash or not reason:
            raise ValueError("prereg_id, strategy_hash and reason are required")
        return self._unlocks.unlock(prereg_id, strategy_hash, reason=reason, now_ms=now_ms)


class SealedUnlocks:
    """An unlock registry that never unlocks: for readers that must not touch the holdout."""

    def find(self, prereg_id: str) -> UnlockRecord | None:
        return None

    def unlock(
        self, prereg_id: str, strategy_hash: str, *, reason: str, now_ms: int
    ) -> UnlockRecord:
        raise HoldoutError("this reader cannot unlock the holdout")
