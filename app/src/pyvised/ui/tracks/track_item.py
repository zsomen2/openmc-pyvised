"""The particle tracks of one geometry view: one graphics item over the slice image.

It strokes the line pieces of each colour as one cached path and draws the dots
with ``QPainter.drawPoints``; cosmetic pens keep both a few pixels wide at any
zoom, and this stays fast for tens of thousands of points, where a scatter item
would not. The many dots have square caps (round ones paint 4-9 times slower);
the few particles in flight are round. The outlined symbols (births, ends, the
marked state) are built in device pixels at paint time, so they keep their size.
"""

import os

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")

import numpy as np
import pyqtgraph as pg
import pyqtgraph.functions as fn
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF

from ...render.palette import RGB
from ...render.tracks import ViewFrame

DOT_WIDTH = {"dot": 3.5, "small": 2.0}
"""Pixels across the dot shapes."""
SYMBOL_SIZE = 4.0
"""Half-size of the outlined symbols [px]."""
HEAD_WIDTH = 8.0
MARK_SIZE = 9.0
LINE_WIDTH = 1.0
MARK_COLOR = QColor(255, 0, 200)
OUTLINE = QColor(25, 25, 25)


def _ring(corners: int = 12) -> np.ndarray:
    angles = np.linspace(0.0, 2 * np.pi, corners + 1)
    points = np.stack([np.cos(angles), np.sin(angles)], axis=1)
    return np.stack([points[:-1], points[1:]], axis=1)


def _outline(*corners: tuple[float, float]) -> np.ndarray:
    points = np.array([*corners, corners[0]], dtype=float)
    return np.stack([points[:-1], points[1:]], axis=1)


TEMPLATES: dict[str, np.ndarray] = {
    "ring": _ring(),
    "cross": np.array([[[-1, -1], [1, 1]], [[-1, 1], [1, -1]]], dtype=float),
    "square": _outline((-0.85, -0.85), (0.85, -0.85), (0.85, 0.85), (-0.85, 0.85)),
    "triangle": _outline((0.0, -1.2), (1.05, 0.7), (-1.05, 0.7)),  # device y points down
}
"""Unit segments (s, 2, 2) of the outlined symbols."""


def polygon(uv: np.ndarray) -> QPolygonF:
    """A QPolygonF holding the (n, 2) points, filled through numpy."""
    poly = fn.create_qpolygonf(len(uv))
    fn.ndarray_from_qpolygonf(poly)[:] = uv
    return poly


def pairs_path(uv: np.ndarray) -> QPainterPath:
    """Line pieces from consecutive point pairs of the (2k, 2) array."""
    return fn.arrayToQPath(uv[:, 0], uv[:, 1], connect="pairs")


def pen(rgb: RGB | QColor, width: float, cap: Qt.PenCapStyle = Qt.PenCapStyle.RoundCap) -> QPen:
    result = QPen(rgb if isinstance(rgb, QColor) else QColor(*rgb))
    result.setCosmetic(True)
    result.setWidthF(width)
    result.setCapStyle(cap)
    return result


def symbol_path(points: np.ndarray, shape: str, size: float) -> QPainterPath:
    """Outlined symbols around the (n, 2) device points."""
    segments = points[:, None, None, :] + TEMPLATES[shape][None] * size
    return pairs_path(segments.reshape(-1, 2))


class TrackItem(pg.GraphicsObject):
    def __init__(self) -> None:
        super().__init__()
        self._lines: list[tuple[QPen, QPainterPath]] = []
        self._dots: list[tuple[QPen, QPolygonF]] = []
        self._symbols: list[tuple[str, QPen, np.ndarray]] = []
        self._heads: list[tuple[QPen, QPolygonF]] = []
        self._mark: np.ndarray | None = None
        self._bounds = QRectF()
        self.setZValue(15)  # above the labels, below the cut line and the inspect marker

    def set_bounds(self, rect: QRectF) -> None:
        if rect != self._bounds:
            self.prepareGeometryChange()
            self._bounds = QRectF(rect)

    def set_frame(self, frame: ViewFrame | None) -> None:
        self._lines, self._dots, self._symbols, self._heads = [], [], [], []
        if frame is not None:
            for rgb, uv in frame.lines:
                self._lines.append((pen(rgb, LINE_WIDTH, Qt.PenCapStyle.FlatCap), pairs_path(uv)))
            for shape, rgb, uv in frame.points:
                if shape in DOT_WIDTH:
                    square = pen(rgb, DOT_WIDTH[shape], Qt.PenCapStyle.SquareCap)
                    self._dots.append((square, polygon(uv)))
                else:
                    self._symbols.append((shape, pen(rgb, 1.5), uv))
            for rgb, uv in frame.heads:
                self._heads.append((pen(rgb, HEAD_WIDTH), polygon(uv)))
        self.update()

    def set_mark(self, uv: tuple[float, float] | None) -> None:
        """Ring the state chosen in the event table (None: no mark)."""
        self._mark = None if uv is None else np.array([uv], dtype=float)
        self.update()

    @property
    def has_content(self) -> bool:
        return bool(self._lines or self._dots or self._symbols or self._heads)

    def boundingRect(self) -> QRectF:
        return self._bounds

    def paint(self, painter: QPainter, *args) -> None:
        for line_pen, path in self._lines:
            painter.setPen(line_pen)
            painter.drawPath(path)
        for dot_pen, points in self._dots:
            painter.setPen(dot_pen)
            painter.drawPoints(points)
        if self._heads:
            outline = pen(OUTLINE, HEAD_WIDTH + 2.5)
            for _head_pen, points in self._heads:
                painter.setPen(outline)
                painter.drawPoints(points)
            for head_pen, points in self._heads:
                painter.setPen(head_pen)
                painter.drawPoints(points)
        if not self._symbols and self._mark is None:
            return
        t = painter.transform()  # item -> device pixels

        def device(uv: np.ndarray) -> np.ndarray:
            x = t.m11() * uv[:, 0] + t.m21() * uv[:, 1] + t.dx()
            y = t.m12() * uv[:, 0] + t.m22() * uv[:, 1] + t.dy()
            return np.stack([x, y], axis=1)

        painter.save()
        painter.resetTransform()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for shape, symbol_pen, uv in self._symbols:
            painter.setPen(symbol_pen)
            painter.drawPath(symbol_path(device(uv), shape, SYMBOL_SIZE))
        if self._mark is not None:
            at = device(self._mark)
            painter.setPen(pen(OUTLINE, 4.0))
            painter.drawPath(symbol_path(at, "ring", MARK_SIZE))
            painter.setPen(pen(MARK_COLOR, 2.0))
            painter.drawPath(symbol_path(at, "ring", MARK_SIZE))
        painter.restore()
