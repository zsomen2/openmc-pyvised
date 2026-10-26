"""Preferences: language, theme, workspace, models, geometry views, simulation and backend
options, and the nuclear-data libraries the user added (mounted into the backend)."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...backend.manager import IMAGE_ENV, default_image, library_mount_name
from ...core.settings import LANGUAGES, AppSettings, PlotBackground, ReloadPolicy, ThemeMode


class SettingsDialog(QDialog):
    def __init__(
        self, settings: AppSettings, parent: QWidget | None = None, image_override: str = ""
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle(self.tr("Settings"))
        self.setMinimumWidth(520)

        # -- general ---------------------------------------------------------
        self.language = QComboBox()
        for code, name in LANGUAGES.items():
            self.language.addItem(name, code)
        self.language.setCurrentIndex(max(0, self.language.findData(settings.language)))

        self.theme = QComboBox()
        self.theme.addItem(self.tr("Auto (follow Windows)"), ThemeMode.AUTO.value)
        self.theme.addItem(self.tr("Light"), ThemeMode.LIGHT.value)
        self.theme.addItem(self.tr("Dark"), ThemeMode.DARK.value)
        self.theme.setCurrentIndex(max(0, self.theme.findData(settings.theme.value)))

        self.workspace = QLineEdit(str(settings.workspace))
        browse = QPushButton(self.tr("Browse…"))
        browse.clicked.connect(self._browse_workspace)
        workspace_row = QHBoxLayout()
        workspace_row.addWidget(self.workspace, 1)
        workspace_row.addWidget(browse)

        self.ask_workspace = QCheckBox(self.tr("Ask for the workspace folder at start-up"))
        self.ask_workspace.setChecked(settings.ask_workspace_at_startup)
        self.reopen = QCheckBox(self.tr("Reopen the last model at start-up"))
        self.reopen.setChecked(settings.reopen_last_model)
        self.reload_policy = QComboBox()
        self.reload_policy.addItem(self.tr("Ask whether to reload"), ReloadPolicy.ASK.value)
        self.reload_policy.addItem(self.tr("Reload automatically"), ReloadPolicy.ALWAYS.value)
        self.reload_policy.addItem(self.tr("Do nothing"), ReloadPolicy.NEVER.value)
        self.reload_policy.setCurrentIndex(
            max(0, self.reload_policy.findData(settings.reload_on_change.value))
        )

        general = QWidget()
        form = QFormLayout(general)
        form.addRow(self.tr("Language:"), self.language)
        form.addRow("", QLabel(self.tr("A new language is used after restarting PyVised.")))
        form.addRow(self.tr("Theme:"), self.theme)
        form.addRow(self.tr("Workspace:"), workspace_row)
        form.addRow("", self.ask_workspace)
        form.addRow("", self.reopen)
        form.addRow(self.tr("When the model files change:"), self.reload_policy)

        # -- geometry --------------------------------------------------------
        self.view_bases = []
        for basis in settings.default_views:
            combo = QComboBox()
            combo.addItem(self.tr("Top (XY)"), "xy")
            combo.addItem(self.tr("Front (XZ)"), "xz")
            combo.addItem(self.tr("Side (YZ)"), "yz")
            combo.setCurrentIndex(max(0, combo.findData(basis)))
            self.view_bases.append(combo)
        views_row = QHBoxLayout()
        for combo in self.view_bases:
            views_row.addWidget(combo)
        self.quality = QDoubleSpinBox()
        self.quality.setRange(0.5, 1.0)
        self.quality.setSingleStep(0.1)
        self.quality.setDecimals(2)
        self.quality.setSuffix(" ×")
        self.quality.setValue(settings.render_quality)
        self.background = QComboBox()
        self.background.addItem(self.tr("Follow the theme"), PlotBackground.THEME.value)
        self.background.addItem(self.tr("Always white"), PlotBackground.WHITE.value)
        self.background.setCurrentIndex(
            max(0, self.background.findData(settings.plot_background.value))
        )
        self.line_mode = QComboBox()
        self.line_mode.addItem(self.tr("Cells and lattice elements"), "cell_instance")
        self.line_mode.addItem(self.tr("Cells"), "cell")
        self.line_mode.addItem(self.tr("Materials"), "material")
        self.line_mode.setCurrentIndex(max(0, self.line_mode.findData(settings.line_mode)))
        self.line_width = QComboBox()
        self.line_width.addItem(self.tr("1 pixel"), 1)
        self.line_width.addItem(self.tr("2 pixels"), 2)
        self.line_width.setCurrentIndex(max(0, self.line_width.findData(settings.line_width)))

        geometry = QWidget()
        geometry_form = QFormLayout(geometry)
        geometry_form.addRow(self.tr("Default views:"), views_row)
        geometry_form.addRow(self.tr("Render quality:"), self.quality)
        geometry_form.addRow(
            "", QLabel(self.tr("Slice pixels per screen pixel; lower is faster on slow PCs."))
        )
        geometry_form.addRow(self.tr("Plot background:"), self.background)
        geometry_form.addRow(self.tr("Boundary lines between:"), self.line_mode)
        geometry_form.addRow(self.tr("Line width:"), self.line_width)

        # -- simulation ------------------------------------------------------
        self.run_threads = QSpinBox()
        self.run_threads.setRange(0, 1024)
        self.run_threads.setSpecialValueText(self.tr("all CPUs of the backend"))
        self.run_threads.setValue(settings.run_threads)
        self.run_entropy = QCheckBox(
            self.tr("Shannon entropy for eigenvalue runs (adds a mesh when the model has none)")
        )
        self.run_entropy.setChecked(settings.run_entropy_mesh)
        simulation = QWidget()
        simulation_form = QFormLayout(simulation)
        simulation_form.addRow(self.tr("Threads per run:"), self.run_threads)
        simulation_form.addRow("", self.run_entropy)
        simulation_form.addRow(
            "",
            QLabel(self.tr("Statepoints are written every ~5 % of the batches, so a stopped run "
                           "keeps its results.")),
        )  # fmt: skip

        # -- backend ---------------------------------------------------------
        self.image = QLineEdit(settings.backend_image)
        self.image.setPlaceholderText(default_image())
        self.keep_running = QCheckBox(self.tr("Keep the backend running after PyVised exits"))
        self.keep_running.setChecked(settings.keep_backend_running)
        self.remove_old = QCheckBox(
            self.tr("Remove the backend images of earlier versions after an update")
        )
        self.remove_old.setChecked(settings.remove_old_images)
        self.cpus = QDoubleSpinBox()
        self.cpus.setRange(0.0, 256.0)
        self.cpus.setDecimals(1)
        self.cpus.setSingleStep(1.0)
        self.cpus.setSpecialValueText(self.tr("no limit"))
        self.cpus.setValue(settings.backend_cpus)

        backend = QWidget()
        backend_form = QFormLayout(backend)
        backend_form.addRow(self.tr("Backend image:"), self.image)
        if image_override:
            note = QLabel(
                self.tr("Overridden by {variable}: {image}").format(
                    variable=IMAGE_ENV, image=image_override
                )
            )
            note.setWordWrap(True)
            backend_form.addRow("", note)
        backend_form.addRow(self.tr("CPU limit:"), self.cpus)
        backend_form.addRow("", self.keep_running)
        backend_form.addRow("", self.remove_old)
        self.libraries = QListWidget()
        self.libraries.setMaximumHeight(110)
        for name, folder in settings.added_libraries:
            self._add_library_item(name, folder)
        add_library = QPushButton(self.tr("Add…"))
        add_library.clicked.connect(self._choose_library)
        remove_library = QPushButton(self.tr("Remove"))
        remove_library.clicked.connect(self._remove_library)
        library_buttons = QVBoxLayout()
        library_buttons.addWidget(add_library)
        library_buttons.addWidget(remove_library)
        library_buttons.addStretch()
        library_row = QHBoxLayout()
        library_row.addWidget(self.libraries, 1)
        library_row.addLayout(library_buttons)
        backend_form.addRow(self.tr("Added libraries:"), library_row)
        library_note = QLabel(
            self.tr(
                "Folders with a cross_sections.xml, e.g. a library from openmc.org. The "
                "built-in ENDF/B-VII.1 library is always there. Changing the list restarts "
                "the backend."
            )
        )
        library_note.setWordWrap(True)
        backend_form.addRow("", library_note)

        self.tabs = tabs = QTabWidget()
        tabs.addTab(general, self.tr("General"))
        tabs.addTab(geometry, self.tr("Geometry"))
        tabs.addTab(simulation, self.tr("Simulation"))
        tabs.addTab(backend, self.tr("Backend"))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    def show_page(self, name: str) -> None:
        """Open on a tab: "general", "geometry", "simulation" or "backend"."""
        pages = {"general": 0, "geometry": 1, "simulation": 2, "backend": 3}
        if name in pages:
            self.tabs.setCurrentIndex(pages[name])

    def _add_library_item(self, name: str, folder: Path) -> None:
        item = QListWidgetItem(f"{name} — {folder}")
        item.setData(Qt.ItemDataRole.UserRole, (name, str(folder)))
        item.setToolTip(str(folder))
        self.libraries.addItem(item)

    def library_list(self) -> list[tuple[str, Path]]:
        out = []
        for row in range(self.libraries.count()):
            item = self.libraries.item(row)
            name, folder = item.data(Qt.ItemDataRole.UserRole)
            out.append((name, Path(folder)))
        return out

    def _choose_library(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, self.tr("Choose a library folder (with cross_sections.xml)")
        )
        if not folder:
            return
        path = Path(folder)
        if not (path / "cross_sections.xml").is_file():
            QMessageBox.warning(
                self,
                self.tr("Library"),
                self.tr("{folder} has no cross_sections.xml.").format(folder=path),
            )
            return
        name = library_mount_name(path.name)
        taken = {library_mount_name(n) for n, _ in self.library_list()}
        number = 2
        base = name
        while name in taken:
            name = f"{base}-{number}"
            number += 1
        self._add_library_item(name, path)

    def _remove_library(self) -> None:
        for item in self.libraries.selectedItems():
            self.libraries.takeItem(self.libraries.row(item))

    def _browse_workspace(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, self.tr("Choose the workspace folder"), self.workspace.text()
        )
        if folder:
            self.workspace.setText(str(Path(folder)))

    def apply(self) -> set[str]:
        """Write the values to the settings; returns the names of the changed settings."""
        s = self.settings
        new = {
            "language": self.language.currentData(),
            "theme": ThemeMode(self.theme.currentData()),
            "workspace": Path(self.workspace.text().strip() or str(s.workspace)),
            "ask_workspace_at_startup": self.ask_workspace.isChecked(),
            "reopen_last_model": self.reopen.isChecked(),
            "reload_on_change": ReloadPolicy(self.reload_policy.currentData()),
            "default_views": tuple(combo.currentData() for combo in self.view_bases),
            "render_quality": round(float(self.quality.value()), 2),
            "plot_background": PlotBackground(self.background.currentData()),
            "line_mode": self.line_mode.currentData(),
            "line_width": int(self.line_width.currentData()),
            "backend_image": self.image.text().strip(),
            "keep_backend_running": self.keep_running.isChecked(),
            "remove_old_images": self.remove_old.isChecked(),
            "backend_cpus": float(self.cpus.value()),
            "run_threads": int(self.run_threads.value()),
            "run_entropy_mesh": self.run_entropy.isChecked(),
            "added_libraries": self.library_list(),
        }
        changed = {name for name, value in new.items() if getattr(s, name) != value}
        for name in changed:
            setattr(s, name, new[name])
        s.sync()
        return changed
