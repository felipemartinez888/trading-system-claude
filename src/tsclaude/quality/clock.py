"""Server clock offset monitoring (DATA_ARCHITECTURE §2: `server_clock` samples).

Every `sample_interval_s` (default 5 min) take `samples_per_round` (default 5) samples of Binance
server time; each sample's offset is `server_time - midpoint(local send, local receive)`; the round
offset is the median. A `CLOCK_SKEW_CHANGED` event is emitted whenever |offset| crosses the
threshold (default 1 s) in either direction. The first round counts as a crossing only if it is
already beyond the threshold.

Each round also keeps its largest round trip (`max_rtt_ms`): the error of the median offset is
bounded by half the round trip of the sample it came from, so by half the largest one (the paper
runtime's observation anchor uses that bound, P-1).

With a monotonic clock (`MonotonicClock` in production) every sample and the round end are also
read on it, so that a consumer can bound exchange time later without trusting the wall clock (P-1
hardening, F-1): the round keeps its smallest round trip, its monotonic duration and how far the
wall clock moved against the monotonic one during it (`step_ms`); `monotonic_ms()` gives the time
elapsed since. The model and its assumptions: `observation_bound_ms` in `sim/paper.py` and
SIMULATION §7.

`server_now_ms()` is the local clock corrected by the last offset; until a round succeeds the
offset is unknown and `server_now_ms()` falls back to the local clock (`offset_known` is False so
consumers can fail closed).
"""

from __future__ import annotations

import logging
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from tsclaude.contracts.events import AggregateType, EventType, NewEvent
from tsclaude.contracts.timeutil import WallClock
from tsclaude.marketdata.types import EventSink

log = logging.getLogger(__name__)

type FetchServerTime = Callable[[], Awaitable[int]]

#: The largest rate error assumed between the local monotonic clock and exchange time, in parts
#: per million. A monotonic clock that is not slewed by a time service (Windows
#: `QueryPerformanceCounter`, Linux `CLOCK_MONOTONIC_RAW`, macOS `mach_absolute_time`) runs at its
#: crystal's rate; commodity PC crystals are specified within about ±100 ppm over temperature and
#: aging, so 500 ppm leaves a factor 5. It does not hold for a monotonic clock that an OS time
#: service slews (Linux `CLOCK_MONOTONIC`, used only where `CLOCK_MONOTONIC_RAW` is missing): chrony
#: may slew far faster. Exchange time is assumed to be disciplined to UTC within a negligible rate.
CLOCK_DRIFT_PPM = 500


def drift_allowance_ms(elapsed_ms: int) -> int:
    """How far exchange time can run ahead of the monotonic clock, at most, over `elapsed_ms`
    monotonic milliseconds: `elapsed * r / (1 - r)` with r = `CLOCK_DRIFT_PPM`, rounded up."""
    if elapsed_ms < 0:
        raise ValueError("elapsed_ms must be >= 0")
    return -(-elapsed_ms * CLOCK_DRIFT_PPM // (1_000_000 - CLOCK_DRIFT_PPM))


def continuity_tolerance_ms(elapsed_ms: int) -> int:
    """How far the wall clock may move against the monotonic clock over `elapsed_ms` before it is
    a discontinuity (a step, a suspend, or a slew faster than r): the 2 ms that two pairs of floored
    readings can disagree by, plus both clocks' rate allowance."""
    return 2 + 2 * drift_allowance_ms(elapsed_ms)


class MonotonicClock:
    """The local monotonic clock in milliseconds: `CLOCK_MONOTONIC_RAW` where the platform has it
    (never slewed), else `time.monotonic` (`QueryPerformanceCounter` on Windows, not adjustable).
    Its origin is arbitrary: only differences between readings mean anything."""

    def __init__(self) -> None:
        self._read: Callable[[], int] = time.monotonic_ns
        if sys.platform != "win32" and hasattr(time, "CLOCK_MONOTONIC_RAW"):
            raw = time.CLOCK_MONOTONIC_RAW
            self._read = lambda: time.clock_gettime_ns(raw)

    def now_ms(self) -> int:
        return self._read() // 1_000_000


@dataclass(frozen=True, slots=True)
class ClockSample:
    local_send_ms: int
    local_recv_ms: int
    server_ms: int
    #: the same two instants on the monotonic clock (None without one)
    mono_send_ms: int | None = None
    mono_recv_ms: int | None = None

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
    max_rtt_ms: int
    min_rtt_ms: int = 0
    #: the monotonic reading taken with `at_ms`; None unless every sample has monotonic readings
    mono_at_ms: int | None = None
    #: monotonic time from the first sample's send to the round end
    mono_span_ms: int | None = None
    #: how far the wall clock moved against the monotonic clock during the round: the spread of
    #: `wall - monotonic` over every reading of the round (0 or 1 for two continuous clocks)
    step_ms: int | None = None


class ClockMonitor:
    def __init__(
        self,
        clock: WallClock,
        fetch_server_time: FetchServerTime,
        emit: EventSink,
        *,
        samples_per_round: int = 5,
        threshold_ms: int = 1000,
        monotonic: WallClock | None = None,
    ) -> None:
        if samples_per_round < 1:
            raise ValueError("samples_per_round must be >= 1")
        self._clock = clock
        self._mono = monotonic
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

    def monotonic_ms(self) -> int | None:
        """The monotonic clock now (None without one): compare with `last_round.mono_at_ms`."""
        return None if self._mono is None else self._mono.now_ms()

    async def sample_round(self) -> ClockRound | None:
        """Take one round; returns None if no sample succeeded (failure is logged and counted)."""
        samples: list[ClockSample] = []
        for _ in range(self._samples):
            t0, m0 = self._clock.now_ms(), self.monotonic_ms()
            try:
                server = await self._fetch()
            except Exception as exc:  # network errors must not kill the monitor
                log.warning("server time sample failed", extra={"error": repr(exc)})
                continue
            t1, m1 = self._clock.now_ms(), self.monotonic_ms()
            samples.append(ClockSample(t0, t1, server, m0, m1))
        if not samples:
            self.failures += 1
            return None
        return self.record(samples)

    def record(self, samples: list[ClockSample]) -> ClockRound:
        offset = round(statistics.median(s.offset_ms for s in samples))
        rtt = round(statistics.median(s.rtt_ms for s in samples))
        max_rtt = max(s.rtt_ms for s in samples)
        min_rtt = min(s.rtt_ms for s in samples)
        at, mono_at = self._clock.now_ms(), self.monotonic_ms()
        readings = [(s.local_send_ms, s.mono_send_ms) for s in samples]
        readings += [(s.local_recv_ms, s.mono_recv_ms) for s in samples]
        readings.append((at, mono_at))
        gaps = [w - m for w, m in readings if m is not None]
        first = samples[0].mono_send_ms
        span = step = None
        if len(gaps) == len(readings) and first is not None and mono_at is not None:
            step = max(gaps) - min(gaps)
            span = mono_at - first
        else:
            mono_at = None  # partial monotonic evidence is no evidence
        rnd = ClockRound(at, offset, rtt, len(samples), max_rtt, min_rtt, mono_at, span, step)
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
