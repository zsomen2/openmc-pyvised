"""Run settings: pre-filled from the model, changed only in the run's copy.

Presets come from the model's sidecar (`run_presets`); a preset may set
builder parameters too. Threads, the library, the entropy mesh and the stop
conditions (time limit, σ(k-eff), tally relative error) are remembered in
the settings.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pyvised_protocol.runs import RunSettings
from pyvised_protocol.schemas import HealthInfo, ModelInfo
from pyvised_protocol.sidecar import RunPreset

from ...core.icons import icon
from ...core.settings import AppSettings
from ..parameters_panel import ParametersPanel

MAX_PARTICLES = 1_000_000_000
MAX_BATCHES = 1_000_000


class RunForm(QWidget):
    runRequested = Signal()
    stopRequested = Signal()

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.info: ModelInfo | None = None
        self.language = settings.language
        self.cpus = 1
        self._running = False

        self.preset = QComboBox()
        self.preset.setToolTip(self.tr("Run sizes the model suggests (its pyvised.json)"))
        self.preset.activated.connect(self._preset_chosen)
        self.preset_note = QLabel()
        self.preset_note.setWordWrap(True)
        self.mode = QLabel()
        self.particles = QSpinBox()
        self.particles.setRange(1, MAX_PARTICLES)
        self.particles.setSingleStep(1000)
        self.particles.setGroupSeparatorShown(True)
        self.batches = QSpinBox()
        self.batches.setRange(1, MAX_BATCHES)
        self.inactive = QSpinBox()
        self.inactive.setRange(0, MAX_BATCHES - 1)
        self.seed = QSpinBox()
        self.seed.setRange(0, 2_000_000_000)
        self.seed.setSpecialValueText(self.tr("as in the model"))
        self.threads = QSpinBox()
        self.threads.setRange(0, 1)
        self.library = QComboBox()
        self.entropy = QCheckBox(self.tr("Shannon entropy"))
        self.entropy.setToolTip(self.tr("Adds an entropy mesh when the model has none"))
        self.entropy.setChecked(settings.run_entropy_mesh)

        limits = settings.run_limits
        self.time_limit_on = QCheckBox(self.tr("Time limit"))
        self.time_limit = QDoubleSpinBox()
        self.time_limit.setRange(0.1, 100_000)
        self.time_limit.setDecimals(1)
        self.time_limit.setSuffix(" min")
        self.keff_on = QCheckBox(self.tr("Stop at σ(k-eff) ≤"))
        self.keff_std = QDoubleSpinBox()
        self.keff_std.setRange(0.1, 100_000)
        self.keff_std.setDecimals(1)
        self.keff_std.setSuffix(" pcm")
        self.rel_on = QCheckBox(self.tr("Stop at tally rel. error ≤"))
        self.rel_err = QDoubleSpinBox()
        self.rel_err.setRange(0.01, 100)
        self.rel_err.setDecimals(2)
        self.rel_err.setSuffix(" %")
        for name, box, spin in (
            ("time_limit", self.time_limit_on, self.time_limit),
            ("keff_std", self.keff_on, self.keff_std),
            ("tally_rel_err", self.rel_on, self.rel_err),
        ):
            on, value = limits[name]
            box.setChecked(on)
            spin.setValue(value)
            spin.setEnabled(on)
            box.toggled.connect(spin.setEnabled)
        self.keff_on.setToolTip(
            self.tr("OpenMC's trigger: with it, the batches above are the most batches")
        )
        self.rel_on.setToolTip(
            self.tr("For every tally bin with a result (OpenMC's tally triggers)")
        )
        self.label = QLineEdit()
        self.label.setMaxLength(60)
        self.label.setPlaceholderText(self.tr("optional, names the run folder"))

        self.parameters = ParametersPanel()
        self.parameters_box = QGroupBox(self.tr("Builder parameters"))
        box_layout = QVBoxLayout(self.parameters_box)
        box_layout.addWidget(self.parameters)

        self.run_button = QPushButton(self.tr("Run"))
        self.run_button.setDefault(True)
        self.run_button.clicked.connect(self.runRequested)
        self.stop_button = QPushButton(self.tr("Stop"))
        self.stop_button.clicked.connect(self.stopRequested)
        self.state = QLabel()
        self.state.setWordWrap(True)
        self.state.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        form = QFormLayout()
        form.addRow(self.tr("Preset:"), self.preset)
        form.addRow("", self.preset_note)
        form.addRow(self.tr("Run mode:"), self.mode)
        form.addRow(self.tr("Particles per batch:"), self.particles)
        form.addRow(self.tr("Batches:"), self.batches)
        self.inactive_label = QLabel(self.tr("Inactive batches:"))
        form.addRow(self.inactive_label, self.inactive)
        form.addRow(self.tr("Seed:"), self.seed)
        form.addRow(self.tr("Threads:"), self.threads)
        form.addRow(self.tr("Library:"), self.library)
        form.addRow("", self.entropy)
        form.addRow(self.time_limit_on, self.time_limit)
        form.addRow(self.keff_on, self.keff_std)
        form.addRow(self.rel_on, self.rel_err)
        form.addRow(self.tr("Label:"), self.label)
        buttons = QHBoxLayout()
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.stop_button)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.parameters_box)
        layout.addLayout(buttons)
        layout.addWidget(self.state)
        layout.addStretch(1)
        self.refresh_icons()
        self.set_backend(None)
        self.set_model(None)

    def refresh_icons(self) -> None:
        self.run_button.setIcon(icon("mdi6.play"))
        self.stop_button.setIcon(icon("mdi6.stop"))

    # -- model and backend ---------------------------------------------------------------

    def set_model(self, info: ModelInfo | None, language: str | None = None) -> None:
        if language is not None:
            self.language = language
        same = info is not None and self.info is not None and info.source == self.info.source
        self.info = info
        self.parameters.set_model(info, self.language)
        self.parameters_box.setVisible(self.parameters.has_parameters)
        self.preset.clear()
        self.preset.addItem(self.tr("Model settings"), None)
        presets = info.sidecar.run_presets if info and info.sidecar else []
        for preset in presets:
            self.preset.addItem(preset.label.get(self.language) or preset.name, preset.name)
        if info is None:
            self.mode.setText(self.tr("no model open"))
            self._update_enabled()
            return
        eigenvalue = info.settings.run_mode == "eigenvalue"
        modes = {
            "eigenvalue": self.tr("eigenvalue (k-eff)"),
            "fixed source": self.tr("fixed source"),
        }
        self.mode.setText(modes.get(info.settings.run_mode, info.settings.run_mode))
        self.inactive.setVisible(eigenvalue)
        self.inactive_label.setVisible(eigenvalue)
        self.keff_on.setEnabled(eigenvalue)
        self.keff_std.setEnabled(eigenvalue and self.keff_on.isChecked())
        self.entropy.setEnabled(eigenvalue)
        if not same:
            self.fill_from_model()
        self._update_enabled()

    def fill_from_model(self) -> None:
        info = self.info
        if info is None:
            return
        s = info.settings
        self.particles.setValue(s.particles or 1000)
        self.batches.setValue(s.batches or 10)
        self.inactive.setValue(s.inactive or 0)
        self.seed.setValue(0)
        self.preset.setCurrentIndex(0)
        self.preset_note.hide()

    def preset_of(self, name: str | None) -> RunPreset | None:
        info = self.info
        presets = info.sidecar.run_presets if info and info.sidecar else []
        return next((p for p in presets if p.name == name), None)

    def _preset_chosen(self, index: int) -> None:
        preset = self.preset_of(self.preset.itemData(index))
        if preset is None:
            self.fill_from_model()
            return
        self.apply_preset(preset)

    def apply_preset(self, preset: RunPreset) -> None:
        info = self.info
        s = info.settings if info is not None else None
        self.particles.setValue(preset.particles or (s.particles if s and s.particles else 1000))
        self.batches.setValue(preset.batches or (s.batches if s and s.batches else 10))
        if preset.inactive is not None:
            self.inactive.setValue(preset.inactive)
        elif s is not None:
            self.inactive.setValue(s.inactive or 0)
        if preset.time_limit is not None:
            self.time_limit_on.setChecked(True)
            self.time_limit.setValue(preset.time_limit / 60.0)
        if preset.keff_std is not None:
            self.keff_on.setChecked(True)
            self.keff_std.setValue(preset.keff_std * 1e5)
        if preset.params:
            self.parameters.set_values(preset.params)
        note = preset.note.get(self.language)
        self.preset_note.setText(note)
        self.preset_note.setVisible(bool(note))
        index = self.preset.findData(preset.name)
        if index >= 0:
            self.preset.setCurrentIndex(index)

    def apply_settings(self) -> None:
        """The run defaults changed in the preferences."""
        self.threads.setValue(min(self.settings.run_threads, self.cpus))
        self.entropy.setChecked(self.settings.run_entropy_mesh)

    def set_backend(self, health: HealthInfo | None) -> None:
        self.cpus = health.cpu_count if health is not None else 1
        self.threads.setRange(0, max(1, self.cpus))
        self.threads.setSpecialValueText(self.tr("all ({count})").format(count=self.cpus))
        self.threads.setValue(min(self.settings.run_threads, self.cpus))
        current = self.library.currentData() or self.settings.run_library
        self.library.clear()
        for lib in health.libraries if health is not None else []:
            text = lib.name if lib.builtin else self.tr("{name} (added)").format(name=lib.name)
            self.library.addItem(text, lib.id)
        index = self.library.findData(current)
        builtin = next(
            (i for i in range(self.library.count()) if self.library.itemData(i) == "builtin"), 0
        )
        self.library.setCurrentIndex(index if index >= 0 else builtin)
        self._update_enabled()

    # -- the request -------------------------------------------------------------------

    @property
    def library_id(self) -> str | None:
        value = self.library.currentData()
        return None if value in (None, "", "builtin") else str(value)

    def valid(self) -> str:
        """Why the settings cannot run ("" when they can)."""
        if self.info is None:
            return self.tr("Open a model first.")
        if (
            self.info.settings.run_mode == "eigenvalue"
            and self.inactive.value() >= self.batches.value()
        ):
            return self.tr("Use more batches than inactive batches.")
        return ""

    def request_settings(self) -> RunSettings:
        """The run settings; values equal to the model's are sent too (the run records them)."""
        eigenvalue = self.info is not None and self.info.settings.run_mode == "eigenvalue"
        threads = self.threads.value()
        self.settings.run_threads = threads
        self.settings.run_library = self.library_id or ""
        self.settings.run_entropy_mesh = self.entropy.isChecked()
        for name, box, spin in (
            ("time_limit", self.time_limit_on, self.time_limit),
            ("keff_std", self.keff_on, self.keff_std),
            ("tally_rel_err", self.rel_on, self.rel_err),
        ):
            self.settings.set_run_limit(name, box.isChecked(), spin.value())
        return RunSettings(
            particles=self.particles.value(),
            batches=self.batches.value(),
            inactive=self.inactive.value() if eigenvalue else None,
            seed=self.seed.value() or None,
            threads=threads or None,
            time_limit=self.time_limit.value() * 60.0 if self.time_limit_on.isChecked() else None,
            keff_std=self.keff_std.value() * 1e-5
            if eigenvalue and self.keff_on.isChecked()
            else None,
            tally_rel_err=self.rel_err.value() / 100.0 if self.rel_on.isChecked() else None,
            entropy_mesh=self.entropy.isChecked(),
        )

    # -- state -----------------------------------------------------------------------------

    def set_running(self, running: bool, text: str = "") -> None:
        self._running = running
        self.state.setText(text)
        self._update_enabled()

    def _update_enabled(self) -> None:
        has_model = self.info is not None
        self.run_button.setEnabled(has_model and not self._running and self.library.count() > 0)
        self.stop_button.setEnabled(self._running)
        self.parameters.set_busy(self._running)
