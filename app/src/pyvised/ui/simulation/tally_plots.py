"""Tally plots chosen from the filters, and convergence from the run's statepoints.

- energy filter: step histogram on log-log axes, optionally per unit lethargy, eV or MeV;
- 1-D mesh (or one axis of a larger mesh): profile with error bars, one curve per energy
  group when the tally also has a small energy filter;
- 2-D mesh: heat map with the geometry's cell outlines from a slice of the same plane;
- cell, surface, material … filters: bars;
- convergence: the mean ± σ, the relative error and the figure of merit at each statepoint,
  also for k-eff.

Log axes are drawn from log10 values (pyqtgraph's error bars ignore log mode), so the
axes only label the decades.
"""

import csv
import io
import math
from collections.abc import Callable
from pathlib import Path
from typing import cast

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from pyvised_protocol.runs import Convergence, RunInfo, RunResults, TallyFilterResult, TallyResult
from pyvised_protocol.schemas import SliceRequest
from pyvised_protocol.sidecar import Sidecar

from ...backend.client import TallyData
from ...render import tallies as tm
from ...render.colorize import LineMode, boundaries
from . import names
from .plotting import ThemedPlot, export_plot, series_color

KEFF = -1
"""Combo value of the k-eff entry (convergence only)."""
MAX_GROUP_CURVES = 12
OUTLINE_PIXELS = 700
BASIS_OF_AXES = {(0, 1): "xy", (0, 2): "xz", (1, 2): "yz"}
TINY = 1e-300

OutlineSource = Callable[[SliceRequest, Callable[[object], None]], bool]
Grid = tuple[str, str, str, np.ndarray, np.ndarray, np.ndarray, np.ndarray]


