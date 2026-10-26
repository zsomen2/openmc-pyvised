"""The main window: Geometry, Simulation and Input tabs, menus, toolbar and the backend status."""

import itertools
import logging
import os
import platform
from pathlib import Path

import PySide6
from PySide6.QtCore import QEvent, QEventLoop, QSize, Qt, QTimer, QUrl, qVersion
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QDesktopServices,
    QGuiApplication,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QSizePolicy,
    QTabWidget,
    QToolBar,
    QToolButton,
    QWidget,
)

from pyvised_protocol.schemas import ModelInfo, SelfTestResult, WeightWindowLayer

from .. import REPOSITORY_URL, __version__
from ..backend.client import ApiClient
from ..backend.manager import IMAGE_ENV, BackendConfig, BackendManager, BackendState
from ..core.examples import CopyState, Example, find_examples
from ..core.icons import icon
from ..core.model_session import MODEL_SUFFIXES, ModelSession
from ..core.paths import examples_dir
from ..core.run_session import RunSession
from ..core.settings import AppSettings, ReloadPolicy
from ..core.tasks import TaskRunner
from ..core.theme import apply_theme
from ..core.track_session import TrackSession
from ..render.colorize import ColorMode
from ..render.labels import LabelKind
from ..render.scales import format_value
from .dialogs.about import AboutDialog
from .dialogs.settings_dialog import SettingsDialog
from .dialogs.setup_dialog import SetupDialog, Status
from .dialogs.text_dialog import TextDialog
from .geometry.geometry_tab import GeometryTab
from .input.input_tab import InputTab
from .simulation.simulation_tab import SimulationTab
from .status_pill import StatusPill
from .tracks.tracks_window import TracksWindow

log = logging.getLogger(__name__)

