"""A read-only, monospaced text window (backend log, diagnostics)."""

from collections.abc import Callable

from PySide6.QtGui import QFontDatabase, QGuiApplication
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit, QVBoxLayout, QWidget


class TextDialog(QDialog):
    def __init__(
        self,
        title: str,
        text: str = "",
        parent: QWidget | None = None,
        refresh: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 520)
        self.view = QPlainTextEdit(readOnly=True)
        self.view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy = buttons.addButton(self.tr("Copy"), QDialogButtonBox.ButtonRole.ActionRole)
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.view.toPlainText()))
        if refresh is not None:
            again = buttons.addButton(self.tr("Refresh"), QDialogButtonBox.ButtonRole.ActionRole)
            again.clicked.connect(refresh)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.view)
        layout.addWidget(buttons)
        self.set_text(text)

    def set_text(self, text: str) -> None:
        self.view.setPlainText(text)
        scrollbar = self.view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
