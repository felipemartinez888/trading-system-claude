"""Time conventions: UTC everywhere, integer epoch milliseconds in storage, ISO-8601 'Z' in APIs."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol

type EpochMs = int

MS_PER_SECOND = 1_000
MS_PER_MINUTE = 60 * MS_PER_SECOND
MS_PER_HOUR = 60 * MS_PER_MINUTE
MS_PER_DAY = 24 * MS_PER_HOUR


class WallClock(Protocol):
    """Source of 'now'. Injected everywhere so tests can control time."""

    def now_ms(self) -> EpochMs: ...


class SystemClock:
    """Real wall clock (UTC epoch milliseconds)."""

    def now_ms(self) -> EpochMs:
        return time.time_ns() // 1_000_000


class ManualClock:
    """Deterministic clock for tests and replays."""

    def __init__(self, start_ms: EpochMs = 0) -> None:
        self._now = start_ms

    def now_ms(self) -> EpochMs:
        return self._now

    def advance(self, delta_ms: int) -> None:
        if delta_ms < 0:
            raise ValueError("ManualClock cannot go backwards")
        self._now += delta_ms

    def set(self, now_ms: EpochMs) -> None:
        if now_ms < self._now:
            raise ValueError("ManualClock cannot go backwards")
        self._now = now_ms


def to_iso(ms: EpochMs) -> str:
    """Epoch ms -> ISO-8601 UTC with millisecond precision and 'Z' suffix."""
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def from_iso(text: str) -> EpochMs:
    """ISO-8601 (must carry a UTC designator) -> epoch ms."""
    normalized = text.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without timezone is ambiguous: {text!r}")
    offset = dt.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError(f"timestamp must be UTC: {text!r}")
    return int(dt.timestamp() * 1000)
