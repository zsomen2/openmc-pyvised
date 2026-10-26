"""The legend next to the views: what the colours of the current mode mean.

Discrete modes (material, cell, universe) list the ids present in the views
with their colour and share of the pixels; a click highlights an entry in both
views, a double click changes its colour (saved in the model's sidecar).
Continuous modes (density, temperature, importance) show a colour bar with its
unit and, when only a few values occur, those values as entries too.
"""

from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QMenu,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...render.palette import RGB
from ...render.scales import Scale, format_value
from .info_panel import swatch

_KEY = Qt.ItemDataRole.UserRole


@dataclass(frozen=True)
class LegendEntry:
    key: float
    """Id (material, cell, universe) or value (continuous modes)."""
    text: str
    rgb: RGB
    share: float
    """Fraction of the coloured pixels of both views."""


BAR_HEIGHT = 14


def paint_color_bar(
    painter: QPainter, rect: QRectF, scale: Scale | None, colors: np.ndarray, text: QColor
) -> None:
    """A horizontal colour bar in the top `BAR_HEIGHT` of `rect`, tick values under it."""
    metrics = painter.fontMetrics()
    margin = metrics.horizontalAdvance("0.000") // 2 + 2
    bar = QRectF(
        rect.left() + margin, rect.top() + 2, max(10, rect.width() - 2 * margin), BAR_HEIGHT
    )
    strip = np.ascontiguousarray(colors[np.newaxis, :, :])
    image = QImage(strip.data, strip.shape[1], 1, strip.shape[1] * 3, QImage.Format.Format_RGB888)
    painter.drawImage(bar, image)
    painter.setPen(QPen(text))
    painter.drawRect(bar)
    if scale is None:
        return
    for value in scale.ticks():
        x = bar.left() + scale.position(value) * bar.width()
        painter.drawLine(round(x), round(bar.bottom()), round(x), round(bar.bottom()) + 3)
        label = format_value(value)
        width = metrics.horizontalAdvance(label)
        left = min(max(rect.left(), x - width / 2), rect.right() - width)
        painter.drawText(QPoint(round(left), round(bar.bottom()) + 4 + metrics.ascent()), label)


