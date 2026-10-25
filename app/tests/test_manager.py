from pathlib import Path

import pytest

from pyvised.backend import manager as manager_module
from pyvised.backend.docker_cli import ContainerInfo
from pyvised.backend.manager import (
    BackendConfig,
    BackendManager,
    BackendState,
    is_pullable,
    resolve_image,
)
from pyvised_protocol import PROTOCOL_VERSION, TOKEN_ENV
from pyvised_protocol.schemas import HealthInfo


class FakeDocker:
    def __init__(self, *, engine=True, image_present=True, pullable_after=True):
        self.engine = engine
        self.image_present = image_present
        self.pullable_after = pullable_after
        self.containers: dict[str, ContainerInfo] = {}
        self.run_args: list[str] | None = None
        self.pulled: list[str] = []
        self.removed: list[str] = []

    def server_version(self):
        return "29.0" if self.engine else None

    def image_id(self, ref):
        return "sha256:img" if self.image_present else None

    def pull(self, ref, on_line=None):
        self.pulled.append(ref)
        if on_line:
            on_line("Pull complete")
        self.image_present = self.pullable_after

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
    image="ghcr.io/x/pyvised-backend:0.1.0",
    probe=lambda url: health(),
):
    config = BackendConfig(
        image=image, workspace=tmp_path / "ws", container_name="pyvised-backend-t"
    )
    mgr = BackendManager(config, docker_factory=lambda exe: docker, health_probe=probe)
    states: list[str] = []
    mgr.stateChanged.connect(lambda state, detail: states.append(state))
    return mgr, states


@pytest.fixture(autouse=True)
def docker_found(monkeypatch):
    monkeypatch.setattr(manager_module, "find_docker", lambda: "docker.exe")
    monkeypatch.setattr(manager_module, "find_docker_desktop", lambda: None)


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
    mgr._start_sequence()
    assert docker.pulled == ["ghcr.io/x/pyvised-backend:0.1.0"]
    assert "pulling" in states and mgr.state is BackendState.READY


def test_local_dev_image_is_never_pulled(tmp_path):
    docker = FakeDocker(image_present=False)
    mgr, _ = make(tmp_path, docker, image="pyvised-backend:dev")
    mgr._start_sequence()
    assert mgr.state is BackendState.IMAGE_MISSING
    assert docker.pulled == []


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
