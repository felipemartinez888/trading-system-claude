"""Server clock offset monitoring (DATA_ARCHITECTURE §2: `server_clock` samples).

Every `sample_interval_s` (default 5 min) take `samples_per_round` (default 5) samples of Binance
server time; each sample's offset is `server_time - midpoint(local send, local receive)`; the round
offset is the median. A `CLOCK_SKEW_CHANGED` event is emitted whenever |offset| crosses the
threshold (default 1 s) in either direction. The first round counts as a crossing only if it is
already beyond the threshold.

`server_now_ms()` is the local clock corrected by the last offset; until a round succeeds the
offset is unknown and `server_now_ms()` falls back to the local clock (`offset_known` is False so
consumers can fail closed).
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from tsclaude.contracts.events import AggregateType, EventType, NewEvent
from tsclaude.contracts.timeutil import WallClock
from tsclaude.marketdata.types import EventSink

log = logging.getLogger(__name__)

type FetchServerTime = Callable[[], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class ClockSample:
    local_send_ms: int
    local_recv_ms: int
    server_ms: int

    @property
    def rtt_ms(self) -> int:
        return self.local_recv_ms - self.local_send_ms

    @property
    def offset_ms(self) -> float:
        return self.server_ms - (self.local_send_ms + self.local_recv_ms) / 2


@dataclass(frozen=True, slots=True)
class ClockRound:
    at_ms: int
    offset_ms: int
    median_rtt_ms: int
    samples: int


class ClockMonitor:
    def __init__(
        self,
        clock: WallClock,
        fetch_server_time: FetchServerTime,
        emit: EventSink,
        *,
        samples_per_round: int = 5,
        threshold_ms: int = 1000,
    ) -> None:
        if samples_per_round < 1:
            raise ValueError("samples_per_round must be >= 1")
        self._clock = clock
        self._fetch = fetch_server_time
        self._emit = emit
        self._samples = samples_per_round
        self._threshold = threshold_ms
        self._offset: int | None = None
        self._exceeded = False
        self.last_round: ClockRound | None = None
        self.failures = 0

    @property
    def offset_known(self) -> bool:
        return self._offset is not None

    @property
    def offset_ms(self) -> int | None:
        return self._offset

    @property
    def skew_exceeded(self) -> bool:
        return self._exceeded

    def server_now_ms(self) -> int:
        return self._clock.now_ms() + (self._offset or 0)

    async def sample_round(self) -> ClockRound | None:
        """Take one round; returns None if no sample succeeded (failure is logged and counted)."""
        samples: list[ClockSample] = []
        for _ in range(self._samples):
            t0 = self._clock.now_ms()
            try:
                server = await self._fetch()
            except Exception as exc:  # network errors must not kill the monitor
                log.warning("server time sample failed", extra={"error": repr(exc)})
                continue
            t1 = self._clock.now_ms()
            samples.append(ClockSample(t0, t1, server))
        if not samples:
            self.failures += 1
            return None
        return self.record(samples)

    def record(self, samples: list[ClockSample]) -> ClockRound:
        offset = round(statistics.median(s.offset_ms for s in samples))
        rtt = round(statistics.median(s.rtt_ms for s in samples))
        rnd = ClockRound(self._clock.now_ms(), offset, rtt, len(samples))
        previous = self._offset
        self._offset = offset
        self.last_round = rnd
        exceeded = abs(offset) > self._threshold
        if exceeded != self._exceeded:
            self._exceeded = exceeded
            log.warning(
                "clock skew threshold crossed",
                extra={"offset_ms": offset, "exceeded": exceeded, "threshold_ms": self._threshold},
            )
            self._emit(
                NewEvent(
                    type=EventType.CLOCK_SKEW_CHANGED,
                    aggregate_type=AggregateType.MARKET_DATA,
                    aggregate_id="clock",
                    ts_ms=rnd.at_ms,
                    payload={
                        "offset_ms": offset,
                        "previous_offset_ms": previous,
                        "threshold_ms": self._threshold,
                        "exceeded": exceeded,
                        "median_rtt_ms": rtt,
                        "samples": len(samples),
                    },
                )
            )
        return rnd
