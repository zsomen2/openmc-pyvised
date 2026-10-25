"""The main window: Geometry and Simulation tabs, menus, toolbar and the backend status."""

import logging
import os
import platform
from pathlib import Path

import PySide6
from PySide6.QtCore import QSize, Qt, QUrl, qVersion
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QTabWidget,
    QToolBar,
    QWidget,
)

from pyvised_protocol.schemas import ModelInfo, SelfTestResult

from .. import REPOSITORY_URL, __version__
from ..backend.client import ApiClient
from ..backend.manager import IMAGE_ENV, BackendConfig, BackendManager, BackendState
from ..core.icons import icon
from ..core.model_session import MODEL_SUFFIXES, ModelSession
from ..core.settings import AppSettings, ReloadPolicy
from ..core.tasks import TaskRunner
from ..core.theme import apply_theme
from ..render.colorize import ColorMode
from .dialogs.about import AboutDialog
from .dialogs.settings_dialog import SettingsDialog
from .dialogs.text_dialog import TextDialog
from .empty_state import EmptyState
from .geometry.geometry_tab import GeometryTab
from .status_pill import StatusPill

log = logging.getLogger(__name__)

BACKEND_SETTINGS = {"workspace", "backend_image", "backend_cpus"}
GEOMETRY_SETTINGS = {"render_quality", "plot_background", "line_mode", "line_width"}


