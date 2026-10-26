"""Builder parameters of the open model (the sidecar's `parameters`), e.g. rod positions.

The Geometry and the Simulation tab each show one panel; both show the values
the model was exported with. Apply re-exports the model with the edited
values, which re-renders the views; a run uses the same values. A parameter
from 0 to 1 in steps of 1 is a switch.
"""

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent, QPalette
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pyvised_protocol.schemas import ModelInfo
from pyvised_protocol.sidecar import ParameterSpec

UNBOUNDED = 1e12


def is_switch(spec: ParameterSpec) -> bool:
    return spec.minimum == 0 and spec.maximum == 1 and spec.step == 1


class SwitchLabel(QLabel):
    """The wrapping text of a switch; a click toggles it (a QCheckBox's own text cannot wrap,
    so a long one would widen the whole panel)."""

    def __init__(self, text: str, box: QCheckBox) -> None:
        super().__init__(text)
        self.box = box
        self.setWordWrap(True)
        self.setBuddy(box)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.box.isEnabled():
            self.box.toggle()
        super().mouseReleaseEvent(event)


def decimals_for(spec: ParameterSpec) -> int:
    step = spec.step or 0.0
    if step <= 0:
        return 4
    return max(0, min(6, math.ceil(-math.log10(step) - 1e-9)))


class ParametersPanel(QWidget):
    applyRequested = Signal(dict)
    """Parameter name -> value to re-export the model with."""
    editedChanged = Signal(bool)
    """Whether the shown values differ from the model's."""

    def __init__(self, parent: QWidget | None = None, *, apply_text: str | None = None) -> None:
        super().__init__(parent)
        self.specs: list[ParameterSpec] = []
        self.applied: dict[str, float] = {}
        self._editors: dict[str, QDoubleSpinBox | QCheckBox] = {}
        self._busy = False
        self.form = QFormLayout()
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        # long labels (a name, a unit and a range) need the whole width
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.apply_button = QPushButton(apply_text or self.tr("Apply"))
        self.apply_button.setToolTip(self.tr("Export the model again with these values"))
        self.apply_button.clicked.connect(self.apply)
        self.defaults_button = QPushButton(self.tr("Defaults"))
        self.defaults_button.setToolTip(self.tr("The values of the model's pyvised.json"))
        self.defaults_button.clicked.connect(self.reset_to_defaults)
        self.empty = QLabel(self.tr("The model has no builder parameters."))
        self.empty.setWordWrap(True)
        self.empty.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.defaults_button)
        buttons.addWidget(self.apply_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.empty)
        layout.addLayout(self.form)
        layout.addLayout(buttons)
        self.set_model(None)

    @property
    def has_parameters(self) -> bool:
        return bool(self.specs)

    def set_model(self, info: ModelInfo | None, language: str = "en") -> None:
        """Show the parameters of `info` with the values it was exported with."""
        while self.form.rowCount():
            self.form.removeRow(0)
        self._editors.clear()
        self.specs = list(info.sidecar.parameters) if info and info.sidecar else []
        self.applied = dict(info.params) if info else {}
        for spec in self.specs:
            value = self.applied.get(spec.name, spec.default)
            label = spec.label.get(language) or spec.name
            if is_switch(spec):
                editor = QCheckBox()
                editor.setAccessibleName(label)
                editor.setChecked(value >= 0.5)
                editor.toggled.connect(self._changed)
                caption = SwitchLabel(label, editor)
                caption.setToolTip(spec.name)
                row = QHBoxLayout()
                row.addWidget(editor, 0, Qt.AlignmentFlag.AlignTop)
                row.addWidget(caption, 1)
                self.form.addRow(row)
            else:
                editor = QDoubleSpinBox()
                editor.setDecimals(decimals_for(spec))
                low = spec.minimum if spec.minimum is not None else -UNBOUNDED
                high = spec.maximum if spec.maximum is not None else UNBOUNDED
                editor.setRange(low, high)
                editor.setSingleStep(spec.step or 1.0)
                if spec.unit:
                    editor.setSuffix(f" {spec.unit}")
                editor.setValue(value)
                editor.setKeyboardTracking(False)
                editor.setCorrectionMode(QAbstractSpinBox.CorrectionMode.CorrectToNearestValue)
                editor.valueChanged.connect(self._changed)
                caption = QLabel(label)
                caption.setWordWrap(True)
                caption.setToolTip(spec.name)
                self.form.addRow(caption, editor)
            editor.setToolTip(spec.name)
            self._editors[spec.name] = editor
        has = bool(self.specs)
        self.empty.setVisible(not has)
        self.apply_button.setVisible(has)
        self.defaults_button.setVisible(has)
        self._update_buttons()

    def set_busy(self, busy: bool) -> None:
        """While the model is exported, the values cannot be applied again."""
        self._busy = busy
        self._update_buttons()

    def values(self) -> dict[str, float]:
        out = {}
        for spec in self.specs:
            editor = self._editors[spec.name]
            if isinstance(editor, QCheckBox):
                out[spec.name] = 1.0 if editor.isChecked() else 0.0
            else:
                out[spec.name] = float(editor.value())
        return out

    def set_values(self, values: dict[str, float]) -> None:
        """Show `values` (e.g. a run preset's) without applying them."""
        for name, value in values.items():
            editor = self._editors.get(name)
            if isinstance(editor, QCheckBox):
                editor.setChecked(value >= 0.5)
            elif editor is not None:
                editor.setValue(value)
        self._update_buttons()

    @property
    def edited(self) -> bool:
        current = self.values()
        return any(
            abs(value - self.applied.get(name, value)) > 1e-12 for name, value in current.items()
        )

    def apply(self) -> None:
        if self.specs and not self._busy:
            self.applyRequested.emit(self.values())

    def reset_to_defaults(self) -> None:
        self.set_values({spec.name: spec.default for spec in self.specs})

    def _changed(self, *args) -> None:
        self._update_buttons()

    def _update_buttons(self) -> None:
        values = self.values()
        edited = self.edited
        self.apply_button.setEnabled(edited and not self._busy)
        self.defaults_button.setEnabled(not self._busy)
        for name, editor in self._editors.items():
            font = editor.font()
            font.setItalic(abs(values[name] - self.applied.get(name, values[name])) > 1e-12)
            editor.setFont(font)
        self.editedChanged.emit(edited)
