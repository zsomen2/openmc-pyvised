"""The particle tracks window with the geometry tab: generate, draw, filter, play, follow."""

import numpy as np
import pytest
from fakes import FakeSession, model_info
from PySide6.QtWidgets import QMenu
from track_fakes import FakeTrackSession, sample_tracks

from pyvised.backend.client import BackendError
from pyvised.core.settings import PlotBackground
from pyvised.core.track_session import TrackSession
from pyvised.render.tracks import MarkerGroup, Timeline, TrackColor
from pyvised.ui.geometry.geometry_tab import GeometryTab
from pyvised.ui.tracks.player import TrackPlayer
from pyvised.ui.tracks.tracks_window import TracksWindow, parse_number
from pyvised_protocol.tracks import TrackRequest


@pytest.fixture
def tab(qtbot, settings):
    settings.plot_background = PlotBackground.WHITE
    widget = GeometryTab(FakeSession(), settings)  # type: ignore[arg-type]
    qtbot.addWidget(widget)
    widget.resize(1400, 800)
    widget.show()
    qtbot.waitExposed(widget)
    with qtbot.waitSignal(widget.allSharp, timeout=5000):
        widget.show_model(model_info(), False)
    return widget


@pytest.fixture
def session() -> FakeTrackSession:
    return FakeTrackSession()


@pytest.fixture
def window(qtbot, tab: GeometryTab, session: FakeTrackSession, settings) -> TracksWindow:
    widget = TracksWindow(
        tab.tracks,
        session,  # type: ignore[arg-type]
        settings,
        lambda: tab.inspected_point,
    )
    qtbot.addWidget(widget)
    widget.set_model(tab.info)
    widget.show()
    return widget


def generate(qtbot, window: TracksWindow) -> None:
    with qtbot.waitSignal(window.session.generated, timeout=3000):
        window.generate()
    qtbot.waitUntil(lambda: not window.overlay._scheduled, timeout=3000)


def test_generate_sends_the_request_and_draws_on_both_views(
    qtbot, window: TracksWindow, session, tab: GeometryTab
):
    assert not window.tabs.isEnabled()  # nothing to display yet
    window.histories.setValue(250)
    window.seed.setValue(17)
    generate(qtbot, window)
    model_id, request = session.requests[-1]
    assert model_id == "fake1" and request.histories == 250 and request.seed == 17
    assert request.start == "model" and request.weight_windows
    assert window.settings.tracks_histories == 250
    assert tab.tracks.scene is not None and window.tabs.isEnabled()
    assert all(item.has_content for item in tab.tracks.items)
    assert "split copies 0" in window.status.text() and "leaked 1" in window.status.text()
    assert "fission" in window.status.text()  # the eigenvalue note
    assert window.last.value() == 2 and window.first.value() == 1
    assert not window.particles[22].isHidden() and window.particles[11].isHidden()


def test_point_source_request_uses_the_inspected_point(qtbot, window: TracksWindow, tab):
    window.start.setCurrentIndex(2)
    assert window.point_box.isVisibleTo(window)
    tab.inspected_point = (1.5, -2.0, 3.0)
    window.use_inspected.click()
    window.point_particle.setCurrentIndex(1)
    window.point_energy.setText("6,6e5")
    request = window.request()
    assert request.start == "point" and request.point.particle == "photon"
    assert request.point.position == (1.5, -2.0, 3.0) and request.point.energy == 6.6e5
    assert request.seed is None  # 0 = random


def test_fission_start_only_for_eigenvalue_models(window: TracksWindow):
    info = model_info(model_id="fixed")
    info.settings.run_mode = "fixed source"
    window.start.setCurrentIndex(1)
    window.set_model(info)
    item = window.start.model().item(1)  # type: ignore[attr-defined]
    assert not item.isEnabled() and window.start.currentData() == "model"
    assert not window.weight_windows.isEnabled()  # the model has its weight windows off


