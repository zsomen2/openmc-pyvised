"""Backend status in the status bar: a coloured dot, a short text and an action menu."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QToolButton, QWidget

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
    detailsRequested = Signal()
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
            (self.tr("Details…"), self.detailsRequested),
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
        self.set_state("idle", "")

    def set_state(self, state: str, detail: str) -> None:
        self.state = state
        text, color = self.describe(state, detail)
        self.setIcon(_dot(color))
        self.setText(text)
        self.setToolTip(detail or text)

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
                return self.tr("Downloading the OpenMC backend…"), AMBER
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

    def explanation(self, state: str, detail: str, image: str) -> str:
        """Longer help text for the details dialog."""
        match state:
            case "docker_missing":
                return self.tr(
                    "PyVised runs OpenMC inside Docker. Install Docker Desktop (free for "
                    "education), start it once, then choose Restart backend."
                )
            case "docker_unavailable":
                return self.tr(
                    "Docker Desktop is installed but its engine did not start. Start Docker "
                    "Desktop, wait until it says it is running, then choose Restart backend."
                )
            case "image_missing":
                return self.tr(
                    "The backend image {image} is not on this PC. Developers build it with "
                    "'just backend-build'."
                ).format(image=image)
            case "error":
                return detail
            case _:
                return detail or self.describe(state, detail)[0]
