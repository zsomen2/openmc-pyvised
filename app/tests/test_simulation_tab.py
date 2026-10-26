"""The Simulation tab with a fake run API: form, parameters, results, plots, history."""

import math
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import Qt
from sim_fakes import FakeRunApi, SimSession, results_for, run_info, sim_model_info

from pyvised.backend.client import Connection, TallyData
from pyvised.core import run_session
from pyvised.core.run_session import RunSession
from pyvised.core.tasks import TaskRunner
from pyvised.render import tallies as tm
from pyvised.ui.parameters_panel import ParametersPanel, SwitchLabel
from pyvised.ui.simulation.simulation_tab import SimulationTab
from pyvised.ui.simulation.tally_plots import TallyPlots
from pyvised.ui.simulation.tally_view import QUALITY_BACKGROUND
from pyvised_protocol.runs import Event, MeshInfo, RunProgress, TallyFilterResult, TallyResult
from pyvised_protocol.schemas import HealthInfo, LibraryInfo

HEALTH = HealthInfo(
    protocol_version=4,
    backend_version="0",
    openmc_version="0.16.0",
    python_version="3.13",
    cpu_count=16,
    libraries=[
        LibraryInfo(id="builtin", name="ENDF/B-VII.1", path="/x.xml", builtin=True),
        LibraryInfo(id="custom:endfb80", name="endfb80", path="/y.xml", builtin=False),
    ],
)


@pytest.fixture
def tab(qtbot, settings, monkeypatch, tmp_path: Path):
    FakeRunApi.instances.clear()
    monkeypatch.setattr(run_session, "ApiClient", FakeRunApi)
    monkeypatch.setattr(run_session.EventStream, "start", lambda self, connection: None)
    folder = tmp_path / "hw"
    folder.mkdir()
    session = SimSession(folder)
    session.info = sim_model_info()
    tasks = TaskRunner()
    runs = RunSession(tasks)
    runs.set_connection(Connection("http://127.0.0.1:1", "t"))
    widget = SimulationTab(session, runs, settings)  # type: ignore[arg-type]
    qtbot.addWidget(widget)
    widget.resize(1500, 900)
    widget.show()
    widget.set_backend(HEALTH)
    widget.show_model(session.info)
    yield widget
    runs.shutdown()
    tasks.shutdown()


def api() -> FakeRunApi:
    return FakeRunApi.instances[-1]


def test_the_form_starts_from_the_model(tab: SimulationTab):
    form = tab.form
    assert tab.stack.currentIndex() == 1
    assert (form.particles.value(), form.batches.value(), form.inactive.value()) == (2000, 220, 20)
    assert [form.preset.itemText(i) for i in range(form.preset.count())] == [
        "Model settings",
        "Validation",
    ]
    assert form.threads.specialValueText() == "all (16)"
    assert [form.library.itemData(i) for i in range(form.library.count())] == [
        "builtin",
        "custom:endfb80",
    ]
    assert form.library_id is None
    form.library.setCurrentIndex(1)
    assert form.library_id == "custom:endfb80"


def test_a_preset_sets_sizes_and_parameters(tab: SimulationTab):
    form = tab.form
    form.preset.setCurrentIndex(1)
    form.preset.activated.emit(1)
    assert (form.particles.value(), form.batches.value()) == (20000, 200)
    assert form.parameters.values() == {"thermal_scattering": 0.0, "rod": 30.0}
    assert form.parameters.edited
    form.preset.activated.emit(0)
    assert form.particles.value() == 2000