def test_particles_only_or_with_lines_and_colours(qtbot, window: TracksWindow, tab):
    generate(qtbot, window)
    overlay = tab.tracks
    assert not overlay.style.lines and overlay.frames[0] is not None
    assert overlay.frames[0].lines == []  # particles only by default
    window.draw.setCurrentIndex(1)
    assert overlay.style.lines and not window.line_rgb.isEnabled()
    window.line_color.setCurrentIndex(window.line_color.count() - 1)  # one colour
    assert overlay.style.line_color is TrackColor.FIXED and window.line_rgb.isEnabled()
    window.line_rgb.set_rgb((10, 20, 30))
    window.line_rgb.colorChanged.emit((10, 20, 30))
    qtbot.waitUntil(lambda: not overlay._scheduled)
    frame = overlay.frames[1]
    assert frame is not None and [rgb for rgb, _ in frame.lines] == [(10, 20, 30)]
    window.particle_color.setCurrentIndex(2)  # by event
    assert overlay.style.particle_color is TrackColor.EVENT
    assert "color:" in window.legend.text()
    window.markers[MarkerGroup.CROSSINGS].setChecked(True)
    assert MarkerGroup.CROSSINGS in overlay.style.markers
    saved = window.settings.tracks_style
    assert saved["lines"] == "1" and saved["line_rgb"] == "#0a141e"
    assert "crossings" in saved["markers"] and saved["particle_color"] == "event"


def test_slab_and_filters(qtbot, window: TracksWindow, tab):
    generate(qtbot, window)
    overlay = tab.tracks
    window.near_cut.setChecked(True)
    window.slab.setValue(0.5)
    assert overlay.style.slab == 0.5
    window.particles[22].setChecked(False)
    assert overlay.filter.particles == frozenset({2112})
    window.energy_min.setText("1e6")
    window.energy_min.editingFinished.emit()
    assert overlay.filter.energy_min == 1e6
    window.first.setValue(2)
    assert overlay.filter.first == 2


def test_playback_controls(qtbot, window: TracksWindow, tab):
    generate(qtbot, window)
    player = tab.player
    assert player.at_end and window.position_label.text() == "all"
    window.to_start_button.click()
    assert player.position == 0.0 and window.slider.value() == 0
    window.forward_button.click()
    assert player.position == 1.0 and "step 1.0 / 3" in window.position_label.text()
    window.back_button.click()
    assert player.position == 0.0
    window.play_button.click()
    assert player.playing
    qtbot.waitUntil(lambda: not player.playing, timeout=5000)  # 3 steps at 20 steps/s
    assert player.at_end
    window.time_timeline.setChecked(True)
    assert player.timeline is Timeline.TIME and window.settings.tracks_style["timeline"] == "time"
    assert "decades/s" in window.speed.currentText()
    window.slider.setValue(500)
    assert window.position_label.text().startswith("t = ")


def test_follow_a_history_and_its_event_table(qtbot, window: TracksWindow, tab):
    generate(qtbot, window)
    moved: list = []
    window.moveCutsRequested.connect(moved.append)
    window.follow_history(1)
    assert tab.tracks.filter.history == 1
    assert window.table_model.rowCount() == 6  # the neutron and its photon
    index = window.table_model.index(4, 3)
    assert window.table_model.data(index) == "secondary particle"
    assert window.table_model.data(window.table_model.index(4, 1)) == "2"  # second track
    window.table.selectRow(2)  # the collision at step 2
    assert tab.tracks.mark == 2 and tab.player.position == 2.0
    window.table.doubleClicked.emit(window.table_model.index(2, 0))
    assert moved == [(2.0, 2.0, 0.0)]
    window.follow.setChecked(False)
    assert window.table_model.rowCount() == 0 and tab.tracks.filter.history is None


def test_hover_and_menu_on_a_track(qtbot, window: TracksWindow, tab):
    generate(qtbot, window)
    texts: list[str] = []
    tab.cursorText.connect(texts.append)
    top = tab.panels[1]  # XY at z = 0
    top.view.hovered.emit((6.0, 2.0))
    assert "history 1" in texts[-1] and "photon" in texts[-1] and "leaked" in texts[-1]
    top.view.hovered.emit((-8.0, -8.0))
    assert "history" not in texts[-1]
    menu = QMenu()
    tab.tracks.add_menu_actions(1, menu, (6.0, 2.0))
    follow = [a for a in menu.actions() if a.text().startswith("Follow history")]
    assert len(follow) == 1
    follow[0].trigger()
    assert window.follow.isChecked() and window.follow_number.value() == 1


def test_geometry_fades_under_the_tracks(qtbot, window: TracksWindow, tab):
    panel = tab.panels[1]
    before = panel.rgba.copy()
    generate(qtbot, window)
    assert panel.render_style.fade > 0
    assert not np.array_equal(panel.rgba, before)
    window.fade.setChecked(False)
    assert panel.render_style.fade == 0 and np.array_equal(panel.rgba, before)
    assert not window.settings.tracks_fade
    window.fade.setChecked(True)
    window.show_tracks.setChecked(False)  # hidden tracks: no fading either
    assert panel.render_style.fade == 0
    qtbot.waitUntil(lambda: not tab.tracks._scheduled)
    assert not any(item.has_content for item in tab.tracks.items)


