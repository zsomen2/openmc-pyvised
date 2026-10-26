from pathlib import Path

import pytest

from pyvised.backend import manager as manager_module
from pyvised.backend.docker_cli import Cancelled, ContainerInfo, PullProgress
from pyvised.backend.manager import (
    BackendConfig,
    BackendManager,
    BackendState,
    Step,
    Transfer,
    is_pullable,
    resolve_image,
)
from pyvised_protocol import PROTOCOL_VERSION, TOKEN_ENV
from pyvised_protocol.schemas import HealthInfo

IMAGE = "ghcr.io/x/pyvised-backend:0.1.0"


class FakeDocker:
    executable = "docker.exe"

    def __init__(self, *, engine=True, image_present=True, pullable_after=True, cancel_pull=False):
        self.engine = engine
        self.image_present = image_present
        self.pullable_after = pullable_after
        self.cancel_pull = cancel_pull
        self.containers: dict[str, ContainerInfo] = {}
        self.run_args: list[str] | None = None
        self.pulled: list[str] = []
        self.removed: list[str] = []
        self.removed_images: list[str] = []
        self.loaded: list[Path] = []
        self.load_holds = [IMAGE]
        self.tags = [IMAGE, "ghcr.io/x/pyvised-backend:0.0.9"]

    def server_version(self):
        return "29.0" if self.engine else None

    def image_id(self, ref):
        return "sha256:img" if self.image_present else None

    def pull(self, ref, on_progress=None, cancel=None):
        self.pulled.append(ref)
        if on_progress:
            on_progress(PullProgress(downloaded=40, total=100, layers_done=0, layers=2))
            on_progress(PullProgress(downloaded=100, total=100, layers_done=2, layers=2))
        if self.cancel_pull:
            raise Cancelled
        self.image_present = self.pullable_after

    def load(self, path, on_progress=None, cancel=None):
        self.loaded.append(path)
        if on_progress:
            on_progress(10, 10)
        self.image_present = IMAGE in self.load_holds
        return list(self.load_holds)

    def image_tags(self, repository):
        return [tag for tag in self.tags if tag.startswith(f"{repository}:")]

    def remove_image(self, ref):
        self.removed_images.append(ref)
        self.image_present = self.image_present and ref != IMAGE
        return True

    def container(self, name):
        return self.containers.get(name)

    def run(self, args):
        self.run_args = list(args)
        name = args[args.index("--name") + 1]
        env = dict(a.split("=", 1) for a in args if a.startswith(f"{TOKEN_ENV}="))
        labels = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "--label")
        self.containers[name] = ContainerInfo(name, True, "sha256:img", labels, env)
        return "cid"

    def host_port(self, name, port):
        return 55001

    def remove(self, name):
        self.removed.append(name)
        self.containers.pop(name, None)

    def stop(self, name, timeout=5):
        self.containers.pop(name, None)

    def logs(self, name, tail=300):
        return "log"


def health(version=PROTOCOL_VERSION) -> HealthInfo:
    return HealthInfo(
        protocol_version=version,
        backend_version="0.1.0",
        openmc_version="0.16.0",
        python_version="3.13",
        cpu_count=4,
    )


def make(
    tmp_path: Path,
    docker: FakeDocker,
    image=IMAGE,
    probe=lambda url: health(),
    remove_old_images=True,
):
    config = BackendConfig(
        image=image,
        workspace=tmp_path / "ws",
        container_name="pyvised-backend-t",
        remove_old_images=remove_old_images,
    )
    mgr = BackendManager(config, docker_factory=lambda exe: docker, health_probe=probe)
    states: list[str] = []
    mgr.stateChanged.connect(lambda state, detail: states.append(state))
    return mgr, states


@pytest.fixture(autouse=True)
def docker_found(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_module, "find_docker", lambda: "docker.exe")
    monkeypatch.setattr(manager_module, "find_docker_desktop", lambda: None)
    monkeypatch.setattr(manager_module, "docker_desktop_version", lambda: "4.96.1")
    monkeypatch.setattr(manager_module, "docker_data_dir", lambda: tmp_path)


