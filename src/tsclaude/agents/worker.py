"""The live half of the paper wiring: reviews run off the event loop (M5-3; ADR-0002, ADR-0007).

`ReviewWorker` runs `AgentGate.review` on **one** worker thread, so the single-process asyncio
runtime never waits for a model: the paper service only submits a signal and, at each poll, takes
the reviews that have finished, in submission order (a review still running holds the ones
behind it; the gate serializes reviews anyway). The gate's own per-call deadline, single retry,
circuit breaker and daily budget apply unchanged. Nothing here raises into the caller: a worker
failure, a full queue or a closed worker becomes an ``AI_UNAVAILABLE`` outcome.

`packet_inputs_from_bars` builds the evidence-packet inputs from closed 1m bars (strategy bars
derived exactly as for the strategy, `features.bars.derive_bars`); only bars that closed at or
before the decision time are used (no look-ahead). `bar_reader_inputs` reads those bars from the
live bar store inside the worker thread, never on the event loop.

This module is only ever constructed in agents mode ``confirmed``; replays never build it.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol

from tsclaude.contracts.market import Bar, Interval, Market
from tsclaude.contracts.signals import SignalRecord, SignalStatus
from tsclaude.contracts.timeutil import WallClock
from tsclaude.features.bars import derive_bars

from .client import AgentClient
from .config import AgentMode, LoadedConfig, load_config
from .gate import AgentGate
from .packet import BarInput, PacketError, PacketInputs
from .real_client import build_client
from .wiring import outcome_payload, unavailable_payload

log = logging.getLogger(__name__)

type BarReader = Callable[[Market, str, int, int], Sequence[Bar]]
type InputsFactory = Callable[[SignalRecord], PacketInputs]

DEFAULT_MAX_QUEUE = 32
ATR_N = 14
LOOKBACK_BARS = 48


class Reviewer(Protocol):
    """What the paper service needs from a live reviewer."""

    def submit(self, signal: SignalRecord) -> None: ...

    def completed(self) -> list[tuple[str, dict[str, Any]]]: ...

    def snapshot(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


class ReviewWorker:
    def __init__(
        self,
        gate: AgentGate,
        inputs: InputsFactory,
        *,
        max_queue: int = DEFAULT_MAX_QUEUE,
    ) -> None:
        if max_queue < 1:
            raise ValueError("max_queue must be > 0")
        self.gate = gate
        self._inputs = inputs
        self.max_queue = max_queue
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-review")
        self._queue: deque[tuple[str, Future[dict[str, Any]] | dict[str, Any]]] = deque()
        self._lock = threading.Lock()
        self.submitted = 0
        self.refused = 0
        self.closed = False

    def submit(self, signal: SignalRecord) -> None:
        sid = signal.signal_id
        with self._lock:
            running = sum(1 for _, item in self._queue if isinstance(item, Future))
            if self.closed or running >= self.max_queue:
                self.refused += 1
                why = "review worker closed" if self.closed else "review queue full"
                self._queue.append((sid, unavailable_payload(sid, why)))
                return
            try:
                fut = self._pool.submit(self._review, signal)
            except Exception as exc:  # e.g. shut down: fail closed
                self.refused += 1
                reason = f"review could not be started: {type(exc).__name__}"
                self._queue.append((sid, unavailable_payload(sid, reason)))
                return
            self.submitted += 1
            self._queue.append((sid, fut))

    def _review(self, signal: SignalRecord) -> dict[str, Any]:
        """Worker thread. `AgentGate.review` never raises; this guard is defensive."""
        try:
            outcome = self.gate.review(
                signal,
                risk_passed=True,
                inputs=lambda: self._inputs(signal),
                status=SignalStatus.CONFIRMED,
            )
            return outcome_payload(signal.signal_id, outcome)
        except Exception as exc:
            log.exception("agent review worker failed")
            return unavailable_payload(
                signal.signal_id, f"review worker error: {type(exc).__name__}"
            )

    def completed(self) -> list[tuple[str, dict[str, Any]]]:
        """Finished reviews in submission order (stops at the first one still running)."""
        out: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            while self._queue:
                sid, item = self._queue[0]
                if isinstance(item, Future):
                    if not item.done():
                        break
                    try:
                        payload = item.result()
                    except BaseException as exc:  # cancelled or crashed: never an approval
                        payload = unavailable_payload(
                            sid, f"review worker error: {type(exc).__name__}"
                        )
                else:
                    payload = item
                self._queue.popleft()
                out.append((sid, payload))
        return out

    @property
    def in_flight(self) -> int:
        with self._lock:
            return sum(1 for _, item in self._queue if isinstance(item, Future) and not item.done())

    def wait_idle(self, timeout_s: float) -> bool:
        """Wait until every submitted review finished (tests and shutdown only)."""
        with self._lock:
            futures = [item for _, item in self._queue if isinstance(item, Future)]
        done = threading.Event()
        if not futures:
            return True
        remaining = len(futures)
        guard = threading.Lock()

        def one(_: Future[dict[str, Any]]) -> None:
            nonlocal remaining
            with guard:
                remaining -= 1
                if remaining == 0:
                    done.set()

        for f in futures:
            f.add_done_callback(one)
        return done.wait(timeout_s)

    def snapshot(self) -> dict[str, Any]:
        """Counts only (no file I/O, safe on the event loop)."""
        gate = self.gate
        with self._lock:
            queued = len(self._queue)
        return {
            "mode": str(gate.mode),
            "submitted": self.submitted,
            "refused": self.refused,
            "queued": queued,
            "in_flight": self.in_flight,
            "closed": self.closed,
            "breaker": None if gate.breaker_state is None else str(gate.breaker_state),
            "gate_counters": dict(gate.counters),
        }

    def close(self) -> None:
        """Stop taking reviews; queued ones are cancelled, a running one is abandoned (its own
        deadlines bound it). Outcomes not journaled by then are lost: a restore marks them
        interrupted (fail closed)."""
        with self._lock:
            self.closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------- packet inputs
def _true_ranges(bars: Sequence[BarInput]) -> list[float]:
    return [max(b.high, a.close) - min(b.low, a.close) for a, b in pairwise(bars)]


def packet_inputs_from_bars(
    signal: SignalRecord,
    bars_1m: Sequence[Bar],
    *,
    lookback: int = LOOKBACK_BARS,
    atr_n: int = ATR_N,
) -> PacketInputs:
    """Packet inputs at the signal's decision time from closed 1m bars of its instrument.

    Only bars with ``close_time_ms <= decision_time_ms`` are used; strategy bars are derived
    from complete buckets only. ATR (simple mean of the last `atr_n` true ranges) is used to
    normalize prices and never leaves this function. Features are the signal's own
    ``market_conditions``; the packet allowlist decides what is kept. Too little history raises
    `PacketError` (the gate maps it to ``AI_UNAVAILABLE``)."""
    decision = signal.decision_time_ms
    mine = sorted(
        (
            b
            for b in bars_1m
            if b.market is signal.market
            and b.symbol == signal.symbol
            and b.interval is Interval.M1
            and b.close_time_ms <= decision
        ),
        key=lambda b: b.open_time_ms,
    )
    if signal.timeframe is Interval.M1:
        strategy_bars = mine
    else:
        strategy_bars = [
            b for b in derive_bars(mine, signal.timeframe) if b.close_time_ms <= decision
        ]
    window = [
        BarInput(b.close_time_ms, b.open, b.high, b.low, b.close, b.volume)
        for b in strategy_bars[-lookback:]
    ]
    if len(window) < 2:
        raise PacketError("not enough closed bars before the decision time")
    ranges = _true_ranges(window)[-atr_n:]
    atr = sum(ranges) / len(ranges)
    if not atr > 0:
        raise PacketError("ATR is not positive")
    features = {
        k: v for k, v in signal.market_conditions.items() if isinstance(v, bool | int | float | str)
    }
    return PacketInputs(signal=signal, bars=window, atr=atr, features=features)


def bar_reader_inputs(reader: BarReader, *, lookback: int = LOOKBACK_BARS) -> InputsFactory:
    """Inputs read from the live bar store (`BarReader`: 1m bars with open in [start, end))."""

    def build(signal: SignalRecord) -> PacketInputs:
        step = signal.timeframe.ms
        end = signal.decision_time_ms + 1  # every 1m bar that closed at or before the decision
        start = end - (lookback + ATR_N + 2) * step
        start -= start % step
        return packet_inputs_from_bars(
            signal, list(reader(signal.market, signal.symbol, start, end)), lookback=lookback
        )

    return build


# ---------------------------------------------------------------------- configuration
def agent_gated_enabled(loaded: LoadedConfig) -> bool:
    """Does a paper session started under this agents configuration get the ``agent_gated``
    book? Only mode ``confirmed`` of a valid file (an invalid file is mode ``off``). Mode
    ``research`` reviews non-LIVE signals only, so it never gates paper (whose signals are LIVE)."""
    return loaded.ok and loaded.config.mode is AgentMode.CONFIRMED


def reviewer_factory(
    config_path: Path,
    data_dir: Path,
    clock: WallClock,
    inputs: InputsFactory,
    *,
    env: Mapping[str, str] | None = None,
    dotenv: Path | None = None,
    client: AgentClient | None = None,
) -> Callable[[], Reviewer | None]:
    """The paper runtime's reviewer factory: one gate per runtime (its breaker, counters and
    budget persist across monitoring runs), a fresh worker per run. `client` overrides the
    production client (tests pass a `MockClient`); otherwise `real_client.build_client`, which
    only checks whether a key is present and today returns a stub that is always unavailable.
    Mode other than ``confirmed`` gives no reviewer."""
    gate: AgentGate | None = None

    def make() -> Reviewer | None:
        nonlocal gate
        loaded = load_config(config_path)
        if not agent_gated_enabled(loaded):
            return None
        if gate is None:
            chosen = client if client is not None else build_client(loaded.config, env, dotenv)
            gate = AgentGate(loaded, client=chosen, clock=clock, data_dir=data_dir)
        return ReviewWorker(gate, inputs)

    return make


__all__ = [
    "BarReader",
    "InputsFactory",
    "ReviewWorker",
    "Reviewer",
    "agent_gated_enabled",
    "bar_reader_inputs",
    "packet_inputs_from_bars",
    "reviewer_factory",
]