def test_a_changed_model_clears_the_tracks(qtbot, window: TracksWindow, tab):
    generate(qtbot, window)
    window.set_model(model_info())  # the same model id: the tracks stay
    assert tab.tracks.scene is not None
    window.set_model(model_info(model_id="other"))
    assert tab.tracks.scene is None and "again" in window.status.text()
    assert not window.tabs.isEnabled()


def test_track_item_paints(qtbot, window: TracksWindow, tab):
    generate(qtbot, window)
    window.draw.setCurrentIndex(1)
    window.follow_history(1)
    window.table.selectRow(0)
    window.to_start_button.click()
    window.forward_button.click()  # a particle in flight
    qtbot.waitUntil(lambda: not tab.tracks._scheduled)
    image = tab.panels[1].view.grab().toImage()
    assert not image.isNull()


def test_parse_number():
    assert parse_number(" 2,5e6 ") == 2.5e6 and parse_number("") is None
    assert parse_number("x") is None and parse_number("inf") is None


# -- player and session ----------------------------------------------------------------------


def test_player_steps_and_ends(qtbot):
    player = TrackPlayer()
    player.set_timeline(Timeline.EVENTS, 0.0, 10.0)
    assert player.at_end and player.fraction == 1.0
    player.to_start()
    player.step(1)
    player.step(1)
    assert player.position == 2.0
    player.seek(2.4)
    player.step(1)
    assert player.position == 3.0
    player.seek(2.4)
    player.step(-1)
    assert player.position == 2.0
    player.set_speed(100.0)
    player.loop = True
    player.play()
    qtbot.wait(200)
    assert player.playing  # loops instead of stopping
    player.pause()
    assert not player.playing


class TrackApi:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.cancelled: list[str] = []

    def tracks(self, model_id, request):
        if self.error is not None:
            raise self.error
        return sample_tracks(model_id)

    def cancel_tracks(self, model_id):
        self.cancelled.append(model_id)

    def close(self):
        pass


def test_track_session_delivers_and_reports(qtbot):
    from pyvised.core.tasks import TaskRunner

    tasks = TaskRunner()
    session = TrackSession(tasks)
    session.api = TrackApi()  # type: ignore[assignment]
    with qtbot.waitSignal(session.generated, timeout=3000) as blocker:
        assert session.generate("m1", TrackRequest())
    assert blocker.args[0].summary.model_id == "m1" and not session.busy
    session.api = TrackApi(BackendError("tracks_failed: x", "tracks_failed", "ERROR: boom"))  # type: ignore[assignment]
    with qtbot.waitSignal(session.failed, timeout=3000) as blocker:
        session.generate("m1", TrackRequest())
    assert blocker.args == ["OpenMC could not track the particles.", "ERROR: boom"]
    cancelled = TrackApi(BackendError("cancelled", "cancelled", ""))
    session.api = cancelled  # type: ignore[assignment]
    failed: list = []
    session.failed.connect(lambda *args: failed.append(args))
    session.generate("m1", TrackRequest())
    session.cancel()
    qtbot.waitUntil(lambda: cancelled.cancelled == ["m1"], timeout=3000)
    qtbot.wait(100)
    assert failed == [] and not session.busy
    tasks.shutdown()


def test_main_window_opens_the_tracks_window(qtbot, settings, tmp_path):
    from pyvised.backend.manager import BackendConfig, BackendManager
    from pyvised.core.tasks import TaskRunner
    from pyvised.ui.main_window import MainWindow

    config = BackendConfig(image="x", workspace=tmp_path, container_name="pyvised-test")
    tasks = TaskRunner()
    win = MainWindow(settings, BackendManager(config), tasks)
    qtbot.addWidget(win)
    assert not win.act_tracks.isEnabled()  # no backend, no model
    win.show_tracks()
    assert win.tracks_window is not None and win.tracks_window.isVisible()
    assert not win.tracks_window.generate_button.isEnabled()
    win.close()
    assert settings.tracks_window is not None
    tasks.shutdown()


def test_view_panels_get_the_menu_entry(qtbot, window: TracksWindow, tab):
    generate(qtbot, window)
    panel = tab.panels[1]
    assert panel.menu_extra is not None
    menu = QMenu()
    panel.menu_extra(menu, (6.0, 2.0))
    assert "Follow history 1" in [a.text() for a in menu.actions()]
