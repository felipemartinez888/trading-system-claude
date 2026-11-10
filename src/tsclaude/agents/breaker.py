"""Circuit breaker over consecutive failed reviews (SIGNALS_AND_AGENTS §4).

CLOSED -> OPEN after `threshold` consecutive failures; OPEN refuses calls for `cooldown_ms`, then
HALF_OPEN lets exactly one review through: success closes, failure re-opens.
"""

from __future__ import annotations

import threading
from enum import StrEnum

from tsclaude.contracts.timeutil import WallClock


class BreakerState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    def __init__(self, threshold: int, cooldown_ms: int, clock: WallClock) -> None:
        self.threshold = threshold
        self.cooldown_ms = cooldown_ms
        self.clock = clock
        self.consecutive_failures = 0
        self.opened_at_ms: int | None = None
        self._trial_in_flight = False
        self._lock = threading.Lock()

    @property
    def state(self) -> BreakerState:
        if self.opened_at_ms is None:
            return BreakerState.CLOSED
        if self.clock.now_ms() - self.opened_at_ms >= self.cooldown_ms:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def allow(self) -> bool:
        with self._lock:
            state = self.state
            if state is BreakerState.CLOSED:
                return True
            if state is BreakerState.HALF_OPEN and not self._trial_in_flight:
                self._trial_in_flight = True
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self.consecutive_failures = 0
            self.opened_at_ms = None
            self._trial_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            self.consecutive_failures += 1
            if self._trial_in_flight or self.consecutive_failures >= self.threshold:
                self.opened_at_ms = self.clock.now_ms()
            self._trial_in_flight = False

    def release_trial(self) -> None:
        """A half-open trial that made no call (e.g. budget refused): let the next one try."""
        with self._lock:
            self._trial_in_flight = False
