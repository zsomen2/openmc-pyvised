"""Read-only code view: monospace text with line numbers, the current line, tinted find
matches, notebook cell headers across the full width, and zoom (Ctrl+wheel,
Ctrl+plus/minus/0) that the tab saves in the settings."""

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QKeyEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QResizeEvent,
    QTextCursor,
    QTextFormat,
    QWheelEvent,
)
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit, QWidget

from ...core.inputs import LineKind
from ...core.settings import CODE_FONT_DEFAULT, CODE_FONT_MAX, CODE_FONT_MIN
from ...core.theme import is_dark
from .highlighter import PythonHighlighter

FONT_FAMILIES = ("Cascadia Mono", "Consolas")
"""Preferred monospace fonts; else the system's fixed font."""
HIGHLIGHT_LIMIT = 500_000
"""[characters] Longer texts stay plain: colouring them would block the app for seconds."""
MAX_TINTED = 2000
"""Find matches tinted at most (all are counted)."""
WHEEL_STEP = 120


@dataclass(frozen=True)
class Position:
    """Where a file was left: scroll bars, and the selection's anchor and cursor as
    (line, column), which survive changes further down the file."""

    top: int
    left: int
    anchor: tuple[int, int]
    cursor: tuple[int, int]


def code_font(size: int) -> QFont:
    families = set(QFontDatabase.families())
    family = next((f for f in FONT_FAMILIES if f in families), None)
    font = QFont(family) if family else QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setFixedPitch(True)
    font.setPointSize(size)
    return font


class LineNumbers(QWidget):
    def __init__(self, view: "CodeView") -> None:
        super().__init__(view)
        self.view = view

    def sizeHint(self) -> QSize:
        return QSize(self.view.gutter_width(), 0)

    def paintEvent(self, event: QPaintEvent) -> None:
        self.view.paint_gutter(event)


