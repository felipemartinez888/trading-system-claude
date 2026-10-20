"""Rate limiting, back-off and connection budgets (pure asyncio; no network libraries).

- `SlidingWindowLimiter`: at most N events in any rolling window (WS control messages, connection
  attempts, the separate fundingRate budget). Strictly stronger than a token bucket of the same rate,
  so a burst can never exceed Binance's per-second message limit.
- `WeightLimiter`: REST request-weight budget seeded from `exchangeInfo.rateLimits`, corrected from
  `X-MBX-USED-WEIGHT-<n><unit>` response headers, blocked by 429 `Retry-After`, stopped by 418.
- `Backoff`: exponential back-off with full jitter, capped.

All time comes from an injected `WallClock` and all waiting from an injected `sleep`, so tests can
drive these with `ManualClock` without real delays.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass

from tsclaude.contracts.timeutil import WallClock
from tsclaude.marketdata.types import RateLimit

type Sleep = Callable[[float], Awaitable[None]]

_WEIGHT_HEADER = re.compile(r"^x-mbx-used-weight-(\d+)([smhd])$", re.IGNORECASE)
_UNIT_MS = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}


class SlidingWindowLimiter:
    """Allow at most `max_events` per rolling `window_ms`; `acquire()` waits for a free slot."""

    def __init__(self, max_events: int, window_ms: int, clock: WallClock, sleep: Sleep) -> None:
        if max_events < 1 or window_ms < 1:
            raise ValueError("max_events and window_ms must be positive")
        self.max_events = max_events
        self.window_ms = window_ms
        self._clock = clock
        self._sleep = sleep
        self._stamps: deque[int] = deque()
        self._lock = asyncio.Lock()

    def _prune(self, now: int) -> None:
        while self._stamps and self._stamps[0] <= now - self.window_ms:
            self._stamps.popleft()

    def wait_ms(self) -> int:
        """Milliseconds until a slot is free (0 if one is free now)."""
        now = self._clock.now_ms()
        self._prune(now)
        if len(self._stamps) < self.max_events:
            return 0
        return self._stamps[0] + self.window_ms - now

    def try_acquire(self) -> bool:
        if self.wait_ms() > 0:
            return False
        self._stamps.append(self._clock.now_ms())
        return True

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                wait = self.wait_ms()
                if wait <= 0:
                    self._stamps.append(self._clock.now_ms())
                    return
                await self._sleep(wait / 1000)

    @property
    def used(self) -> int:
        self._prune(self._clock.now_ms())
        return len(self._stamps)


@dataclass
class _Window:
    window_ms: int
    limit: int | None  # None: seen in headers but not declared in exchangeInfo
    used: int = 0
    window_start_ms: int = 0


class RestBannedState(Exception):
    """Raised by the limiter while REST is stopped after HTTP 418."""

    def __init__(self, until_ms: int | None) -> None:
        super().__init__("REST stopped after HTTP 418 (IP banned)")
        self.until_ms = until_ms


class WeightLimiter:
    """REST weight budget for one Binance API family (spot or USD-M; their IP limits are separate).

    Binance counts request weight per IP in fixed windows (e.g. 1 minute). We keep, per declared
    `REQUEST_WEIGHT` window, the used weight in the current window; the authoritative value comes
    from the `X-MBX-USED-WEIGHT-*` header after every response. We only use `budget_fraction` of
    each declared limit, to stay well clear of it when other tools share the IP.
    """

    def __init__(self, clock: WallClock, sleep: Sleep, budget_fraction: float = 0.5) -> None:
        if not 0 < budget_fraction <= 1:
            raise ValueError("budget_fraction must be in (0, 1]")
        self._clock = clock
        self._sleep = sleep
        self._fraction = budget_fraction
        self._windows: dict[str, _Window] = {}  # key: header suffix, e.g. "1M"
        self._seeded = False
        self._blocked_until_ms = 0
        self._banned = False
        self._banned_until_ms: int | None = None
        self._lock = asyncio.Lock()

    @property
    def seeded(self) -> bool:
        return self._seeded

    @property
    def windows(self) -> dict[str, tuple[int, int | None]]:
        """Header suffix -> (used in current window, declared limit)."""
        now = self._clock.now_ms()
        out: dict[str, tuple[int, int | None]] = {}
        for key, w in self._windows.items():
            self._roll(w, now)
            out[key] = (w.used, w.limit)
        return out

    def seed(self, rate_limits: Iterable[RateLimit]) -> None:
        for rl in rate_limits:
            if rl.rate_limit_type != "REQUEST_WEIGHT":
                continue
            key = rl.header_suffix.upper()
            existing = self._windows.get(key)
            if existing is None:
                self._windows[key] = _Window(window_ms=rl.window_ms, limit=rl.limit)
            else:
                existing.limit = rl.limit
        self._seeded = True

    def _roll(self, w: _Window, now: int) -> None:
        start = now - now % w.window_ms
        if start != w.window_start_ms:
            w.window_start_ms = start
            w.used = 0

    def _budget(self, w: _Window) -> int | None:
        return None if w.limit is None else max(1, int(w.limit * self._fraction))

    def check_banned(self) -> None:
        if not self._banned:
            return
        now = self._clock.now_ms()
        if self._banned_until_ms is not None and now >= self._banned_until_ms:
            self._banned = False
            self._banned_until_ms = None
            return
        raise RestBannedState(self._banned_until_ms)

    def _wait_ms(self, weight: int) -> int:
        now = self._clock.now_ms()
        wait = max(0, self._blocked_until_ms - now)
        for w in self._windows.values():
            self._roll(w, now)
            budget = self._budget(w)
            if budget is None:
                continue
            # A single request heavier than the whole budget may still run in a fresh window.
            if w.used > 0 and w.used + weight > budget:
                wait = max(wait, w.window_start_ms + w.window_ms - now)
        return wait

    async def acquire(self, weight: int) -> None:
        async with self._lock:
            while True:
                self.check_banned()
                wait = self._wait_ms(weight)
                if wait <= 0:
                    now = self._clock.now_ms()
                    for w in self._windows.values():
                        self._roll(w, now)
                        w.used += weight
                    return
                await self._sleep(wait / 1000)

    def observe_headers(self, headers: Mapping[str, str]) -> None:
        """Correct used weight from `X-MBX-USED-WEIGHT-<n><unit>` headers (authoritative)."""
        now = self._clock.now_ms()
        for name, value in headers.items():
            m = _WEIGHT_HEADER.match(name)
            if m is None:
                continue
            try:
                used = int(value)
            except ValueError:
                continue
            key = f"{m.group(1)}{m.group(2)}".upper()
            w = self._windows.get(key)
            if w is None:
                w = _Window(window_ms=int(m.group(1)) * _UNIT_MS[m.group(2).lower()], limit=None)
                self._windows[key] = w
            self._roll(w, now)
            w.used = used

    def block_for(self, retry_after_ms: int) -> None:
        """HTTP 429: no REST call until `Retry-After` has elapsed."""
        self._blocked_until_ms = max(self._blocked_until_ms, self._clock.now_ms() + retry_after_ms)

    def next_window_ms(self) -> int:
        """Milliseconds until the shortest weight window rolls over (fallback when no Retry-After)."""
        now = self._clock.now_ms()
        if not self._windows:
            return 60_000
        return min(w.window_ms - now % w.window_ms for w in self._windows.values())

    def ban(self, retry_after_ms: int | None) -> None:
        """HTTP 418: stop all REST. Without `Retry-After`, stopped until the process restarts."""
        self._banned = True
        self._banned_until_ms = (
            None if retry_after_ms is None else self._clock.now_ms() + retry_after_ms
        )

    @property
    def banned(self) -> bool:
        try:
            self.check_banned()
        except RestBannedState:
            return True
        return False


class Backoff:
    """Exponential back-off with full jitter: delay ~ U(0, min(cap, base * 2**attempt))."""

    def __init__(
        self, base_s: float, cap_s: float, rng: Callable[[float, float], float] | None = None
    ) -> None:
        if base_s <= 0 or cap_s < base_s:
            raise ValueError("need 0 < base_s <= cap_s")
        self.base_s = base_s
        self.cap_s = cap_s
        self._rng = rng if rng is not None else random.SystemRandom().uniform
        self.attempt = 0

    def ceiling(self, attempt: int) -> float:
        return min(self.cap_s, self.base_s * (2 ** min(attempt, 30)))

    def next_delay(self) -> float:
        delay = self._rng(0.0, self.ceiling(self.attempt))
        self.attempt += 1
        return delay

    def reset(self) -> None:
        self.attempt = 0
