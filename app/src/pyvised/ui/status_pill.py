"""Backend status in the status bar: a coloured dot, a short text and an action menu."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QToolButton, QWidget

from ..backend.manager import Transfer

GREY, AMBER, GREEN, RED = "#8c959f", "#d29922", "#2da44e", "#cf222e"


def _dot(color: str, size: int = 12) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return QIcon(pixmap)


class StatusPill(QToolButton):
    setupRequested = Signal()
    selftestRequested = Signal()
    restartRequested = Signal()
    logRequested = Signal()
    diagnosticsRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setAutoRaise(True)
        menu = QMenu(self)
        for text, signal in (
            (self.tr("Backend setup…"), self.setupRequested),
            (self.tr("Run self-test"), self.selftestRequested),
            (self.tr("Restart backend"), self.restartRequested),
            (self.tr("Show backend log…"), self.logRequested),
            (self.tr("Copy diagnostics"), self.diagnosticsRequested),
        ):
            action = QAction(text, self)
            action.triggered.connect(signal.emit)
            menu.addAction(action)
        self.setMenu(menu)
        self.state = "idle"
        self.detail = ""
        self.transfer: Transfer | None = None
        self.set_state("idle", "")

    def set_state(self, state: str, detail: str) -> None:
        if state not in ("pulling", "loading"):
            self.transfer = None
        self.state = state
        self.detail = detail
        text, color = self.describe(state, detail)
        self.setIcon(_dot(color))
        self.setText(text)
        self.setToolTip(detail or text)

    def set_transfer(self, transfer: Transfer) -> None:
        """Progress of the image download or load, shown as a percentage."""
        self.transfer = transfer
        self.set_state(self.state, self.detail)

    def describe(self, state: str, detail: str) -> tuple[str, str]:
        match state:
            case "checking":
                return self.tr("Checking Docker…"), AMBER
            case "docker_missing":
                return self.tr("Docker Desktop is not installed"), RED
            case "starting_docker":
                return self.tr("Starting Docker Desktop…"), AMBER
            case "docker_unavailable":
                return self.tr("Docker is not running"), RED
            case "image_missing":
                return self.tr("Backend image missing"), RED
            case "pulling":
                return self._with_percent(self.tr("Downloading the OpenMC backend…")), AMBER
            case "loading":
                return self._with_percent(self.tr("Loading the OpenMC backend…")), AMBER
            case "starting":
                return self.tr("Starting the OpenMC backend…"), AMBER
            case "ready":
                return self.tr("Ready · OpenMC {version}").format(version=detail or "?"), GREEN
            case "stopping":
                return self.tr("Stopping the backend…"), AMBER
            case "stopped":
                return self.tr("Backend stopped"), GREY
            case "error":
                return self.tr("Backend error"), RED
            case _:
                return self.tr("Backend not started"), GREY

    def _with_percent(self, text: str) -> str:
        fraction = self.transfer.fraction if self.transfer is not None else None
        return text if fraction is None else f"{text} {fraction * 100:.0f} %"
