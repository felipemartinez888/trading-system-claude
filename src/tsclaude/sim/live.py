"""`LiveClock`: closed 1m bars from the market-data subscription, in the simulator's clock order.

The market-data service publishes a `BarClosed` per (market, symbol) as each kline closes (or is
recovered by REST), so bars of one minute arrive symbol by symbol and possibly out of order across
symbols. The simulator needs the global order of `sim.clock` (ascending minute, then
``(market, symbol)``). `LiveClock` buffers bars per minute and **releases** minute ``M``

- as soon as every expected (market, symbol) has delivered ``M``, or
- when the wall clock passes ``M + 1 min + settle_ms`` (missing symbols are then a gap for that
  minute; the simulator reports it and the risk engine blocks the symbol while the gap is open),

always in minute order. A bar for an already released minute is **late**: it is not processed
(counted and kept in `late`), because the simulator may already have acted on that minute. A
`replaced` bar (a WebSocket close replacing a REST version) replaces the buffered version if the
minute is still open, else it is late as well.

Everything released is recorded (`released`), so ``HistoricalClock(clock.released, REPLAY)``
reproduces exactly the bar sequence the live session processed (replay parity, ADR-0005).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from tsclaude.contracts.market import Bar, Interval
from tsclaude.contracts.signals import RunMode
from tsclaude.contracts.timeutil import MS_PER_MINUTE
from tsclaude.sim.clock import ClockError, SymbolKey, bar_key, order_key

DEFAULT_SETTLE_MS = 90_000


@dataclass
class LiveClockStats:
    offered: int = 0
    released: int = 0
    late: int = 0
    duplicates: int = 0
    replaced: int = 0
    ignored: int = 0  # not 1m, or an instrument outside the universe
    incomplete_minutes: int = 0  # released at the deadline with symbols missing


class LiveClock:
    def __init__(
        self,
        symbols: Iterable[SymbolKey],
        *,
        settle_ms: int = DEFAULT_SETTLE_MS,
        start_ms: int | None = None,
        processed: Iterable[SymbolKey] = (),
        mode: RunMode = RunMode.LIVE,
    ) -> None:
        """`start_ms` + `processed` (restart): the first minute to release and the instruments
        whose bar of that minute was already processed before the restart (they count as
        delivered for completeness; a re-delivery of them is late)."""
        self.expected = frozenset(symbols)
        if not self.expected:
            raise ValueError("a live clock needs at least one instrument")
        if settle_ms < 0:
            raise ValueError("settle_ms must be >= 0")
        if start_ms is not None and start_ms % MS_PER_MINUTE:
            raise ValueError("start_ms must be a minute open")
        self.settle_ms = settle_ms
        self._mode = mode
        self._next: int | None = start_ms  # next minute to release
        self._buffer: dict[int, dict[SymbolKey, Bar]] = {}
        self._released: list[Bar] = []
        self._last_key: tuple[int, str, str] | None = None
        self._done: frozenset[SymbolKey] = frozenset(processed)
        self._done_minute = start_ms
        if self._done and start_ms is None:
            raise ValueError("processed instruments need start_ms")
        if self._done and start_ms is not None:
            self._last_key = max((start_ms, str(m), s) for m, s in self._done)
        self.late: list[Bar] = []
        self.stats = LiveClockStats()

    @property
    def mode(self) -> RunMode:
        return self._mode

    @property
    def released(self) -> tuple[Bar, ...]:
        return tuple(self._released)

    @property
    def next_minute_ms(self) -> int | None:
        return self._next

    def __iter__(self) -> Iterator[Bar]:
        """The bars released so far, in the order released (the `Clock` protocol)."""
        return iter(list(self._released))

    def offer(self, bar: Bar, *, replaced: bool = False) -> str:
        """Hand a closed bar to the clock: ``buffered``, ``replaced``, ``duplicate``, ``late``
        or ``ignored``."""
        self.stats.offered += 1
        if bar.interval is not Interval.M1 or bar_key(bar) not in self.expected:
            self.stats.ignored += 1
            return "ignored"
        minute = bar.open_time_ms
        stale = self._last_key is not None and order_key(bar) <= self._last_key
        if stale or (self._next is not None and minute < self._next):
            self.stats.late += 1
            self.late.append(bar)
            return "late"
        slot = self._buffer.setdefault(minute, {})
        key = bar_key(bar)
        if key in slot:
            if replaced:
                slot[key] = bar
                self.stats.replaced += 1
                return "replaced"
            self.stats.duplicates += 1
            return "duplicate"
        slot[key] = bar
        return "buffered"

    def earliest_buffered(self) -> int | None:
        """Open time of the earliest buffered (not yet released) minute, if any."""
        return min(self._buffer) if self._buffer else None

    def skip_to(self, minute_ms: int) -> None:
        """Start (or continue) at `minute_ms`: earlier minutes were handled elsewhere (restart:
        resolved over backfilled bars). Buffered bars before it become late."""
        if minute_ms % MS_PER_MINUTE:
            raise ValueError("minute_ms must be a minute open")
        if self._next is not None and minute_ms <= self._next:
            return
        self._next = minute_ms
        for minute in sorted(m for m in self._buffer if m < minute_ms):
            for bar in sorted(self._buffer.pop(minute).values(), key=order_key):
                self.stats.late += 1
                self.late.append(bar)
        self._last_key = max(self._last_key or (0, "", ""), (minute_ms - 1, "", ""))

    def release(self, now_ms: int) -> list[Bar]:
        """Minutes that are complete or past their deadline, in global clock order."""
        out: list[Bar] = []
        while self._buffer:
            if self._next is None:
                self._next = min(self._buffer)
            minute = self._next
            slot = self._buffer.get(minute, {})
            done = self._done if minute == self._done_minute else frozenset()
            complete = self.expected <= set(slot) | done
            due = now_ms >= minute + MS_PER_MINUTE + self.settle_ms
            if not (complete or due):
                break
            if not complete and slot:
                self.stats.incomplete_minutes += 1
            for bar in sorted(slot.values(), key=order_key):
                key = order_key(bar)
                if self._last_key is not None and key <= self._last_key:
                    raise ClockError(f"live clock order violated at {key}")  # defensive
                self._last_key = key
                out.append(bar)
            self._buffer.pop(minute, None)
            self._next = minute + MS_PER_MINUTE
        self._released.extend(out)
        self.stats.released += len(out)
        return out
