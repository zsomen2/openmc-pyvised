"""Preferences: language, theme, workspace and backend options."""

from pathlib import Path

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
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...backend.manager import IMAGE_ENV, default_image
from ...core.settings import LANGUAGES, AppSettings, ThemeMode


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

        general = QWidget()
        form = QFormLayout(general)
        form.addRow(self.tr("Language:"), self.language)
        form.addRow("", QLabel(self.tr("A new language is used after restarting PyVised.")))
        form.addRow(self.tr("Theme:"), self.theme)
        form.addRow(self.tr("Workspace:"), workspace_row)
        form.addRow("", self.ask_workspace)

        # -- backend ---------------------------------------------------------
        self.image = QLineEdit(settings.backend_image)
        self.image.setPlaceholderText(default_image())
        self.keep_running = QCheckBox(self.tr("Keep the backend running after PyVised exits"))
        self.keep_running.setChecked(settings.keep_backend_running)
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

        tabs = QTabWidget()
        tabs.addTab(general, self.tr("General"))
        tabs.addTab(backend, self.tr("Backend"))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

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
            "backend_image": self.image.text().strip(),
            "keep_backend_running": self.keep_running.isChecked(),
            "backend_cpus": float(self.cpus.value()),
        }
        changed = {name for name, value in new.items() if getattr(s, name) != value}
        for name in changed:
            setattr(s, name, new[name])
        s.sync()
        return changed