def test_parameters_panel_switch_and_values(qtbot, tab: SimulationTab):
    panel: ParametersPanel = tab.form.parameters
    switch = panel._editors["thermal_scattering"]
    spin = panel._editors["rod"]
    assert switch.isChecked() and spin.value() == 30.0  # type: ignore[union-attr]
    assert not panel.edited and not panel.apply_button.isEnabled()
    spin.setValue(-12)  # type: ignore[union-attr]
    assert panel.edited and panel.apply_button.isEnabled()
    with qtbot.waitSignal(panel.applyRequested) as blocker:
        panel.apply_button.click()
    assert blocker.args[0] == {"thermal_scattering": 1.0, "rod": -12.0}
    assert tab.session.opened[-1][1] == {"thermal_scattering": 1.0, "rod": -12.0}
    panel.reset_to_defaults()
    assert panel.values() == {"thermal_scattering": 1.0, "rod": 30.0}


def test_a_switch_label_wraps_and_toggles(qtbot, tab: SimulationTab):
    panel: ParametersPanel = tab.form.parameters
    switch = panel._editors["thermal_scattering"]
    caption = panel.findChild(SwitchLabel)
    assert caption is not None and caption.wordWrap() and switch.isChecked()  # type: ignore[union-attr]
    qtbot.mouseClick(caption, Qt.MouseButton.LeftButton)
    assert not switch.isChecked() and panel.edited  # type: ignore[union-attr]


def test_run_sends_the_settings(qtbot, tab: SimulationTab):
    form = tab.form
    form.batches.setValue(100)
    form.keff_on.setChecked(True)
    form.keff_std.setValue(50)
    form.time_limit_on.setChecked(True)
    form.time_limit.setValue(2)
    form.label.setText("quick")
    form.parameters._editors["rod"].setValue(10)  # type: ignore[union-attr]
    form.run_button.click()
    qtbot.waitUntil(lambda: bool(api().started), timeout=3000)
    request = api().started[0]
    assert request.path == "hw/model.py" and request.label == "quick"
    assert request.params == {"thermal_scattering": 1.0, "rod": 10.0}
    s = request.settings
    assert (s.particles, s.batches, s.inactive) == (2000, 100, 20)
    assert s.keff_std == pytest.approx(5e-4) and s.time_limit == pytest.approx(120.0)
    assert s.threads is None and s.tally_rel_err is None
    assert tab.session.opened[-1][1]["rod"] == 10.0, "the Geometry tab gets the same values"
    qtbot.waitUntil(lambda: tab.selected == "run3", timeout=3000)
    assert not form.run_button.isEnabled() and form.stop_button.isEnabled()
    tab.runs.events.received.emit(
        Event(
            type="run.progress",
            run_id="run3",
            data=RunProgress(
                k=[1.0, 1.1],
                entropy=[3.0, 3.1],
                mean=[None, None],
                std=[None, None],
                batch=2,
                batches=100,
            ).model_dump(mode="json"),
        )
    )
    assert tab.live.progress_bar.value() == 2
    assert "batch 2 of 100" in tab.live.status.text()
    form.stop_button.click()
    qtbot.waitUntil(lambda: api().stopped == ["run3"], timeout=3000)


def test_results_of_a_finished_run(qtbot, tab: SimulationTab):
    qtbot.waitUntil(lambda: len(tab.runs.history) == 2, timeout=3000)
    tab.select("run1", pinned=True)
    qtbot.waitUntil(lambda: tab.results is not None, timeout=3000)
    html = tab.summary.browser.toPlainText()
    assert "1.04050 ± 0.00040" in html
    assert "k-eff model B" in html and "k-eff other code" not in html, (
        "only the matching references"
    )
    assert "$" in html  # β_eff from the sidecar
    qtbot.waitUntil(lambda: tab.tallies.model.rowCount() == 3, timeout=3000)
    model = tab.tallies.model
    rel_column = model.columnCount() - 4  # rel. error, FOM, normalised, normalised σ
    assert model.headerData(0, Qt.Orientation.Horizontal) == "energy"
    assert model.headerData(rel_column, Qt.Orientation.Horizontal) == "rel. error"
    colours = [
        model.data(model.index(r, rel_column), Qt.ItemDataRole.BackgroundRole) for r in range(3)
    ]
    assert colours == [
        QUALITY_BACKGROUND[tm.Quality.FAIR],
        QUALITY_BACKGROUND[tm.Quality.FAIR],
        QUALITY_BACKGROUND[tm.Quality.POOR],
    ]
    assert "normalised" in model.headerData(model.columnCount() - 2, Qt.Orientation.Horizontal)
    assert "energy" in tab.tallies.csv_text().splitlines()[0]
    qtbot.waitUntil(lambda: "log of run1" in tab.log.toPlainText(), timeout=3000)


