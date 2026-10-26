"""Result plots: a pyqtgraph plot that follows the theme, colours and image export."""

import os

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")

from pathlib import Path
from typing import cast

import pyqtgraph as pg
import pyqtgraph.exporters
from PySide6.QtCore import QEvent
from PySide6.QtGui import QColor, QPalette
from PySide6.QtSvg import QSvgGenerator
from PySide6.QtWidgets import QWidget

SERIES = [
    "#0072B2",
    "#D55E00",
    "#009E73",
    "#CC79A7",
    "#E69F00",
    "#56B4E9",
    "#7F7F7F",
    "#000000",
]
"""Okabe-Ito colours (safe for colour-blind readers); the last turns white on dark plots."""
REFERENCE = "#7A3DB8"
INACTIVE_ALPHA = 40


def series_color(index: int, dark: bool = False) -> QColor:
    color = QColor(SERIES[index % len(SERIES)])
    if dark and color == QColor("#000000"):
        return QColor("#FFFFFF")
    return color


class ThemedPlot(pg.PlotWidget):
    """A plot in the window's colours (light or dark), redrawn on a theme switch."""

    def __init__(self, parent: QWidget | None = None, *, title: str = "") -> None:
        super().__init__(parent)
        self.item = cast(pg.PlotItem, self.getPlotItem())
        self.item.setMenuEnabled(True)
        self.item.showGrid(x=True, y=True, alpha=0.15)
        for name in ("bottom", "left", "right"):
            self.item.getAxis(name).enableAutoSIPrefix(False)
        self.legend: pg.LegendItem | None = None
        self._title = title
        self.apply_palette()

    @property
    def dark(self) -> bool:
        base = self.palette().color(QPalette.ColorRole.Base)
        return base.lightness() < 128

    def foreground(self) -> QColor:
        return self.palette().color(QPalette.ColorRole.Text)

    def apply_palette(self) -> None:
        background = self.palette().color(QPalette.ColorRole.Base)
        foreground = self.foreground()
        self.setBackground(background)
        for name in ("bottom", "left", "right", "top"):
            axis = self.item.getAxis(name)
            axis.setPen(foreground)
            axis.setTextPen(foreground)
        if self._title:
            self.item.setTitle(self._title, color=foreground.name())
        if self.legend is not None:
            self.legend.setLabelTextColor(foreground)
            self._legend_background()

    def set_title(self, title: str) -> None:
        self._title = title
        self.item.setTitle(title, color=self.foreground().name())

    def add_legend(self) -> pg.LegendItem:
        if self.legend is None:
            self.legend = self.item.addLegend(offset=(-10, 10))
            self.legend.setLabelTextColor(self.foreground())
            self._legend_background()
        return self.legend

    def _legend_background(self) -> None:
        if self.legend is not None:
            background = QColor(self.palette().color(QPalette.ColorRole.Base))
            background.setAlpha(200)
            self.legend.setBrush(pg.mkBrush(background))

    def clear_plot(self) -> None:
        self.item.clear()
        if self.legend is not None:
            self.legend.clear()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        palette = event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange)
        if palette and "item" in self.__dict__:  # not during PlotWidget.__init__
            self.apply_palette()


def export_plot(plot: QWidget, path: Path) -> None:
    """Save the plot widget as PNG (twice its screen size) or SVG, by the file's suffix.

    SVG goes through Qt's own generator: pyqtgraph's SVG exporter fails on Qt 6 paths.
    """
    if path.suffix.lower() == ".svg":
        generator = QSvgGenerator()
        generator.setFileName(str(path))
        generator.setSize(plot.size())
        generator.setViewBox(plot.rect())
        generator.setTitle(path.stem)
        QWidget.render(plot, generator)  # QGraphicsView.render takes a QPainter instead
    elif isinstance(plot, pg.PlotWidget):
        exporter = pyqtgraph.exporters.ImageExporter(cast(pg.PlotItem, plot.getPlotItem()))
        exporter.parameters()["width"] = max(800, int(plot.width() * 2))
        exporter.export(str(path))
    elif not plot.grab().save(str(path)):
        raise OSError(f"could not write {path}")
    if not path.exists():
        raise OSError(f"could not write {path}")
