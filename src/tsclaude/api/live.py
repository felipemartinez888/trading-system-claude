"""Fan-out of push messages to WebSocket clients.

`publish` is thread-safe: it is called from request worker threads (start/stop) and from the
heartbeat. Each subscriber queue remembers the event loop it belongs to, and messages are delivered
with `call_soon_threadsafe`. Slow clients never block producers: when a queue is full the oldest
message is dropped (every message is a full status snapshot, so only the newest matters).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import Any

type LiveMessage = dict[str, Any]


class LiveHub:
    def __init__(self, max_queue: int = 32) -> None:
        self._max_queue = max_queue
        self._lock = threading.Lock()
        self._subscribers: dict[asyncio.Queue[LiveMessage], asyncio.AbstractEventLoop] = {}

    def subscribe(self) -> asyncio.Queue[LiveMessage]:
        """Register a queue bound to the running event loop (call from a coroutine)."""
        queue: asyncio.Queue[LiveMessage] = asyncio.Queue(maxsize=self._max_queue)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._subscribers[queue] = loop
        return queue

    def unsubscribe(self, queue: asyncio.Queue[LiveMessage]) -> None:
        with self._lock:
            self._subscribers.pop(queue, None)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, message: LiveMessage) -> None:
        with self._lock:
            targets = list(self._subscribers.items())
        for queue, loop in targets:
            try:
                loop.call_soon_threadsafe(_offer, queue, message)
            except RuntimeError:  # the subscriber's loop is closed
                self.unsubscribe(queue)


def _offer(queue: asyncio.Queue[LiveMessage], message: LiveMessage) -> None:
    if queue.full():
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    queue.put_nowait(message)
