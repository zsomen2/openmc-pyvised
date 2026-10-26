import time
from pathlib import Path

import pytest
from fakes import FakeApi
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox, QTabWidget
from sim_fakes import FakeRunApi, run_info

from pyvised.backend.client import Connection
from pyvised.backend.manager import BackendConfig, BackendManager, BackendState, Transfer
from pyvised.core import model_session, run_session
from pyvised.core.tasks import TaskRunner
from pyvised.render.colorize import ColorMode
from pyvised.ui import main_window
from pyvised.ui.dialogs.setup_dialog import Status
from pyvised.ui.main_window import MainWindow
from pyvised_protocol.runs import Event
from pyvised_protocol.schemas import SelfTestResult, SelfTestStep


@pytest.fixture
def window(qtbot, settings, tmp_path: Path):
    config = BackendConfig(
        image="pyvised-backend:dev", workspace=tmp_path, container_name="pyvised-backend-test"
    )
    backend = BackendManager(config)
    tasks = TaskRunner()
    win = MainWindow(settings, backend, tasks)
    qtbot.addWidget(win)
    yield win
    win.close()
    tasks.shutdown()


def test_tabs_and_title(window: MainWindow):
    tabs = window.centralWidget()
    assert isinstance(tabs, QTabWidget)
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["Geometry", "Simulation"]
    assert window.windowTitle() == "PyVised"


def test_status_pill_follows_backend_state(window: MainWindow):
    window.backend.stateChanged.emit("ready", "0.16.0")
    assert window.status_pill.text() == "Ready · OpenMC 0.16.0"
    assert window.act_selftest.isEnabled()
    assert window.setup_dialog is None, "nothing to set up"
    window.backend.stateChanged.emit("docker_missing", "")
    assert window.status_pill.text() == "Docker Desktop is not installed"
    assert not window.act_selftest.isEnabled()
    assert window.setup_dialog is not None and window.setup_dialog.isVisible()


def test_download_progress_shows_in_the_pill(window: MainWindow):
    window.backend.stateChanged.emit("pulling", "ghcr.io/x/b:1")
    window.backend.transferChanged.emit(Transfer("pull", done=250, total=1000))
    assert window.status_pill.text() == "Downloading the OpenMC backend… 25 %"
    window.backend.stateChanged.emit("starting", "")
    assert window.status_pill.text() == "Starting the OpenMC backend…"


class FakeSelfTestApi:
    result = SelfTestResult(
        ok=True,
        steps=[SelfTestStep(name="build", ok=True, seconds=1.0)],
        k_eff=1.39,
        k_eff_std=0.01,
    )

    def __init__(self, connection):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def selftest(self):
        return self.result


def test_a_new_image_runs_the_selftest_once(qtbot, window: MainWindow, monkeypatch):
    monkeypatch.setattr(main_window, "ApiClient", FakeSelfTestApi)
    window.backend.connection = Connection("http://127.0.0.1:1", "t")
    window.backend.facts.image_id = "sha256:new"
    window.backend.state = BackendState.READY
    window.backend.stateChanged.emit("ready", "0.16.0")
    qtbot.waitUntil(lambda: window.settings.selftest_image == "sha256:new")
    dialog = window.setup_dialog
    assert dialog is not None and not dialog.isVisible(), "a passed automatic test stays quiet"
    assert dialog.rows["selftest"].status is Status.OK
    assert "k-eff = 1.3900 ± 0.0100" in dialog.rows["selftest"].help.text()


def test_a_failed_selftest_opens_the_setup_dialog(qtbot, window: MainWindow, monkeypatch):
    failed = SelfTestResult(
        ok=False, steps=[SelfTestStep(name="render", ok=False, seconds=0.2, detail="boom")]
    )
    monkeypatch.setattr(FakeSelfTestApi, "result", failed)
    monkeypatch.setattr(main_window, "ApiClient", FakeSelfTestApi)
    window.backend.connection = Connection("http://127.0.0.1:1", "t")
    window.backend.facts.image_id = "sha256:new"
    window.backend.state = BackendState.READY
    window.backend.stateChanged.emit("ready", "0.16.0")
    qtbot.waitUntil(lambda: window.setup_dialog is not None and window.setup_dialog.isVisible())
    row = window.setup_dialog.rows["selftest"]
    assert row.status is Status.FAILED and row.help.text() == "render: boom"
    assert window.settings.selftest_image == ""


