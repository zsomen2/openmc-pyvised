"""The backend's server-sent events (``GET /events``), read in a background thread.

Events arrive as `received` signals in the GUI thread. When the connection
drops (a backend restart, sleep), the stream reconnects with a growing pause
and reports `connected` again, so the receiver can ask for the full state
of what it follows; events in between are lost.
"""

import logging
import threading

import httpx
from PySide6.QtCore import QObject, Signal

from pyvised_protocol import API_PREFIX
from pyvised_protocol.runs import parse_sse

from .client import Connection

log = logging.getLogger(__name__)

READ_TIMEOUT = 45.0
"""The backend sends a comment every 15 s; silence for this long means a dead connection."""
RETRY_DELAYS = (0.5, 1.0, 2.0, 5.0, 10.0)


class EventStream(QObject):
    received = Signal(object)
    """A `pyvised_protocol.runs.Event`."""
    connected = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, connection: Connection) -> None:
        self.stop()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(connection, self._stop), daemon=True, name="pyvised-events"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()  # ends a blocked read
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def _run(self, connection: Connection, stop: threading.Event) -> None:
        failures = 0
        while not stop.is_set():
            client = httpx.Client(
                base_url=f"{connection.base_url}{API_PREFIX}",
                headers={"Authorization": f"Bearer {connection.token}"},
                timeout=httpx.Timeout(10.0, read=READ_TIMEOUT),
            )
            with self._lock:
                if stop.is_set():
                    client.close()
                    return
                self._client = client
            try:
                with client.stream("GET", "/events") as response:
                    response.raise_for_status()
                    failures = 0
                    self.connected.emit(True)
                    for event in parse_sse(response.iter_lines()):
                        if stop.is_set():
                            return
                        self.received.emit(event)
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                if stop.is_set():
                    return
                log.info("event stream interrupted: %s", exc)
            finally:
                client.close()
            if stop.is_set():
                return
            self.connected.emit(False)
            stop.wait(RETRY_DELAYS[min(failures, len(RETRY_DELAYS) - 1)])
            failures += 1
