"""A dedicated single-worker thread for live-bar batch writes, with an explicit lifecycle.

Why not `asyncio.to_thread`: cancelling a task awaiting a thread does **not** stop the thread, and
the default executor can run several writes at once. Here the rules are explicit:

- exactly one worker thread and **at most one operation outstanding** (`submit` while busy raises
  `WriterBusy`: a caller can never overlap two writes, even after a timeout or a cancellation);
- `busy` is true from `submit` until the operation has returned (commit or rollback), whatever
  happened to the awaiting future, so "the write is over" is a fact of the writer, not of a task;
- a write that cannot be interrupted (a SQLite transaction) is **joined**: `wait_idle` /
  `join` poll until it is over and report whether it was, within a deadline;
- `close()` refuses new work; the worker finishes the operation in flight and exits. The store
  must only be closed after `join()` returned True.

`WriterSink` adapts this to the `BarSink` protocol used by the backfill engine and turns the three
awkward cases (slow write, cancelled caller, stuck writer) into defined outcomes.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from tsclaude.contracts.market import Bar
from tsclaude.store.livebars import Checkpoint, WriteResult

log = logging.getLogger(__name__)

_POLL_S = 0.01


class WriterBusy(RuntimeError):
    """An operation is already outstanding (a bug in the caller: writes never overlap)."""


class WriterClosed(RuntimeError):
    """The writer no longer accepts operations."""


class WriterStuck(RuntimeError):
    """An operation did not finish within the join deadline; the store must stay open."""


class BatchStore(Protocol):
    def write_many(
        self,
        bars: Sequence[Bar],
        observed_session_id: str | None = None,
        *,
        checkpoint: Checkpoint | None = None,
    ) -> list[WriteResult]: ...


class BarSink(Protocol):
    """Where the backfill engine sends one chunk: all bars plus the checkpoint, one transaction."""

    async def write(
        self,
        bars: Sequence[Bar],
        checkpoint: Checkpoint | None,
        observed_session_id: str | None = None,
    ) -> list[WriteResult]: ...


@dataclass
class _Job:
    bars: Sequence[Bar]
    checkpoint: Checkpoint | None
    observed_session_id: str | None
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[list[WriteResult]]


class BarWriter:
    def __init__(self, store: BatchStore, *, name: str = "tsc-bar-writer") -> None:
        self._store = store
        self._cv = threading.Condition()
        self._job: _Job | None = None
        self._closing = False
        # daemon: a writer stuck in I/O must never keep the interpreter alive at exit; an open
        # SQLite transaction is rolled back by WAL recovery, so a hard exit loses at most the batch
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ state
    @property
    def busy(self) -> bool:
        with self._cv:
            return self._job is not None

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def closing(self) -> bool:
        with self._cv:
            return self._closing

    # ------------------------------------------------------------------ commands
    def submit(
        self,
        bars: Sequence[Bar],
        checkpoint: Checkpoint | None = None,
        observed_session_id: str | None = None,
    ) -> asyncio.Future[list[WriteResult]]:
        """Queue one batch; call on the event loop. The future resolves when the batch is over."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[WriteResult]] = loop.create_future()
        with self._cv:
            if self._closing:
                raise WriterClosed("the bar writer is closing")
            if self._job is not None:
                raise WriterBusy("a write is already outstanding")
            self._job = _Job(list(bars), checkpoint, observed_session_id, loop, future)
            self._cv.notify_all()
        return future

    def close(self) -> None:
        """Refuse new work; the operation in flight (if any) still runs to completion."""
        with self._cv:
            self._closing = True
            self._cv.notify_all()

    async def wait_idle(self, timeout: float) -> bool:
        """Wait until no operation is outstanding. True if idle, False if `timeout` elapsed."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.busy:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(_POLL_S)
        return True

    async def join(self, timeout: float) -> bool:
        """`close()`, then wait until the worker thread has exited. True once it has."""
        self.close()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._thread.is_alive():
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(_POLL_S)
        self._thread.join(0)
        return True

    # ------------------------------------------------------------------ worker
    def _run(self) -> None:
        while True:
            with self._cv:
                while self._job is None and not self._closing:
                    self._cv.wait()
                job = self._job
                if job is None:
                    return  # closing and nothing outstanding
            outcome: list[WriteResult] | BaseException
            try:
                outcome = self._store.write_many(
                    job.bars, job.observed_session_id, checkpoint=job.checkpoint
                )
            except BaseException as exc:  # reported to the awaiting task, never raised here
                outcome = exc
            with self._cv:
                self._job = None  # the operation is over: only now may another be submitted
                self._cv.notify_all()
            try:
                job.loop.call_soon_threadsafe(_resolve, job.future, outcome)
            except RuntimeError:  # the loop is already closed (application shutdown)
                log.debug("bar writer: event loop closed before the result was delivered")


def _resolve(future: asyncio.Future[list[WriteResult]], outcome: object) -> None:
    if future.done():  # the awaiting task was cancelled; the write still ran to completion
        return
    if isinstance(outcome, BaseException):
        future.set_exception(outcome)
    else:
        future.set_result(outcome)  # type: ignore[arg-type]


class WriterSink:
    """`BarSink` over a `BarWriter`: timeouts and cancellation never overlap or abandon a write."""

    def __init__(
        self,
        writer: BarWriter,
        *,
        write_timeout_s: float,
        join_timeout_s: float,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self._writer = writer
        self._write_timeout_s = write_timeout_s
        self._join_timeout_s = join_timeout_s
        self._on_event = on_event or (lambda _msg: None)
        self.write_slow = 0
        self.stuck = False

    async def write(
        self,
        bars: Sequence[Bar],
        checkpoint: Checkpoint | None,
        observed_session_id: str | None = None,
    ) -> list[WriteResult]:
        if self.stuck:
            raise WriterStuck("an earlier write never finished; refusing to start another")
        future = self._writer.submit(bars, checkpoint, observed_session_id)
        # an outcome nobody awaits (cancelled caller) must not log "exception never retrieved"
        future.add_done_callback(lambda f: f.cancelled() or f.exception())
        try:
            return await asyncio.wait_for(asyncio.shield(future), self._write_timeout_s)
        except TimeoutError:
            self.write_slow += 1
            self._on_event(
                f"write_slow: batch exceeded {self._write_timeout_s:g} s; joining it (never "
                "abandoned, never overlapped)"
            )
            await self._join_or_stick()
            # the batch's own result or exception (delivered to the loop a moment after it ended)
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # The caller was cancelled while the batch may still be running: it cannot be
            # interrupted, so wait for it here, then let the cancellation continue.
            await self._join_or_stick()
            raise

    async def _join_or_stick(self) -> None:
        if await self._writer.wait_idle(self._join_timeout_s):
            return
        self.stuck = True
        self._on_event(
            f"writer_stuck: a batch did not finish within {self._join_timeout_s:g} s; the live bar "
            "store will be left open"
        )
        raise WriterStuck(f"a write did not finish within {self._join_timeout_s:g} s")


class InlineSink:
    """Synchronous sink on the calling thread, for unit tests and one-off tools (never the app:
    it blocks the event loop for the duration of each transaction)."""

    def __init__(self, store: BatchStore) -> None:
        self._store = store

    async def write(
        self,
        bars: Sequence[Bar],
        checkpoint: Checkpoint | None,
        observed_session_id: str | None = None,
    ) -> list[WriteResult]:
        return self._store.write_many(bars, observed_session_id, checkpoint=checkpoint)
