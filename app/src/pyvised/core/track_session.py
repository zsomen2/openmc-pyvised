"""Tracked runs through the backend: generate particle tracks of the open model, cancel.

One request at a time: a new one (or `cancel`) makes the backend end the
running tracked run, whose late answer is then ignored.
"""

import logging

from PySide6.QtCore import QObject, Signal

from pyvised_protocol.tracks import TrackData, TrackRequest

from ..backend.client import ApiClient, BackendError, Connection
from .tasks import TaskRunner

log = logging.getLogger(__name__)


class TrackSession(QObject):
    generated = Signal(object)
    """The `TrackData` of the latest request."""
    failed = Signal(str, str)
    """Message and detail of a request that failed."""
    busyChanged = Signal(bool)

    def __init__(self, tasks: TaskRunner, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.tasks = tasks
        self.api: ApiClient | None = None
        self.busy = False
        self._request = 0
        """Counts requests; answers of older ones are dropped."""
        self._model_id = ""

    @property
    def ready(self) -> bool:
        return self.api is not None

    def set_connection(self, connection: Connection | None) -> None:
        if self.api is not None:
            self.api.close()
        self.api = ApiClient(connection) if connection is not None else None
        if self.busy:
            self._request += 1
            self._set_busy(False)

    def generate(self, model_id: str, request: TrackRequest) -> bool:
        if self.api is None:
            return False
        self._request += 1
        number, self._model_id = self._request, model_id
        self._set_busy(True)
        self.tasks.submit(
            self.api.tracks,
            model_id,
            request,
            on_result=lambda data: self._done(number, data),
            on_error=lambda error: self._failed(number, error),
        )
        return True

    def cancel(self) -> None:
        if self.api is None or not self.busy:
            return
        self._request += 1
        self._set_busy(False)
        self.tasks.submit(
            self.api.cancel_tracks,
            self._model_id,
            on_error=lambda error: log.warning("cancelling the tracks failed: %s", error),
        )

    def _set_busy(self, busy: bool) -> None:
        if busy != self.busy:
            self.busy = busy
            self.busyChanged.emit(busy)

    def _done(self, number: int, data: TrackData) -> None:
        if number != self._request:
            return
        self._set_busy(False)
        self.generated.emit(data)

    def _failed(self, number: int, error: Exception) -> None:
        if number != self._request:
            return
        self._set_busy(False)
        code = error.code if isinstance(error, BackendError) else ""
        if code == "cancelled":
            return
        detail = error.detail if isinstance(error, BackendError) and error.detail else str(error)
        messages = {
            "tracks_failed": self.tr("OpenMC could not track the particles."),
            "tracks_timeout": self.tr("Tracking the particles took too long."),
            "bad_track_start": self.tr("The tracks cannot start there."),
            "unknown_model": self.tr("The backend has forgotten the model; reload it."),
        }
        self.failed.emit(messages.get(code, self.tr("The tracks could not be generated.")), detail)