def test_happy_path_starts_container_and_reaches_ready(tmp_path):
    docker = FakeDocker()
    mgr, states = make(tmp_path, docker)
    mgr._start_sequence()
    assert mgr.state is BackendState.READY
    assert states == ["checking", "starting", "ready"]
    assert mgr.connection is not None and mgr.connection.base_url == "http://127.0.0.1:55001"
    args = docker.run_args
    assert args[args.index("-p") + 1] == "127.0.0.1::8765"
    assert f"{tmp_path / 'ws'}:/workspace" in args
    assert (tmp_path / "ws").is_dir()


def test_running_matching_container_is_reused(tmp_path):
    docker = FakeDocker()
    mgr, _ = make(tmp_path, docker)
    mgr._start_sequence()
    first_token = mgr.connection.token
    docker.run_args = None
    mgr._start_sequence()
    assert docker.run_args is None, "no second docker run"
    assert mgr.connection.token == first_token


def test_changed_configuration_recreates_container(tmp_path):
    docker = FakeDocker()
    mgr, _ = make(tmp_path, docker)
    mgr._start_sequence()
    mgr.config = BackendConfig(
        image=mgr.config.image,
        workspace=tmp_path / "other",
        container_name=mgr.config.container_name,
    )
    mgr._start_sequence()
    assert docker.removed == ["pyvised-backend-t"]
    assert f"{tmp_path / 'other'}:/workspace" in docker.run_args


def test_registry_image_is_pulled(tmp_path):
    docker = FakeDocker(image_present=False)
    mgr, states = make(tmp_path, docker)
    transfers: list[Transfer] = []
    mgr.transferChanged.connect(transfers.append)
    mgr._start_sequence()
    assert docker.pulled == [IMAGE]
    assert "pulling" in states and mgr.state is BackendState.READY
    assert transfers[0].kind == "pull" and transfers[0].fraction == pytest.approx(0.4)
    assert transfers[-1].fraction == 1.0 and transfers[-1].layers_done == 2
    assert mgr.transfer is None, "cleared once the image is there"


def test_a_cancelled_pull_leaves_the_image_missing(tmp_path):
    docker = FakeDocker(image_present=False, cancel_pull=True)
    mgr, _ = make(tmp_path, docker)
    mgr._start_sequence()
    assert mgr.state is BackendState.IMAGE_MISSING and mgr.step is Step.IMAGE
    assert docker.run_args is None


def test_a_failed_pull_is_an_error_of_the_image_step(tmp_path):
    docker = FakeDocker(image_present=False, pullable_after=False)
    mgr, _ = make(tmp_path, docker)
    mgr._guard(mgr._start_sequence)
    assert mgr.state is BackendState.ERROR and mgr.step is Step.IMAGE
    assert "still missing" in mgr.detail


def test_loading_an_image_file_starts_the_backend(tmp_path):
    docker = FakeDocker(image_present=False)
    mgr, states = make(tmp_path, docker)
    archive = tmp_path / "pyvised-backend-0.1.0.tar.gz"
    mgr._load_sequence(archive)
    assert docker.loaded == [archive] and docker.pulled == []
    assert "loading" in states and mgr.state is BackendState.READY


def test_loading_a_file_with_another_image_is_an_error(tmp_path):
    docker = FakeDocker(image_present=False)
    docker.load_holds = ["ghcr.io/x/pyvised-backend:0.0.9"]
    mgr, _ = make(tmp_path, docker)
    mgr._guard(lambda: mgr._load_sequence(tmp_path / "old.tar.gz"))
    assert mgr.state is BackendState.ERROR
    assert "holds ghcr.io/x/pyvised-backend:0.0.9, not " + IMAGE in mgr.detail


def test_old_versions_are_removed_after_a_successful_start(tmp_path):
    docker = FakeDocker()
    mgr, _ = make(tmp_path, docker)
    mgr._start_sequence()
    assert docker.removed_images == ["ghcr.io/x/pyvised-backend:0.0.9"]
    kept = FakeDocker()
    mgr, _ = make(tmp_path, kept, remove_old_images=False)
    mgr._start_sequence()
    assert kept.removed_images == []
    local = FakeDocker()
    mgr, _ = make(tmp_path, local, image="pyvised-backend:dev")
    mgr._start_sequence()
    assert local.removed_images == [], "development tags are never cleaned up"


def test_remove_image_stops_the_container_first(tmp_path):
    docker = FakeDocker()
    mgr, _ = make(tmp_path, docker)
    mgr._start_sequence()
    mgr.remove_image()
    assert mgr._worker is not None
    mgr._worker.join()
    assert docker.removed == ["pyvised-backend-t"]
    assert IMAGE in docker.removed_images
    assert mgr.state is BackendState.IMAGE_MISSING and mgr.facts.image_id is None


