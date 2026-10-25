"""One geometry view: the coloured slice with locked 1:1 axes in cm, zoom, pan and hover.

The wheel zooms about the cursor, dragging pans, a double click asks for the
whole model again. Zooming and panning transform the current image at once;
the panel then asks the backend for a sharp slice of the new window.
"""

import os

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")

from typing import cast

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QLabel, QWidget

from ...render.views import Window


class _ViewBox(pg.ViewBox):
    doubleClicked = Signal()

    def mouseClickEvent(self, ev) -> None:
        if ev.double() and ev.button() == Qt.MouseButton.LeftButton:
            ev.accept()
            self.doubleClicked.emit()
            return
        super().mouseClickEvent(ev)


class SliceView(pg.PlotWidget):
    hovered = Signal(object)
    """(u, v) in cm under the cursor, or None when it leaves the plot."""
    windowChanged = Signal()
    """The visible window moved, was zoomed or resized."""
    resetRequested = Signal()

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
        self._box.sigRangeChanged.connect(lambda *args: self.windowChanged.emit())
        self._box.sigResized.connect(lambda *args: self.windowChanged.emit())
        self._box.doubleClicked.connect(self.resetRequested)
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

    def show_message(self, text: str) -> None:
        self.message.setText(text)
        self.message.setVisible(bool(text))
        self._place_message()

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
