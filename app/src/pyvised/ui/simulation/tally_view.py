"""Tally tables: every bin with mean, σ, the relative error coloured by MCNP's guideline,
the figure of merit and, with the sidecar's normalisation (MCNP ``fm``), the normalised
value. Filters with a single bin are named above the table instead of getting a column.
The normalisation panel edits the model's pyvised.json."""

import math

import numpy as np
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from pyvised_protocol.runs import RunInfo, RunResults, TallyResult
from pyvised_protocol.sidecar import Sidecar, TallyNormalisation

from ...backend.client import TallyData
from ...render import tallies as tm
from . import names

QUALITY_BACKGROUND = {
    tm.Quality.GOOD: QColor(46, 160, 67, 70),
    tm.Quality.FAIR: QColor(230, 160, 0, 80),
    tm.Quality.POOR: QColor(220, 50, 50, 80),
}
ParentIndex = QModelIndex | QPersistentModelIndex
NO_PARENT = QModelIndex()


class TallyTableModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.tally: TallyResult | None = None
        self.mean = np.zeros((0,))
        self.std = np.zeros((0,))
        self.norm: TallyNormalisation | None = None
        self.seconds: float | None = None
        self.unit = "eV"
        self._filters: list[int] = []
        self._columns: list[str] = []
        self._shape: tuple[int, ...] = ()
        self._nuclides = self._scores = False

    def set_data(
        self,
        data: TallyData | None,
        norm: TallyNormalisation | None,
        seconds: float | None,
        unit: str = "eV",
    ) -> None:
        self.beginResetModel()
        self.tally = data.tally if data is not None else None
        self.mean = np.asarray(data.mean) if data is not None else np.zeros((0,))
        self.std = np.asarray(data.std) if data is not None else np.zeros((0,))
        self.norm, self.seconds, self.unit = norm, seconds, unit
        self._shape = tuple(self.mean.shape)
        tally = self.tally
        self._filters = [i for i, f in enumerate(tally.filters) if f.bins > 1] if tally else []
        self._nuclides = tally is not None and len(tally.nuclides) > 1
        self._scores = tally is not None and len(tally.scores) > 1
        columns = [names.filter_title(tally.filters[i]) for i in self._filters] if tally else []
        if self._nuclides:
            columns.append(self.tr("nuclide"))
        if self._scores:
            columns.append(self.tr("score"))
        columns += [self.tr("mean"), self.tr("σ"), self.tr("rel. error"), "FOM"]
        if norm is not None:
            unit_text = f" [{norm.unit}]" if norm.unit else ""
            columns += [self.tr("normalised") + unit_text, self.tr("normalised σ")]
        self._columns = columns
        self.endResetModel()

    def rowCount(self, parent: ParentIndex = NO_PARENT) -> int:
        return 0 if parent.isValid() or self.tally is None else int(self.mean.size)

    def columnCount(self, parent: ParentIndex = NO_PARENT) -> int:
        return 0 if parent.isValid() else len(self._columns)

    def headerData(self, section: int, orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self._columns[section] if section < len(self._columns) else None
        return None

    def _cell(self, row: int) -> tuple[tuple[int, ...], float, float | None]:
        index = np.unravel_index(row, self._shape)
        std = float(self.std[index])
        return (
            tuple(int(i) for i in index),
            float(self.mean[index]),
            (std if math.isfinite(std) else None),
        )

    def data(self, index: ParentIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or self.tally is None:
            return None
        bins, mean, std = self._cell(index.row())
        column = index.column()
        tally = self.tally
        rel = tm.rel_err(mean, std)
        values_from = len(self._filters) + int(self._nuclides) + int(self._scores)
        if role == Qt.ItemDataRole.BackgroundRole and column == values_from + 2:
            return QUALITY_BACKGROUND.get(tm.quality(rel))
        if role == Qt.ItemDataRole.TextAlignmentRole and column >= values_from:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        if column < len(self._filters):
            f_index = self._filters[column]
            return tm.bin_label(tally.filters[f_index], bins[f_index], self.unit)
        column -= len(self._filters)
        if self._nuclides:
            if column == 0:
                return tally.nuclides[bins[-2]]
            column -= 1
        if self._scores:
            if column == 0:
                return tally.scores[bins[-1]]
            column -= 1
        scale = tm.factor(self.norm)
        values = [
            f"{mean:.6g}",
            "–" if std is None else f"{std:.3g}",
            "–" if rel is None else f"{100 * rel:.2f} %",
            _fom_text(tm.figure_of_merit(rel, self.seconds)),
        ]
        if self.norm is not None:
            values += [f"{mean * scale:.6g}", "–" if std is None else f"{std * scale:.3g}"]
        return values[column] if column < len(values) else None


def _fom_text(fom: float | None) -> str:
    return "–" if fom is None else f"{fom:.3g}"


class NormalisationPanel(QGroupBox):
    """The sidecar's normalisation of one tally (MCNP ``fm``, area or volume division)."""

    saveRequested = Signal(object)
    """`TallyNormalisation`, or None to remove it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(self.tr("Normalisation (pyvised.json)"), parent)
        self.source = QLineEdit()
        self.source.setPlaceholderText("1")
        self.source.setToolTip(self.tr("Source strength, e.g. particles per second"))
        self.divide = QComboBox()
        self.divide.addItem(self.tr("nothing"), "none")
        self.divide.addItem(self.tr("volume [cm³]"), "volume")
        self.divide.addItem(self.tr("area [cm²]"), "area")
        self.divisor = QLineEdit()
        self.divisor.setPlaceholderText(self.tr("volume or area"))
        self.multiplier = QLineEdit()
        self.multiplier.setPlaceholderText("1")
        self.unit = QLineEdit()
        self.note = QLineEdit()
        self.hint = QLabel(
            self.tr(
                "OpenMC divides neither volume nor surface tallies by the volume or the area: "
                "divide here to get a flux. result = mean × source × multiplier ÷ divisor"
            )
        )
        self.hint.setWordWrap(True)
        self.save = QPushButton(self.tr("Save"))
        self.save.clicked.connect(self._save)
        self.remove = QPushButton(self.tr("Remove"))
        self.remove.clicked.connect(lambda: self.saveRequested.emit(None))
        self.divide.currentIndexChanged.connect(self._divide_changed)
        form = QFormLayout()
        form.addRow(self.tr("Source strength:"), self.source)
        form.addRow(self.tr("Divide by:"), self.divide)
        form.addRow(self.tr("Volume or area:"), self.divisor)
        form.addRow(self.tr("Multiplier:"), self.multiplier)
        form.addRow(self.tr("Unit:"), self.unit)
        form.addRow(self.tr("Note:"), self.note)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.remove)
        buttons.addWidget(self.save)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.hint)
        layout.addLayout(buttons)
        self.show_normalisation(None, editable=False)

    def show_normalisation(self, norm: TallyNormalisation | None, *, editable: bool) -> None:
        n = norm or TallyNormalisation()
        self.source.setText("" if norm is None else f"{n.source_strength:g}")
        self.divide.setCurrentIndex(max(0, self.divide.findData(n.divide_by)))
        self.divisor.setText("" if n.divisor is None else f"{n.divisor:.10g}")
        self.multiplier.setText("" if norm is None else f"{n.multiplier:g}")
        self.unit.setText(n.unit)
        self.note.setText(n.note)
        for widget in (self.source, self.divide, self.multiplier, self.unit, self.note, self.save):
            widget.setEnabled(editable)
        self.remove.setEnabled(editable and norm is not None)
        self._divide_changed()
        self.divisor.setEnabled(editable and self.divide.currentData() != "none")

    def _divide_changed(self, *args) -> None:
        self.divisor.setEnabled(self.save.isEnabled() and self.divide.currentData() != "none")

    @staticmethod
    def _number(edit: QLineEdit, default: float | None) -> float | None:
        text = edit.text().strip().replace(",", ".")
        if not text:
            return default
        return float(text)

    def value(self) -> TallyNormalisation:
        divide = self.divide.currentData()
        divisor = self._number(self.divisor, None) if divide != "none" else None
        if divide != "none" and not divisor:
            raise ValueError(self.tr("Enter the volume or the area to divide by."))
        return TallyNormalisation(
            source_strength=self._number(self.source, 1.0) or 1.0,
            divide_by=divide,
            divisor=divisor,
            multiplier=self._number(self.multiplier, 1.0) or 1.0,
            unit=self.unit.text().strip(),
            note=self.note.text().strip(),
        )

    def _save(self) -> None:
        try:
            value = self.value()
        except ValueError as exc:
            self.hint.setText(str(exc))
            return
        self.saveRequested.emit(value)


class TallyView(QWidget):
    tallyChanged = Signal(int)
    normalisationSaved = Signal(int, object)
    """Tally id and `TallyNormalisation` (None: removed)."""
    exportRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.results: RunResults | None = None
        self.run: RunInfo | None = None
        self.sidecar: Sidecar | None = None
        self.editable = False
        self.data: TallyData | None = None
        self.combo = QComboBox()
        self.combo.currentIndexChanged.connect(self._chosen)
        self.unit = QComboBox()
        self.unit.addItem("eV", "eV")
        self.unit.addItem("MeV", "MeV")
        self.unit.currentIndexChanged.connect(lambda *_: self._refresh())
        self.export_button = QPushButton(self.tr("Export CSV…"))
        self.export_button.clicked.connect(self.exportRequested)
        self.filters = QLabel()
        self.filters.setWordWrap(True)
        self.model = TallyTableModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.totals = QLabel()
        self.totals.setWordWrap(True)
        self.totals.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.normalisation = NormalisationPanel()
        self.normalisation.saveRequested.connect(self._save)
        top = QHBoxLayout()
        top.addWidget(QLabel(self.tr("Tally:")))
        top.addWidget(self.combo, 1)
        top.addWidget(QLabel(self.tr("Energy in")))
        top.addWidget(self.unit)
        top.addWidget(self.export_button)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addLayout(top)
        left_layout.addWidget(self.filters)
        left_layout.addWidget(self.table, 1)
        left_layout.addWidget(self.totals)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self.normalisation)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        self.set_results(None, None, None, editable=False)

    @property
    def tally_id(self) -> int | None:
        value = self.combo.currentData()
        return int(value) if value is not None else None

    def set_results(
        self,
        run: RunInfo | None,
        results: RunResults | None,
        sidecar: Sidecar | None,
        *,
        editable: bool,
    ) -> None:
        if results is not None and results is self.results:  # a refresh: keep table and edits
            changed = sidecar is not self.sidecar or editable != self.editable
            self.run, self.sidecar, self.editable = run, sidecar, editable
            if changed:
                self._refresh()
            return
        keep = self.tally_id
        self.run, self.results, self.sidecar, self.editable = run, results, sidecar, editable
        self.data = None
        self.combo.blockSignals(True)
        self.combo.clear()
        for tally in results.tallies if results else []:
            self.combo.addItem(f"{tally.id}  {tally.name}".rstrip(), tally.id)
        index = self.combo.findData(keep) if keep is not None else -1
        self.combo.setCurrentIndex(max(0, index))
        self.combo.blockSignals(False)
        self.export_button.setEnabled(False)
        self.model.set_data(None, None, None)
        self._describe()
        if self.tally_id is not None:
            self.tallyChanged.emit(self.tally_id)

    def set_sidecar(self, sidecar: Sidecar | None) -> None:
        self.sidecar = sidecar
        self._refresh()

    def set_data(self, data: TallyData) -> None:
        if data.tally.id != self.tally_id:
            return
        self.data = data
        self._refresh()

    def current_tally(self) -> TallyResult | None:
        tid = self.tally_id
        results = self.results
        return next((t for t in results.tallies if t.id == tid), None) if results else None

    def norm(self) -> TallyNormalisation | None:
        tid = self.tally_id
        return self.sidecar.tallies.get(tid) if self.sidecar and tid is not None else None

    def _chosen(self, index: int) -> None:
        self.data = None
        self.model.set_data(None, None, None)
        self.export_button.setEnabled(False)
        self._describe()
        if self.tally_id is not None:
            self.tallyChanged.emit(self.tally_id)

    def _describe(self) -> None:
        tally = self.current_tally()
        if tally is None:
            self.filters.setText(self.tr("The run has no tallies.") if self.results else "")
            self.totals.setText("")
            self.normalisation.show_normalisation(None, editable=False)
            return
        parts = [names.filter_summary(f) for f in tally.filters]
        text = self.tr("Filters: {filters}").format(filters=" · ".join(parts) or "–")
        text += " · " + self.tr("{n} realizations").format(n=tally.realizations)
        if tally.zero_bins:
            text += " · " + self.tr("{n} bins without a result").format(n=tally.zero_bins)
        self.filters.setText(text)
        norm = self.norm()
        scale = tm.factor(norm)
        lines = []
        for nuclide, score, total in tm.totals(tally):
            rel = tm.rel_err(total.value, total.std)
            value = f"{total.value:.6g}"
            if norm is not None:
                value += f" → {total.value * scale:.6g} {norm.unit}".rstrip()
            rel_text = "–" if rel is None else f"{100 * rel:.2f} %"
            name = score if nuclide == "total" else f"{score} ({nuclide})"
            lines.append(
                self.tr("Total {name}: {value}, rel. error {rel}").format(
                    name=name, value=value, rel=rel_text
                )
            )
        self.totals.setText("\n".join(lines))
        self.normalisation.show_normalisation(norm, editable=self.editable)

    def _refresh(self) -> None:
        self._describe()
        if self.data is None:
            return
        seconds = self.results.active_seconds if self.results else None
        self.model.set_data(self.data, self.norm(), seconds, self.unit.currentData())
        self.table.resizeColumnsToContents()
        self.export_button.setEnabled(True)

    def _save(self, norm: TallyNormalisation | None) -> None:
        if self.tally_id is not None:
            self.normalisationSaved.emit(self.tally_id, norm)

    def csv_text(self) -> str:
        if self.data is None:
            return ""
        seconds = self.results.active_seconds if self.results else None
        return tm.csv_text(self.data.tally, self.data.mean, self.data.std, self.norm(), seconds)
