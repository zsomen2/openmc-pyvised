from pathlib import Path

import pytest

from pyvised import __version__
from pyvised.backend.client import Connection
from pyvised.backend.manager import (
    IMAGE_SPACE_NEEDED,
    BackendConfig,
    BackendManager,
    BackendState,
    DockerFacts,
    Step,
    Transfer,
)
from pyvised.ui.dialogs.setup_dialog import SetupDialog, Status, format_bytes, short_path
from pyvised_protocol import PROTOCOL_VERSION
from pyvised_protocol.schemas import HealthInfo


@pytest.fixture
def backend(tmp_path: Path) -> BackendManager:
    config = BackendConfig(
        image="ghcr.io/x/pyvised-backend:1.0.0", workspace=tmp_path, container_name="c"
    )
    return BackendManager(config)


@pytest.fixture
def dialog(qtbot, backend: BackendManager) -> SetupDialog:
    widget = SetupDialog(backend)
    qtbot.addWidget(widget)
    return widget


def put(backend: BackendManager, state: BackendState, detail: str = "", step=None) -> None:
    """What `BackendManager._set` does, without a worker thread."""
    backend.state = state
    backend.detail = detail
    backend.step = step or {
        BackendState.DOCKER_MISSING: Step.DOCKER,
        BackendState.DOCKER_UNAVAILABLE: Step.ENGINE,
        BackendState.IMAGE_MISSING: Step.IMAGE,
        BackendState.PULLING: Step.IMAGE,
        BackendState.READY: Step.BACKEND,
    }.get(state, backend.step)
    backend.stateChanged.emit(state.value, detail)


def facts(tmp_path: Path, free: int = 400 * 10**9) -> DockerFacts:
    return DockerFacts(
        docker="docker.exe",
        desktop=tmp_path / "Docker Desktop.exe",
        desktop_version="4.96.1",
        engine="29.9.2",
        data_dir=tmp_path,
        free_bytes=free,
        image_id="sha256:img",
    )


def statuses(dialog: SetupDialog) -> dict[str, Status]:
    return {str(key): row.status for key, row in dialog.rows.items()}


def test_short_path():
    assert short_path(Path(r"C:\Users\student\PyVised")) == r"C:\Users\student\PyVised"
    deep = Path(r"C:\Users\someone\AppData\Local\Temp\a-very-long-folder-name\shots\installed\ws")
    assert short_path(deep) == r"C:\Users\…\installed\ws"


def test_format_bytes():
    assert format_bytes(946_000_000) == "946 MB"
    assert format_bytes(3.4e9) == "3.4 GB"
    assert format_bytes(412e9) == "412 GB"
    assert format_bytes(512) == "512 B"


def test_docker_missing_offers_the_download_page(dialog: SetupDialog, backend):
    put(backend, BackendState.DOCKER_MISSING)
    row = dialog.rows[Step.DOCKER]
    assert row.status is Status.FAILED
    assert "Docker Desktop" in row.help.text()
    assert not dialog.get_docker.isHidden()
    assert statuses(dialog)["image"] is Status.PENDING


def test_a_missing_image_offers_download_and_file(dialog: SetupDialog, backend, tmp_path):
    backend.facts = facts(tmp_path)
    put(backend, BackendState.IMAGE_MISSING, backend.config.image)
    assert statuses(dialog) == {
        "docker": Status.OK,
        "engine": Status.OK,
        "disk": Status.OK,
        "image": Status.FAILED,
        "backend": Status.PENDING,
        "selftest": Status.PENDING,
    }
    assert not dialog.download.isHidden() and not dialog.load_file.isHidden()
    assert f"pyvised-backend-{__version__}.tar.gz" in dialog.rows[Step.IMAGE].help.text()
    backend.config = BackendConfig(
        image="pyvised-backend:dev", workspace=tmp_path, container_name="c"
    )
    dialog.refresh()
    assert dialog.download.isHidden() and not dialog.load_file.isHidden()
    assert "just backend-build" in dialog.rows[Step.IMAGE].help.text()


def test_download_progress(dialog: SetupDialog, backend, tmp_path):
    backend.facts = facts(tmp_path)
    put(backend, BackendState.PULLING, backend.config.image)
    row = dialog.rows[Step.IMAGE]
    assert row.status is Status.RUNNING and not dialog.cancel.isHidden()
    backend.transfer = Transfer("pull", done=412_000_000, total=946_000_000, rate=12e6)
    backend.transferChanged.emit(backend.transfer)
    assert not row.progress.isHidden() and row.progress.value() == 436
    assert row.progress_text.text() == "412 MB of 946 MB · 12 MB/s · about 45 s left"
    backend.transferChanged.emit(Transfer("pull", done=946, total=946, layers=3, unpacking=True))
    assert row.progress.maximum() == 0, "busy bar while unpacking"
    assert row.progress_text.text() == "Unpacking the image…"
    backend.transferChanged.emit(Transfer("pull", layers_done=2, layers=5))
    assert row.progress_text.text() == "2 of 5 layers", "plain docker pull: no byte counts"
    backend.transferChanged.emit(Transfer("pull", done=5_000_000, total=6_000_000, sized=False))
    assert row.progress_text.text() == "5.0 MB so far" and row.progress.maximum() == 0
    put(backend, BackendState.STARTING, step=Step.BACKEND)
    assert row.status is Status.OK and row.progress.isHidden()


def test_low_disk_space_is_a_warning(dialog: SetupDialog, backend, tmp_path):
    backend.facts = facts(tmp_path, free=IMAGE_SPACE_NEEDED // 2)
    put(backend, BackendState.IMAGE_MISSING)
    assert dialog.rows["disk"].status is Status.WARNING


def test_ready_and_selftest(dialog: SetupDialog, backend, tmp_path):
    backend.facts = facts(tmp_path)
    backend.connection = Connection("http://127.0.0.1:55001", "t")
    backend.health = HealthInfo(
        protocol_version=PROTOCOL_VERSION,
        backend_version="1.0.0",
        openmc_version="0.16.0",
        python_version="3.13",
        cpu_count=16,
    )
    put(backend, BackendState.READY, "0.16.0")
    assert dialog.rows[Step.BACKEND].result.text() == "OpenMC 0.16.0 · 16 CPUs"
    assert "http://127.0.0.1:55001" in dialog.rows[Step.BACKEND].help.text()
    assert dialog.rows["selftest"].status is Status.PENDING and dialog.summary.text() == ""
    dialog.set_selftest(Status.OK, "passed", "build 1.0 s")
    assert dialog.summary.text() == "Everything is ready."
    assert not dialog.rerun.isHidden() and not dialog.remove.isHidden()


def test_an_error_marks_the_step_that_failed(dialog: SetupDialog, backend, tmp_path):
    backend.facts = facts(tmp_path)
    put(backend, BackendState.ERROR, "'docker pull' failed\ndenied", step=Step.IMAGE)
    row = dialog.rows[Step.IMAGE]
    assert row.status is Status.FAILED and "denied" in row.help.text()
    assert row.help.text().startswith("The registry does not offer this image")
    assert not dialog.download.isHidden(), "retry the download"
    assert dialog.rows[Step.ENGINE].status is Status.OK
