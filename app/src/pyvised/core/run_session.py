"""Simulation runs through the backend: start, stop, live progress, history and results.

The backend runs one simulation at a time and keeps running it when the app
only reconnects. The session follows the backend's current run (`current`,
also one of another model), accumulates its progress and log from the event
stream, and on every (re)connection asks for the full state, because events
sent meanwhile are lost. The history lists the runs of the open model.
Results and tally arrays of finished runs are cached.
"""

import logging
import math
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from pyvised_protocol.runs import (
    Event,
    LogChunk,
    RunInfo,
    RunProgress,
    RunRequest,
    RunResults,
    RunStatus,
)

from ..backend.client import ApiClient, BackendError, Connection, TallyData
from ..backend.events import EventStream
from .tasks import TaskRunner

log = logging.getLogger(__name__)

MAX_LOG_CHARS = 4_000_000
"""Live log text kept in memory; older text is dropped (the file has all of it)."""
HISTORY_POLL_MS = 10_000
"""History refresh while it lists a run nobody sends events about (run by another backend)."""


def progress_from_results(results: RunResults) -> RunProgress:
    """The live view of a finished run, rebuilt from its statepoint: k per generation, the
    entropy, and the running mean ± σ of the active generations (as OpenMC prints it)."""
    k = list(results.k_generation)
    first = results.inactive * max(1, results.generations_per_batch)
    mean: list[float | None] = []
    std: list[float | None] = []
    total = squares = 0.0
    for index, value in enumerate(k):
        if index < first:
            mean.append(None)
            std.append(None)
            continue
        n = index - first + 1
        total += value
        squares += value * value
        average = total / n
        mean.append(average if n > 1 else None)
        variance = (squares / n - average * average) / (n - 1) if n > 1 else 0.0
        std.append(math.sqrt(max(variance, 0.0)) if n > 1 else None)
    return RunProgress(
        k=k,
        entropy=list(results.entropy),
        mean=mean,
        std=std,
        batch=results.batches,
        batches=results.batches,
        inactive=results.inactive,
        generations_per_batch=results.generations_per_batch,
        elapsed=results.runtime.get("total", 0.0),
        rate=results.rate_active,
        statepoint=results.batches,
    )


def merge_progress(progress: RunProgress, update: RunProgress) -> bool:
    """Add an event's new generations to `progress`; False when some were missed."""
    if update.first > len(progress.k):
        return False
    for name in ("k", "entropy", "mean", "std"):
        values = getattr(progress, name)
        new = getattr(update, name)
        start = update.first if name != "entropy" or values or not update.first else len(values)
        del values[start:]
        values.extend(new)
    for name in (
        "batch",
        "batches",
        "inactive",
        "generations_per_batch",
        "elapsed",
        "eta",
        "rate",
        "statepoint",
        "trigger",
        "phase",
    ):
        setattr(progress, name, getattr(update, name))
    return True


