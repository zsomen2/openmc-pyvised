from pathlib import Path

import pytest
from PySide6.QtWidgets import QTabWidget

from pyvised.backend.manager import BackendConfig, BackendManager
from pyvised.core.tasks import TaskRunner
from pyvised.ui.main_window import MainWindow


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
    window.backend.stateChanged.emit("docker_missing", "")
    assert window.status_pill.text() == "Docker Desktop is not installed"
    assert not window.act_selftest.isEnabled()


def test_open_is_disabled_until_the_geometry_viewer_exists(window: MainWindow):
    assert not window.act_open.isEnabled()