def test_open_needs_a_ready_backend(window: MainWindow):
    assert not window.act_open.isEnabled() and not window.act_reload.isEnabled()
    window.backend.stateChanged.emit("ready", "0.16.0")
    assert window.act_open.isEnabled()
    assert not window.act_reload.isEnabled(), "nothing to reload yet"


def test_a_model_requested_early_opens_when_the_backend_is_ready(
    qtbot, window: MainWindow, monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(model_session, "ApiClient", FakeApi)
    model = tmp_path / "hw" / "model.py"
    model.parent.mkdir()
    model.write_text("", encoding="utf-8")
    window.request_open(model)
    assert window.session.path is None
    window.backend.connection = Connection("http://127.0.0.1:1", "t")
    with qtbot.waitSignal(window.session.opened, timeout=5000):
        window.backend.stateChanged.emit("ready", "0.16.0")
    assert window.windowTitle() == "PyVised — Test model"
    assert window.settings.recent_models == [model]
    assert window.act_reload.isEnabled()
    assert window.geometry_tab.stack.currentIndex() == 1


def test_examples_open_as_copies_in_the_workspace(window: MainWindow, monkeypatch, tmp_path: Path):
    actions = window.examples_menu.actions()
    assert actions[0].text() == "1. Iron sphere with a point source"
    example = window.examples[0]
    copy = tmp_path / "examples" / example.folder.name / example.model.name
    actions[0].trigger()
    assert copy.is_file() and window._pending_open == copy, "opens once the backend is ready"

    copy.write_text("# changed\n", encoding="utf-8")
    choice = {"text": "Open my copy"}

    def answer(box: QMessageBox) -> int:
        next(b for b in box.buttons() if b.text() == choice["text"]).click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", answer)
    actions[0].trigger()
    assert copy.read_text(encoding="utf-8") == "# changed\n"
    choice["text"] = "Restore the original"
    actions[0].trigger()
    assert copy.read_bytes() == example.model.read_bytes()


def test_colour_mode_controls_stay_in_sync(window: MainWindow):
    window.set_color_mode(ColorMode.CELL)
    assert window.color_combo.currentData() == "cell"
    assert window.color_actions[ColorMode.CELL].isChecked()
    assert not window.color_actions[ColorMode.MATERIAL].isChecked()
    assert window.settings.color_mode == "cell"
    window.act_lines.trigger()
    assert window.settings.show_lines is False


def test_icons_follow_a_palette_change(qtbot, window: MainWindow):
    def icon_colour() -> int:
        image = window.act_open.icon().pixmap(32, 32).toImage()
        pixels = [image.pixelColor(x, y) for x in range(32) for y in range(32)]
        return max((p for p in pixels if p.alpha() > 200), key=lambda p: p.alpha()).lightness()

    original = QApplication.palette()
    dark = QPalette(original)
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        dark.setColor(group, QPalette.ColorRole.WindowText, QColor("white"))
    try:
        QApplication.setPalette(dark)  # the change events are posted
        qtbot.waitUntil(lambda: icon_colour() > 200)  # light icons on a dark palette
        QApplication.setPalette(original)
        qtbot.waitUntil(lambda: icon_colour() < 100)  # dark icons again
    finally:
        QApplication.setPalette(original)


def test_exit_stops_the_run_and_waits_for_its_results(qtbot, window: MainWindow, monkeypatch):
    FakeRunApi.instances.clear()
    monkeypatch.setattr(run_session, "ApiClient", FakeRunApi)
    monkeypatch.setattr(run_session.EventStream, "start", lambda self, connection: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Ok)
    window.runs.set_connection(Connection("http://127.0.0.1:1", "t"))
    running = run_info("run3", "running", k=None)
    window.runs.events.received.emit(
        Event(type="run.state", run_id="run3", data=running.model_dump(mode="json"))
    )
    assert window.runs.running
    stopped = run_info("run3", "stopped", stop_reason="user")
    QTimer.singleShot(
        300,
        lambda: window.runs.events.received.emit(
            Event(type="run.finished", run_id="run3", data=stopped.model_dump(mode="json"))
        ),
    )
    began = time.monotonic()
    window.close()
    assert FakeRunApi.instances[-1].stopped == ["run3"]
    assert not window.runs.running and time.monotonic() - began < 10, "not the whole timeout"