class ColorBar(QWidget):
    """A horizontal colour bar with tick values under it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.scale: Scale | None = None
        self.colors = np.zeros((2, 3), dtype=np.uint8)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(220, BAR_HEIGHT + 2 * self.fontMetrics().height())

    def minimumSizeHint(self) -> QSize:
        return QSize(120, self.sizeHint().height())

    def set_scale(self, scale: Scale | None, colors: np.ndarray) -> None:
        self.scale, self.colors = scale, colors
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        text = self.palette().color(QPalette.ColorRole.Text)
        paint_color_bar(painter, QRectF(self.rect()), self.scale, self.colors, text)


def legend_pixmap(
    title: str,
    entries: list[LegendEntry],
    scale: Scale | None,
    colors: np.ndarray | None,
    background: QColor,
    foreground: QColor,
    font: QFont,
    height: int,
) -> QPixmap:
    """The legend as an image next to an exported view: title, colour bar, entries."""
    metrics = QFontMetrics(font)
    line = metrics.height() + 4
    rows = max(
        1, (height - 3 * line - (BAR_HEIGHT + 2 * line if colors is not None else 0)) // line
    )
    shown = entries[:rows]
    texts = [title] + [e.text for e in shown]
    width = max(200, max(metrics.horizontalAdvance(s) for s in texts) + line + 24)
    pixmap = QPixmap(width, height)
    pixmap.fill(background)
    painter = QPainter(pixmap)
    painter.setFont(font)
    painter.setPen(foreground)
    y = 8
    bold = QFont(font)
    bold.setBold(True)
    painter.setFont(bold)
    painter.drawText(QPoint(8, y + metrics.ascent()), title)
    painter.setFont(font)
    y += line + 2
    if colors is not None:
        paint_color_bar(
            painter, QRectF(8, y, width - 16, BAR_HEIGHT + 2 * line), scale, colors, foreground
        )
        y += BAR_HEIGHT + 2 * line
    for entry in shown:
        painter.fillRect(8, y + 2, line - 6, line - 6, QColor(*entry.rgb))
        painter.setPen(foreground)
        painter.drawRect(8, y + 2, line - 6, line - 6)
        painter.drawText(QPoint(8 + line, y + metrics.ascent() + 1), entry.text)
        y += line
    if len(entries) > len(shown):
        painter.drawText(QPoint(8, y + metrics.ascent()), f"… (+{len(entries) - len(shown)})")
    painter.end()
    return pixmap


class LegendPanel(QWidget):
    highlightRequested = Signal(object)
    """(kind, id) of an entry to highlight in the views, or None to clear."""
    colorEditRequested = Signal(str, int)
    colorResetRequested = Signal(str, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind: str | None = None
        """"material", "cell", "universe" for editable entries; None for values."""
        self.title = QLabel()
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        self.bar = ColorBar()
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(False)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 0, 0)
        layout.addWidget(self.title)
        layout.addWidget(self.bar)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.hint)
        self.tree.itemClicked.connect(self._clicked)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self._selected: tuple[str, int] | None = None
        self.clear()

    def clear(self) -> None:
        self.kind = None
        self.title.setText(self.tr("Legend"))
        self.bar.hide()
        self.tree.clear()
        self.hint.hide()

    def show_entries(self, title: str, kind: str | None, entries: list[LegendEntry]) -> None:
        """A list of colours: ids of `kind` (editable), or values when `kind` is None."""
        self.kind = kind
        self.title.setText(title)
        self.bar.hide()
        self._fill(entries)
        if kind is not None:
            self.hint.setText(
                self.tr("Click: highlight in the views. Double click: change the colour.")
            )
            self.hint.show()
        else:
            self.hint.hide()

    def show_scale(
        self, title: str, scale: Scale | None, colors: np.ndarray, entries: list[LegendEntry]
    ) -> None:
        """A colour bar, plus the values present when there are only a few."""
        self.kind = None
        self.title.setText(title)
        self.bar.set_scale(scale, colors)
        self.bar.show()
        self._fill(entries)
        self.hint.setVisible(scale is None)
        self.hint.setText(self.tr("No values in this model."))

    def select(self, selection: tuple[str, int] | None) -> None:
        """Mark the highlighted entry (or none) without emitting anything."""
        self._selected = selection
        self.tree.blockSignals(True)
        self.tree.clearSelection()
        if selection is not None and selection[0] == self.kind:
            for i in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(i)
                if item is not None and item.data(0, _KEY) == selection[1]:
                    item.setSelected(True)
                    self.tree.scrollToItem(item)
        self.tree.blockSignals(False)

    def _fill(self, entries: list[LegendEntry]) -> None:
        self.tree.clear()
        for entry in entries:
            share = f"{100 * entry.share:.1f} %" if entry.share >= 0.001 else "< 0.1 %"
            item = QTreeWidgetItem([entry.text, share])
            item.setIcon(0, swatch(entry.rgb, 14))
            item.setData(0, _KEY, entry.key)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.tree.addTopLevelItem(item)
        self.select(self._selected)

    def _key(self, item: QTreeWidgetItem) -> int | None:
        key = item.data(0, _KEY)
        return int(key) if self.kind is not None and key is not None else None

    def _clicked(self, item: QTreeWidgetItem) -> None:
        key = self._key(item)
        if key is None or self.kind is None:
            return
        selection = (self.kind, key)
        if selection == self._selected:
            self._selected = None
            item.setSelected(False)
            self.highlightRequested.emit(None)
        else:
            self._selected = selection
            self.highlightRequested.emit(selection)

    def _double_clicked(self, item: QTreeWidgetItem) -> None:
        key = self._key(item)
        if key is not None and self.kind is not None:
            self.colorEditRequested.emit(self.kind, key)

    def _context_menu(self, position) -> None:
        item = self.tree.itemAt(position)
        key = self._key(item) if item is not None else None
        if key is None or self.kind is None:
            return
        kind = self.kind
        menu = QMenu(self)
        edit = menu.addAction(self.tr("Change colour…"))
        reset = menu.addAction(self.tr("Reset colour"))
        chosen = menu.exec(self.tree.viewport().mapToGlobal(position))
        if chosen is edit:
            self.colorEditRequested.emit(kind, key)
        elif chosen is reset:
            self.colorResetRequested.emit(kind, key)
