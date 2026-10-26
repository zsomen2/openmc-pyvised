"""Server-sent events: the run manager publishes from its threads, ``GET /events`` streams.

Each subscriber gets a bounded queue on the event loop. A subscriber that
falls too far behind is dropped; the client reconnects and asks for the
run's full status. A comment line every `HEARTBEAT` seconds keeps idle
connections open and lets the server notice clients that went away.
"""

import asyncio
import contextlib
import logging
import threading
from collections.abc import AsyncIterator

from pyvised_protocol.runs import Event, format_sse

log = logging.getLogger(__name__)

HEARTBEAT = 15.0
QUEUE_SIZE = 2000
_CLOSED = b""


class EventHub:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: set[asyncio.Queue[bytes]] = set()
        self._lock = threading.Lock()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """The loop the queues live on (the server's, at start-up)."""
        self._loop = loop

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._queues)

    def publish(self, event: Event) -> None:
        """Hand `event` to every subscriber; safe to call from any thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        data = format_sse(event)
        with self._lock:
            queues = list(self._queues)
        for queue in queues:
            try:
                loop.call_soon_threadsafe(self._put, queue, data)
            except RuntimeError:  # the loop is shutting down
                return

    def _put(self, queue: asyncio.Queue[bytes], data: bytes) -> None:
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            log.warning("an event subscriber fell behind and was dropped")
            with self._lock:
                self._queues.discard(queue)
            while not queue.empty():  # make room for the end marker
                queue.get_nowait()
            queue.put_nowait(_CLOSED)

    async def stream(self, heartbeat: float = HEARTBEAT) -> AsyncIterator[bytes]:
        """Encoded events for one subscriber, with heartbeat comments in between."""
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue(QUEUE_SIZE)
        with self._lock:
            self._queues.add(queue)
        try:
            yield b": connected\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), heartbeat)
                except TimeoutError:
                    yield b": ping\n\n"
                    continue
                if data == _CLOSED:
                    return
                yield data
        finally:
            with self._lock:
                self._queues.discard(queue)

    def close(self) -> None:
        """End every stream (server shutdown)."""
        loop = self._loop
        with self._lock:
            queues = list(self._queues)
        if loop is None or loop.is_closed():
            return
        for queue in queues:
            with contextlib.suppress(RuntimeError):  # the loop is already closing
                loop.call_soon_threadsafe(self._put, queue, _CLOSED)
