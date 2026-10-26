"""The runs of the open model: a table to pick one (its results fill the other tabs) or
several to compare, k-eff or a tally total against the run or a builder parameter, and the
reactivity differences between them (in pcm, and $ with the sidecar's β_eff)."""

import csv
import io
import math

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QItemSelection, QItemSelectionModel, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pyvised_protocol.runs import RunInfo, RunResults
from pyvised_protocol.sidecar import Sidecar

from ...render import tallies as tm
from .live_plots import format_duration, group_digits
from .plotting import ThemedPlot, series_color
from .summary_view import local_time

RUN_ROLE = Qt.ItemDataRole.UserRole


def params_text(params: dict[str, float]) -> str:
    return ", ".join(f"{k} = {v:g}" for k, v in params.items())


def run_size(run: RunInfo) -> str:
    plan = run.plan
    if plan is None:
        return "–"
    text = f"{group_digits(plan.particles)} × {run.batches_done or plan.batches}"
    if plan.inactive:
        text += f" ({plan.inactive})"
    return text


class HistoryView(QWidget):
    showRequested = Signal(str)
    """Run id whose results the other tabs should show."""
    compareRequested = Signal(list)
    """Run ids to compare (their results are loaded, then `show_comparison` follows)."""
    openFolderRequested = Signal(str)
    deleteRequested = Signal(str)
    refreshRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.runs: list[RunInfo] = []
        self.sidecar: Sidecar | None = None
        self._compared: list[tuple[RunInfo, RunResults]] = []
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            [
                self.tr("Started"),
                self.tr("Label"),
                self.tr("State"),
                self.tr("Particles × batches"),
                self.tr("Parameters"),
                self.tr("Library"),
                self.tr("k-eff"),
                self.tr("Run time"),
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemDoubleClicked.connect(lambda *_: self._show())
        self.show_button = QPushButton(self.tr("Show results"))
        self.show_button.clicked.connect(self._show)
        self.compare_button = QPushButton(self.tr("Compare"))
        self.compare_button.clicked.connect(self._compare)
        self.folder_button = QPushButton(self.tr("Open run folder"))
        self.folder_button.clicked.connect(self._open_folder)
        self.delete_button = QPushButton(self.tr("Delete…"))
        self.delete_button.clicked.connect(self._delete)
        self.refresh_button = QPushButton(self.tr("Refresh"))
        self.refresh_button.clicked.connect(self.refreshRequested)
        buttons = QHBoxLayout()
        for button in (
            self.show_button,
            self.compare_button,
            self.folder_button,
            self.delete_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch()
        buttons.addWidget(self.refresh_button)

        self.quantity = QComboBox()
        self.quantity.currentIndexChanged.connect(lambda *_: self._draw())
        self.axis = QComboBox()
        self.axis.currentIndexChanged.connect(lambda *_: self._draw())
        self.export_button = QPushButton(self.tr("Export CSV…"))
        self.plot = ThemedPlot()
        self.compare_table = QTableWidget(0, 0)
        self.compare_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.compare_table.verticalHeader().setVisible(False)
        self.compare_note = QLabel(
            self.tr("Select two or more runs with results and press Compare.")
        )
        self.compare_note.setWordWrap(True)
        controls = QHBoxLayout()
        controls.addWidget(QLabel(self.tr("Show:")))
        controls.addWidget(self.quantity, 1)
        controls.addWidget(QLabel(self.tr("against:")))
        controls.addWidget(self.axis, 1)
        controls.addWidget(self.export_button)
        compare = QWidget()
        compare_layout = QVBoxLayout(compare)
        compare_layout.setContentsMargins(0, 0, 0, 0)
        compare_layout.addLayout(controls)
        inner = QSplitter(Qt.Orientation.Horizontal)
        inner.addWidget(self.plot)
        inner.addWidget(self.compare_table)
        inner.setStretchFactor(0, 3)
        inner.setStretchFactor(1, 2)
        compare_layout.addWidget(inner, 1)
        compare_layout.addWidget(self.compare_note)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(self.table, 1)
        top_layout.addLayout(buttons)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(compare)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        self._selection_changed()

    # -- runs --------------------------------------------------------------------------------

    def state_text(self, run: RunInfo) -> str:
        states = {
            "preparing": self.tr("preparing"),
            "running": self.tr("running"),
            "summarising": self.tr("reading the results"),
            "finished": self.tr("finished"),
            "stopped": self.tr("stopped"),
            "failed": self.tr("failed"),
        }
        return states.get(run.state, run.state)

    def set_runs(self, runs: list[RunInfo], sidecar: Sidecar | None) -> None:
        selected = set(self.selected_ids())
        self.runs, self.sidecar = runs, sidecar
        self.table.blockSignals(True)
        self.table.setRowCount(len(runs))
        for row, run in enumerate(runs):
            k = "–"
            if run.k_eff is not None:
                k = f"{run.k_eff:.5f} ± {run.k_std or 0:.5f}"
            values = [
                local_time(run.started or run.created),
                run.label,
                self.state_text(run),
                run_size(run),
                params_text(run.params),
                run.library_name or (run.library or ""),
                k,
                format_duration(run.seconds) if run.finished else "",
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(RUN_ROLE, run.id)
                if column == 4:
                    item.setToolTip(text)
                if column == 2 and run.error:
                    item.setToolTip(run.error)
                self.table.setItem(row, column, item)
        # all the rows selected before (selectRow would keep only the last one)
        model = self.table.model()
        rows = QItemSelection()
        for row, run in enumerate(runs):
            if run.id in selected:
                rows.select(model.index(row, 0), model.index(row, model.columnCount() - 1))
        self.table.selectionModel().select(rows, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        self._selection_changed()

    def selected_ids(self) -> list[str]:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        return [self.runs[r].id for r in rows if r < len(self.runs)]

    def select_run(self, run_id: str) -> None:
        for row, run in enumerate(self.runs):
            if run.id == run_id:
                self.table.selectRow(row)
                return

    def _selection_changed(self) -> None:
        ids = self.selected_ids()
        one = len(ids) == 1
        active = any(r.active for r in self.runs if r.id in ids)
        self.show_button.setEnabled(one)
        self.folder_button.setEnabled(one)
        self.delete_button.setEnabled(one and not active)
        self.compare_button.setEnabled(len(ids) >= 2)

    def _show(self) -> None:
        ids = self.selected_ids()
        if len(ids) == 1:
            self.showRequested.emit(ids[0])

    def _compare(self) -> None:
        ids = self.selected_ids()
        if len(ids) >= 2:
            self.compareRequested.emit(ids)

    def _open_folder(self) -> None:
        ids = self.selected_ids()
        if len(ids) == 1:
            self.openFolderRequested.emit(ids[0])

    def _delete(self) -> None:
        ids = self.selected_ids()
        if len(ids) == 1:
            self.deleteRequested.emit(ids[0])

    # -- comparison ---------------------------------------------------------------------------

    def show_comparison(self, pairs: list[tuple[RunInfo, RunResults]]) -> None:
        """Runs (oldest first) with their results."""
        self._compared = sorted(pairs, key=lambda p: p[0].created)
        self.quantity.blockSignals(True)
        current = self.quantity.currentData()
        self.quantity.clear()
        if any(r.keff and r.keff.combined for _, r in self._compared):
            self.quantity.addItem("k-eff", ("keff", 0, 0))
        tallies: dict[int, str] = {}
        for _, results in self._compared:
            for tally in results.tallies:
                for index, (nuclide, score, _total) in enumerate(tm.totals(tally)):
                    label = f"{tally.id} {tally.name}".strip() + f": {score}"
                    if nuclide != "total":
                        label += f" ({nuclide})"
                    tallies.setdefault(tally.id * 1000 + index, label)
        for key, label in sorted(tallies.items()):
            self.quantity.addItem(
                self.tr("tally {label} (total)").format(label=label),
                ("tally", key // 1000, key % 1000),
            )
        index = self.quantity.findData(current) if current is not None else -1
        self.quantity.setCurrentIndex(max(0, index))
        self.quantity.blockSignals(False)
        self.axis.blockSignals(True)
        current_axis = self.axis.currentData()
        self.axis.clear()
        self.axis.addItem(self.tr("run"), None)
        names = sorted({n for run, _ in self._compared for n in run.params})
        for name in names:
            values = {run.params.get(name) for run, _ in self._compared}
            if len(values) > 1:
                self.axis.addItem(name, name)
        index = self.axis.findData(current_axis)
        self.axis.setCurrentIndex(max(0, index))
        self.axis.blockSignals(False)
        self._draw()

    def _values(self) -> list[tuple[RunInfo, float | None, float | None]]:
        kind = self.quantity.currentData()
        out = []
        for run, results in self._compared:
            value = std = None
            if kind is not None and kind[0] == "keff":
                combined = results.keff.combined if results.keff else None
                if combined is not None:
                    value, std = combined.value, combined.std
            elif kind is not None:
                tally = next((t for t in results.tallies if t.id == kind[1]), None)
                if tally is not None and kind[2] < len(tally.totals):
                    scale = tm.factor(self.sidecar.tallies.get(tally.id) if self.sidecar else None)
                    total = tally.totals[kind[2]]
                    value = total.value * scale
                    std = total.std * scale if total.std is not None else None
            out.append((run, value, std))
        return out

    def _draw(self) -> None:
        self.plot.clear_plot()
        values = self._values()
        kind = self.quantity.currentData()
        axis = self.axis.currentData()
        keff = kind is not None and kind[0] == "keff"
        xs, ys, ss, labels = [], [], [], []
        for n, (run, value, std) in enumerate(values):
            if value is None:
                continue
            x = float(run.params[axis]) if axis is not None and axis in run.params else float(n + 1)
            xs.append(x)
            ys.append(value)
            ss.append(std or 0.0)
            labels.append(run.label or local_time(run.created))
        if xs:
            x, y, s = np.array(xs), np.array(ys), np.array(ss)
            order = np.argsort(x)
            color = series_color(0, self.plot.dark)
            self.plot.addItem(
                pg.PlotDataItem(
                    x[order],
                    y[order],
                    pen=pg.mkPen(color, width=1.5),
                    symbol="o",
                    symbolBrush=color,
                    symbolSize=7,
                )
            )
            self.plot.addItem(
                pg.ErrorBarItem(x=x, y=y, top=s, bottom=s, beam=0.0, pen=pg.mkPen(color))
            )
            if keff:  # k = 1 for orientation; it must not stretch the axis range
                critical = pg.InfiniteLine(
                    pos=1.0, angle=0, pen=pg.mkPen((128, 128, 128), style=Qt.PenStyle.DotLine)
                )
                self.plot.item.addItem(critical, ignoreBounds=True)
            if axis is None:
                self.plot.item.getAxis("bottom").setTicks([[(x, f"{x:g}") for x in xs]])
            else:
                self.plot.item.getAxis("bottom").setTicks(None)
        self.plot.item.setLabel("bottom", axis if axis is not None else self.tr("run"))
        self.plot.item.setLabel("left", self.quantity.currentText())
        self._table(values, keff)

    def _table(self, values, keff: bool) -> None:
        beta = self.sidecar.beta_eff if self.sidecar else None
        headers = [self.tr("Run"), self.tr("Parameters"), self.tr("Value"), self.tr("σ")]
        if keff:
            headers += [self.tr("Δk vs first [pcm]"), self.tr("Δρ vs first [pcm]")]
            if beta:
                headers.append(self.tr("Δρ vs first [$]"))
        else:
            headers += [self.tr("rel. error"), self.tr("Δ vs first")]
        self.compare_table.clear()
        self.compare_table.setColumnCount(len(headers))
        self.compare_table.setHorizontalHeaderLabels(headers)
        self.compare_table.setRowCount(len(values))
        first = next(((v, s) for _r, v, s in values if v is not None), None)
        for row, (run, value, std) in enumerate(values):
            cells = [run.label or local_time(run.created), params_text(run.params)]
            if value is None:
                cells += ["–", "–"]
            else:
                cells += [f"{value:.6g}", "–" if std is None else f"{std:.2g}"]
                if keff and first is not None:
                    k0, s0 = first
                    dk = value - k0
                    drho = tm.reactivity(value) - tm.reactivity(k0)
                    sdrho = math.hypot((std or 0) / value**2, (s0 or 0) / k0**2)
                    cells += [f"{tm.pcm(dk):+.0f}", f"{tm.pcm(drho):+.0f} ± {tm.pcm(sdrho):.0f}"]
                    if beta:
                        cells.append(f"{drho / beta:+.3f} ± {sdrho / beta:.3f}")
                elif first is not None:
                    rel = tm.rel_err(value, std)
                    cells += [
                        "–" if rel is None else f"{100 * rel:.2f} %",
                        f"{100 * (value / first[0] - 1):+.2f} %" if first[0] else "–",
                    ]
            for column, text in enumerate(cells):
                self.compare_table.setItem(row, column, QTableWidgetItem(text))
        self.compare_table.resizeColumnsToContents()
        self.compare_note.setText(
            self.tr("ρ = (k − 1)/k; worth of a rod = ρ(out) − ρ(in).") if keff else ""
        )

    def csv_text(self) -> str:
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        columns = self.compare_table.columnCount()
        headers = [self.compare_table.horizontalHeaderItem(c) for c in range(columns)]
        writer.writerow([h.text() if h is not None else "" for h in headers])
        for row in range(self.compare_table.rowCount()):
            items = [self.compare_table.item(row, c) for c in range(columns)]
            writer.writerow([item.text() if item is not None else "" for item in items])
        return out.getvalue()