def test_plots_from_the_filters(qtbot, tab: SimulationTab):
    qtbot.waitUntil(lambda: len(tab.runs.history) == 2, timeout=3000)
    tab.select("run1", pinned=True)
    qtbot.waitUntil(lambda: tab.plots.data is not None, timeout=3000)
    plots = tab.plots
    kinds = [plots.kind.itemData(i)[0] for i in range(plots.kind.count())]
    assert kinds == ["spectrum", "convergence"]
    assert plots.stack.currentIndex() == 0 and plots.series
    _name, x, y, _std = plots.series[0]
    assert len(x) == 4 and len(y) == 3
    plots.lethargy.setChecked(True)
    assert plots.series[0][2][0] != plots.series[0][2][0], "the bin from 0 eV has no lethargy"
    plots.normalised.setChecked(True)
    scale = tm.factor(tab.sidecar.tallies[2])  # type: ignore[union-attr]
    assert plots.series[0][2][1] == pytest.approx(2.0e-7 * scale / math.log(5e5 / 0.5))
    plots.kind.setCurrentIndex(1)
    assert plots.stack.currentIndex() == 2 and plots.series[0][1].tolist() == [50, 100, 220]
    assert "batch" in plots.csv_text().lower() or plots.csv_text().startswith("series")
    plots.tally.setCurrentIndex(plots.tally.findData(-1))
    assert plots.series[0][2].tolist()[-1] == pytest.approx(1.0405)


def test_history_compare_and_delete(qtbot, tab: SimulationTab, monkeypatch):
    qtbot.waitUntil(lambda: len(tab.runs.history) == 2, timeout=3000)
    history = tab.history
    assert history.table.rowCount() == 2
    with qtbot.waitSignal(history.compareRequested):
        history.table.selectAll()
        history.compare_button.click()
    qtbot.waitUntil(lambda: history.compare_table.rowCount() == 2, timeout=3000)
    assert history.quantity.itemData(0) == ("keff", 0, 0)
    first, second = history.compare_table.item(0, 2).text(), history.compare_table.item(1, 2).text()
    assert (first, second) == ("1.0405", "1.041")
    drho = history.compare_table.item(1, 5).text()
    assert drho.startswith("+46"), drho  # (1/1.0405 - 1/1.041) in pcm
    assert "Δρ vs first [$]" in history.csv_text().splitlines()[0]
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    tab.delete_run("run1")
    qtbot.waitUntil(lambda: api().deleted == ["run1"], timeout=3000)
    qtbot.waitUntil(lambda: len(tab.runs.history) == 1, timeout=3000)


def test_normalisation_is_saved_in_the_sidecar(qtbot, tab: SimulationTab):
    qtbot.waitUntil(lambda: len(tab.runs.history) == 2, timeout=3000)
    tab.select("run1", pinned=True)
    qtbot.waitUntil(lambda: tab.tallies.current_tally() is not None, timeout=3000)
    panel = tab.tallies.normalisation
    assert panel.source.text() == "1e+15" and panel.divide.currentData() == "area"
    panel.multiplier.setText("2")
    panel.save.click()
    sidecar = tab.session.info.sidecar
    assert sidecar is not None and sidecar.tallies[2].multiplier == 2.0
    assert sidecar.tallies[2].divisor == pytest.approx(1.25e5)
    panel.divisor.setText("2*pi*r*h")
    assert panel.value().divisor == "2*pi*r*h"
    panel.divisor.setText("2*pi*r*")
    with pytest.raises(ValueError):
        panel.value()
    panel.remove.click()
    assert 2 not in tab.session.info.sidecar.tallies  # type: ignore[union-attr]