class RunSession(QObject):
    currentChanged = Signal(object)
    """The `RunInfo` the session follows changed (started, new state, finished) or None."""
    progressChanged = Signal(object)
    """The accumulated `RunProgress` of the current run."""
    logReset = Signal(str)
    """The current run's whole log so far."""
    logAppended = Signal(str)
    historyChanged = Signal()
    finished = Signal(object)
    """A followed run ended (`RunInfo`)."""
    startFailed = Signal(str, str)
    """Message and detail of a run that could not start."""
    busyChanged = Signal(bool)
    """A run is being started (the backend exports the model first)."""

    def __init__(self, tasks: TaskRunner, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.tasks = tasks
        self.api: ApiClient | None = None
        self.model: str | None = None
        """Workspace-relative path of the open model (its history is shown)."""
        self.current: RunInfo | None = None
        self.progress = RunProgress()
        self.history: list[RunInfo] = []
        self.starting = False
        self.current_log = ""
        """The followed run's log so far (the newest `MAX_LOG_CHARS`)."""
        self._log_offset = 0
        self._log_fetch: str | None = None
        """The run whose log is being read (one request at a time)."""
        self._log_refetch = False
        """Text was missed while reading: read on from the new end afterwards."""
        self._status_pending: set[str] = set()
        self._results: dict[str, RunResults] = {}
        self._tallies: dict[tuple[str, int], TallyData] = {}
        self._history_timer = QTimer(self)
        self._history_timer.setInterval(HISTORY_POLL_MS)
        self._history_timer.timeout.connect(self.refresh_history)
        self.events = EventStream(self)
        self.events.received.connect(self._event)
        self.events.connected.connect(self._connected)

    @property
    def ready(self) -> bool:
        return self.api is not None

    @property
    def running(self) -> bool:
        return self.starting or (self.current is not None and self.current.active)

    def set_connection(self, connection: Connection | None) -> None:
        self.events.stop()
        if self.api is not None:
            self.api.close()
        self.api = ApiClient(connection) if connection is not None else None
        self._results.clear()
        self._tallies.clear()
        if connection is not None:
            self.events.start(connection)
        elif self.current is not None and self.current.active:
            self.current = None
            self.currentChanged.emit(None)

    def shutdown(self) -> None:
        self._history_timer.stop()
        self.events.stop()

    def set_model(self, model: str | None) -> None:
        if model != self.model:
            self.model = model
            self.history = []
            self.historyChanged.emit()
        self.refresh_history()

    # -- requests ---------------------------------------------------------------------------

    def _submit(
        self,
        func: Callable[..., Any],
        *args: Any,
        on_result: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> bool:
        if self.api is None:
            return False
        self.tasks.submit(func, *args, on_result=on_result, on_error=on_error or self._log_error)
        return True

    @staticmethod
    def _log_error(error: Exception) -> None:
        log.warning("run request failed: %s", error)

    def start(self, request: RunRequest) -> bool:
        if self.api is None or self.running:
            return False
        self.starting = True
        self.busyChanged.emit(True)
        return self._submit(
            self.api.start_run, request, on_result=self._started, on_error=self._start_failed
        )

    def _started(self, info: RunInfo) -> None:
        self.starting = False
        self.busyChanged.emit(False)
        if self.current is None or self.current.id != info.id or not self._has_progress():
            self._follow(info, RunProgress(batches=info.plan.batches if info.plan else 0))
        self.refresh_history()

    def _start_failed(self, error: Exception) -> None:
        self.starting = False
        self.busyChanged.emit(False)
        detail = error.detail if isinstance(error, BackendError) and error.detail else str(error)
        code = error.code if isinstance(error, BackendError) else ""
        messages = {
            "run_active": self.tr("Another simulation is running."),
            "build_failed": self.tr("The model script stopped with an error."),
            "build_timeout": self.tr("The model script took too long."),
            "bad_settings": self.tr("The run settings do not fit the model."),
            "incomplete_settings": self.tr("The run settings are incomplete."),
            "not_found": self.tr("The model file was not found in the workspace."),
        }
        self.startFailed.emit(
            messages.get(code, self.tr("The simulation could not start.")), detail
        )

    def stop(self, on_error: Callable[[Exception], None] | None = None) -> bool:
        if self.api is None or self.current is None or not self.current.active:
            return False
        return self._submit(self.api.stop_run, self.current.id, on_error=on_error)

    def refresh_history(self) -> None:
        if self.api is None or self.model is None:
            return
        model = self.model
        self._submit(self.api.runs, model, on_result=lambda runs: self._history_loaded(model, runs))

    def _history_loaded(self, model: str, runs: list[RunInfo]) -> None:
        if model != self.model:
            return
        self.history = runs
        self.historyChanged.emit()
        current = self.current.id if self.current is not None else None
        if any(run.active and run.id != current for run in runs):
            if not self._history_timer.isActive():
                self._history_timer.start()
        else:
            self._history_timer.stop()

    def delete(self, run_id: str, on_done: Callable[[], None] | None = None) -> None:
        def deleted(_value: Any) -> None:
            self._results.pop(run_id, None)
            for key in [k for k in self._tallies if k[0] == run_id]:
                del self._tallies[key]
            if self.current is not None and self.current.id == run_id:
                self.current = None
                self.currentChanged.emit(None)
            self.refresh_history()
            if on_done is not None:
                on_done()

        if self.api is not None:
            self._submit(self.api.delete_run, run_id, on_result=deleted)

    def results(
        self,
        run_id: str,
        on_result: Callable[[RunResults], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        cached = self._results.get(run_id)
        if cached is not None:
            on_result(cached)
            return

        def loaded(results: RunResults) -> None:
            self._results[run_id] = results
            on_result(results)

        if self.api is not None:
            self._submit(self.api.run_results, run_id, on_result=loaded, on_error=on_error)

    def cached_results(self, run_id: str) -> RunResults | None:
        return self._results.get(run_id)

    def tally(
        self,
        run_id: str,
        tally_id: int,
        on_result: Callable[[TallyData], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        key = (run_id, tally_id)
        cached = self._tallies.get(key)
        if cached is not None:
            on_result(cached)
            return

        def loaded(data: TallyData) -> None:
            self._tallies[key] = data
            on_result(data)

        if self.api is not None:
            self._submit(self.api.tally_data, run_id, tally_id, on_result=loaded, on_error=on_error)

    def log_text(self, run_id: str, on_result: Callable[[str], None]) -> None:
        """The whole log of a run (several requests for a long one)."""
        api = self.api
        if api is None:
            return

        def read() -> str:
            parts, offset = [], 0
            while True:
                chunk: LogChunk = api.run_log(run_id, offset)
                parts.append(chunk.text)
                if chunk.next >= chunk.size or chunk.next == offset:
                    return "".join(parts)
                offset = chunk.next

        self._submit(read, on_result=on_result)

    # -- following the current run --------------------------------------------------------------

    def _has_progress(self) -> bool:
        return bool(self.progress.k) or self.progress.batch > 0

    def _follow(self, info: RunInfo, progress: RunProgress | None = None) -> None:
        previous = self.current
        new = previous is None or previous.id != info.id
        if not new and previous is not None and not previous.active and info.active:
            return  # a late answer: the run has ended since
        self.current = info
        if progress is not None:
            self.progress = progress
        elif new:
            self.progress = RunProgress()
        self.currentChanged.emit(info)
        self.progressChanged.emit(self.progress)
        if new:
            self._log_offset = 0
            self.current_log = ""
            self.logReset.emit("")
            self._fetch_log(info.id)
        elif previous is not None and previous.active and not info.active:
            self._fetch_log(info.id)  # the end of the log, in case its events were missed

    def _fetch_log(self, run_id: str) -> None:
        """Read the followed run's log from what the session has on (one request at a time)."""
        api = self.api
        if api is None:
            return
        if self._log_fetch == run_id:
            self._log_refetch = True
            return
        self._log_fetch, self._log_refetch = run_id, False
        start = self._log_offset

        def read() -> LogChunk:
            parts, offset = [], start
            while True:
                chunk = api.run_log(run_id, offset)
                parts.append(chunk.text)
                if chunk.next >= chunk.size or chunk.next == offset:
                    text = "".join(parts)
                    return LogChunk(offset=start, text=text, next=chunk.next, size=chunk.size)
                offset = chunk.next

        def failed(error: Exception) -> None:
            if self._log_fetch == run_id:
                self._log_fetch = None
            self._log_error(error)

        self._submit(read, on_result=lambda chunk: self._log_loaded(run_id, chunk), on_error=failed)

    def _log_loaded(self, run_id: str, chunk: LogChunk) -> None:
        if self._log_fetch == run_id:
            self._log_fetch = None
        if self.current is None or self.current.id != run_id:
            return
        if chunk.offset == 0 and chunk.next >= self._log_offset:  # all of it
            self._log_offset = chunk.next
            self.current_log = chunk.text[-MAX_LOG_CHARS:]
            self.logReset.emit(self.current_log)
        elif not self._append_log(chunk.offset, chunk.text):
            self._log_refetch = True
        if self._log_refetch:
            self._fetch_log(run_id)

    def _append_log(self, offset: int, text: str) -> bool:
        """Add what the session lacks of `text`, the log from byte `offset` on; False when it
        starts after the session's end (text in between was missed)."""
        if offset > self._log_offset:
            return False
        data = text.encode("utf-8")
        if offset + len(data) > self._log_offset:
            new = data[self._log_offset - offset :].decode("utf-8", errors="replace")
            self._log_offset = offset + len(data)
            self.current_log = (self.current_log + new)[-MAX_LOG_CHARS:]
            self.logAppended.emit(new)
        return True

    def _request_status(self, run_id: str) -> None:
        """Ask for a run's whole state, unless that is already asked."""
        if self.api is None or run_id in self._status_pending:
            return
        self._status_pending.add(run_id)

        def loaded(status: RunStatus) -> None:
            self._status_pending.discard(run_id)
            self._status_loaded(status)

        def failed(error: Exception) -> None:
            self._status_pending.discard(run_id)
            self._log_error(error)

        self._submit(self.api.run_status, run_id, on_result=loaded, on_error=failed)

    def _connected(self, up: bool) -> None:
        if not up or self.api is None:
            return
        # events sent while the stream was down are lost: ask for the whole state
        self._submit(self.api.active_run, on_result=self._active_loaded)
        self.refresh_history()

    def _active_loaded(self, status: RunStatus | None) -> None:
        if status is not None:
            self._follow(status.run, status.progress or RunProgress())
        elif self.current is not None and self.current.active:
            self._request_status(self.current.id)  # it ended while the stream was down

    def _status_loaded(self, status: RunStatus) -> None:
        was_active = self.current is not None and self.current.active
        self._follow(status.run, status.progress)
        if was_active and not status.run.active:
            self.finished.emit(status.run)
            self.refresh_history()

    def _event(self, event: Event) -> None:
        run_id = event.run_id
        if run_id is None:
            return
        if event.type in ("run.state", "run.finished"):
            info = RunInfo.model_validate(event.data)
            following = self.current is not None and self.current.id == run_id
            if following or event.type == "run.state":
                self._follow(info)
            if event.type == "run.finished":
                self._results.pop(run_id, None)
                self.finished.emit(info)
                self.refresh_history()
            elif not following:
                self.refresh_history()
            return
        if self.current is None or self.current.id != run_id:
            if event.type == "run.progress":
                # a run started elsewhere (another client, or before a reconnection)
                self._request_status(run_id)
            return
        if event.type == "run.progress":
            update = RunProgress.model_validate(event.data)
            if merge_progress(self.progress, update):
                self.progressChanged.emit(self.progress)
            else:
                self._request_status(run_id)
        elif event.type == "run.log":
            text, offset = str(event.data.get("text", "")), int(event.data.get("offset", 0))
            if not self._append_log(offset, text):
                self._fetch_log(run_id)
