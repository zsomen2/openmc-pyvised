"""About dialog: logo, versions, links."""

import platform

import PySide6
from PySide6.QtCore import Qt, qVersion
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ... import REPOSITORY_URL, __version__
from ...core.paths import logo_path


class AboutDialog(QDialog):
    def __init__(self, openmc_version: str | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("About PyVised"))

        logo = QLabel()
        pixmap = QPixmap(str(logo_path()))
        if not pixmap.isNull():
            logo.setPixmap(
                pixmap.scaled(
                    112,
                    112,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

        title = QLabel(f"<h2>PyVised {__version__}</h2>")
        about = QLabel(
            self.tr(
                "A graphical interface for OpenMC: geometry visualisation and simulation "
                "control, made for teaching Monte Carlo particle transport."
            )
        )
        about.setWordWrap(True)
        backend = openmc_version or self.tr("not connected")
        versions = QLabel(
            self.tr("OpenMC in the backend: {version}").format(version=backend)
            + f"<br>Qt {qVersion()} · PySide6 {PySide6.__version__} · "
            f"Python {platform.python_version()}"
        )
        links = QLabel(
            f'<a href="{REPOSITORY_URL}">{REPOSITORY_URL}</a><br>'
            '<a href="https://docs.openmc.org">docs.openmc.org</a><br>'
            + self.tr("License: GNU GPL v3 or later")
        )
        links.setOpenExternalLinks(True)
        links.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)

        text = QVBoxLayout()
        for widget in (title, about, versions, links):
            text.addWidget(widget)
        text.addStretch()
        top = QHBoxLayout()
        top.addWidget(logo, alignment=Qt.AlignmentFlag.AlignTop)
        top.addSpacing(12)
        top.addLayout(text, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(buttons)
        self.setMinimumWidth(480)
