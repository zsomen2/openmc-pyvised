"""One geometry view: the coloured slice with locked 1:1 axes in cm, zoom, pan and hover.

The wheel zooms about the cursor, dragging pans, a double click asks for the
whole model again, a click inspects the point and a right click opens the
view's menu. Zooming and panning transform the current image at once; the
panel then asks the backend for a sharp slice of the new window. On top of the
image the view draws the other view's cut plane as a draggable line, the
region labels and a marker at the inspected point.
"""

import os

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")

from typing import cast

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QLabel, QWidget

from ...render.views import Window

CUT_LINE_COLOR = QColor(0, 150, 255)
MARKER_COLOR = QColor(255, 140, 0)
MAX_LABEL_ITEMS = 400


class _ViewBox(pg.ViewBox):
    doubleClicked = Signal()
    clicked = Signal(object)
    """(u, v) of a left click."""
    contextRequested = Signal(object, object)
    """(u, v) of a right click and the global screen position (QPoint)."""

    def mouseClickEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            ev.accept()
            if ev.double():
                self.doubleClicked.emit()
            else:
                point = self.mapSceneToView(ev.scenePos())
                self.clicked.emit((point.x(), point.y()))
            return
        if ev.button() == Qt.MouseButton.RightButton:
            ev.accept()
            point = self.mapSceneToView(ev.scenePos())
            screen = ev.screenPos()
            self.contextRequested.emit(
                (point.x(), point.y()), QPoint(round(screen.x()), round(screen.y()))
            )
            return
        super().mouseClickEvent(ev)