def log10(values: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.log10(np.where(values > 0, values, np.nan))
    return out


def axis_names(f: TallyFilterResult | None) -> tuple[str, str, str]:
    """The mesh's axes: x y z, or r φ z (cylindrical), or r θ φ (spherical)."""
    kind = f.mesh.kind if f is not None and f.mesh is not None else "regular"
    return {"cylindrical": ("r", "φ", "z"), "spherical": ("r", "θ", "φ")}.get(kind, ("x", "y", "z"))


def axis_label(name: str) -> str:
    return f"{name} [{'rad' if name in ('φ', 'θ') else 'cm'}]"


def number_text(value: float) -> str:
    return repr(float(value)) if math.isfinite(value) else ""


class TallyPlots(QWidget):
    tallyChanged = Signal(int)
    exportRequested = Signal(str)
    """"image" or "data"."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.run: RunInfo | None = None
        self.results: RunResults | None = None
        self.sidecar: Sidecar | None = None
        self.data: TallyData | None = None
        self.outline_source: OutlineSource | None = None
        self.series: list[tuple[str, np.ndarray, np.ndarray, np.ndarray]] = []
        """What the plot shows, for the CSV export: (name, x, y, σ)."""
        self.grid: Grid | None = None
        """What the heat map shows, for the CSV export: (name, x label, y label, x centres,
        y centres, values, σ), the arrays indexed [x, y]."""
        self._outline_key: tuple | None = None

        self.tally = QComboBox()
        self.tally.currentIndexChanged.connect(self._tally_chosen)
        self.kind = QComboBox()
        self.kind.currentIndexChanged.connect(lambda *_: self._kind_chosen())
        self.score = QComboBox()
        self.score.currentIndexChanged.connect(lambda *_: self.redraw())
        self.nuclide = QComboBox()
        self.nuclide.currentIndexChanged.connect(lambda *_: self.redraw())
        self.lethargy = QCheckBox(self.tr("per lethargy"))
        self.mev = QCheckBox("MeV")
        self.log_x = QCheckBox(self.tr("log x"))
        self.log_y = QCheckBox(self.tr("log y"))
        self.normalised = QCheckBox(self.tr("normalised"))
        self.error_bars = QCheckBox(self.tr("error bars"))
        self.error_bars.setChecked(True)
        self.outlines = QCheckBox(self.tr("cell outlines"))
        self.outlines.setChecked(True)
        self.axes = QComboBox()
        self.position_boxes: list[QSpinBox] = []
        """One per mesh axis: the bin of an axis that is not plotted."""
        for _axis in range(3):
            position = QSpinBox()
            position.setVisible(False)
            position.valueChanged.connect(lambda *_: self.redraw())
            self.position_boxes.append(position)
        for box in (
            self.lethargy,
            self.mev,
            self.log_x,
            self.log_y,
            self.normalised,
            self.error_bars,
            self.outlines,
        ):
            box.toggled.connect(lambda *_: self.redraw())
        self.axes.currentIndexChanged.connect(lambda *_: self.redraw())
        self.bin_boxes: list[tuple[int, QComboBox]] = []
        self.bins_row = QHBoxLayout()
        self.bins_row.setContentsMargins(0, 0, 0, 0)
        self.image_button = QPushButton(self.tr("Export image…"))
        self.image_button.clicked.connect(lambda: self.exportRequested.emit("image"))
        self.data_button = QPushButton(self.tr("Export data…"))
        self.data_button.clicked.connect(lambda: self.exportRequested.emit("data"))
        self.message = QLabel()
        self.message.setWordWrap(True)

        self.line = ThemedPlot()
        self.heat = ThemedPlot()
        self.heat.item.getViewBox().setAspectLocked(True)
        self.colorbar: pg.ColorBarItem | None = None
        self.value_plot = ThemedPlot()
        self.error_plot = ThemedPlot()
        self.error_plot.setXLink(self.value_plot)
        self.fom_box = pg.ViewBox()
        self.error_plot.item.showAxis("right")
        self.error_plot.item.scene().addItem(self.fom_box)
        self.error_plot.item.getAxis("right").linkToView(self.fom_box)
        self.fom_box.setXLink(self.error_plot.item)
        self.error_plot.item.getViewBox().sigResized.connect(self._sync_fom_box)
        convergence = QWidget()
        convergence_layout = QVBoxLayout(convergence)
        convergence_layout.setContentsMargins(0, 0, 0, 0)
        convergence_layout.addWidget(self.value_plot, 3)
        convergence_layout.addWidget(self.error_plot, 2)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.line)
        self.stack.addWidget(self.heat)
        self.stack.addWidget(convergence)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel(self.tr("Tally:")))
        row1.addWidget(self.tally, 2)
        row1.addWidget(QLabel(self.tr("Plot:")))
        row1.addWidget(self.kind, 1)
        row1.addWidget(self.score)
        row1.addWidget(self.nuclide)
        row1.addStretch()
        row1.addWidget(self.image_button)
        row1.addWidget(self.data_button)
        row2 = QHBoxLayout()
        for widget in (
            self.lethargy,
            self.mev,
            self.log_x,
            self.log_y,
            self.normalised,
            self.error_bars,
            self.outlines,
            self.axes,
            *self.position_boxes,
        ):
            row2.addWidget(widget)
        row2.addLayout(self.bins_row)
        row2.addStretch()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(row1)
        layout.addLayout(row2)
        layout.addWidget(self.message)
        layout.addWidget(self.stack, 1)
        self.set_results(None, None, None)

    # -- inputs -------------------------------------------------------------------------------

    @property
    def tally_id(self) -> int | None:
        value = self.tally.currentData()
        return int(value) if value is not None else None

    def current_tally(self) -> TallyResult | None:
        tid = self.tally_id
        return (
            next((t for t in self.results.tallies if t.id == tid), None) if self.results else None
        )

    def set_results(
        self, run: RunInfo | None, results: RunResults | None, sidecar: Sidecar | None
    ) -> None:
        if results is not None and results is self.results:  # a refresh: keep the plot
            changed = sidecar is not self.sidecar
            self.run, self.sidecar = run, sidecar
            if changed:
                self.redraw()
            return
        keep = self.tally_id
        self.run, self.results, self.sidecar = run, results, sidecar
        self.data = None
        self.tally.blockSignals(True)
        self.tally.clear()
        if results is not None:
            for t in results.tallies:
                self.tally.addItem(f"{t.id}  {t.name}".rstrip(), t.id)
            if results.keff_convergence is not None:
                self.tally.addItem(self.tr("k-eff (convergence)"), KEFF)
        index = self.tally.findData(keep) if keep is not None else -1
        self.tally.setCurrentIndex(max(0, index))
        self.tally.blockSignals(False)
        self._tally_chosen()

    def set_sidecar(self, sidecar: Sidecar | None) -> None:
        self.sidecar = sidecar
        self.redraw()

    def set_data(self, data: TallyData) -> None:
        if data.tally.id == self.tally_id:
            self.data = data
            self.redraw()

    def _tally_chosen(self, *args) -> None:
        self.data = None
        tally = self.current_tally()
        self.kind.blockSignals(True)
        self.kind.clear()
        kind_names = {
            tm.PlotKind.SPECTRUM: self.tr("energy spectrum"),
            tm.PlotKind.MESH_1D: self.tr("profile along the mesh"),
            tm.PlotKind.MESH_2D: self.tr("mesh heat map"),
            tm.PlotKind.BARS: self.tr("bars"),
            tm.PlotKind.CONVERGENCE: self.tr("convergence"),
        }
        if tally is not None:
            for kind, index in tm.plot_kinds(tally):
                text = kind_names[kind]
                if index >= 0 and sum(1 for k, _ in tm.plot_kinds(tally) if k is kind) > 1:
                    text += f" ({names.filter_title(tally.filters[index])} {index + 1})"
                self.kind.addItem(text, (kind.value, index))
        elif self.tally_id == KEFF:
            self.kind.addItem(
                kind_names[tm.PlotKind.CONVERGENCE], (tm.PlotKind.CONVERGENCE.value, -1)
            )
        self.kind.blockSignals(False)
        for combo, values in (
            (self.score, tally.scores if tally else []),
            (self.nuclide, tally.nuclides if tally else []),
        ):
            combo.blockSignals(True)
            combo.clear()
            for i, value in enumerate(values):
                combo.addItem(value, i)
            combo.setVisible(len(values) > 1)
            combo.blockSignals(False)
        self._kind_chosen()
        if tally is not None:
            self.tallyChanged.emit(tally.id)

    def plot_kind(self) -> tuple[tm.PlotKind | None, int]:
        data = self.kind.currentData()
        if data is None:
            return None, -1
        return tm.PlotKind(data[0]), int(data[1])

    def _kind_chosen(self) -> None:
        kind, index = self.plot_kind()
        tally = self.current_tally()
        spectrum = kind is tm.PlotKind.SPECTRUM
        for box in (self.lethargy, self.mev):
            box.setVisible(spectrum)
        self.log_x.setVisible(spectrum)
        self.log_y.setVisible(kind is not None and kind is not tm.PlotKind.CONVERGENCE)
        self.error_bars.setVisible(
            kind in (tm.PlotKind.SPECTRUM, tm.PlotKind.MESH_1D, tm.PlotKind.BARS)
        )
        self.normalised.setVisible(kind is not None and self.tally_id != KEFF)
        self.outlines.setVisible(kind is tm.PlotKind.MESH_2D)
        if spectrum:
            self.log_x.setChecked(True)
            self.log_y.setChecked(True)
        mesh = tally.filters[index] if tally is not None and index >= 0 else None
        self._fill_axes(kind, mesh)
        self._fill_bins(tally, index, kind)
        self.redraw()

    def _fill_axes(self, kind: tm.PlotKind | None, f: TallyFilterResult | None) -> None:
        self.axes.blockSignals(True)
        self.axes.clear()
        labels = axis_names(f)
        axes = tm.mesh_axes(f) if f is not None else []
        if kind is tm.PlotKind.MESH_2D:
            for i, a in enumerate(axes):
                for b in axes[i + 1 :]:
                    self.axes.addItem(f"{labels[a]}–{labels[b]}", (a, b))
        elif kind is tm.PlotKind.MESH_1D:
            for a in axes:
                self.axes.addItem(labels[a], (a,))
        self.axes.setVisible(self.axes.count() > 1)
        self.axes.blockSignals(False)
        dims = f.mesh.dimension if f is not None and f.mesh is not None else []
        for axis, position in enumerate(self.position_boxes):
            n = dims[axis] if axis < len(dims) else 1
            position.blockSignals(True)
            position.setRange(1, max(1, n))
            position.setValue((n + 1) // 2)  # the middle bin
            position.setPrefix(f"{labels[axis]}: " + self.tr("bin "))
            position.setSuffix(f" / {n}")
            position.setVisible(False)
            position.blockSignals(False)

    def _positions(self, f: TallyFilterResult, plotted: tuple[int, ...]) -> list[int]:
        """The chosen bin of each mesh axis (0-based); shows the controls of the axes with
        more than one bin that are not plotted."""
        dims = f.mesh.dimension if f.mesh is not None else []
        for axis, position in enumerate(self.position_boxes):
            position.setVisible(axis < len(dims) and dims[axis] > 1 and axis not in plotted)
        return [position.value() - 1 for position in self.position_boxes]

    def _fill_bins(self, tally: TallyResult | None, index: int, kind: tm.PlotKind | None) -> None:
        while self.bins_row.count():
            item = self.bins_row.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self.bin_boxes = []
        if tally is None:
            return
        grouped = self._group_filter(tally, index, kind)
        for f_index, f in enumerate(tally.filters):
            if f_index in (index, grouped) or f.bins <= 1:
                continue
            combo = QComboBox()
            combo.setToolTip(names.filter_title(f))
            for b in range(min(f.bins, 5000)):
                combo.addItem(f"{names.filter_title(f)}: {tm.bin_label(f, b)}", b)
            combo.currentIndexChanged.connect(lambda *_: self.redraw())
            self.bins_row.addWidget(combo)
            self.bin_boxes.append((f_index, combo))

    @staticmethod
    def _group_filter(tally: TallyResult, index: int, kind: tm.PlotKind | None) -> int | None:
        """The energy filter whose groups get one curve each in a mesh profile."""
        if kind is not tm.PlotKind.MESH_1D:
            return None
        for f_index, f in enumerate(tally.filters):
            if f_index != index and f.kind == "energy" and 1 < f.bins <= MAX_GROUP_CURVES:
                return f_index
        return None

    def fixed_bins(self, tally: TallyResult) -> list[int]:
        fixed = [0] * len(tally.filters)
        for f_index, combo in self.bin_boxes:
            fixed[f_index] = int(combo.currentData() or 0)
        return fixed

    def scale(self) -> tuple[float, str]:
        tid = self.tally_id
        norm = self.sidecar.tallies.get(tid) if self.sidecar and tid is not None else None
        if not self.normalised.isChecked() or norm is None:
            return 1.0, ""
        return tm.factor(norm), norm.unit

    # -- drawing --------------------------------------------------------------------------------

    def redraw(self) -> None:
        self.series = []
        self.grid = None
        kind, index = self.plot_kind()
        tally = self.current_tally()
        self.message.setText("")
        if kind is tm.PlotKind.CONVERGENCE:
            self.stack.setCurrentIndex(2)
            self._convergence(tally)
            return
        if kind is None or tally is None:
            self.stack.setCurrentIndex(0)
            self.line.clear_plot()
            if self.results is None:
                self.message.setText(self.tr("Pick a run with results."))
            elif not self.results.tallies and self.results.keff_convergence is None:
                self.message.setText(self.tr("The run has no tallies."))
            return
        if self.data is None:
            self.stack.setCurrentIndex(0)
            self.line.clear_plot()
            self.message.setText(self.tr("Loading the tally…"))
            return
        if kind is tm.PlotKind.MESH_2D:
            self.stack.setCurrentIndex(1)
            self._heat_map(tally, index)
        else:
            self.stack.setCurrentIndex(0)
            if kind is tm.PlotKind.SPECTRUM:
                self._spectrum(tally, index)
            elif kind is tm.PlotKind.MESH_1D:
                self._profile(tally, index)
            else:
                self._bars(tally, index)

    def _values(
        self, tally: TallyResult, axis: int, fixed: list[int]
    ) -> tuple[np.ndarray, np.ndarray]:
        assert self.data is not None
        nuclide = int(self.nuclide.currentData() or 0)
        score = int(self.score.currentData() or 0)
        factor, _unit = self.scale()
        mean = tm.select(self.data.mean, axis, fixed, nuclide, score) * factor
        std = tm.select(self.data.std, axis, fixed, nuclide, score) * factor
        return mean, std

    def _y_label(self, tally: TallyResult, extra: str = "") -> str:
        score = tally.scores[int(self.score.currentData() or 0)] if tally.scores else ""
        _factor, unit = self.scale()
        label = score + (f" [{unit}]" if unit else "") + extra
        return label

    def _draw_steps(
        self,
        plot: ThemedPlot,
        edges: np.ndarray,
        y: np.ndarray,
        std: np.ndarray,
        color: QColor,
        name: str,
        *,
        log_x: bool,
        log_y: bool,
    ) -> None:
        x_edges = log10(edges) if log_x else edges
        centres = (x_edges[1:] + x_edges[:-1]) / 2
        y_shown = log10(y) if log_y else y
        finite = np.isfinite(y_shown) & np.isfinite(centres)
        curve = pg.PlotDataItem(
            x_edges,
            np.where(np.isfinite(y_shown), y_shown, np.nan),
            stepMode="center",
            pen=pg.mkPen(color, width=1.8),
            connect="finite",
            name=name or None,
        )
        plot.addItem(curve)
        if self.error_bars.isChecked() and finite.any():
            if log_y:
                top = log10(y + std) - y_shown
                low = np.maximum(y - std, y * 1e-3)
                bottom = y_shown - log10(low)
            else:
                top = bottom = std
            ok = finite & np.isfinite(top) & np.isfinite(bottom)
            bars = pg.ErrorBarItem(
                x=centres[ok],
                y=y_shown[ok],
                top=top[ok],
                bottom=bottom[ok],
                beam=0.0,
                pen=pg.mkPen(color, width=1),
            )
            plot.addItem(bars)

    def _prepare_line_plot(self, log_x: bool, log_y: bool) -> None:
        self.line.clear_plot()
        self.line.item.getAxis("bottom").setLogMode(log_x)
        self.line.item.getAxis("left").setLogMode(log_y)
        self.line.item.getAxis("bottom").setTicks(None)

    def _spectrum(self, tally: TallyResult, index: int) -> None:
        f = tally.filters[index]
        log_x, log_y = self.log_x.isChecked(), self.log_y.isChecked()
        self._prepare_line_plot(log_x, log_y)
        y, std = self._values(tally, index, self.fixed_bins(tally))
        edges = np.asarray(f.edges, dtype=np.float64)
        if self.lethargy.isChecked():
            widths = tm.lethargy_widths(edges)
            y, std = y / widths, std / widths
        if log_x and edges[0] <= 0 and len(edges) > 1:
            edges = edges.copy()
            edges[0] = edges[1] / 100.0  # a zero lower edge has no place on a log axis
            self.message.setText(
                self.tr("The lowest bin starts at 0; it is drawn from {e} eV.").format(
                    e=f"{edges[0]:.3g}"
                )
            )
        unit = "MeV" if self.mev.isChecked() else "eV"
        shown = edges / 1e6 if unit == "MeV" else edges
        self._draw_steps(
            self.line, shown, y, std, series_color(0, self.line.dark), "", log_x=log_x, log_y=log_y
        )
        self.line.item.setLabel("bottom", self.tr("energy [{unit}]").format(unit=unit))
        extra = " " + self.tr("per unit lethargy") if self.lethargy.isChecked() else ""
        self.line.item.setLabel("left", self._y_label(tally, extra))
        self.line.set_title(f"{tally.id} {tally.name}".strip())
        self.series = [(tally.name or str(tally.id), shown, y, std)]

    def _mesh_axis_choice(self, default: tuple[int, ...]) -> tuple[int, ...]:
        data = self.axes.currentData()
        return tuple(data) if data is not None else default

    def _profile(self, tally: TallyResult, index: int) -> None:
        f = tally.filters[index]
        axes = tm.mesh_axes(f) or [0]
        (axis,) = self._mesh_axis_choice((axes[0],))
        log_y = self.log_y.isChecked()
        self._prepare_line_plot(False, log_y)
        fixed = self.fixed_bins(tally)
        group = self._group_filter(tally, index, tm.PlotKind.MESH_1D)
        groups = range(tally.filters[group].bins) if group is not None else [None]
        legend = self.line.add_legend() if group is not None else None
        edges = tm.mesh_edges(f, axis)
        positions = self._positions(f, (axis,))
        for n, g in enumerate(groups):
            if group is not None and g is not None:
                fixed[group] = g
            y, std = self._values(tally, index, fixed)
            line = tm.mesh_values(f, y, [axis], positions)
            sigma = tm.mesh_values(f, std, [axis], positions)
            name = (
                tm.bin_label(tally.filters[group], g) + " eV"
                if group is not None and g is not None
                else ""
            )
            color = series_color(n, self.line.dark)
            self._draw_steps(self.line, edges, line, sigma, color, name, log_x=False, log_y=log_y)
            self.series.append((name or (tally.name or str(tally.id)), edges, line, sigma))
        if legend is not None:
            legend.setVisible(True)
        self.line.item.setLabel("bottom", axis_label(axis_names(f)[axis]))
        self.line.item.setLabel("left", self._y_label(tally))
        self.line.set_title(f"{tally.id} {tally.name}".strip())

    def _bars(self, tally: TallyResult, index: int) -> None:
        f = tally.filters[index]
        log_y = self.log_y.isChecked()
        self._prepare_line_plot(False, log_y)
        y, std = self._values(tally, index, self.fixed_bins(tally))
        x = np.arange(len(y), dtype=np.float64)
        shown = log10(y) if log_y else y
        color = series_color(0, self.line.dark)
        base = 0.0
        if log_y:  # bars rise from below the smallest value, not from 10^0
            finite = shown[np.isfinite(shown)]
            base = float(np.floor(finite.min() - 0.1)) if finite.size else 0.0
        heights = np.nan_to_num(shown, nan=base) - base
        bars = pg.BarGraphItem(x=x, y0=base, height=heights, width=0.6, brush=pg.mkBrush(color))
        self.line.addItem(bars)
        if self.error_bars.isChecked() and not log_y:
            self.line.addItem(
                pg.ErrorBarItem(
                    x=x, y=y, top=std, bottom=std, beam=0.2, pen=pg.mkPen(self.line.foreground())
                )
            )
        labels = [(float(i), tm.bin_label(f, i)) for i in range(len(y))]
        step = max(1, len(labels) // 30)
        self.line.item.getAxis("bottom").setTicks([labels[::step]])
        self.line.item.setLabel("bottom", names.filter_title(f))
        self.line.item.setLabel("left", self._y_label(tally))
        self.line.set_title(f"{tally.id} {tally.name}".strip())
        self.series = [(tally.name or str(tally.id), x, y, std)]

    def _heat_map(self, tally: TallyResult, index: int) -> None:
        f = tally.filters[index]
        axes = tm.mesh_axes(f)
        a, b = self._mesh_axis_choice((axes[0], axes[1]))
        positions = self._positions(f, (a, b))
        y, std = self._values(tally, index, self.fixed_bins(tally))
        grid = tm.mesh_values(f, y, [a, b], positions)
        x_edges, y_edges = tm.mesh_edges(f, a), tm.mesh_edges(f, b)
        log = self.log_y.isChecked()
        shown = log10(grid) if log else grid
        self.heat.clear_plot()
        cmap = cast(pg.ColorMap, pg.colormap.get("viridis"))
        finite = shown[np.isfinite(shown)]
        levels = (float(finite.min()), float(finite.max())) if finite.size else (0.0, 1.0)
        if levels[0] == levels[1]:
            levels = (levels[0] - 0.5, levels[1] + 0.5)
        uniform = all(np.allclose(np.diff(e), np.diff(e)[0]) for e in (x_edges, y_edges))
        if uniform:
            image = pg.ImageItem(np.nan_to_num(shown, nan=levels[0]), axisOrder="col-major")
            image.setRect(
                x_edges[0], y_edges[0], x_edges[-1] - x_edges[0], y_edges[-1] - y_edges[0]
            )
            image.setLookupTable(cast(np.ndarray, cmap.getLookupTable(nPts=256)))
            image.setLevels(levels)
            item = image
        else:
            xx, yy = np.meshgrid(x_edges, y_edges, indexing="ij")
            item = pg.PColorMeshItem(xx, yy, np.nan_to_num(shown, nan=levels[0]), colorMap=cmap)
            item.setLevels(levels)
        self.heat.addItem(item)
        title = self._y_label(tally) + (" (log10)" if log else "")
        if self.colorbar is None:  # inserted into the plot's layout once, then reused
            self.colorbar = pg.ColorBarItem(values=levels, colorMap=cmap, interactive=False)
            self.colorbar.setImageItem(item, insert_in=self.heat.item)
        else:
            self.colorbar.setImageItem(item)
        self.colorbar.setLevels(levels)
        self.colorbar.getAxis("left").setLabel(title)
        labels = axis_names(f)
        self.heat.item.setLabel("bottom", axis_label(labels[a]))
        self.heat.item.setLabel("left", axis_label(labels[b]))
        self.heat.set_title(f"{tally.id} {tally.name}".strip())
        self.grid = (
            tally.name or str(tally.id),
            axis_label(labels[a]),
            axis_label(labels[b]),
            (x_edges[1:] + x_edges[:-1]) / 2,
            (y_edges[1:] + y_edges[:-1]) / 2,
            grid,
            tm.mesh_values(f, std, [a, b], positions),
        )
        cartesian = f.mesh is not None and f.mesh.kind in ("regular", "rectilinear")
        if self.outlines.isChecked() and cartesian and (a, b) in BASIS_OF_AXES:
            third = 3 - a - b
            if f.mesh is not None and third < len(f.mesh.grids) and f.mesh.grids[third]:
                g = tm.mesh_edges(f, third)
                p = min(positions[third], len(g) - 2)
                cut = float((g[p] + g[p + 1]) / 2)
            else:
                cut = 0.0
            self._request_outline(BASIS_OF_AXES[(a, b)], x_edges, y_edges, cut, a, b, third)

    def _request_outline(self, basis, x_edges, y_edges, cut, a, b, third) -> None:
        if self.outline_source is None:
            return
        width = float(x_edges[-1] - x_edges[0])
        height = float(y_edges[-1] - y_edges[0])
        if width <= 0 or height <= 0:
            return
        scale = OUTLINE_PIXELS / max(width, height)
        pixels = (max(16, round(width * scale)), max(16, round(height * scale)))
        origin = [0.0, 0.0, 0.0]
        origin[a] = float((x_edges[0] + x_edges[-1]) / 2)
        origin[b] = float((y_edges[0] + y_edges[-1]) / 2)
        origin[third] = cut
        request = SliceRequest(
            basis=basis,
            origin=(origin[0], origin[1], origin[2]),
            width=(width, height),
            pixels=pixels,
            show_overlaps=False,
        )
        key = (request.model_dump_json(),)
        self._outline_key = key
        rect = (float(x_edges[0]), float(y_edges[0]), width, height)
        self.outline_source(request, lambda data: self._outline_ready(key, data, rect))

    def _outline_ready(self, key, data, rect) -> None:
        if key != self._outline_key or self.stack.currentIndex() != 1:
            return
        edge = boundaries(data, LineMode.CELL, 1)
        rgba = np.zeros((*edge.shape, 4), dtype=np.uint8)
        color = self.heat.foreground() if not self.heat.dark else QColor("white")
        rgba[edge] = (color.red(), color.green(), color.blue(), 200)
        overlay = pg.ImageItem(rgba, axisOrder="row-major")
        overlay.setRect(rect[0], rect[1] + rect[3], rect[2], -rect[3])  # row 0 is the top edge
        overlay.setZValue(10)
        self.heat.addItem(overlay)

    def _convergence_series(self, tally: TallyResult | None) -> tuple[Convergence | None, int, str]:
        results = self.results
        if results is None:
            return None, 0, ""
        if self.tally_id == KEFF or tally is None:
            return results.keff_convergence, 0, "k-eff"
        convergence = results.convergence.get(tally.id)
        if convergence is None:
            return None, 0, ""
        nuclide = int(self.nuclide.currentData() or 0)
        score = int(self.score.currentData() or 0)
        n_scores = max(1, len(tally.scores))
        if convergence.binned:
            fixed = self.fixed_bins(tally)
            index = int(np.ravel_multi_index((*fixed, nuclide, score), tuple(tally.shape)))
            parts = [
                tm.bin_label(f, b) for f, b in zip(tally.filters, fixed, strict=True) if f.bins > 1
            ]
            name = ", ".join(parts)
        else:
            index = nuclide * n_scores + score
            name = self.tr("total")
        return convergence, index, name

    def _convergence(self, tally: TallyResult | None) -> None:
        self.value_plot.clear_plot()
        self.error_plot.clear_plot()
        self.fom_box.clear()
        convergence, index, name = self._convergence_series(tally)
        if convergence is None or not convergence.batches:
            self.message.setText(self.tr("The run kept no statepoints for this tally."))
            return
        batches = np.asarray(convergence.batches, dtype=np.float64)
        mean = np.array(
            [
                np.nan if (v := m[index] if index < len(m) else None) is None else v
                for m in convergence.mean
            ],
            dtype=np.float64,
        )
        std = np.array(
            [
                np.nan if (v := s[index] if index < len(s) else None) is None else v
                for s in convergence.std
            ],
            dtype=np.float64,
        )
        factor = self.scale()[0] if self.tally_id != KEFF else 1.0
        mean, std = mean * factor, std * factor
        dark = self.value_plot.dark
        color = series_color(0, dark)
        x_label = self.tr("batch")
        self.value_plot.addItem(
            pg.PlotDataItem(
                batches,
                mean,
                pen=pg.mkPen(color, width=2),
                symbol="o",
                symbolSize=5,
                symbolBrush=color,
            )
        )
        band = QColor(color)
        band.setAlpha(60)
        upper, lower = pg.PlotDataItem(batches, mean + std), pg.PlotDataItem(batches, mean - std)
        self.value_plot.addItem(pg.FillBetweenItem(upper, lower, brush=pg.mkBrush(band)))
        label = "k-eff" if self.tally_id == KEFF else self._y_label(tally) if tally else ""
        self.value_plot.item.setLabel("left", label)
        self.value_plot.set_title(name)
        with np.errstate(divide="ignore", invalid="ignore"):
            rel = np.abs(std / mean)
        self.error_plot.addItem(
            pg.PlotDataItem(
                batches,
                100 * rel,
                pen=pg.mkPen(series_color(1, dark), width=2),
                symbol="s",
                symbolSize=5,
                name=self.tr("rel. error [%]"),
            )
        )
        seconds = np.asarray(convergence.seconds, dtype=np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            fom = 1.0 / (rel * rel * seconds / 60.0)
        fom[~np.isfinite(fom)] = np.nan
        fom_curve = pg.PlotDataItem(
            batches, fom, pen=pg.mkPen(series_color(2, dark), width=1.5, style=Qt.PenStyle.DashLine)
        )
        self.fom_box.addItem(fom_curve)
        self.error_plot.item.setLabel("left", self.tr("rel. error [%]"))
        self.error_plot.item.setLabel("right", self.tr("FOM (dashed)"))
        self.error_plot.item.setLabel("bottom", x_label)
        self.error_plot.item.getAxis("right").setPen(self.error_plot.foreground())
        self.series = [
            (name or label, batches, mean, std),
            (self.tr("rel. error"), batches, rel, np.full_like(rel, np.nan)),
            ("FOM", batches, fom, np.full_like(fom, np.nan)),
        ]
        self._sync_fom_box()

    def _sync_fom_box(self) -> None:
        box = self.error_plot.item.getViewBox()
        self.fom_box.setGeometry(box.sceneBoundingRect())
        self.fom_box.linkedViewChanged(box, self.fom_box.XAxis)

    # -- export ----------------------------------------------------------------------------

    def current_plot(self) -> QWidget:
        """The plot on screen (the convergence view: both of its plots)."""
        return self.stack.currentWidget() or self.line

    def save_image(self, path: Path) -> None:
        export_plot(self.current_plot(), path)

    def csv_text(self) -> str:
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        if self.grid is not None:  # one row per mesh cell, x fastest
            name, x_label, y_label, xs, ys, values, std = self.grid
            writer.writerow(["series", x_label, y_label, "value", "std. dev."])
            for j, yj in enumerate(ys):
                for i, xi in enumerate(xs):
                    writer.writerow(
                        [name, f"{xi:.8g}", f"{yj:.8g}", number_text(values[i, j]),
                         number_text(std[i, j])]
                    )  # fmt: skip
            return out.getvalue()
        writer.writerow(["series", "x", "value", "std. dev."])
        for name, x, y, std in self.series:
            centres = (x[1:] + x[:-1]) / 2 if len(x) == len(y) + 1 else x
            for xi, yi, si in zip(centres, y, std, strict=False):
                writer.writerow([name, f"{xi:.8g}", number_text(yi), number_text(si)])
        return out.getvalue()