def test_a_run_of_another_model_is_followed_but_not_editable(qtbot, tab: SimulationTab):
    other = run_info("x9", "running", k=None).model_copy(update={"model": "tr/BME.py"})
    tab.runs.events.received.emit(
        Event(type="run.state", run_id="x9", data=other.model_dump(mode="json"))
    )
    assert "(another model)" in tab.form.state.text()
    assert not tab.form.run_button.isEnabled()


def test_plots_export_as_png_and_svg(qtbot, tab: SimulationTab, tmp_path: Path):
    qtbot.waitUntil(lambda: len(tab.runs.history) == 2, timeout=3000)
    tab.select("run1", pinned=True)
    qtbot.waitUntil(lambda: tab.plots.data is not None, timeout=3000)
    tab.plots.save_image(tmp_path / "spectrum.png")
    tab.plots.save_image(tmp_path / "spectrum.svg")
    assert (tmp_path / "spectrum.png").stat().st_size > 1000
    assert "<svg" in (tmp_path / "spectrum.svg").read_text(encoding="utf-8")[:2000]
    rows = tab.plots.csv_text().splitlines()
    assert rows[0] == "series,x,value,std. dev." and len(rows) == 4


def test_mesh_plots_take_the_other_axes_at_the_chosen_bins(qtbot):
    mesh = TallyFilterResult(
        kind="mesh",
        bins=24,
        mesh=MeshInfo(
            id=1,
            kind="regular",
            dimension=[4, 3, 2],
            grids=[[0, 1, 2, 3, 4], [0, 1, 2, 3], [0, 1, 2]],
        ),
    )
    tally = TallyResult(
        id=7, name="flux", scores=["flux"], nuclides=["total"], filters=[mesh], shape=[24, 1, 1]
    )
    results = results_for("run1").model_copy(update={"tallies": [tally], "convergence": {}})
    plots = TallyPlots()
    qtbot.addWidget(plots)
    plots.set_results(run_info("run1"), results, None)
    values = np.arange(24.0).reshape(tally.shape)  # bin i + 4 j + 12 k
    plots.set_data(TallyData(tally=tally, mean=values, std=values / 10))
    assert plots.plot_kind() == (tm.PlotKind.MESH_2D, 0)
    x, y, z = plots.position_boxes
    assert plots.axes.currentData() == (0, 1)
    assert not x.isVisibleTo(plots) and not y.isVisibleTo(plots) and z.isVisibleTo(plots)
    assert plots.grid is not None and plots.grid[5][:, 0].tolist() == [0, 1, 2, 3]
    z.setValue(2)
    rows = plots.csv_text().splitlines()
    assert rows[0] == "series,x [cm],y [cm],value,std. dev." and len(rows) == 13
    assert rows[1] == "flux,0.5,0.5,12.0,1.2" and rows[2].startswith("flux,1.5,0.5,13")
    kinds = [plots.kind.itemData(i)[0] for i in range(plots.kind.count())]
    plots.kind.setCurrentIndex(kinds.index(tm.PlotKind.MESH_1D.value))
    assert x.isVisibleTo(plots) is False and y.isVisibleTo(plots) and z.isVisibleTo(plots)
    assert (y.value(), z.value()) == (2, 1), "the middle bins"
    assert plots.series[0][2].tolist() == [4, 5, 6, 7]