def test_an_operation_reports_its_end(qtbot, tmp_path):
    mgr, _ = make(tmp_path, FakeDocker())
    with qtbot.waitSignal(mgr.operationFinished, timeout=5000):
        mgr.start()
        assert mgr.busy
    assert not mgr.busy and mgr.state is BackendState.READY


def test_the_checks_collect_facts(tmp_path):
    docker = FakeDocker()
    mgr, _ = make(tmp_path, docker)
    mgr._start_sequence()
    facts = mgr.facts
    assert facts.docker == "docker.exe" and facts.engine == "29.0"
    assert facts.desktop_version == "4.96.1" and facts.data_dir == tmp_path
    assert facts.free_bytes is not None and facts.free_bytes > 0
    assert facts.image_id == "sha256:img"


def test_local_dev_image_is_never_pulled(tmp_path):
    docker = FakeDocker(image_present=False)
    mgr, _ = make(tmp_path, docker, image="pyvised-backend:dev")
    mgr._start_sequence()
    assert mgr.state is BackendState.IMAGE_MISSING
    assert docker.pulled == []


def test_transfer_rate_and_remaining_time():
    transfer = Transfer("pull", done=400, total=1000, rate=100.0)
    assert transfer.fraction == 0.4 and transfer.remaining_s == 6.0
    assert Transfer("pull", done=5).fraction is None and Transfer("pull").remaining_s is None
    emitted: list[Transfer] = []
    meter = manager_module._Meter("load", emitted.append, interval=10.0)
    meter.update(10, 100)
    meter.update(20, 100)
    assert [t.done for t in emitted] == [10], "throttled"
    meter.finish()
    assert emitted[-1].done == 20 and emitted[-1].kind == "load"


def test_docker_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_module, "find_docker", lambda: None)
    mgr, _ = make(tmp_path, FakeDocker())
    mgr._start_sequence()
    assert mgr.state is BackendState.DOCKER_MISSING


def test_engine_down_without_desktop(tmp_path):
    mgr, _ = make(tmp_path, FakeDocker(engine=False))
    mgr._start_sequence()
    assert mgr.state is BackendState.DOCKER_UNAVAILABLE


def test_protocol_mismatch_is_an_error(tmp_path):
    mgr, _ = make(tmp_path, FakeDocker(), probe=lambda url: health(version=PROTOCOL_VERSION + 1))
    mgr._start_sequence()
    assert mgr.state is BackendState.ERROR
    assert mgr.connection is None


def test_helpers(monkeypatch, settings):
    assert BackendConfig.from_settings(settings).container_name == "openmc-pyvised"
    assert is_pullable("ghcr.io/zsomen2/pyvised-backend:0.1.0")
    assert not is_pullable("pyvised-backend:dev")
    monkeypatch.setenv(manager_module.IMAGE_ENV, "pyvised-backend:dev")
    assert resolve_image("custom:1") == "pyvised-backend:dev"
    monkeypatch.delenv(manager_module.IMAGE_ENV)
    assert resolve_image("custom:1") == "custom:1"
    assert resolve_image("").startswith("ghcr.io/zsomen2/pyvised-backend:")


def test_added_libraries_are_mounted_read_only(tmp_path):
    config = BackendConfig(
        image="i",
        workspace=tmp_path / "ws",
        container_name="c",
        libraries=(("ENDF/B-VIII.0", tmp_path / "e8"), ("jeff 3.3", tmp_path / "j")),
    )
    assert config.mounts() == [
        f"{tmp_path / 'ws'}:/workspace",
        f"{tmp_path / 'e8'}:/data/custom/ENDF-B-VIII.0:ro",
        f"{tmp_path / 'j'}:/data/custom/jeff-3.3:ro",
    ]
    plain = BackendConfig(image="i", workspace=tmp_path / "ws", container_name="c")
    assert config.fingerprint("sha") != plain.fingerprint("sha"), "a new library recreates it"
    docker = FakeDocker()
    mgr, _ = make(tmp_path, docker)
    mgr.config = config
    mgr._start_sequence()
    args = docker.run_args
    assert f"{tmp_path / 'j'}:/data/custom/jeff-3.3:ro" in args
