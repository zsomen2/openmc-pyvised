"""Live view of a run: k per generation with OpenMC's running mean ± σ, the Shannon entropy,
and a progress line (batch, rate, time left). The inactive batches are shaded; reference
values of the model (sidecar) are dashed lines."""

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget

from pyvised_protocol.runs import RunInfo, RunProgress

from .plotting import INACTIVE_ALPHA, REFERENCE, ThemedPlot, series_color


def group_digits(value: float) -> str:
    """A count with thin spaces between the thousands (reads the same in English and Hungarian)."""
    return f"{value:,.0f}".replace(",", " ")


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "–"
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


class LivePlots(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.k_plot = ThemedPlot()
        self.k_plot.item.setLabel("left", "k")
        self.entropy_plot = ThemedPlot()
        self.entropy_plot.item.setLabel("left", self.tr("Shannon entropy"))
        self.entropy_plot.setXLink(self.k_plot)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(8)
        self.status = QLabel()
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.phase = QLabel()
        self.phase.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status, 1)
        status_row.addWidget(self.phase)
        plots = QHBoxLayout()
        plots.addWidget(self.k_plot, 3)
        plots.addWidget(self.entropy_plot, 2)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(plots, 1)
        layout.addWidget(self.progress_bar)
        layout.addLayout(status_row)
        self._references: list[tuple[str, float, float | None]] = []
        self._build_items()
        self.clear()

    def _build_items(self) -> None:
        dark = self.k_plot.dark
        color = series_color(0, dark)
        mean_color = series_color(1, dark)
        self.k_points = pg.ScatterPlotItem(size=4, pen=None, brush=pg.mkBrush(color))
        self.mean_curve = pg.PlotDataItem(pen=pg.mkPen(mean_color, width=2))
        self.band_upper = pg.PlotDataItem(pen=pg.mkPen(None))
        self.band_lower = pg.PlotDataItem(pen=pg.mkPen(None))
        band = QColor(mean_color)
        band.setAlpha(60)
        self.band = pg.FillBetweenItem(self.band_upper, self.band_lower, brush=pg.mkBrush(band))
        shade = QColor(128, 128, 128, INACTIVE_ALPHA)
        self.k_inactive = pg.LinearRegionItem(movable=False, brush=pg.mkBrush(shade))
        self.entropy_inactive = pg.LinearRegionItem(movable=False, brush=pg.mkBrush(shade))
        for region in (self.k_inactive, self.entropy_inactive):
            region.setZValue(-10)
            for line in region.lines:
                line.setPen(pg.mkPen(None))
        self.entropy_curve = pg.PlotDataItem(pen=pg.mkPen(series_color(2, dark), width=1.5))
        legend = self.k_plot.add_legend()
        for item in (self.k_inactive, self.band, self.band_upper, self.band_lower, self.k_points):
            self.k_plot.addItem(item)
        self.k_plot.addItem(self.mean_curve)
        legend.addItem(self.k_points, self.tr("k per generation"))
        legend.addItem(self.mean_curve, self.tr("mean ± σ (active)"))
        self.entropy_plot.addItem(self.entropy_inactive)
        self.entropy_plot.addItem(self.entropy_curve)
        self._reference_lines: list[pg.InfiniteLine] = []

    def clear(self) -> None:
        self.set_progress(RunProgress(), None)

    def set_references(self, references: list[tuple[str, float, float | None]]) -> None:
        """Dashed lines at k-eff references: (label, value, σ)."""
        self._references = references
        for line in self._reference_lines:
            self.k_plot.removeItem(line)
        self._reference_lines = []
        pen = pg.mkPen(REFERENCE, width=1.2, style=Qt.PenStyle.DashLine)
        fill = QColor(self.k_plot.palette().color(QPalette.ColorRole.Base))
        fill.setAlpha(210)  # the label stays legible over the k points
        for label, value, _std in references:
            line = pg.InfiniteLine(
                pos=value,
                angle=0,
                pen=pen,
                label=label,
                labelOpts={
                    "position": 0.02,
                    "color": REFERENCE,
                    "fill": pg.mkBrush(fill),
                    "anchors": [(0, 1), (0, 1)],
                },
            )
            self.k_plot.addItem(line)
            self._reference_lines.append(line)

    def set_progress(self, progress: RunProgress, run: RunInfo | None) -> None:
        g = max(1, progress.generations_per_batch)
        n = len(progress.k)
        x = np.arange(1, n + 1, dtype=np.float64)
        self.k_points.setData(x, np.asarray(progress.k, dtype=np.float64))
        mean = np.array([np.nan if m is None else m for m in progress.mean], dtype=np.float64)
        std = np.array([np.nan if s is None else s for s in progress.std], dtype=np.float64)
        active = np.isfinite(mean)
        if active.any():
            self.mean_curve.setData(x[active], mean[active])
            self.band_upper.setData(x[active], (mean + std)[active])
            self.band_lower.setData(x[active], (mean - std)[active])
        else:
            for item in (self.mean_curve, self.band_upper, self.band_lower):
                item.setData([], [])
        entropy = np.asarray(progress.entropy, dtype=np.float64)
        self.entropy_curve.setData(np.arange(1, len(entropy) + 1, dtype=np.float64), entropy)
        self.entropy_plot.setVisible(bool(len(entropy)) or run is None)
        inactive = progress.inactive * g
        for region in (self.k_inactive, self.entropy_inactive):
            region.setRegion((0.5, inactive + 0.5))
            region.setVisible(inactive > 0)
        label = self.tr("generation") if g > 1 else self.tr("batch")
        self.k_plot.item.setLabel("bottom", label)
        self.entropy_plot.item.setLabel("bottom", label)
        self._status(progress, run)

    def _status(self, progress: RunProgress, run: RunInfo | None) -> None:
        total = max(progress.batches, progress.batch, 1)
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(progress.batch)
        if run is None:
            self.status.setText(self.tr("No run yet."))
            self.phase.setText("")
            return
        parts = [self.tr("batch {batch} of {batches}").format(batch=progress.batch, batches=total)]
        if progress.mean and progress.mean[-1] is not None:
            std = progress.std[-1] or 0.0
            parts.append(f"k = {progress.mean[-1]:.5f} ± {std:.5f}")
        if progress.rate:
            parts.append(self.tr("{rate} particles/s").format(rate=group_digits(progress.rate)))
        parts.append(self.tr("elapsed {time}").format(time=format_duration(progress.elapsed)))
        if run.active and progress.eta is not None:
            parts.append(self.tr("{time} left").format(time=format_duration(progress.eta)))
        if progress.statepoint:
            parts.append(self.tr("statepoint {batch}").format(batch=progress.statepoint))
        self.status.setText(" · ".join(parts))
        phase = progress.trigger if progress.trigger else self.phase_text(progress.phase)
        self.phase.setText(phase if run.active else "")
        self.phase.setToolTip(progress.trigger)

    def phase_text(self, phase: str) -> str:
        """The backend's phase names in the UI language (OpenMC's own texts stay English)."""
        names = {
            "inactive batches": self.tr("inactive batches"),
            "active batches": self.tr("active batches"),
            "batches": self.tr("batches"),
            "Starting the simulation": self.tr("starting the simulation"),
            "Finishing": self.tr("finishing"),
            "Initializing source particles": self.tr("sampling the source"),
            "Preparing distributed cell instances": self.tr("preparing the geometry"),
            "Writing summary.h5 file": self.tr("writing summary.h5"),
        }
        if phase in names:
            return names[phase]
        if phase.startswith("Reading "):
            return self.tr("reading {name}").format(name=phase.removeprefix("Reading "))
        return phase
