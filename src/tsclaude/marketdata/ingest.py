"""Ingestion lifecycle: bounded startup catch-up, on-demand history, deterministic Stop.

One `IngestionCoordinator` per monitoring run (rebuilt at every Start). It owns

- the **runner**: a single asyncio task that works through the catch-up job and then, when the
  history policy asks for it (or an operator/consumer requests it), the history job. Pairs and
  chunks are processed strictly one after the other, so the workload in flight is one REST page
  and one SQLite transaction, never more;
- the **writer**: one dedicated `BarWriter` thread. Cancelling an asyncio task does not stop a
  thread, so Stop is explicit: ask the runner to finish its current chunk (`stop_grace_s`), cancel
  it if it does not (a REST fetch is safe to cancel; a write is joined by the sink), then join the
  writer thread. Only then may the owner close the SQLite connection. A writer that does not finish
  within `write_join_timeout_s` is reported `stuck` and the store is left open (`safe_to_close`
  False): closing under a running transaction is the one thing that must never happen;
- the **status** the dashboard reads (`status()`), including an honest `stopping` / `stuck` state.

It never decides trading readiness: `history_complete()` only reports whether the requested
window was fully loaded, and consumers that need it fail closed when it is not.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict
from typing import Any

from tsclaude.contracts.market import Market
from tsclaude.contracts.timeutil import MS_PER_DAY
from tsclaude.marketdata.backfill import (
    Backfiller,
    BackfillResult,
    BackfillSettings,
    PairProgress,
    RunLimits,
)
from tsclaude.marketdata.bars import KlineFetcher
from tsclaude.marketdata.config import BackfillConfig
from tsclaude.marketdata.writer import BarWriter, WriterSink
from tsclaude.quality.gaps import GapRegistry
from tsclaude.runtime.sessions import ObservationInterval
from tsclaude.store.livebars import LiveBarStore

log = logging.getLogger(__name__)

MS_PER_MIN = 60_000
CATCHUP, HISTORY = "catchup", "history"
Pair = tuple[Market, str]

# coordinator phases
IDLE, RUNNING, STOPPING, STOPPED, STUCK = "idle", "running", "stopping", "stopped", "stuck"
# history states
H_IDLE, H_PENDING, H_RUNNING, H_COMPLETE, H_PARTIAL, H_FAILED, H_CANCELLED = (
    "not_requested",
    "pending",
    "running",
    "complete",
    "incomplete",
    "failed",
    "cancelled",
)


class HistoryIncomplete(RuntimeError):
    """A consumer required the history window and it is not fully loaded (fail closed)."""


class IngestionCoordinator:
    def __init__(
        self,
        *,
        bars: LiveBarStore,
        gaps: GapRegistry,
        fetch: KlineFetcher,
        emit: Callable[[Any], Any],
        now_ms: Callable[[], int],
        config: BackfillConfig,
        pairs: Callable[[], Sequence[Pair]],
        intervals: Callable[[], Sequence[ObservationInterval]],
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bars = bars
        self._gaps = gaps
        self._fetch = fetch
        self._emit = emit
        self._now = now_ms
        self._cfg = config
        self._pairs = pairs
        self._intervals = intervals
        self._mono = monotonic
        self._sleep = sleep
        self.phase = IDLE
        self.results: list[BackfillResult] = []
        self.progress: dict[tuple[str, Market, str], PairProgress] = {}
        self.events: list[str] = []  # operator-visible notes (write_slow, writer_stuck, ...)
        self.catchup_done = asyncio.Event()
        self._history_done = asyncio.Event()
        self._history_state = H_IDLE
        self._history_requested = False
        self._stop = False
        self._end_ms = 0
        self._runner: asyncio.Task[None] | None = None
        self._writer: BarWriter | None = None
        self._sink: WriterSink | None = None
        self._run_deadline: float | None = None

    # ------------------------------------------------------------------ policy
    @property
    def legacy(self) -> bool:
        """No catch-up cap configured: the whole lookback is the catch-up (pre-S1 behavior)."""
        return self._cfg.startup.catchup_max_minutes is None

    def _limits(self) -> RunLimits:
        r = self._cfg.run
        return RunLimits(
            chunk_minutes=r.chunk_minutes,
            pause_s=r.inter_chunk_pause_ms / 1000,
            chunk_timeout_s=r.chunk_timeout_s,
            max_consecutive_errors=r.max_consecutive_chunk_errors,
            pair_budget_s=r.pair_budget_s,
        )

    def _settings(self, job: str) -> BackfillSettings:
        c = self._cfg
        max_ms = int(c.max_backfill_days * MS_PER_DAY)
        if job == CATCHUP:
            cap = c.startup.catchup_max_minutes
            if cap is None:  # legacy: whole lookback, extended to the last stored bar
                return BackfillSettings(int(c.lookback_days * MS_PER_DAY), max_ms)
            return BackfillSettings(cap * MS_PER_MIN, max_ms, extend_to_last_stored=False)
        return BackfillSettings(
            int(c.lookback_days * MS_PER_DAY), max_ms, extend_to_last_stored=False
        )

    # ------------------------------------------------------------------ lifecycle
    def start(self, end_ms: int) -> None:
        """Begin the catch-up (and, per policy, the history) in the background. Returns at once."""
        if self.phase != IDLE:
            raise RuntimeError(f"ingestion already {self.phase}")
        self._end_ms = end_ms
        self._writer = BarWriter(self._bars)
        r = self._cfg.run
        self._sink = WriterSink(
            self._writer,
            write_timeout_s=r.write_timeout_s,
            join_timeout_s=r.write_join_timeout_s,
            on_event=self._note,
        )
        policy = self._cfg.startup.history
        if not self.legacy and policy == "background":
            self._history_requested = True
            self._history_state = H_PENDING
        self.phase = RUNNING
        self._runner = asyncio.create_task(self._run(), name="md-ingest")

    def request_history(self) -> str:
        """Ask for the history window (idempotent). Returns what happened:
        started | already_running | complete | not_needed | refused (history policy off)."""
        if self.phase != RUNNING or self._sink is None:
            return "refused"
        if self.legacy:
            return "not_needed"  # the catch-up already covers the whole lookback
        if self._cfg.startup.history == "off":
            return "refused"
        if self._history_state == H_COMPLETE:
            return "complete"
        if self._history_requested:
            return "already_running"
        self._history_requested = True
        self._history_state = H_PENDING
        self._history_done.clear()
        if self._runner is not None and self._runner.done() and self.phase == RUNNING:
            self._runner = asyncio.create_task(self._run(), name="md-ingest-history")
        return "started"

    async def wait_catchup(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self.catchup_done.wait(), timeout)

    async def wait_history(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._history_done.wait(), timeout)

    def catchup_complete(self) -> bool:
        return self._jobs_complete(CATCHUP)

    def history_complete(self) -> bool:
        """The whole `lookback_days` window is loaded for every pair (fail closed otherwise)."""
        if self.legacy:
            return self.catchup_done.is_set() and self.catchup_complete()
        return self._history_state == H_COMPLETE

    def _jobs_complete(self, job: str) -> bool:
        pairs = list(self._pairs())
        return bool(pairs) and all(
            (p := self.progress.get((job, m, s))) is not None and p.state == "complete"
            for m, s in pairs
        )

    async def stop(self) -> bool:
        """Stop deterministically. Returns True when the writer is idle/joined and the store is
        safe to close; False when the writer is stuck (the caller must leave the store open)."""
        if self.phase in (IDLE, STOPPED):
            self.phase = STOPPED
            self._history_done.set()
            self.catchup_done.set()
            return self.safe_to_close()
        self.phase = STOPPING if self.phase != STUCK else STUCK
        self._stop = True
        runner = self._runner
        if runner is not None and not runner.done():
            grace = self._cfg.run.stop_grace_s
            done, _ = await asyncio.wait({runner}, timeout=grace)
            if not done:  # still in a REST fetch (or paused): cancel it; a write joins itself
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
        joined = True
        if self._writer is not None:
            joined = await self._writer.join(self._cfg.run.write_join_timeout_s)
        if not joined or (self._sink is not None and self._sink.stuck):
            self.phase = STUCK
            self._note("writer_stuck: the bar writer did not exit; the live bar store stays open")
            log.error("bar writer did not exit; leaving the live bar store open")
        else:
            self.phase = STOPPED
        self.catchup_done.set()  # nobody waits on a run that no longer exists
        self._history_done.set()
        return self.safe_to_close()

    def safe_to_close(self) -> bool:
        """The SQLite connection may be closed: no writer thread can still be running."""
        return self._writer is None or (not self._writer.alive and not self._writer.busy)

    # ------------------------------------------------------------------ runner
    async def _run(self) -> None:
        assert self._sink is not None
        try:
            if not self.catchup_done.is_set():
                self._run_deadline = self._mono() + self._cfg.run.run_budget_s
                try:
                    await self._job(CATCHUP)
                finally:
                    self.catchup_done.set()
            if self._history_requested and not self._stop and self._history_state != H_COMPLETE:
                self._history_state = H_RUNNING
                self._run_deadline = self._mono() + self._cfg.run.run_budget_s
                try:
                    await self._job(HISTORY)
                finally:
                    self._finish_history()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ingestion run failed")

    def _finish_history(self) -> None:
        states = [self.progress.get((HISTORY, m, s)) for m, s in self._pairs()]
        if self._stop:
            self._history_state = H_CANCELLED
        elif states and all(p is not None and p.state == "complete" for p in states):
            self._history_state = H_COMPLETE
        elif any(p is not None and p.state == "failed" for p in states):
            self._history_state = H_FAILED
        else:
            self._history_state = H_PARTIAL
        self._history_requested = False  # a retry (after partial/failed) may be requested again
        self._history_done.set()

    async def _job(self, job: str) -> None:
        assert self._sink is not None
        backfiller = Backfiller(
            self._bars,
            self._gaps,
            self._fetch,
            self._emit,
            self._now,
            self._settings(job),
            sink=self._sink,
            limits=self._limits(),
            job=job,
            monotonic=self._mono,
            sleep=self._sleep,
        )
        intervals = list(self._intervals())
        for market, symbol in self._pairs():
            if self._stop:
                return
            prog = self.progress.setdefault(
                (job, market, symbol), PairProgress(market, symbol, job)
            )
            try:
                res = await backfiller.run(
                    market,
                    symbol,
                    self._end_ms,
                    intervals,
                    progress=prog,
                    should_stop=lambda: self._stop,
                    deadline=self._run_deadline,
                )
                self.results.append(res)
            except asyncio.CancelledError:
                prog.state = "cancelled"
                raise
            except Exception as exc:
                prog.state, prog.last_error = "failed", f"{type(exc).__name__}: {exc}"
                log.exception(
                    "ingestion failed", extra={"job": job, "market": str(market), "symbol": symbol}
                )
                if self._sink is not None and self._sink.stuck:
                    return  # nothing more may be written
            if self._run_deadline is not None and self._mono() >= self._run_deadline:
                self._note(f"{job}: run budget reached; remaining pairs resume on the next run")
                for m, s in self._pairs():
                    self.progress.setdefault((job, m, s), PairProgress(m, s, job))
                return

    def _note(self, message: str) -> None:
        self.events.append(message)
        del self.events[:-50]
        log.warning("ingestion: %s", message)

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        sink, writer = self._sink, self._writer
        return {
            "phase": self.phase,
            "catchup_done": self.catchup_done.is_set(),
            "history": {
                "policy": "legacy" if self.legacy else self._cfg.startup.history,
                "state": self._history_state,
                "complete": self.history_complete(),
            },
            "writer": {
                "alive": writer.alive if writer is not None else False,
                "busy": writer.busy if writer is not None else False,
                "write_slow": sink.write_slow if sink is not None else 0,
                "stuck": sink.stuck if sink is not None else False,
            },
            "safe_to_close_store": self.safe_to_close(),
            "limits": {
                "chunk_minutes": self._cfg.run.chunk_minutes,
                "pair_budget_s": self._cfg.run.pair_budget_s,
                "run_budget_s": self._cfg.run.run_budget_s,
            },
            "pairs": [asdict(p) | {"market": str(p.market)} for p in self.progress.values()],
            "notes": list(self.events[-10:]),
        }
