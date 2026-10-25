"""Centred placeholder shown while a tab has nothing to display."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..core.icons import icon


class EmptyState(QWidget):
    def __init__(
        self, icon_name: str, title: str, text: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._icon = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self._title = QLabel(title, alignment=Qt.AlignmentFlag.AlignCenter)
        font = self._title.font()
        font.setPointSizeF(font.pointSizeF() * 1.4)
        font.setBold(True)
        self._title.setFont(font)
        self._text = QLabel(text, alignment=Qt.AlignmentFlag.AlignCenter, wordWrap=True)
        self._text.setForegroundRole(QPalette.ColorRole.PlaceholderText)

        layout = QVBoxLayout(self)
        layout.addStretch(2)
        layout.addWidget(self._icon)
        layout.addSpacing(8)
        layout.addWidget(self._title)
        layout.addWidget(self._text)
        layout.addStretch(3)
        self.refresh_icon()

    def refresh_icon(self) -> None:
        self._icon.setPixmap(icon(self._icon_name).pixmap(72, 72))