BACKEND_SETTINGS = {"workspace", "backend_image", "backend_cpus", "added_libraries"}
GEOMETRY_SETTINGS = {"render_quality", "plot_background", "line_mode", "line_width"}
RUN_SETTINGS = {"run_threads", "run_entropy_mesh"}
RUN_END_TIMEOUT_MS = 60_000
"""How long exit and backend restart wait for a stopped run to save its results."""
SETUP_STATES = {
    BackendState.DOCKER_MISSING,
    BackendState.DOCKER_UNAVAILABLE,
    BackendState.IMAGE_MISSING,
    BackendState.PULLING,
    BackendState.LOADING,
    BackendState.ERROR,
}
"""Backend states that open the setup dialog by themselves."""


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
        self.runs = RunSession(tasks, self)
        self.track_session = TrackSession(tasks, self)
        self.tracks_window: TracksWindow | None = None
        self.setup_dialog: SetupDialog | None = None
        self._pending_open: Path | None = None
        self._backend_ready = False
        self._selftest_running = False
        self.setMinimumSize(900, 600)

        self.geometry_tab = GeometryTab(self.session, settings, language=self.language)
        self.simulation_tab = SimulationTab(
            self.session, self.runs, settings, language=self.language
        )
        self.input_tab = InputTab(self.session, settings)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self.geometry_tab, self.tr("Geometry"))
        self.tabs.addTab(self.simulation_tab, self.tr("Simulation"))
        self.tabs.addTab(self.input_tab, self.tr("Input"))
        self.tabs.currentChanged.connect(self._tab_changed)
        self.setCentralWidget(self.tabs)

        self.examples, _problems = find_examples(examples_dir())
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
        self.status_pill.setupRequested.connect(self.show_setup)
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
        self.geometry_tab.statusMessage.connect(
            lambda text: self.statusBar().showMessage(text, 6000)
        )
        self.simulation_tab.statusMessage.connect(
            lambda text: self.statusBar().showMessage(text, 8000)
        )
        self.simulation_tab.runSelected.connect(self._run_selected)
        self.input_tab.statusMessage.connect(lambda text: self.statusBar().showMessage(text, 6000))

        backend.stateChanged.connect(self._on_backend_state)
        backend.transferChanged.connect(self.status_pill.set_transfer)
        backend.operationFinished.connect(self._update_actions)
        self._on_backend_state(backend.state.value, backend.detail)
        self._refresh_icons()  # again on every palette change, see changeEvent
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
        self.act_export_image = self._action(
            self.tr("&Export image…"),
            lambda: self.geometry_tab.export_view(None, "image"),
            "Ctrl+E",
        )
        self.act_export_data = self._action(
            self.tr("Export slice &data (.npz)…"),
            lambda: self.geometry_tab.export_view(None, "data"),
        )
        self.act_exit = self._action(self.tr("E&xit"), self.close, QKeySequence.StandardKey.Quit)
        self.act_lines = self._action(self.tr("Boundary &lines"), self.geometry_tab.set_lines)
        self.act_lines.setCheckable(True)
        self.act_lines.setChecked(self.settings.show_lines)
        self.act_reset_views = self._action(
            self.tr("Re&set views"), self.geometry_tab.reset_views, "Ctrl+0"
        )
        self.act_tracks = self._action(self.tr("Particle &tracks…"), self.show_tracks, "Ctrl+T")
        self.act_tracks.setToolTip(self.tr("Generate particle tracks and play them on the views"))
        self.act_setup = self._action(self.tr("&Backend setup…"), self.show_setup)
        self.act_selftest = self._action(
            self.tr("Run backend &self-test"), lambda: self.run_selftest()
        )
        self.act_backend_log = self._action(self.tr("Backend &log…"), self.show_backend_log)
        self.act_diagnostics = self._action(self.tr("Copy &diagnostics"), self.copy_diagnostics)
        self.act_restart = self._action(self.tr("&Restart backend"), self.restart_backend)
        self.act_libraries = self._action(
            self.tr("Nuclear-data &libraries…"), lambda: self.open_settings("backend")
        )
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
        file_menu.addAction(self.act_export_image)
        file_menu.addAction(self.act_export_data)
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
        self.labels_menu = view.addMenu(self.tr("&Labels"))
        self.label_actions: dict[LabelKind, QAction] = {}
        group = QActionGroup(self)
        for kind, text in self._label_kinds():
            action = self._action(text, lambda checked, k=kind: self.set_labels(k))
            action.setCheckable(True)
            action.setChecked(kind is self.geometry_tab.label_kind)
            group.addAction(action)
            self.labels_menu.addAction(action)
            self.label_actions[kind] = action
        view.addSeparator()
        view.addAction(self.act_reset_views)
        tools = bar.addMenu(self.tr("&Tools"))
        self.examples_menu = tools.addMenu(self.tr("&Examples"))
        for example in self.examples:
            title = example.title(self.language).replace("&", "&&")
            action = self.examples_menu.addAction(f"{example.number}. {title}")
            action.triggered.connect(lambda checked, e=example: self.open_example(e))
        if not self.examples:
            self.examples_menu.addAction(self.tr("No examples found")).setEnabled(False)
        tools.addAction(self.act_tracks)
        tools.addSeparator()
        tools.addAction(self.act_setup)
        tools.addAction(self.act_selftest)
        tools.addAction(self.act_backend_log)
        tools.addAction(self.act_diagnostics)
        tools.addAction(self.act_libraries)
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
            (ColorMode.UNIVERSE, self.tr("Universe")),
            (ColorMode.DENSITY, self.tr("Density")),
            (ColorMode.TEMPERATURE, self.tr("Temperature")),
            (ColorMode.IMPORTANCE, self.tr("Importance (weight windows)")),
            (ColorMode.NONE, self.tr("None (lines only)")),
        ]

    def _label_kinds(self) -> list[tuple[LabelKind, str]]:
        return [
            (LabelKind.NONE, self.tr("No labels")),
            (LabelKind.CELL, self.tr("Cell numbers")),
            (LabelKind.MATERIAL, self.tr("Material numbers")),
            (LabelKind.UNIVERSE, self.tr("Universe numbers")),
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
        self.level_label = QLabel(" " + self.tr("Level:") + " ")
        self.level_combo = QComboBox()
        self.level_combo.setToolTip(
            self.tr("Colour by the universes at this level (0: the root universe's cells)")
        )
        self.level_combo.currentIndexChanged.connect(self._level_chosen)
        self.level_label_action = toolbar.addWidget(self.level_label)
        self.level_combo_action = toolbar.addWidget(self.level_combo)
        self.ww_combo = QComboBox()
        self.ww_combo.setToolTip(self.tr("Particle and energy group of the weight windows"))
        self.ww_combo.currentIndexChanged.connect(self._weight_windows_chosen)
        self.ww_combo_action = toolbar.addWidget(self.ww_combo)
        toolbar.addAction(self.act_lines)
        self.labels_button = QToolButton()
        self.labels_button.setText(self.tr("Labels"))
        self.labels_button.setToolTip(self.tr("Numbers on the regions"))
        self.labels_button.setMenu(self.labels_menu)
        self.labels_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.labels_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        toolbar.addWidget(self.labels_button)
        toolbar.addAction(self.act_tracks)
        toolbar.addAction(self.act_reset_views)
        toolbar.addAction(self.act_export_image)
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
        self.act_export_image.setIcon(icon("mdi6.image-move"))
        self.labels_button.setIcon(icon("mdi6.numeric"))
        self.act_settings.setIcon(icon("mdi6.cog-outline"))
        self.act_setup.setIcon(icon("mdi6.clipboard-check-outline"))
        self.act_selftest.setIcon(icon("mdi6.stethoscope"))
        self.act_restart.setIcon(icon("mdi6.restart"))
        self.tabs.setTabIcon(0, icon("mdi6.cube-outline"))
        self.tabs.setTabIcon(1, icon("mdi6.chart-bell-curve-cumulative"))
        self.tabs.setTabIcon(2, icon("mdi6.file-code-outline"))
        self.act_libraries.setIcon(icon("mdi6.database-outline"))
        self.act_tracks.setIcon(icon("mdi6.atom-variant"))
        self.examples_menu.setIcon(icon("mdi6.school-outline"))
        if self.tracks_window is not None:
            self.tracks_window.refresh_icons()
        self.geometry_tab.refresh_icon()
        self.simulation_tab.refresh_icons()
        self.input_tab.refresh_icons()

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
        self.simulation_tab.restore_state(self.settings.simulation_splitter)
        self.input_tab.restore_state(self.settings.input_splitter)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        # a light/dark switch: Qt emits colorSchemeChanged while the palette still has the
        # old colours, so the icons are re-tinted when the new palette arrives
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self._refresh_icons()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.runs.running:
            if not self._confirm_run_ends(exiting=True):
                event.ignore()
                return
            if not self.backend.config.keep_running:
                self._stop_run_and_wait()
        self.settings.window_geometry = self.saveGeometry()
        self.settings.window_state = self.saveState()
        self.settings.geometry_splitter = self.geometry_tab.save_state()
        self.settings.simulation_splitter = self.simulation_tab.save_state()
        self.settings.input_splitter = self.input_tab.save_state()
        if self.tracks_window is not None:
            self.settings.tracks_window = self.tracks_window.saveGeometry()
        self.settings.sync()
        self.track_session.cancel()
        self.runs.shutdown()
        super().closeEvent(event)

    def _confirm_run_ends(self, *, exiting: bool) -> bool:
        """Ask before something ends the running simulation (exit, backend restart)."""
        if exiting and self.backend.config.keep_running:
            text = self.tr(
                "A simulation is running. It keeps running in the backend, and PyVised shows "
                "it again at the next start."
            )
        else:
            text = self.tr(
                "A simulation is running. It stops now and keeps the results of its latest "
                "statepoint."
            )
        title = self.tr("Exit") if exiting else self.tr("Restart backend")
        buttons = QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        answer = QMessageBox.question(self, title, text, buttons, QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Ok

    def _stop_run_and_wait(self) -> None:
        """Stop the running simulation and wait until the backend has saved its results, so
        stopping the container does not cut them off."""
        loop = QEventLoop()  # kept alive by the callbacks while they can still come
        if not self.runs.stop(on_error=lambda _error: loop.quit()):
            return
        dialog = QProgressDialog(
            self.tr("Stopping the simulation and saving its results…"),
            self.tr("Do not wait"),
            0,
            0,
            self,
        )
        dialog.setWindowTitle(self.tr("Simulation"))
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.canceled.connect(loop.quit)
        self.runs.finished.connect(loop.quit)
        timer = QTimer(loop)
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(RUN_END_TIMEOUT_MS)
        dialog.show()
        if self.runs.running:
            loop.exec()
        self.runs.finished.disconnect(loop.quit)
        dialog.close()

    def _update_actions(self) -> None:
        ready = self._backend_ready
        self.act_open.setEnabled(ready and not self.session.busy)
        self.act_reload.setEnabled(
            ready and not self.session.busy and self.session.path is not None
        )
        self.act_selftest.setEnabled(ready and not self._selftest_running)
        self.act_restart.setEnabled(not self.backend.busy or not ready)
        shown = self.session.info is not None
        self.act_export_image.setEnabled(shown)
        self.act_export_data.setEnabled(shown)
        health = self.backend.health
        tracks = health is not None and "tracks" in health.capabilities
        self.act_tracks.setEnabled(ready and shown and tracks)

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

    def open_example(self, example: Example) -> None:
        """Open the workspace copy of an example; copy it there first if needed."""
        workspace = self.backend.config.workspace
        state = example.copy_state(workspace)
        restore = state is CopyState.MISSING
        if state is CopyState.CHANGED:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle(self.tr("Examples"))
            text = self.tr("Your workspace already has a changed copy of the example “{title}”.")
            box.setText(text.format(title=example.title(self.language)))
            box.setInformativeText(
                self.tr(
                    "Open your copy, or replace its files with the original? The runs of "
                    "your copy are kept either way."
                )
            )
            own = box.addButton(self.tr("Open my copy"), QMessageBox.ButtonRole.AcceptRole)
            original = box.addButton(
                self.tr("Restore the original"), QMessageBox.ButtonRole.DestructiveRole
            )
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(own)
            box.exec()
            if box.clickedButton() not in (own, original):
                return
            restore = box.clickedButton() is original
        path = example.copy_model(workspace)
        if restore:
            try:
                path = example.copy_to(workspace)
            except OSError as exc:
                log.warning("copying example %s failed: %s", example.folder.name, exc)
                QMessageBox.warning(
                    self,
                    self.tr("Examples"),
                    self.tr("The example could not be copied into the workspace: {error}").format(
                        error=exc
                    ),
                )
                return
            self.session.accept_file_changes()
        self.request_open(path)

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
        self._update_mode_controls()

    def _update_mode_controls(self) -> None:
        """Show the level and weight-window choices only where the colour mode uses them."""
        mode = self.geometry_tab.color_mode
        info = self.session.info
        universe = mode is ColorMode.UNIVERSE and info is not None
        self.level_label_action.setVisible(universe)
        self.level_combo_action.setVisible(universe)
        many = self.ww_combo.count() > 1
        self.ww_combo_action.setVisible(mode is ColorMode.IMPORTANCE and many)
        has_windows = info is not None and bool(info.weight_windows)
        index = self.color_combo.findData(ColorMode.IMPORTANCE.value)
        item = self.color_combo.model().item(index)  # type: ignore[attr-defined]
        if item is not None:
            item.setEnabled(has_windows or info is None)
        self.color_actions[ColorMode.IMPORTANCE].setEnabled(has_windows or info is None)

    def _fill_mode_controls(self, info: ModelInfo) -> None:
        """Level and weight-window choices of a newly opened model."""
        self.level_combo.blockSignals(True)
        self.level_combo.clear()
        self.level_combo.addItem(self.tr("Deepest"), None)
        for level in range(info.max_level + 1):
            self.level_combo.addItem(str(level), level)
        current = self.geometry_tab.universe_level
        self.level_combo.setCurrentIndex(max(0, self.level_combo.findData(current)))
        self.level_combo.blockSignals(False)
        self.ww_combo.blockSignals(True)
        self.ww_combo.clear()
        seen = set()
        for ww in info.weight_windows:
            bounds = ww.energy_bounds or [0.0, 0.0]
            for low, high in itertools.pairwise(bounds):
                energy = (low * high) ** 0.5 if low > 0 else high / 2
                key = (ww.particle, format_value(low), format_value(high))
                if key in seen:
                    continue
                seen.add(key)
                text = self.tr("{particle}, {low} – {high} eV").format(
                    particle=ww.particle, low=key[1], high=key[2]
                )
                self.ww_combo.addItem(text, (ww.particle, energy))
        self.ww_combo.blockSignals(False)
        self._update_mode_controls()

    def _level_chosen(self, index: int) -> None:
        self.geometry_tab.set_universe_level(self.level_combo.itemData(index))

    def _weight_windows_chosen(self, index: int) -> None:
        data = self.ww_combo.itemData(index)
        if data is not None:
            particle, energy = data
            self.geometry_tab.set_weight_window_layer(
                WeightWindowLayer(particle=particle, energy=energy)
            )

    def show_tracks(self) -> None:
        """Open (or bring up) the particle tracks window next to the main window."""
        window = self.tracks_window
        if window is None:
            window = TracksWindow(
                self.geometry_tab.tracks,
                self.track_session,
                self.settings,
                lambda: self.geometry_tab.inspected_point,
                self,
            )
            window.moveCutsRequested.connect(self.geometry_tab.move_cuts)
            saved = self.settings.tracks_window
            if saved is None or not window.restoreGeometry(saved):
                frame = self.frameGeometry()
                window.move(frame.right() - window.width() - 24, frame.top() + 90)
            self.tracks_window = window
        window.set_model(self.session.info)
        self.tabs.setCurrentWidget(self.geometry_tab)
        window.show()
        window.raise_()
        window.activateWindow()

    def set_labels(self, kind: LabelKind) -> None:
        self.geometry_tab.set_labels(kind)
        for key, action in self.label_actions.items():
            action.setChecked(key is kind)

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
        self.geometry_tab.set_busy(True)
        self.simulation_tab.set_busy(True)
        self._update_actions()

    def _model_opened(self, info: ModelInfo, reloaded: bool) -> None:
        self.statusBar().clearMessage()
        if self.session.path is not None:
            self.settings.add_recent_model(self.session.path)
            self.settings.sync()
        self.geometry_tab.set_busy(False)
        self.simulation_tab.set_busy(False)
        self.geometry_tab.show_model(info, reloaded)
        self.simulation_tab.show_model(info)
        self.input_tab.show_model(self.session.path)
        if self.tracks_window is not None:
            self.tracks_window.set_model(info)
        self._fill_mode_controls(info)
        if self.geometry_tab.color_mode is ColorMode.IMPORTANCE and not info.weight_windows:
            self.set_color_mode(ColorMode.MATERIAL)
        if not reloaded:
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
        self.geometry_tab.set_busy(False)
        self.simulation_tab.set_busy(False)
        self._update_actions()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(self.tr("Open model"))
        box.setText(message)
        if detail:
            box.setDetailedText(detail)
            box.setInformativeText(detail.strip().splitlines()[-1][:300])
        box.exec()

    def _run_selected(self, run_id: str | None) -> None:
        """The run shown in the Simulation tab: the Input tab lists the code it kept."""
        run = self.simulation_tab.run_info(run_id)
        folder = self.simulation_tab.run_folder(run_id) if run_id is not None else None
        self.input_tab.set_run(run, folder)

    def _tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.input_tab:
            self.input_tab.focus_code()

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
            self.runs.set_connection(connection)
            self.track_session.set_connection(connection)
            self.simulation_tab.set_backend(self.backend.health)
            if self._pending_open is not None:
                path, self._pending_open = self._pending_open, None
                self.session.open(path)
            elif self.session.path is not None:
                self.session.reload()  # a restarted backend has forgotten the model
        elif not ready and self.session.ready:
            self.session.set_connection(None, self.backend.config.workspace)
            self.runs.set_connection(None)
            self.track_session.set_connection(None)
            self.simulation_tab.set_backend(None)
        self._update_actions()
        if BackendState(state) in SETUP_STATES:
            self.show_setup()
        image_id = self.backend.facts.image_id
        if ready and image_id and image_id != self.settings.selftest_image:
            self.run_selftest(auto=True)  # once per new image: the first start, an update

    def backend_config(self) -> BackendConfig:
        return BackendConfig.from_settings(self.settings)

    def restart_backend(self) -> None:
        if self.runs.running:
            if not self._confirm_run_ends(exiting=False):
                return
            self._stop_run_and_wait()
        self.backend.restart(self.backend_config())

    def _setup(self) -> SetupDialog:
        if self.setup_dialog is None:
            dialog = SetupDialog(self.backend, self)
            if self.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):  # --screenshot
                dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            dialog.retryRequested.connect(self.restart_backend)
            dialog.loadRequested.connect(self.load_backend_image)
            dialog.removeRequested.connect(self.remove_backend_image)
            dialog.selftestRequested.connect(lambda: self.run_selftest())
            dialog.diagnosticsRequested.connect(self.copy_diagnostics)
            self.setup_dialog = dialog
        return self.setup_dialog

    def show_setup(self) -> None:
        dialog = self._setup()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def load_backend_image(self, path: Path) -> None:
        if self.runs.running:
            if not self._confirm_run_ends(exiting=False):
                return
            self._stop_run_and_wait()
        self.backend.load_image(path)

    def remove_backend_image(self) -> None:
        text = self.tr(
            "Remove the OpenMC backend image {image} from Docker? This frees about 3.4 GB; "
            "PyVised downloads the image again at its next start."
        ).format(image=self.backend.config.image)
        buttons = QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        title = self.tr("Remove the backend image")
        answer = QMessageBox.question(self, title, text, buttons, QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Ok:
            return
        if self.runs.running:
            if not self._confirm_run_ends(exiting=False):
                return
            self._stop_run_and_wait()
        self.backend.remove_image()

    def run_selftest(self, auto: bool = False) -> None:
        """Build, render and run a test model in the backend; the result goes to the setup
        dialog, which an automatic run (after a new image) shows only on failure."""
        connection = self.backend.connection
        dialog = self._setup()
        if connection is None:
            self.show_setup()  # it says why the backend is not ready
            return
        if self._selftest_running:
            return
        self._selftest_running = True
        image_id = self.backend.facts.image_id or ""
        dialog.set_selftest(
            Status.RUNNING,
            self.tr("running…"),
            self.tr("Building, rendering and running a small test model…"),
        )
        if not auto:
            self.show_setup()
        self._update_actions()
        self.statusBar().showMessage(self.tr("Running the backend self-test…"))

        def call() -> SelfTestResult:
            with ApiClient(connection) as api:
                return api.selftest()

        self.tasks.submit(
            call,
            on_result=lambda result: self._selftest_done(result, image_id),
            on_error=self._selftest_failed,
        )

    def _step_name(self, name: str) -> str:
        names = {
            "build": self.tr("build"),
            "render": self.tr("render"),
            "transport": self.tr("transport"),
        }
        return names.get(name, name)

    def _selftest_done(self, result: SelfTestResult, image_id: str) -> None:
        self._selftest_running = False
        self.statusBar().clearMessage()
        self._update_actions()
        dialog = self._setup()
        if result.ok:
            times = ", ".join(
                f"{self._step_name(step.name)} {step.seconds:.1f} s" for step in result.steps
            )
            if result.k_eff is not None and result.k_eff_std is not None:
                times += f"; k-eff = {result.k_eff:.4f} ± {result.k_eff_std:.4f}"
            dialog.set_selftest(Status.OK, self.tr("passed"), times)
            if image_id:
                self.settings.selftest_image = image_id
                self.settings.sync()
            self.statusBar().showMessage(self.tr("The backend self-test passed."), 6000)
            return
        failed = [step for step in result.steps if not step.ok]
        detail = "\n".join(f"{self._step_name(step.name)}: {step.detail}" for step in failed)
        dialog.set_selftest(Status.FAILED, self.tr("failed"), detail)
        self.show_setup()

    def _selftest_failed(self, error: Exception) -> None:
        self._selftest_running = False
        self.statusBar().clearMessage()
        self._update_actions()
        self._setup().set_selftest(Status.FAILED, self.tr("failed"), str(error))
        self.show_setup()

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
        facts = self.backend.facts
        lines = [
            f"PyVised {__version__}",
            f"Python {platform.python_version()} · Qt {qVersion()} · PySide6 {PySide6.__version__}",
            f"OS {platform.platform()}",
            f"settings {self.settings.file_path}",
            f"workspace {self.settings.workspace}",
            f"docker {facts.docker or '-'} · engine {facts.engine or '-'}"
            f" · Docker Desktop {facts.desktop_version or '-'} ({facts.desktop or '-'})",
            f"docker data {facts.data_dir or '-'} · free {facts.free_bytes or '-'} bytes",
            f"image {self.backend.config.image} · id {facts.image_id or '-'}",
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

    def open_settings(self, page: str = "") -> None:
        dialog = SettingsDialog(self.settings, self, image_override=os.environ.get(IMAGE_ENV, ""))
        dialog.show_page(page)
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
        if changed & RUN_SETTINGS:
            self.simulation_tab.form.apply_settings()
        if changed & BACKEND_SETTINGS:
            self.restart_backend()
        elif changed & {"keep_backend_running", "remove_old_images"}:
            self.backend.config = self.backend_config()

    def show_about(self) -> None:
        health = self.backend.health
        AboutDialog(health.openmc_version if health else None, self).exec()