class MainWindow(QMainWindow):
    def __init__(
        self,
        settings: AppSettings,
        backend: BackendManager,
        tasks: TaskRunner,
        language: str | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.language = language or settings.language
        """Language of this run; `--lang` overrides the setting without saving it."""
        self.backend = backend
        self.tasks = tasks
        self.session = ModelSession(tasks, self)
        self._pending_open: Path | None = None
        self._backend_ready = False
        self.setMinimumSize(900, 600)

        self.geometry_tab = GeometryTab(self.session, settings, language=self.language)
        self.simulation_page = EmptyState(
            "mdi6.chart-bell-curve-cumulative",
            self.tr("No model open"),
            self.tr("Simulation controls arrive in a later development phase."),
        )
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self.geometry_tab, self.tr("Geometry"))
        self.tabs.addTab(self.simulation_page, self.tr("Simulation"))
        self.setCentralWidget(self.tabs)

        self._create_actions()
        self._create_menus()
        self._create_toolbar()
        self.cursor_label = QLabel()
        self.cursor_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.render_label = QLabel()
        self.statusBar().addWidget(self.cursor_label, 1)
        self.statusBar().addPermanentWidget(self.render_label)
        self.status_pill = StatusPill(self)
        self.statusBar().addPermanentWidget(self.status_pill)
        self.status_pill.detailsRequested.connect(self.show_backend_details)
        self.status_pill.selftestRequested.connect(self.run_selftest)
        self.status_pill.restartRequested.connect(self.restart_backend)
        self.status_pill.logRequested.connect(self.show_backend_log)
        self.status_pill.diagnosticsRequested.connect(self.copy_diagnostics)

        self.session.opening.connect(self._model_opening)
        self.session.opened.connect(self._model_opened)
        self.session.failed.connect(self._model_failed)
        self.session.filesChanged.connect(self._model_files_changed)
        self.geometry_tab.cursorText.connect(self.cursor_label.setText)
        self.geometry_tab.renderTime.connect(
            lambda seconds: self.render_label.setText(f"{seconds * 1000:.0f} ms")
        )
        self.geometry_tab.reloadRequested.connect(self.reload_model)

        backend.stateChanged.connect(self._on_backend_state)
        self._on_backend_state(backend.state.value, backend.detail)
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._refresh_icons)
        self._refresh_icons()
        self._update_title()
        self._restore_window()

    # -- construction ----------------------------------------------------------

    def _action(
        self, text: str, slot, shortcut: QKeySequence | QKeySequence.StandardKey | str | None = None
    ) -> QAction:
        action = QAction(text, self)
        action.triggered.connect(slot)
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
        return action

    def _create_actions(self) -> None:
        self.act_open = self._action(
            self.tr("&Open model…"), self.choose_model, QKeySequence.StandardKey.Open
        )
        self.act_reload = self._action(self.tr("&Reload model"), self.reload_model, "F5")
        self.act_switch_workspace = self._action(
            self.tr("Switch &workspace…"), self.switch_workspace
        )
        self.act_open_workspace = self._action(
            self.tr("Open workspace &folder"), self.open_workspace_folder
        )
        self.act_exit = self._action(self.tr("E&xit"), self.close, QKeySequence.StandardKey.Quit)
        self.act_lines = self._action(self.tr("Boundary &lines"), self.geometry_tab.set_lines)
        self.act_lines.setCheckable(True)
        self.act_lines.setChecked(self.settings.show_lines)
        self.act_reset_views = self._action(
            self.tr("Re&set views"), self.geometry_tab.reset_views, "Ctrl+0"
        )
        self.act_selftest = self._action(self.tr("Run backend &self-test"), self.run_selftest)
        self.act_backend_log = self._action(self.tr("Backend &log…"), self.show_backend_log)
        self.act_diagnostics = self._action(self.tr("Copy &diagnostics"), self.copy_diagnostics)
        self.act_restart = self._action(self.tr("&Restart backend"), self.restart_backend)
        self.act_settings = self._action(
            self.tr("&Preferences…"), self.open_settings, QKeySequence.StandardKey.Preferences
        )
        self.act_openmc_docs = self._action(
            self.tr("OpenMC &documentation"),
            lambda: QDesktopServices.openUrl(QUrl("https://docs.openmc.org")),
        )
        self.act_repository = self._action(
            self.tr("PyVised on &GitHub"), lambda: QDesktopServices.openUrl(QUrl(REPOSITORY_URL))
        )
        self.act_about = self._action(self.tr("&About PyVised"), self.show_about)

    def _create_menus(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu(self.tr("&File"))
        file_menu.addAction(self.act_open)
        self.recent_menu = QMenu(self.tr("Open &recent"), self)
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        file_menu.addMenu(self.recent_menu)
        file_menu.addAction(self.act_reload)
        file_menu.addSeparator()
        file_menu.addAction(self.act_switch_workspace)
        file_menu.addAction(self.act_open_workspace)
        file_menu.addSeparator()
        file_menu.addAction(self.act_exit)
        view = bar.addMenu(self.tr("&View"))
        self.color_menu = view.addMenu(self.tr("&Colour by"))
        self.color_actions: dict[ColorMode, QAction] = {}
        for mode, text in self._color_modes():
            action = self._action(text, lambda checked, m=mode: self.set_color_mode(m))
            action.setCheckable(True)
            self.color_menu.addAction(action)
            self.color_actions[mode] = action
        view.addAction(self.act_lines)
        view.addSeparator()
        view.addAction(self.act_reset_views)
        tools = bar.addMenu(self.tr("&Tools"))
        tools.addAction(self.act_selftest)
        tools.addAction(self.act_backend_log)
        tools.addAction(self.act_diagnostics)
        tools.addSeparator()
        tools.addAction(self.act_restart)
        settings_menu = bar.addMenu(self.tr("&Settings"))
        settings_menu.addAction(self.act_settings)
        help_menu = bar.addMenu(self.tr("&Help"))
        help_menu.addAction(self.act_openmc_docs)
        help_menu.addAction(self.act_repository)
        help_menu.addSeparator()
        help_menu.addAction(self.act_about)

    def _color_modes(self) -> list[tuple[ColorMode, str]]:
        return [
            (ColorMode.MATERIAL, self.tr("Material")),
            (ColorMode.CELL, self.tr("Cell")),
            (ColorMode.NONE, self.tr("None (lines only)")),
        ]

    def _create_toolbar(self) -> None:
        toolbar = QToolBar(self.tr("Main toolbar"), self)
        toolbar.setObjectName("main_toolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(20, 20))
        toolbar.addAction(self.act_open)
        toolbar.addAction(self.act_reload)
        toolbar.addSeparator()
        toolbar.addWidget(QLabel(self.tr("Colour by:") + " "))
        self.color_combo = QComboBox()
        for mode, text in self._color_modes():
            self.color_combo.addItem(text, mode.value)
        self.color_combo.currentIndexChanged.connect(
            lambda index: self.set_color_mode(ColorMode(self.color_combo.itemData(index)))
        )
        toolbar.addWidget(self.color_combo)
        toolbar.addAction(self.act_lines)
        toolbar.addAction(self.act_reset_views)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        toolbar.addAction(self.act_settings)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)
        self._show_color_mode(self.geometry_tab.color_mode)

    def _refresh_icons(self, *args) -> None:
        self.act_open.setIcon(icon("mdi6.folder-open-outline"))
        self.act_reload.setIcon(icon("mdi6.refresh"))
        self.act_lines.setIcon(icon("mdi6.vector-square"))
        self.act_reset_views.setIcon(icon("mdi6.fit-to-screen-outline"))
        self.act_settings.setIcon(icon("mdi6.cog-outline"))
        self.act_selftest.setIcon(icon("mdi6.stethoscope"))
        self.act_restart.setIcon(icon("mdi6.restart"))
        self.tabs.setTabIcon(0, icon("mdi6.cube-outline"))
        self.tabs.setTabIcon(1, icon("mdi6.chart-bell-curve-cumulative"))
        self.geometry_tab.refresh_icon()
        self.simulation_page.refresh_icon()

    def _restore_window(self) -> None:
        geometry = self.settings.window_geometry
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self.resize(1280, 800)
        state = self.settings.window_state
        if state is not None:
            self.restoreState(state)
        self.geometry_tab.restore_state(self.settings.geometry_splitter)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.settings.window_geometry = self.saveGeometry()
        self.settings.window_state = self.saveState()
        self.settings.geometry_splitter = self.geometry_tab.save_state()
        self.settings.sync()
        super().closeEvent(event)

    def _update_actions(self) -> None:
        ready = self._backend_ready
        self.act_open.setEnabled(ready and not self.session.busy)
        self.act_reload.setEnabled(
            ready and not self.session.busy and self.session.path is not None
        )
        self.act_selftest.setEnabled(ready)
        self.act_restart.setEnabled(not self.backend.busy or not ready)

    def _update_title(self) -> None:
        info = self.session.info
        if info is None:
            self.setWindowTitle("PyVised")
            return
        title = info.sidecar.title.get(self.language) if info.sidecar else ""
        self.setWindowTitle(f"PyVised — {title or Path(info.source).name}")

    # -- models ------------------------------------------------------------------

    def choose_model(self) -> None:
        start = self.session.path.parent if self.session.path else self.settings.workspace
        patterns = " ".join(f"*{suffix}" for suffix in MODEL_SUFFIXES)
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Open model"),
            str(start),
            self.tr("OpenMC models ({patterns});;All files (*)").format(patterns=patterns),
        )
        if path:
            self.request_open(Path(path))

    def request_open(self, path: Path) -> None:
        """Open `path` now, or as soon as the backend is ready."""
        if self.session.ready:
            self.session.open(path)
        else:
            self._pending_open = path

    def reload_model(self) -> None:
        if self.session.path is not None and self.session.ready:
            self.session.reload()

    def set_color_mode(self, mode: ColorMode) -> None:
        self.geometry_tab.set_color_mode(mode)
        self._show_color_mode(mode)

    def _show_color_mode(self, mode: ColorMode) -> None:
        self.color_combo.blockSignals(True)
        self.color_combo.setCurrentIndex(max(0, self.color_combo.findData(mode.value)))
        self.color_combo.blockSignals(False)
        for key, action in self.color_actions.items():
            action.setChecked(key is mode)

    def _fill_recent_menu(self) -> None:
        self.recent_menu.clear()
        recent = self.settings.recent_models
        for path in recent:
            action = self.recent_menu.addAction(str(path))
            action.triggered.connect(lambda checked, p=path: self.request_open(p))
            action.setEnabled(self.session.ready)
        if recent:
            self.recent_menu.addSeparator()
            clear = self.recent_menu.addAction(self.tr("Clear the list"))
            clear.triggered.connect(self.settings.clear_recent_models)
        else:
            self.recent_menu.addAction(self.tr("No recent models")).setEnabled(False)

    def _model_opening(self, name: str) -> None:
        self.statusBar().showMessage(self.tr("Opening {name}…").format(name=name))
        self._update_actions()

    def _model_opened(self, info: ModelInfo, reloaded: bool) -> None:
        self.statusBar().clearMessage()
        if self.session.path is not None:
            self.settings.add_recent_model(self.session.path)
            self.settings.sync()
        self.geometry_tab.show_model(info, reloaded)
        self.tabs.setCurrentWidget(self.geometry_tab)
        self._update_title()
        self._update_actions()
        if info.warnings:
            self.statusBar().showMessage(
                self.tr("The model has warnings; see the Model section."), 8000
            )

    def _model_failed(self, message: str, detail: str) -> None:
        log.warning("opening a model failed: %s: %s", message, detail)
        self.statusBar().clearMessage()
        self._update_actions()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(self.tr("Open model"))
        box.setText(message)
        if detail:
            box.setDetailedText(detail)
            box.setInformativeText(detail.strip().splitlines()[-1][:300])
        box.exec()

    def _model_files_changed(self) -> None:
        policy = self.settings.reload_on_change
        if policy is ReloadPolicy.ALWAYS:
            self.reload_model()
        elif policy is ReloadPolicy.ASK:
            self.geometry_tab.show_files_changed()

    # -- backend -------------------------------------------------------------

    def _on_backend_state(self, state: str, detail: str) -> None:
        self.status_pill.set_state(state, detail)
        ready = state == BackendState.READY.value
        self._backend_ready = ready
        connection = self.backend.connection
        if ready and connection is not None and not self.session.ready:
            self.session.set_connection(connection, self.backend.config.workspace)
            if self._pending_open is not None:
                path, self._pending_open = self._pending_open, None
                self.session.open(path)
            elif self.session.path is not None:
                self.session.reload()  # a restarted backend has forgotten the model
        elif not ready and self.session.ready:
            self.session.set_connection(None, self.backend.config.workspace)
        self._update_actions()

    def backend_config(self) -> BackendConfig:
        return BackendConfig.from_settings(self.settings)

    def restart_backend(self) -> None:
        self.backend.restart(self.backend_config())

    def show_backend_details(self) -> None:
        text = self.status_pill.explanation(
            self.backend.state.value, self.backend.detail, self.backend.config.image
        )
        lines = [
            text,
            "",
            self.tr("Image: {image}").format(image=self.backend.config.image),
            self.tr("Container: {name}").format(name=self.backend.config.container_name),
            self.tr("Workspace: {path}").format(path=self.backend.config.workspace),
        ]
        if self.backend.connection is not None:
            lines.append(self.tr("Address: {url}").format(url=self.backend.connection.base_url))
        QMessageBox.information(self, self.tr("OpenMC backend"), "\n".join(lines))

    def run_selftest(self) -> None:
        connection = self.backend.connection
        if connection is None:
            QMessageBox.information(
                self, self.tr("Self-test"), self.tr("The backend is not ready yet.")
            )
            return

        def call() -> SelfTestResult:
            with ApiClient(connection) as api:
                return api.selftest()

        self.act_selftest.setEnabled(False)
        self.statusBar().showMessage(self.tr("Running the backend self-test…"))
        self.tasks.submit(call, on_result=self._show_selftest, on_error=self._selftest_failed)

    def _show_selftest(self, result: SelfTestResult) -> None:
        self.statusBar().clearMessage()
        self.act_selftest.setEnabled(self.backend.state is BackendState.READY)
        lines = []
        for step in result.steps:
            mark = "✓" if step.ok else "✗"
            lines.append(f"{mark} {step.name} ({step.seconds:.1f} s) {step.detail}".rstrip())
        box = QMessageBox(self)
        box.setWindowTitle(self.tr("Self-test"))
        if result.ok:
            box.setIcon(QMessageBox.Icon.Information)
            box.setText(self.tr("The backend works: it built, rendered and ran a test model."))
        else:
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(self.tr("The backend self-test failed."))
        box.setInformativeText("\n".join(lines))
        box.exec()

    def _selftest_failed(self, error: Exception) -> None:
        self.statusBar().clearMessage()
        self.act_selftest.setEnabled(self.backend.state is BackendState.READY)
        QMessageBox.warning(self, self.tr("Self-test"), str(error))

    def show_backend_log(self) -> None:
        def load() -> None:
            self.tasks.submit(
                self.backend.logs,
                on_result=dialog.set_text,
                on_error=lambda exc: dialog.set_text(str(exc)),
            )

        dialog = TextDialog(self.tr("Backend log"), self.tr("Loading…"), self, refresh=load)
        load()
        dialog.exec()

    def copy_diagnostics(self) -> None:
        self.statusBar().showMessage(self.tr("Collecting diagnostics…"))
        self.tasks.submit(
            self._diagnostics_text,
            on_result=self._diagnostics_ready,
            on_error=lambda exc: self._diagnostics_ready(str(exc)),
        )

    def _diagnostics_text(self) -> str:
        health = self.backend.health
        lines = [
            f"PyVised {__version__}",
            f"Python {platform.python_version()} · Qt {qVersion()} · PySide6 {PySide6.__version__}",
            f"OS {platform.platform()}",
            f"settings {self.settings.file_path}",
            f"workspace {self.settings.workspace}",
            f"image {self.backend.config.image}",
            f"backend state {self.backend.state.value}: {self.backend.detail}",
            f"health {health.model_dump_json() if health else '-'}",
            f"model {self.session.info.source if self.session.info else '-'}",
            "",
            "--- docker info ---",
            self.backend.docker_info(),
            "",
            "--- backend log (last 200 lines) ---",
            self.backend.logs(200),
        ]
        return "\n".join(lines)

    def _diagnostics_ready(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)
        self.statusBar().showMessage(self.tr("Diagnostics copied to the clipboard."), 5000)

    # -- workspace and settings ------------------------------------------------

    def switch_workspace(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, self.tr("Choose the workspace folder"), str(self.settings.workspace)
        )
        if not folder or Path(folder) == self.settings.workspace:
            return
        self.settings.workspace = Path(folder)
        self.settings.sync()
        self.restart_backend()

    def open_workspace_folder(self) -> None:
        folder = self.settings.workspace
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self, image_override=os.environ.get(IMAGE_ENV, ""))
        if dialog.exec() != SettingsDialog.DialogCode.Accepted:
            return
        changed = dialog.apply()
        if "theme" in changed:
            apply_theme(self.settings.theme)
        if "language" in changed:
            QMessageBox.information(
                self, self.tr("Language"), self.tr("Restart PyVised to switch the language.")
            )
        if changed & GEOMETRY_SETTINGS:
            self.geometry_tab.apply_settings()
        if changed & BACKEND_SETTINGS:
            self.restart_backend()
        elif "keep_backend_running" in changed:
            self.backend.config = self.backend_config()

    def show_about(self) -> None:
        health = self.backend.health
        AboutDialog(health.openmc_version if health else None, self).exec()