class CodeView(QPlainTextEdit):
    fontSizeChanged = Signal(int)

    def __init__(self, font_size: int = CODE_FONT_DEFAULT, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.font_size = font_size
        self.headers: list[int] = []
        """Block numbers of a notebook's cell headers."""
        self.matches: list[tuple[int, int]] = []
        """Find matches as (start, end) positions."""
        self.current_match: int | None = None
        self._fixed: list[QTextEdit.ExtraSelection] = []
        """The header and match selections (the current line is added on every move)."""
        self._wheel = 0
        self.gutter = LineNumbers(self)
        self.highlighter = PythonHighlighter(self.document(), is_dark())
        self.blockCountChanged.connect(self._update_margins)
        self.updateRequest.connect(self._gutter_update)
        self.cursorPositionChanged.connect(self._update_selections)
        self.set_font_size(font_size)

    # -- text ----------------------------------------------------------------------------------

    def show_text(self, text: str, kinds: list[LineKind] | None) -> None:
        """Show `text` from its top; `kinds` says what each line is (see `InputText`)."""
        self.highlighter.set_lines(kinds, enabled=len(text) <= HIGHLIGHT_LIMIT)
        self.headers = [n for n, kind in enumerate(kinds or ()) if kind is LineKind.HEADER]
        self.matches, self.current_match = [], None
        self._fixed = []
        self.setPlainText(text)
        self._refresh_fixed()

    def position(self) -> Position:
        """Scroll position, selection and cursor by line and column."""
        cursor = self.textCursor()
        document = self.document()
        anchor = document.findBlock(cursor.anchor())
        return Position(
            top=self.verticalScrollBar().value(),
            left=self.horizontalScrollBar().value(),
            anchor=(anchor.blockNumber(), cursor.anchor() - anchor.position()),
            cursor=(cursor.blockNumber(), cursor.positionInBlock()),
        )

    def _offset(self, line: int, column: int) -> int:
        block = self.document().findBlockByNumber(min(line, self.blockCount() - 1))
        return block.position() + min(column, max(0, block.length() - 1))

    def restore_position(self, position: Position) -> None:
        cursor = QTextCursor(self.document())
        cursor.setPosition(self._offset(*position.anchor))
        cursor.setPosition(self._offset(*position.cursor), QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cursor)
        self.verticalScrollBar().setValue(position.top)
        self.horizontalScrollBar().setValue(position.left)

    def go_to_line(self, line: int) -> None:
        """Put the cursor at the start of `line` (1-based) and centre it."""
        block = self.document().findBlockByNumber(max(0, min(line - 1, self.blockCount() - 1)))
        self.setTextCursor(QTextCursor(block))
        self.centerCursor()

    def set_matches(
        self, matches: list[tuple[int, int]], current: int | None, select: bool = True
    ) -> None:
        """Tint the find matches; `select` selects the current one and scrolls to it."""
        self.matches, self.current_match = matches, current
        if select and current is not None and 0 <= current < len(matches):
            start, end = matches[current]
            cursor = self.textCursor()
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(cursor)
            if not self.viewport().rect().contains(self.cursorRect()):
                self.centerCursor()
            self.ensureCursorVisible()
        self._refresh_fixed()

    # -- selections ------------------------------------------------------------------------------

    def _colors(self) -> dict[str, QColor]:
        palette = self.palette()
        highlight = QColor(palette.color(QPalette.ColorRole.Highlight))
        text = QColor(palette.color(QPalette.ColorRole.Text))
        dark = is_dark()
        line, header = QColor(highlight), QColor(text)
        line.setAlpha(40 if dark else 26)
        header.setAlpha(22 if dark else 14)
        return {
            "line": line,
            "header": header,
            "match": QColor(255, 200, 0, 75) if dark else QColor(255, 210, 0, 110),
            "current": QColor(255, 140, 0, 150) if dark else QColor(255, 150, 0, 170),
        }

    def _selection(self, cursor: QTextCursor, color: QColor, full: bool = False):
        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor  # type: ignore[attr-defined]
        selection.format.setBackground(color)  # type: ignore[attr-defined]
        if full:
            selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)  # type: ignore[attr-defined]
        return selection

    def _refresh_fixed(self) -> None:
        colors = self._colors()
        fixed = []
        document = self.document()
        for number in self.headers:
            cursor = QTextCursor(document.findBlockByNumber(number))
            fixed.append(self._selection(cursor, colors["header"], full=True))
        for index, (start, end) in enumerate(self.matches[:MAX_TINTED]):
            cursor = QTextCursor(document)
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            color = colors["current" if index == self.current_match else "match"]
            fixed.append(self._selection(cursor, color))
        self._fixed = fixed
        self._update_selections()

    def _update_selections(self) -> None:
        cursor = self.textCursor()
        cursor.clearSelection()
        line = self._selection(cursor, self._colors()["line"], full=True)
        self.setExtraSelections([line, *self._fixed])
        self.gutter.update()

    # -- line numbers ------------------------------------------------------------------------------

    def gutter_width(self) -> int:
        digits = len(str(max(1, self.blockCount())))
        return 14 + self.fontMetrics().horizontalAdvance("9") * max(3, digits)

    def _update_margins(self, *args) -> None:
        width = self.gutter_width()
        self.setViewportMargins(width, 0, 0, 0)
        rect = self.contentsRect()
        self.gutter.setGeometry(QRect(rect.left(), rect.top(), width, rect.height()))

    def _gutter_update(self, rect: QRect, dy: int) -> None:
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_margins()

    def paint_gutter(self, event: QPaintEvent) -> None:
        painter = QPainter(self.gutter)
        palette = self.palette()
        painter.fillRect(event.rect(), palette.color(QPalette.ColorRole.Base))
        width = self.gutter.width()
        painter.setPen(palette.color(QPalette.ColorRole.Mid))
        painter.drawLine(width - 1, event.rect().top(), width - 1, event.rect().bottom())
        dim = palette.color(QPalette.ColorRole.PlaceholderText)
        bright = palette.color(QPalette.ColorRole.Text)
        current = self.textCursor().blockNumber()
        height = self.fontMetrics().height()
        block = self.firstVisibleBlock()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        while block.isValid() and top <= event.rect().bottom():
            bottom = top + round(self.blockBoundingRect(block).height())
            if block.isVisible() and bottom >= event.rect().top():
                number = block.blockNumber()
                painter.setPen(bright if number == current else dim)
                painter.drawText(
                    0, top, width - 8, height, Qt.AlignmentFlag.AlignRight, str(number + 1)
                )
            block = block.next()
            top = bottom
        painter.end()

    # -- zoom and theme ----------------------------------------------------------------------------

    def set_font_size(self, size: int) -> None:
        self.font_size = min(CODE_FONT_MAX, max(CODE_FONT_MIN, int(size)))
        font = code_font(self.font_size)
        self.setFont(font)
        self.setTabStopDistance(4 * QFontMetricsF(font).horizontalAdvance(" "))
        self._update_margins()

    def zoom(self, step: int) -> None:
        """One point larger (+1) or smaller (-1); 0 returns to the default size."""
        size = CODE_FONT_DEFAULT if step == 0 else self.font_size + step
        if min(CODE_FONT_MAX, max(CODE_FONT_MIN, size)) != self.font_size or step == 0:
            self.set_font_size(size)
            self.fontSizeChanged.emit(self.font_size)

    @staticmethod
    def _zoom_step(event: QKeyEvent) -> int | None:
        modifiers = event.modifiers()
        if not modifiers & Qt.KeyboardModifier.ControlModifier:
            return None
        if modifiers & Qt.KeyboardModifier.AltModifier:  # AltGr on Windows
            return None
        steps = {Qt.Key.Key_Plus: 1, Qt.Key.Key_Equal: 1, Qt.Key.Key_Minus: -1, Qt.Key.Key_0: 0}
        return steps.get(Qt.Key(event.key()))

    def event(self, event: QEvent) -> bool:
        # Ctrl+0 is also the window's "Reset views": the zoom keys go to the view instead
        override = event.type() == QEvent.Type.ShortcutOverride
        if override and isinstance(event, QKeyEvent) and self._zoom_step(event) is not None:
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        step = self._zoom_step(event)
        if step is None:
            super().keyPressEvent(event)
            return
        self.zoom(step)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            super().wheelEvent(event)
            return
        self._wheel += event.angleDelta().y()
        while abs(self._wheel) >= WHEEL_STEP:
            step = 1 if self._wheel > 0 else -1
            self._wheel -= step * WHEEL_STEP
            self.zoom(step)
        event.accept()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            self._update_margins()
        elif event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self.highlighter.set_dark(is_dark())
            self._refresh_fixed()
