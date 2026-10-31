"""The open model: opening it through the backend, slice requests, watching its files.

Models must live in the workspace, the folder the backend container sees as
``/workspace``. The files are watched on the Windows side (inotify does not
cross Docker Desktop bind mounts), so editing a builder in VS Code can reload
the geometry.
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from pyvised_protocol.schemas import ModelInfo, OpenModelRequest, SliceRequest
from pyvised_protocol.sidecar import SIDECAR_NAME, update_sidecar

from ..backend.client import ApiClient, BackendError, Connection
from .tasks import TaskRunner

log = logging.getLogger(__name__)

MODEL_SUFFIXES = (".py", ".ipynb", ".xml")
WATCHED_SUFFIXES = {".py", ".ipynb", ".xml", ".json", ".csv", ".txt", ".dat", ".yaml", ".yml"}
"""Files in the model's folder whose changes mean the model may have changed."""
CHANGE_DELAY_MS = 500


def relative_to_workspace(path: Path, workspace: Path) -> str | None:
    """`path` relative to `workspace` with forward slashes, or None when it is outside."""
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return None


def folder_signature(source: Path) -> tuple[tuple[str, int, int], ...]:
    """Name, mtime and size of the files that may feed the model (not the sidecar)."""
    entries = []
    try:
        candidates = sorted(source.parent.iterdir())
    except OSError:
        return ()
    for path in candidates:
        if path.name == SIDECAR_NAME or path.suffix.lower() not in WATCHED_SUFFIXES:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_file():
            entries.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(entries)


class ModelSession(QObject):
    opening = Signal(str)
    """File name of the model being opened."""
    opened = Signal(object, bool)
    """ModelInfo, and whether it is the same model reloaded."""
    failed = Signal(str, str)
    """Message and detail (builder output, OpenMC error)."""
    filesChanged = Signal()

    def __init__(self, tasks: TaskRunner, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.tasks = tasks
        self.workspace: Path | None = None
        self.path: Path | None = None
        self.info: ModelInfo | None = None
        self.generation = 0
        """Counts opened models; results of older slice requests are dropped."""
        self.busy = False
        self._api: ApiClient | None = None
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._schedule_check)
        self._watcher.directoryChanged.connect(self._schedule_check)
        self._check_timer = QTimer(self, singleShot=True, interval=CHANGE_DELAY_MS)
        self._check_timer.timeout.connect(self._check_files)
        self._signature: tuple = ()

    @property
    def ready(self) -> bool:
        return self._api is not None

    def set_connection(self, connection: Connection | None, workspace: Path) -> None:
        """Called when the backend becomes ready (or goes away)."""
        if self._api is not None:
            self._api.close()
        self._api = ApiClient(connection) if connection is not None else None
        self.workspace = workspace

    def relative_path(self, path: Path) -> str | None:
        return None if self.workspace is None else relative_to_workspace(path, self.workspace)

    def open(self, path: Path, params: dict[str, float] | None = None) -> None:
        if self._api is None:
            self.failed.emit(self.tr("The OpenMC backend is not ready yet."), "")
            return
        relative = self.relative_path(path)
        if relative is None:
            self.failed.emit(
                self.tr("The model is not inside the workspace."),
                self.tr(
                    "PyVised opens models from the workspace folder {workspace}, the folder "
                    "the OpenMC backend can see. Copy the model's folder there, or switch "
                    "the workspace (File menu)."
                ).format(workspace=self.workspace),
            )
            return
        self.busy = True
        self.opening.emit(path.name)
        request = OpenModelRequest(path=relative, params=params or {})
        self.tasks.submit(
            self._api.open_model,
            request,
            on_result=lambda info: self._opened(path, info),
            on_error=self._failed,
        )

    def reload(self) -> None:
        if self.path is not None:
            params = self.info.params if self.info is not None else None
            self.open(self.path, params)

    def request_slice(
        self,
        request: SliceRequest,
        on_result: Callable[[Any], None],
        on_error: Callable[[Exception], None],
    ) -> bool:
        if self._api is None or self.info is None:
            return False
        self.tasks.submit(
            self._api.slice, self.info.model_id, request, on_result=on_result, on_error=on_error
        )
        return True

    def inspect(
        self,
        point: tuple[float, float, float],
        on_result: Callable[[Any], None],
        on_error: Callable[[Exception], None],
    ) -> bool:
        """Ask for the nesting path at `point` (an `InspectResult`)."""
        if self._api is None or self.info is None:
            return False
        self.tasks.submit(
            self._api.inspect, self.info.model_id, point, on_result=on_result, on_error=on_error
        )
        return True

    def update_sidecar(self, changes: dict[str, Any]) -> None:
        """Write `changes` to the open model's sidecar and keep `info.sidecar` in step.

        Raises OSError or ValueError when the file cannot be written.
        """
        if self.path is None or self.info is None:
            return
        self.info.sidecar = update_sidecar(self.path.parent, changes)

    # -- results ---------------------------------------------------------------

    def _opened(self, path: Path, info: ModelInfo) -> None:
        self.busy = False
        same = self.path is not None and self.path == path
        self.path, self.info = path, info
        self.generation += 1
        self._watch(path)
        log.info("opened %s as %s (%.1f s)", info.source, info.model_id, info.build_seconds)
        self.opened.emit(info, same)

    def _failed(self, error: Exception) -> None:
        self.busy = False
        if not isinstance(error, BackendError):
            self.failed.emit(self.tr("The model could not be opened."), str(error))
            return
        messages = {
            "build_failed": self.tr("The model script stopped with an error."),
            "build_timeout": self.tr("The model script took too long."),
            "no_export": self.tr("The model script exported no model."),
            "openmc_error": self.tr("OpenMC could not load the model."),
            "info_failed": self.tr("OpenMC's Python API could not read the model."),
            "bad_xml": self.tr("The model's XML is not valid."),
            "incomplete_xml": self.tr("Some XML files of the model are missing."),
            "not_found": self.tr("The model file was not found in the workspace."),
            "unsupported": self.tr("This kind of file cannot be opened."),
        }
        message = messages.get(error.code, self.tr("The model could not be opened."))
        self.failed.emit(message, error.detail or str(error))

    # -- file watching -----------------------------------------------------------

    def _watch(self, path: Path) -> None:
        watched = self._watcher.files() + self._watcher.directories()
        if watched:
            self._watcher.removePaths(watched)
        self._watcher.addPaths([str(path), str(path.parent)])
        self._signature = folder_signature(path)

    def accept_file_changes(self) -> None:
        """Take the model folder's files as they are now, after the app replaced them itself
        (and opens the model again): no "files changed" for that."""
        if self.path is not None:
            self._signature = folder_signature(self.path)

    def _schedule_check(self, *args) -> None:
        self._check_timer.start()

    def _check_files(self) -> None:
        if self.path is None:
            return
        # editors save by replacing the file, which drops it from the watcher
        if str(self.path) not in self._watcher.files() and self.path.exists():
            self._watcher.addPath(str(self.path))
        signature = folder_signature(self.path)
        if signature != self._signature:
            self._signature = signature
            self.filesChanged.emit()
