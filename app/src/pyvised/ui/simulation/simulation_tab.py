"""The Simulation tab: run settings on the left; the live k and entropy plots above the
results of the shown run (Summary, Tallies, Plots, Log, History) on the right.

The shown run is the one that runs now, or the one picked in the history. Its
results come from the backend once it has ended; tally arrays are loaded per
tally. Builder parameters are the same values as in the Geometry tab: a run
with edited values also re-exports the model there.
"""

import re
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFontDatabase, QTextCursor
from PySide6.QtWidgets import (
    QFileDialog,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pyvised_protocol.runs import RunInfo, RunProgress, RunRequest, RunResults
from pyvised_protocol.schemas import HealthInfo, ModelInfo
from pyvised_protocol.sidecar import Sidecar

from ...core.icons import icon
from ...core.model_session import ModelSession
from ...core.run_session import RunSession, progress_from_results
from ...core.settings import AppSettings
from ...render import tallies as tm
from ..empty_state import EmptyState
from .history_view import HistoryView
from .live_plots import LivePlots
from .run_form import RunForm
from .summary_view import SummaryView
from .tally_plots import TallyPlots
from .tally_view import TallyView

LOG_BLOCKS = 50_000
MIN_FORM_WIDTH = 330
"""[px] The run form's side at least."""


class SimulationTab(QWidget):
    statusMessage = Signal(str)

    def __init__(
        self,
        session: ModelSession,
        runs: RunSession,
        settings: AppSettings,
        parent: QWidget | None = None,
        language: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.runs = runs
        self.settings = settings
        self.language = language or settings.language
        self.info: ModelInfo | None = None
        self.selected: str | None = None
        """Id of the run whose results are shown."""
        self.results: RunResults | None = None
        self._pinned = False
        """The user picked the shown run in the history (a new run does not replace it)."""
        self._comparing: list[str] = []

        self.empty = EmptyState(
            "mdi6.chart-bell-curve-cumulative",
            self.tr("No model open"),
            self.tr("Open a model to run it: the run settings start from the model's own."),
        )
        self.form = RunForm(settings)
        scroll = self.form_scroll = QScrollArea()
        scroll.setWidget(self.form)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._fit_form()
        self.live = LivePlots()
        self.summary = SummaryView()
        self.tallies = TallyView()
        self.plots = TallyPlots()
        self.plots.outline_source = self._outline
        self.log = QPlainTextEdit(readOnly=True)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log.setMaximumBlockCount(LOG_BLOCKS)
        self.history = HistoryView()
        self.result_tabs = QTabWidget()
        self.result_tabs.setDocumentMode(True)
        self.result_tabs.addTab(self.summary, self.tr("Summary"))
        self.result_tabs.addTab(self.tallies, self.tr("Tallies"))
        self.result_tabs.addTab(self.plots, self.tr("Plots"))
        self.result_tabs.addTab(self.log, self.tr("Log"))
        self.result_tabs.addTab(self.history, self.tr("History"))
        self.folder_button = QToolButton()
        self.folder_button.setAutoRaise(True)
        self.folder_button.setToolTip(self.tr("Open the folder of the shown run"))
        self.folder_button.clicked.connect(lambda: self.open_run_folder())
        self.result_tabs.setCornerWidget(self.folder_button)
        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(self.live)
        right.addWidget(self.result_tabs)
        right.setStretchFactor(0, 2)
        right.setStretchFactor(1, 3)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(scroll)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([360, 1000])
        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(6, 6, 6, 6)
        content_layout.addWidget(self.splitter)
        self.stack.addWidget(content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)

        self.form.runRequested.connect(self.start_run)
        self.form.stopRequested.connect(self.stop_run)
        self.form.parameters.applyRequested.connect(self._apply_parameters)
        self.runs.currentChanged.connect(self._current_changed)
        self.runs.progressChanged.connect(self._progress_changed)
        self.runs.logReset.connect(self._log_reset)
        self.runs.logAppended.connect(self._log_appended)
        self.runs.historyChanged.connect(self._history_changed)
        self.runs.finished.connect(self._run_finished)
        self.runs.startFailed.connect(self._start_failed)
        self.runs.busyChanged.connect(lambda busy: self._update_state())
        self.history.showRequested.connect(lambda run_id: self.select(run_id, pinned=True))
        self.history.compareRequested.connect(self.compare)
        self.history.openFolderRequested.connect(self.open_run_folder)
        self.history.deleteRequested.connect(self.delete_run)
        self.history.refreshRequested.connect(self.runs.refresh_history)
        self.history.export_button.clicked.connect(
            lambda: self._save_text(self.history.csv_text(), "comparison")
        )
        self.tallies.tallyChanged.connect(self._load_tally)
        self.plots.tallyChanged.connect(self._load_tally)
        self.tallies.normalisationSaved.connect(self.save_normalisation)
        self.tallies.exportRequested.connect(
            lambda: self._save_text(self.tallies.csv_text(), f"tally_{self.tallies.tally_id}")
        )
        self.plots.exportRequested.connect(self._export_plot)
        self.show_model(None)

    # -- model and backend -----------------------------------------------------------------

    @property
    def sidecar(self) -> Sidecar | None:
        return self.info.sidecar if self.info is not None else None

    def show_model(self, info: ModelInfo | None) -> None:
        source = info.source if info is not None else None
        if self.info is None or self.info.source != source:  # another model: its own runs
            self.selected, self.results, self._pinned = None, None, False
            self._comparing = []
            self.log.clear()
        self.info = info
        self.stack.setCurrentIndex(0 if info is None else 1)
        self.form.set_model(info, self.language)
        self._fit_form()
        self.runs.set_model(info.source if info is not None else None)
        self.tallies.set_sidecar(self.sidecar)
        self.plots.set_sidecar(self.sidecar)
        self._references()
        self._update_state()

    def set_backend(self, health: HealthInfo | None) -> None:
        self.form.set_backend(health)
        self._update_state()

    def _fit_form(self) -> None:
        """The run form only scrolls vertically, so its side is as wide as the form needs
        (the model's parameters and the language change that)."""
        scroll = self.form_scroll
        bar = scroll.verticalScrollBar().sizeHint().width()
        width = self.form.minimumSizeHint().width() + bar + 2 * scroll.frameWidth()
        scroll.setMinimumWidth(max(MIN_FORM_WIDTH, width))

    def set_busy(self, busy: bool) -> None:
        """The model is being exported (parameters applied)."""
        self.form.parameters.set_busy(busy or self.runs.running)

    def refresh_icons(self) -> None:
        self.empty.refresh_icon()
        self.form.refresh_icons()
        self.folder_button.setIcon(icon("mdi6.folder-open-outline"))

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self._show_selected()
            self.plots.redraw()  # the series colours follow the theme

    # -- running ------------------------------------------------------------------------------

    def start_run(self) -> None:
        problem = self.form.valid()
        if problem:
            QMessageBox.information(self, self.tr("Run"), problem)
            return
        info = self.info
        if info is None:
            return
        parameters = self.form.parameters
        params = parameters.values() if parameters.has_parameters else {}
        if parameters.has_parameters and parameters.edited and self.session.path is not None:
            self.session.open(self.session.path, params)  # the same values in the Geometry tab
        request = RunRequest(
            path=info.source,
            library=self.form.library_id,
            params=params,
            label=self.form.label.text().strip(),
            settings=self.form.request_settings(),
            created=datetime.now().astimezone(),
        )
        self._pinned = False
        self.runs.start(request)
        self._update_state()

    def stop_run(self) -> None:
        if self.runs.stop():
            self.statusMessage.emit(self.tr("Stopping the run; it keeps its latest statepoint."))

    def _apply_parameters(self, params: dict[str, float]) -> None:
        if self.session.path is not None:
            self.session.open(self.session.path, params)

    def _start_failed(self, message: str, detail: str) -> None:
        self._update_state()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(self.tr("Run"))
        box.setText(message)
        if detail:
            box.setDetailedText(detail)
            box.setInformativeText(detail.strip().splitlines()[-1][:300])
        box.exec()

    def _update_state(self) -> None:
        current = self.runs.current
        if self.runs.starting:
            text = self.tr("Exporting the model and starting OpenMC…")
        elif current is not None and current.active:
            text = self.tr("Running: {name}").format(name=current.label or current.path)
            if self.info is not None and current.model != self.info.source:
                text += " " + self.tr("(another model)")
        else:
            text = ""
        self.form.set_running(self.runs.running, text)

    def _current_changed(self, run: RunInfo | None) -> None:
        self._update_state()
        if run is None:
            return
        mine = self.info is not None and run.model == self.info.source
        follow = run.active and mine and (not self._pinned or self.selected is None)
        if follow and self.selected != run.id:
            self.select(run.id)
        if run.id == self.selected:
            self._show_selected()
        self._refresh_live()

    def _progress_changed(self, progress) -> None:
        current = self.runs.current
        if current is not None and current.active:
            self.live.set_progress(progress, current)

    def _refresh_live(self) -> None:
        """The live plots: the run going on now, else the shown run's history."""
        current = self.runs.current
        run = self.run_info(self.selected)
        if current is not None and current.active:
            self.live.set_progress(self.runs.progress, current)
            self._references(current.params)
        elif run is not None and self.results is not None and not run.active:
            self.live.set_progress(progress_from_results(self.results), run)
            self._references(run.params)
        elif current is not None and current.id == self.selected:
            self.live.set_progress(self.runs.progress, current)
            self._references(current.params)
        else:
            self.live.set_progress(RunProgress(), None)
            self._references()

    def _run_finished(self, run: RunInfo) -> None:
        self._update_state()
        if run.id == self.selected:
            self.results = None
            self.select(run.id, pinned=self._pinned)
        name = run.label or run.path.rsplit("/", 1)[-1]
        self.statusMessage.emit(
            self.tr("Run {name}: {state}").format(name=name, state=self.summary.state_text(run))
        )

    def _references(self, params: dict[str, float] | None = None) -> None:
        sidecar = self.sidecar
        if sidecar is None:
            self.live.set_references([])
            return
        values = params if params is not None else self.form.parameters.values()
        if self.info is not None and params is None:
            values = {**self.info.params, **values}
        refs = [r for r in tm.matching(sidecar.references, values) if r.tally is None]
        self.live.set_references([(r.name, r.value, tm.reference_std(r)) for r in refs])

    # -- the shown run ---------------------------------------------------------------------------

    def run_info(self, run_id: str | None) -> RunInfo | None:
        if run_id is None:
            return None
        current = self.runs.current
        if current is not None and current.id == run_id:
            return current
        return next((r for r in self.runs.history if r.id == run_id), None)

    def select(self, run_id: str, *, pinned: bool = False) -> None:
        """Show the results (and the log) of a run."""
        changed = run_id != self.selected
        self.selected, self._pinned = run_id, pinned
        run = self.run_info(run_id)
        if changed:
            self.results = None
            self.log.clear()
            self.history.select_run(run_id)
        self.results = self.runs.cached_results(run_id) if self.results is None else self.results
        self._show_selected()
        if run is None:
            return
        if run.has_results and self.results is None and not run.active:
            self.runs.results(
                run_id,
                lambda r: self._results_loaded(run_id, r),
                lambda e: self._results_failed(run_id, e),
            )
        if changed:
            if self._following_selected():
                self.log.setPlainText(self.runs.current_log)
                self._log_to_end()
            else:
                self.runs.log_text(run_id, lambda text: self._log_loaded(run_id, text))

    def _results_loaded(self, run_id: str, results: RunResults) -> None:
        if run_id == self.selected:
            self.results = results
            self._show_selected()

    def _results_failed(self, run_id: str, error: Exception) -> None:
        if run_id == self.selected:
            self.statusMessage.emit(self.tr("No results: {error}").format(error=error))

    def _show_selected(self) -> None:
        run = self.run_info(self.selected)
        mine = run is not None and self.info is not None and run.model == self.info.source
        sidecar = self.sidecar if mine else None
        results = self.results if run is not None and not run.active else None
        self.folder_button.setEnabled(run is not None)
        self._refresh_live()
        self.summary.show_run(run, results, sidecar)
        self.tallies.set_results(run, results, sidecar, editable=mine)
        self.plots.set_results(run, results, sidecar)

    def _load_tally(self, tally_id: int) -> None:
        run_id = self.selected
        if run_id is None or self.results is None:
            return

        def loaded(data) -> None:
            if run_id == self.selected:
                self.tallies.set_data(data)
                self.plots.set_data(data)

        self.runs.tally(run_id, tally_id, loaded, lambda e: self.statusMessage.emit(str(e)))

    def _outline(self, request, on_result) -> bool:
        return self.session.request_slice(request, on_result, lambda error: None)

    # -- log --------------------------------------------------------------------------------

    def _following_selected(self) -> bool:
        current = self.runs.current
        return current is not None and current.id == self.selected

    def _log_reset(self, text: str) -> None:
        if self._following_selected():
            self.log.setPlainText(text)
            self._log_to_end()

    def _log_appended(self, text: str) -> None:
        if not self._following_selected():
            return
        bar = self.log.verticalScrollBar()
        position = bar.value()
        at_end = position >= bar.maximum() - 4
        # a cursor of its own: the view and the user's selection stay where they are
        cursor = QTextCursor(self.log.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        if at_end:
            self._log_to_end()
        else:
            bar.setValue(position)

    def _log_loaded(self, run_id: str, text: str) -> None:
        if run_id == self.selected:
            self.log.setPlainText(text)
            self._log_to_end()

    def _log_to_end(self) -> None:
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    # -- history ------------------------------------------------------------------------------

    def _history_changed(self) -> None:
        self.history.set_runs(self.runs.history, self.sidecar)
        if self.selected is None and self.runs.history:
            newest = self.runs.history[0]
            self.select(newest.id)
        elif self.selected is not None:
            run = self.run_info(self.selected)
            if run is not None and run.has_results and self.results is None and not run.active:
                self.select(self.selected, pinned=self._pinned)
            else:
                self._show_selected()

    def compare(self, run_ids: list[str]) -> None:
        wanted = [i for i in run_ids if (run := self.run_info(i)) is not None and run.has_results]
        if len(wanted) < 2:
            self.statusMessage.emit(self.tr("Pick at least two runs with results."))
            return
        self._comparing = wanted  # a newer comparison replaces this list
        loaded: dict[str, RunResults] = {}

        def check() -> None:
            if self._comparing is not wanted or any(i not in loaded for i in wanted):
                return
            if len(wanted) < 2:
                self.statusMessage.emit(self.tr("Pick at least two runs with results."))
                return
            pairs = [(self.run_info(i), loaded[i]) for i in wanted]
            self.history.show_comparison([(r, res) for r, res in pairs if r is not None])

        def one(run_id: str, results: RunResults) -> None:
            loaded[run_id] = results
            check()

        def failed(run_id: str, error: Exception) -> None:
            if run_id in wanted:
                wanted.remove(run_id)
            self.statusMessage.emit(self.tr("No results: {error}").format(error=error))
            check()

        for run_id in list(wanted):
            self.runs.results(
                run_id, lambda r, i=run_id: one(i, r), lambda e, i=run_id: failed(i, e)
            )

    def run_folder(self, run_id: str) -> Path | None:
        run = self.run_info(run_id)
        workspace = self.session.workspace
        if run is None or workspace is None:
            return None
        return workspace / Path(run.path)

    def open_run_folder(self, run_id: str | None = None) -> None:
        folder = self.run_folder(run_id or self.selected or "")
        if folder is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def delete_run(self, run_id: str) -> None:
        run = self.run_info(run_id)
        if run is None:
            return
        answer = QMessageBox.question(
            self,
            self.tr("Delete run"),
            self.tr("Delete the run folder {path} with all its files?").format(path=run.path),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if run_id == self.selected:
            self.selected, self.results = None, None
            self._show_selected()
            self.log.clear()
        self.runs.delete(run_id)

    # -- tallies --------------------------------------------------------------------------------

    def save_normalisation(self, tally_id: int, norm) -> None:
        sidecar = self.sidecar
        if sidecar is None and self.info is None:
            return
        current = dict(sidecar.tallies) if sidecar is not None else {}
        if norm is None:
            current.pop(tally_id, None)
        else:
            current[tally_id] = norm
        data = {str(k): v.model_dump(exclude_defaults=False) for k, v in sorted(current.items())}
        try:
            self.session.update_sidecar({"tallies": data or None})
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self.tr("Normalisation"), str(exc))
            return
        self.tallies.set_sidecar(self.sidecar)
        self.plots.set_sidecar(self.sidecar)
        self._show_selected()
        if self.selected is not None and self.tallies.tally_id is not None:
            self._load_tally(self.tallies.tally_id)
        self.statusMessage.emit(self.tr("Normalisation saved in pyvised.json."))

    # -- export --------------------------------------------------------------------------------

    def _default_folder(self) -> Path:
        folder = self.run_folder(self.selected) if self.selected else None
        if folder is not None and folder.is_dir():
            return folder
        return self.session.path.parent if self.session.path else Path.home()

    def _save_text(self, text: str, name: str) -> None:
        if not text:
            return
        safe = re.sub(r"[^\w.-]+", "_", name)
        path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("Export CSV"),
            str(self._default_folder() / f"{safe}.csv"),
            self.tr("CSV files (*.csv)"),
        )
        if not path:
            return
        try:
            Path(path).write_text(text, encoding="utf-8-sig")
        except OSError as exc:
            QMessageBox.warning(self, self.tr("Export"), str(exc))
            return
        self.statusMessage.emit(self.tr("Saved {path}").format(path=path))

    def _export_plot(self, kind: str) -> None:
        tally = self.plots.tally_id
        name = f"tally_{tally}" if tally is not None and tally >= 0 else "keff"
        if kind == "data":
            self._save_text(self.plots.csv_text(), f"{name}_plot")
            return
        png = self.tr("PNG image (*.png)")
        svg = self.tr("SVG image (*.svg)")
        path, chosen = QFileDialog.getSaveFileName(
            self,
            self.tr("Export plot"),
            str(self._default_folder() / f"{name}.png"),
            f"{png};;{svg}",
        )
        if not path:
            return
        target = Path(path)
        if chosen == svg and target.suffix.lower() != ".svg":
            target = target.with_suffix(".svg")
        try:
            self.plots.save_image(target)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self.tr("Export"), str(exc))
            return
        self.statusMessage.emit(self.tr("Saved {path}").format(path=target))

    # -- state -------------------------------------------------------------------------------------

    def save_state(self):
        return self.splitter.saveState()

    def restore_state(self, state) -> None:
        if state is not None:
            self.splitter.restoreState(state)
