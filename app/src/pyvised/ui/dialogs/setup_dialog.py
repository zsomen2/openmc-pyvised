"""The backend setup dialog: the checks that get the OpenMC backend running.

It opens by itself when something keeps the backend from starting (Docker missing or
not running, the image missing or downloading, an error), and from the status pill's
menu at any time. Each check is a row with a status icon and a short result; a check
that fails explains in plain language what to do and offers the fix: get or open
Docker Desktop, download the image or load it from a file (with progress), retry,
run the self-test again. The self-test row is filled in by the main window, which
runs the test once per new image.
"""

import os
from enum import StrEnum
from pathlib import Path

import qtawesome as qta
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPalette
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ... import __version__
from ...backend.manager import (
    IMAGE_SPACE_NEEDED,
    BackendManager,
    BackendState,
    Step,
    Transfer,
    is_pullable,
)
from ..status_pill import AMBER, GREEN, GREY, RED

DOCKER_DESKTOP_URL = "https://docs.docker.com/desktop/setup/install/windows-install/"
ICON_SIZE = 20
REFUSED = ("denied", "unauthorized", "manifest unknown", "not found")
"""Docker's words when a registry has no (public) image of that name and tag."""
UNREACHABLE = ("no such host", "timeout", "dial tcp", "connection")


class Status(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    WARNING = "warning"
    FAILED = "failed"


STATUS_ICONS = {
    Status.PENDING: ("mdi6.circle-outline", GREY),
    Status.RUNNING: ("mdi6.progress-clock", AMBER),
    Status.OK: ("mdi6.check-circle", GREEN),
    Status.WARNING: ("mdi6.alert-circle", AMBER),
    Status.FAILED: ("mdi6.close-circle", RED),
}


def short_path(path: Path, limit: int = 60) -> str:
    """A long path with its middle left out (word wrap cannot break it):
    C:\\Users\\…\\installed\\workspace."""
    text = str(path)
    parts = path.parts
    if len(text) <= limit or len(parts) < 5:
        return text
    return str(Path(*parts[:2], "…", *parts[-2:]))


def format_bytes(count: float) -> str:
    """Decimal units, as Docker shows them: 946 MB, 3.4 GB."""
    for unit, size in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if count >= size:
            value = count / size
            return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"
    return f"{count:.0f} B"


class CheckRow(QWidget):
    """One check: status icon, title and result; below them help text, an optional
    progress bar and buttons, each shown only when there is something to show."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.status = Status.PENDING
        self.icon = QLabel()
        self.icon.setFixedSize(ICON_SIZE + 4, ICON_SIZE + 4)
        self.title = QLabel(title)
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        self.result = QLabel()
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.result.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.help = QLabel(wordWrap=True)
        self.help.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.help.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(10)
        self.progress_text = QLabel()
        self.progress_text.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.buttons = QHBoxLayout()
        self.buttons.setContentsMargins(0, 2, 0, 0)
        self.buttons.addStretch()
        self._button_widgets: list[QPushButton] = []

        layout = QGridLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(2)
        layout.addWidget(self.icon, 0, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.title, 0, 1)
        layout.addWidget(self.result, 0, 2)
        layout.addWidget(self.help, 1, 1, 1, 2)
        layout.addWidget(self.progress, 2, 1, 1, 2)
        layout.addWidget(self.progress_text, 3, 1, 1, 2)
        layout.addLayout(self.buttons, 4, 1, 1, 2)
        layout.setColumnStretch(1, 1)
        self.set(Status.PENDING)
        self.set_progress(None)

    def set(self, status: Status, result: str = "", help_text: str = "") -> None:
        self.status = status
        name, color = STATUS_ICONS[status]
        self.icon.setPixmap(qta.icon(name, color=color).pixmap(ICON_SIZE, ICON_SIZE))
        self.result.setText(result)
        self.help.setText(help_text)
        self.help.setHidden(not help_text)

    def set_progress(self, fraction: float | None, text: str = "", busy: bool = False) -> None:
        """A progress bar (busy = no known total) with a line of text; None hides both."""
        shown = fraction is not None or busy
        self.progress.setHidden(not shown)
        self.progress_text.setHidden(not shown or not text)
        if busy and fraction is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(round((fraction or 0.0) * 1000))
        self.progress_text.setText(text)

    def set_buttons(self, buttons: list[QPushButton]) -> None:
        for button in self._button_widgets:
            if button not in buttons:
                self.buttons.removeWidget(button)
                button.hide()
        for index, button in enumerate(buttons):
            self.buttons.insertWidget(index, button)
            button.show()
        self._button_widgets = list(buttons)


class SetupDialog(QDialog):
    retryRequested = Signal()
    loadRequested = Signal(Path)
    removeRequested = Signal()
    selftestRequested = Signal()
    diagnosticsRequested = Signal()

    def __init__(self, backend: BackendManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.setWindowTitle(self.tr("OpenMC backend setup"))
        self.setMinimumWidth(600)
        intro = QLabel(
            self.tr(
                "PyVised runs OpenMC in a Docker container on this PC. These checks get it "
                "ready; this window opens again whenever something needs your attention."
            ),
            wordWrap=True,
        )
        self.rows = {
            Step.DOCKER: CheckRow(self.tr("Docker Desktop")),
            Step.ENGINE: CheckRow(self.tr("Docker engine")),
            "disk": CheckRow(self.tr("Disk space")),
            Step.IMAGE: CheckRow(self.tr("OpenMC backend image")),
            Step.BACKEND: CheckRow(self.tr("OpenMC backend")),
            "selftest": CheckRow(self.tr("Self-test")),
        }
        self.get_docker = QPushButton(self.tr("Get Docker Desktop…"))
        self.get_docker.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(DOCKER_DESKTOP_URL)))
        self.open_docker = QPushButton(self.tr("Open Docker Desktop"))
        self.open_docker.clicked.connect(self._open_docker_desktop)
        self.download = QPushButton(self.tr("Download"))
        self.download.clicked.connect(self.backend.start)
        self.load_file = QPushButton(self.tr("Load from file…"))
        self.load_file.clicked.connect(self._choose_file)
        self.cancel = QPushButton(self.tr("Cancel"))
        self.cancel.clicked.connect(self.backend.cancel_transfer)
        self.remove = QPushButton(self.tr("Remove from this PC…"))
        self.remove.setToolTip(
            self.tr("Remove the backend image from Docker, e.g. before uninstalling PyVised")
        )
        self.remove.clicked.connect(self.removeRequested.emit)
        self.rerun = QPushButton(self.tr("Run again"))
        self.rerun.clicked.connect(self.selftestRequested.emit)

        checks = QFrame()
        checks.setFrameShape(QFrame.Shape.StyledPanel)
        # never taller than the rows need: spare height goes below the panel, not into rows
        checks.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        check_layout = QVBoxLayout(checks)
        for row in self.rows.values():
            check_layout.addWidget(row)
        self.summary = QLabel()
        copy = QPushButton(self.tr("Copy diagnostics"))
        copy.clicked.connect(self.diagnosticsRequested.emit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        self.retry = buttons.addButton(self.tr("Retry"), QDialogButtonBox.ButtonRole.ActionRole)
        self.retry.clicked.connect(self.retryRequested.emit)
        bottom = QHBoxLayout()
        bottom.addWidget(copy)
        bottom.addWidget(buttons, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(checks)
        layout.addWidget(self.summary)
        layout.addStretch()
        layout.addLayout(bottom)

        self._selftest: tuple[Status, str, str] = (Status.PENDING, "", "")
        backend.stateChanged.connect(self.refresh)
        backend.transferChanged.connect(self.show_transfer)
        backend.operationFinished.connect(self.refresh)  # buttons wait for the worker
        self.refresh()

    # -- public ----------------------------------------------------------------

    def set_selftest(self, status: Status, result: str = "", help_text: str = "") -> None:
        self._selftest = (status, result, help_text)
        self.refresh()

    def refresh(self, *args) -> None:
        backend = self.backend
        state = BackendState(backend.state)
        steps = [Step.DOCKER, Step.ENGINE, Step.IMAGE, Step.BACKEND]
        reached = steps.index(backend.step)
        for index, step in enumerate(steps):
            row = self.rows[step]
            if state is BackendState.IDLE:
                row.set(Status.PENDING)
            elif index < reached:
                row.set(Status.OK, self._result(step))
            elif index > reached:
                row.set(Status.PENDING)
            else:
                self._show_step(step, state)
        self._show_disk()
        transferring = state in (BackendState.PULLING, BackendState.LOADING)
        if not transferring:
            self.rows[Step.IMAGE].set_progress(None)
        elif backend.transfer is not None:
            self.show_transfer(backend.transfer)
        self._show_buttons(state)
        self._show_selftest(state)
        ready = state is BackendState.READY and self._selftest[0] is Status.OK
        self.summary.setText(self.tr("Everything is ready.") if ready else "")
        self.retry.setEnabled(not backend.busy)

    def show_transfer(self, transfer: Transfer) -> None:
        row = self.rows[Step.IMAGE]
        if transfer.unpacking:
            row.set_progress(None, self.tr("Unpacking the image…"), busy=True)
            return
        fraction = transfer.fraction
        if fraction is None:
            if transfer.done > 0:  # the sizes of some layers are not known yet
                text = self.tr("{done} so far").format(done=format_bytes(transfer.done))
            elif transfer.layers:  # no byte counts (plain `docker pull`): finished layers
                text = self.tr("{done} of {count} layers").format(
                    done=transfer.layers_done, count=transfer.layers
                )
            else:
                text = ""
            row.set_progress(None, text, busy=True)
            return
        parts = [
            self.tr("{done} of {total}").format(
                done=format_bytes(transfer.done), total=format_bytes(transfer.total)
            )
        ]
        if transfer.rate > 0:
            parts.append(self.tr("{rate}/s").format(rate=format_bytes(transfer.rate)))
        remaining = transfer.remaining_s
        if remaining is not None:
            parts.append(self._remaining(remaining))
        row.set_progress(fraction, " · ".join(parts))

    # -- rows ------------------------------------------------------------------

    def _result(self, step: Step) -> str:
        facts = self.backend.facts
        match step:
            case Step.DOCKER:
                if facts.desktop_version:
                    return self.tr("Docker Desktop {version}").format(version=facts.desktop_version)
                return facts.docker or ""
            case Step.ENGINE:
                return self.tr("version {version}").format(version=facts.engine or "?")
            case Step.IMAGE:
                return self.backend.config.image
            case Step.BACKEND:
                health = self.backend.health
                if health is None:
                    return ""
                return self.tr("OpenMC {version} · {cpus} CPUs").format(
                    version=health.openmc_version or "?", cpus=health.cpu_count
                )

    def _show_step(self, step: Step, state: BackendState) -> None:
        row = self.rows[step]
        backend = self.backend
        if state is BackendState.ERROR:
            detail = backend.detail
            if step is Step.IMAGE:
                detail = f"{self._image_failure(detail)}\n\n{detail}"
            row.set(Status.FAILED, self.tr("failed"), detail)
            return
        match state:
            case BackendState.CHECKING:
                row.set(Status.RUNNING, self.tr("checking…"))
            case BackendState.DOCKER_MISSING:
                row.set(
                    Status.FAILED,
                    self.tr("not installed"),
                    self.tr(
                        "PyVised runs OpenMC inside Docker. Install Docker Desktop (free for "
                        "education), start it once and accept its terms, then choose Retry."
                    ),
                )
            case BackendState.STARTING_DOCKER:
                row.set(
                    Status.RUNNING,
                    self.tr("starting…"),
                    self.tr("Docker Desktop is starting; this can take a few minutes."),
                )
            case BackendState.DOCKER_UNAVAILABLE:
                row.set(
                    Status.FAILED,
                    self.tr("not running"),
                    self.tr(
                        "Docker Desktop is installed, but its engine did not start. Open Docker "
                        "Desktop and wait until it says the engine is running, then choose "
                        "Retry. If it never starts: virtualization must be enabled in the "
                        "BIOS/UEFI, and WSL 2 must be installed and up to date (run "
                        "'wsl --update' in a terminal)."
                    ),
                )
            case BackendState.IMAGE_MISSING:
                image = backend.config.image
                if is_pullable(image):
                    text = self.tr(
                        "The backend image is not on this PC yet. Download it (about 1 GB, "
                        "once per PC) or, without internet access, load the file "
                        "pyvised-backend-{version}.tar.gz from the PyVised release page."
                    ).format(version=__version__)
                else:
                    text = self.tr(
                        "The backend image {image} is not on this PC. Developers build it "
                        "with 'just backend-build'."
                    ).format(image=image)
                row.set(Status.FAILED, self.tr("missing"), text)
            case BackendState.PULLING:
                row.set(Status.RUNNING, self.tr("downloading…"), backend.config.image)
            case BackendState.LOADING:
                row.set(
                    Status.RUNNING,
                    self.tr("loading…"),
                    self.tr("Loading {file} into Docker.").format(file=backend.detail),
                )
            case BackendState.STARTING:
                row.set(Status.RUNNING, self.tr("starting…"))
            case BackendState.READY:
                row.set(Status.OK, self._result(step), self._backend_details())
                row.help.setToolTip(str(backend.config.workspace))
            case BackendState.STOPPING:
                row.set(Status.RUNNING, self.tr("stopping…"))
            case BackendState.STOPPED:
                row.set(Status.PENDING, self.tr("stopped"))

    def _image_failure(self, detail: str) -> str:
        """What a failed download or load means, in plain words (Docker's text follows)."""
        text = detail.lower()
        if any(word in text for word in REFUSED):
            return self.tr(
                "The registry does not offer this image: this version may not be published "
                "yet. Load the image from a file instead."
            )
        if any(word in text for word in UNREACHABLE):
            return self.tr(
                "ghcr.io cannot be reached. Check the internet connection and the proxy "
                "settings of Docker Desktop, or load the image from a file."
            )
        if "no space" in text:
            return self.tr("The disk is full. Free some space, then choose Retry.")
        return self.tr("Getting the image failed. Choose Retry, or load the image from a file.")

    def _backend_details(self) -> str:
        backend = self.backend
        connection = backend.connection
        lines = [
            self.tr("Container {name}").format(name=backend.config.container_name)
            + (f" · {connection.base_url}" if connection else ""),
            self.tr("Workspace {path}").format(path=short_path(backend.config.workspace)),
        ]
        return "\n".join(lines)

    def _show_disk(self) -> None:
        row = self.rows["disk"]
        facts = self.backend.facts
        if facts.free_bytes is None or facts.data_dir is None:
            row.set(Status.PENDING)
            return
        drive = facts.data_dir.anchor.rstrip("\\") or str(facts.data_dir)
        result = self.tr("{free} free on {drive}").format(
            free=format_bytes(facts.free_bytes), drive=drive
        )
        if facts.free_bytes >= IMAGE_SPACE_NEEDED:
            row.set(Status.OK, result)
        else:
            row.set(
                Status.WARNING,
                result,
                self.tr(
                    "The backend image needs about 4.5 GB, and runs need room for their "
                    "results. Free some space on {drive}."
                ).format(drive=drive),
            )

    def _show_buttons(self, state: BackendState) -> None:
        backend = self.backend
        docker: list[QPushButton] = []
        engine: list[QPushButton] = []
        image: list[QPushButton] = []
        if state is BackendState.DOCKER_MISSING:
            docker = [self.get_docker]
        elif state is BackendState.DOCKER_UNAVAILABLE and backend.facts.desktop is not None:
            engine = [self.open_docker]
        elif state in (BackendState.PULLING, BackendState.LOADING):
            image = [self.cancel]
        elif state is BackendState.IMAGE_MISSING or (
            state is BackendState.ERROR and backend.step is Step.IMAGE
        ):
            image = [self.load_file]
            if is_pullable(backend.config.image):
                image.insert(0, self.download)
        elif state is BackendState.READY:
            image = [self.remove]
        self.rows[Step.DOCKER].set_buttons(docker)
        self.rows[Step.ENGINE].set_buttons(engine)
        self.rows[Step.IMAGE].set_buttons(image)
        for button in image:
            button.setEnabled(not backend.busy or button is self.cancel)

    def _show_selftest(self, state: BackendState) -> None:
        row = self.rows["selftest"]
        status, result, help_text = self._selftest
        ready = state is BackendState.READY
        if not ready and status is not Status.RUNNING:
            row.set(Status.PENDING, "", self.tr("Runs once the backend is ready."))
            row.set_buttons([])
            return
        if status is Status.PENDING:
            help_text = help_text or self.tr(
                "Builds, renders and runs a small test model inside the backend."
            )
        row.set(status, result, help_text)
        row.set_buttons([self.rerun] if ready and status is not Status.RUNNING else [])

    def _remaining(self, seconds: float) -> str:
        if seconds < 60:
            return self.tr("about {n} s left").format(n=max(1, round(seconds / 5) * 5))
        return self.tr("about {n} min left").format(n=round(seconds / 60))

    # -- actions ---------------------------------------------------------------

    def _open_docker_desktop(self) -> None:
        desktop = self.backend.facts.desktop
        if desktop is not None:
            os.startfile(desktop)  # type: ignore[attr-defined]  # Windows only

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Load the backend image"),
            str(Path.home() / "Downloads"),
            self.tr("Docker image archives (*.tar.gz *.tgz *.tar);;All files (*)"),
        )
        if path:
            self.loadRequested.emit(Path(path))