class SliceView(pg.PlotWidget):
    hovered = Signal(object)
    """(u, v) in cm under the cursor, or None when it leaves the plot."""
    windowChanged = Signal()
    """The visible window moved, was zoomed or resized."""
    resetRequested = Signal()
    clicked = Signal(object)
    contextRequested = Signal(object, object)
    cutLineDragged = Signal(float, bool)
    """The other view's cut line was dragged to a position; True when the drag ended."""

    def __init__(self, parent: QWidget | None = None) -> None:
        self._box = _ViewBox(defaultPadding=0.02)
        super().__init__(parent, viewBox=self._box)
        plot = self._plot()
        plot.setMenuEnabled(False)
        plot.hideButtons()
        self._box.setAspectLocked(True, ratio=1)
        for name in ("bottom", "left"):
            plot.getAxis(name).enableAutoSIPrefix(False)
        self.image = pg.ImageItem(axisOrder="row-major")
        plot.addItem(self.image)
        self.window: Window | None = None
        """Rectangle covered by the image shown now."""

        self.cut_line = pg.InfiniteLine(
            movable=True,
            pen=pg.mkPen(CUT_LINE_COLOR, width=1.5, style=Qt.PenStyle.DashLine),
            hoverPen=pg.mkPen(CUT_LINE_COLOR, width=3),
        )
        self.cut_line.setZValue(20)
        self.cut_line.hide()
        plot.addItem(self.cut_line, ignoreBounds=True)
        self.cut_line.sigDragged.connect(lambda line: self.cutLineDragged.emit(line.value(), False))
        self.cut_line.sigPositionChangeFinished.connect(
            lambda line: self.cutLineDragged.emit(line.value(), True)
        )
        self.marker = pg.ScatterPlotItem(
            size=11, symbol="+", pen=pg.mkPen(MARKER_COLOR, width=2.5), brush=None
        )
        self.marker.setZValue(30)
        plot.addItem(self.marker, ignoreBounds=True)
        self._labels: list[pg.TextItem] = []
        self._label_font = QFont()
        self._label_font.setPointSizeF(max(7.0, self._label_font.pointSizeF() * 0.85))

        self._box.sigRangeChanged.connect(lambda *args: self.windowChanged.emit())
        self._box.sigResized.connect(lambda *args: self.windowChanged.emit())
        self._box.doubleClicked.connect(self.resetRequested)
        self._box.clicked.connect(self.clicked)
        self._box.contextRequested.connect(self.contextRequested)
        cast(pg.GraphicsScene, self.scene()).sigMouseMoved.connect(self._mouse_moved)

        self.message = QLabel(self)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setWordWrap(True)
        self.message.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.message.hide()

    def _plot(self) -> pg.PlotItem:
        return cast(pg.PlotItem, self.getPlotItem())

    # -- content -------------------------------------------------------------------

    def set_axis_labels(self, horizontal: str, vertical: str) -> None:
        plot = self._plot()
        plot.setLabel("bottom", f"{horizontal} [cm]")
        plot.setLabel("left", f"{vertical} [cm]")

    def show_image(self, rgba: np.ndarray, window: Window) -> None:
        """Show an RGBA slice (row 0 = top edge) covering `window`."""
        self.image.setImage(rgba[::-1], autoLevels=False)  # the plot's y axis points up
        self.image.setRect(QRectF(window.u0, window.v0, window.width, window.height))
        self.window = window

    def clear_image(self) -> None:
        self.image.clear()
        self.window = None
        self.show_labels([])
        self.show_marker(None)

    def show_message(self, text: str) -> None:
        self.message.setText(text)
        self.message.setVisible(bool(text))
        self._place_message()

    def set_cut_line(self, vertical: bool | None, value: float = 0.0) -> None:
        """Show the other view's cut plane: a vertical line at u = value or a horizontal one
        at v = value; None hides it (the planes are parallel)."""
        if vertical is None:
            self.cut_line.hide()
            return
        if self.cut_line.moving:
            return  # the user is dragging it: the line is where it should be
        self.cut_line.setAngle(90 if vertical else 0)
        self.cut_line.setValue(value)
        self.cut_line.show()

    def show_labels(self, labels: list[tuple[float, float, str, QColor]]) -> None:
        """Texts centred at (u, v) in cm, each in its colour."""
        labels = labels[:MAX_LABEL_ITEMS]
        plot = self._plot()
        while len(self._labels) < len(labels):
            item = pg.TextItem(anchor=(0.5, 0.5))
            item.setFont(self._label_font)
            item.setZValue(10)
            plot.addItem(item, ignoreBounds=True)
            self._labels.append(item)
        for item, (u, v, text, color) in zip(self._labels, labels, strict=False):
            item.setText(text, color)
            item.setPos(u, v)
            item.show()
        for item in self._labels[len(labels) :]:
            item.hide()

    def label_count(self) -> int:
        return sum(1 for item in self._labels if item.isVisible())

    def show_marker(self, point: tuple[float, float] | None) -> None:
        """Mark the inspected point, or remove the mark."""
        self.marker.setData(
            [] if point is None else [point[0]], [] if point is None else [point[1]]
        )

    # -- view window -----------------------------------------------------------------

    def visible_window(self) -> Window:
        (u0, u1), (v0, v1) = self._box.viewRange()
        return Window(u0, v0, u1 - u0, v1 - v0)

    def view_size(self) -> tuple[float, float]:
        """Logical pixels of the plotting area."""
        rect = self._box.boundingRect()
        return rect.width(), rect.height()

    def show_range(self, u_range: tuple[float, float], v_range: tuple[float, float]) -> None:
        self._box.setRange(xRange=u_range, yRange=v_range, padding=0.02)

    def show_window(self, center: tuple[float, float], width: float) -> None:
        """Centre the view on `center`, `width` cm wide (the height follows the aspect)."""
        w, h = self.view_size()
        height = width * (h / w) if w > 0 else width
        cu, cv = center
        self._box.setRange(
            xRange=(cu - width / 2, cu + width / 2),
            yRange=(cv - height / 2, cv + height / 2),
            padding=0.0,
        )

    def centre_on(self, point: tuple[float, float]) -> None:
        """Pan so that `point` is in the middle, keeping the zoom."""
        window = self.visible_window()
        self.show_window(point, window.width)

    def set_colors(self, background: QColor, foreground: QColor) -> None:
        self.setBackground(background)
        plot = self._plot()
        for name in ("bottom", "left"):
            axis = plot.getAxis(name)
            axis.setPen(foreground)
            axis.setTextPen(foreground)
        self.message.setStyleSheet(
            f"color: {foreground.name()}; background: transparent; padding: 12px;"
        )

    # -- events ------------------------------------------------------------------------

    def _mouse_moved(self, scene_pos: QPointF) -> None:
        if not self._box.sceneBoundingRect().contains(scene_pos):
            self.hovered.emit(None)
            return
        point = self._box.mapSceneToView(scene_pos)
        self.hovered.emit((point.x(), point.y()))

    def leaveEvent(self, event) -> None:
        self.hovered.emit(None)
        super().leaveEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_message()

    def _place_message(self) -> None:
        message = self.__dict__.get("message")  # pyqtgraph resizes before __init__ is done
        if message is not None:
            message.setGeometry(self.rect())