def test_compare_with_cached_results_and_a_run_without(qtbot, tab: SimulationTab):
    qtbot.waitUntil(lambda: len(tab.runs.history) == 2, timeout=3000)
    for run_id in ("run1", "run2"):
        tab.runs.results(run_id, lambda results: None)
    qtbot.waitUntil(lambda: tab.runs.cached_results("run1") is not None, timeout=3000)
    qtbot.waitUntil(lambda: tab.runs.cached_results("run2") is not None, timeout=3000)
    api().listed.append(run_info("run0", "failed", k=None))
    with qtbot.waitSignal(tab.runs.historyChanged, timeout=3000):
        tab.runs.refresh_history()
    tab.compare(["run2", "run1", "run0"])
    assert tab.history.compare_table.rowCount() == 2, "run0 has no results"


def test_a_history_refresh_keeps_the_selected_rows(qtbot, tab: SimulationTab):
    qtbot.waitUntil(lambda: len(tab.runs.history) == 2, timeout=3000)
    tab.history.table.selectAll()
    assert tab.history.selected_ids() == ["run2", "run1"]
    with qtbot.waitSignal(tab.runs.historyChanged, timeout=3000):
        tab.runs.refresh_history()
    assert tab.history.selected_ids() == ["run2", "run1"]
    assert tab.history.compare_button.isEnabled()


def test_appended_log_text_leaves_the_view_where_it_is(qtbot, tab: SimulationTab):
    run = run_info("run3", "running", k=None)
    api().listed.insert(0, run)
    tab.result_tabs.setCurrentWidget(tab.log)
    tab.runs.events.received.emit(
        Event(type="run.state", run_id="run3", data=run.model_dump(mode="json"))
    )
    qtbot.waitUntil(lambda: tab.selected == "run3", timeout=3000)
    qtbot.waitUntil(lambda: "log of run3" in tab.log.toPlainText(), timeout=3000)
    offset = len(b"log of run3\n")

    def append(text: str) -> None:
        nonlocal offset
        tab.runs.events.received.emit(
            Event(type="run.log", run_id="run3", data={"text": text, "offset": offset})
        )
        offset += len(text.encode())

    append("".join(f"line {i}\n" for i in range(400)))
    bar = tab.log.verticalScrollBar()
    assert bar.maximum() > 50 and bar.value() == bar.maximum(), "at the end it follows"
    bar.setValue(20)
    append("".join(f"more {i}\n" for i in range(50)))
    assert bar.value() == 20
    assert tab.log.toPlainText().endswith("more 49\n")


def test_preferences_update_the_run_form(tab: SimulationTab, settings):
    settings.run_threads = 4
    settings.run_entropy_mesh = False
    tab.form.apply_settings()
    assert tab.form.threads.value() == 4 and not tab.form.entropy.isChecked()


def test_another_model_shows_its_own_runs(qtbot, tab: SimulationTab):
    qtbot.waitUntil(lambda: tab.selected == "run2", timeout=3000)
    api().listed = [run_info("tr1").model_copy(update={"model": "tr/BME.py"})]
    tab.show_model(sim_model_info().model_copy(update={"source": "tr/BME.py"}))
    qtbot.waitUntil(lambda: tab.selected == "tr1", timeout=3000)
    qtbot.waitUntil(lambda: "log of tr1" in tab.log.toPlainText(), timeout=3000)
    assert "run2" not in tab.log.toPlainText()


def test_a_history_refresh_keeps_the_result_views(qtbot, tab: SimulationTab):
    qtbot.waitUntil(lambda: len(tab.runs.history) == 2, timeout=3000)
    tab.select("run1", pinned=True)
    qtbot.waitUntil(lambda: tab.plots.data is not None, timeout=3000)
    tab.plots.kind.setCurrentIndex(1)  # convergence
    tab.tallies.normalisation.multiplier.setText("7")  # not saved yet
    with qtbot.waitSignal(tab.runs.historyChanged, timeout=3000):
        tab.runs.refresh_history()
    assert tab.plots.kind.currentIndex() == 1 and tab.plots.data is not None
    assert tab.tallies.normalisation.multiplier.text() == "7"
