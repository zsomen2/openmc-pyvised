"""Lifecycle of the backend container: find Docker, get the image, start, health-check, stop.

Every step runs in a worker thread; progress is reported through `stateChanged`
(queued into the GUI thread by Qt).
"""

import hashlib
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from pyvised_protocol import CONTAINER_PORT, PROTOCOL_VERSION, TOKEN_ENV
from pyvised_protocol.schemas import HealthInfo

from .. import BACKEND_IMAGE_REPOSITORY, __version__
from ..core.settings import AppSettings
from .client import Connection, probe_health
from .docker_cli import DockerCli, DockerError, find_docker, find_docker_desktop

log = logging.getLogger(__name__)

CONTAINER_NAME = "openmc-pyvised"
IMAGE_ENV = "PYVISED_BACKEND_IMAGE"
"""Overrides the image, e.g. `pyvised-backend:dev` for development (see the justfile)."""

DOCKER_START_TIMEOUT = 180.0
HEALTH_TIMEOUT = 90.0


class BackendState(StrEnum):
    IDLE = "idle"
    CHECKING = "checking"
    DOCKER_MISSING = "docker_missing"
    STARTING_DOCKER = "starting_docker"
    DOCKER_UNAVAILABLE = "docker_unavailable"
    IMAGE_MISSING = "image_missing"
    PULLING = "pulling"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass(frozen=True)
class BackendConfig:
    image: str
    workspace: Path
    container_name: str
    cpus: float = 0.0
    keep_running: bool = False

    def fingerprint(self, image_id: str) -> str:
        """Label value that tells whether a running container matches this configuration."""
        text = f"{PROTOCOL_VERSION}|{image_id}|{self.workspace}|{self.cpus:g}"
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    @classmethod
    def from_settings(cls, settings: AppSettings) -> "BackendConfig":
        return cls(
            image=resolve_image(settings.backend_image),
            workspace=settings.workspace,
            container_name=CONTAINER_NAME,
            cpus=settings.backend_cpus,
            keep_running=settings.keep_backend_running,
        )


def default_image() -> str:
    return f"{BACKEND_IMAGE_REPOSITORY}:{__version__}"


def resolve_image(configured: str) -> str:
    """Environment override, then the user's setting, then the image of this version."""
    return os.environ.get(IMAGE_ENV, "").strip() or configured.strip() or default_image()


def is_pullable(reference: str) -> bool:
    """Registry references contain a '/', local development tags do not."""
    return "/" in reference.split("@", 1)[0]


class BackendManager(QObject):
    stateChanged = Signal(str, str)  # BackendState value, detail text

    def __init__(
        self,
        config: BackendConfig,
        *,
        docker_factory: Callable[[str], DockerCli] = DockerCli,
        health_probe: Callable[[str], HealthInfo] = probe_health,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.state = BackendState.IDLE
        self.detail = ""
        self.connection: Connection | None = None
        self.health: HealthInfo | None = None
        self._docker_factory = docker_factory
        self._health_probe = health_probe
        self._docker: DockerCli | None = None
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    # -- public API (GUI thread) ----------------------------------------------

    @property
    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def start(self) -> None:
        self._spawn(self._start_sequence)

    def restart(self, config: BackendConfig | None = None) -> None:
        """Stop the current container and start again (optionally with a new configuration)."""

        def sequence() -> None:
            self._stop_container(force=True)
            if config is not None:
                self.config = config
            self._start_sequence()

        self._spawn(sequence)

    def shutdown(self) -> None:
        """Called when the app exits: stop the container unless it should keep running."""
        if self._worker is not None:
            self._worker.join(timeout=2.0)
        if not self.config.keep_running:
            self._stop_container(force=False)

    def logs(self, tail: int = 300) -> str:
        docker = self._docker_or_none()
        return docker.logs(self.config.container_name, tail) if docker else ""

    def docker_info(self) -> str:
        docker = self._docker_or_none()
        return docker.info_text() if docker else "docker not found"

    # -- worker side ---------------------------------------------------------

    def _spawn(self, target: Callable[[], None]) -> None:
        with self._lock:
            if self.busy:
                log.info("backend operation already running; request ignored")
                return
            self._worker = threading.Thread(
                target=self._guard, args=(target,), daemon=True, name="pyvised-backend"
            )
            self._worker.start()

    def _guard(self, target: Callable[[], None]) -> None:
        try:
            target()
        except DockerError as exc:
            log.error("docker: %s %s", exc, exc.output)
            self._set(BackendState.ERROR, f"{exc}\n{exc.output}".strip())
        except Exception as exc:
            log.exception("backend start failed")
            self._set(BackendState.ERROR, f"{type(exc).__name__}: {exc}")

    def _set(self, state: BackendState, detail: str = "") -> None:
        self.state = state
        self.detail = detail
        log.info("backend %s %s", state.value, detail.splitlines()[0] if detail else "")
        self.stateChanged.emit(state.value, detail)

    def _docker_or_none(self) -> DockerCli | None:
        if self._docker is None:
            executable = find_docker()
            if executable:
                self._docker = self._docker_factory(executable)
        return self._docker

    def _start_sequence(self) -> None:
        self.connection = None
        self.health = None
        self._set(BackendState.CHECKING)
        docker = self._docker_or_none()
        if docker is None:
            self._set(BackendState.DOCKER_MISSING)
            return
        if not self._ensure_engine(docker):
            return
        image = self.config.image
        image_id = docker.image_id(image)
        if image_id is None:
            if not is_pullable(image):
                self._set(BackendState.IMAGE_MISSING, image)
                return
            self._set(BackendState.PULLING, image)
            docker.pull(image, on_line=lambda line: self._set(BackendState.PULLING, line))
            image_id = docker.image_id(image)
            if image_id is None:
                raise DockerError(f"image {image} is still missing after the pull")
        connection = self._ensure_container(docker, image_id)
        self._set(BackendState.STARTING)
        health = self._wait_for_health(docker, connection)
        if health.protocol_version != PROTOCOL_VERSION:
            self._set(
                BackendState.ERROR,
                f"backend protocol {health.protocol_version}, app expects {PROTOCOL_VERSION}",
            )
            return
        self.connection = connection
        self.health = health
        self._set(BackendState.READY, health.openmc_version or "")

    def _ensure_engine(self, docker: DockerCli) -> bool:
        if docker.server_version():
            return True
        desktop = find_docker_desktop()
        if desktop is None:
            self._set(BackendState.DOCKER_UNAVAILABLE)
            return False
        self._set(BackendState.STARTING_DOCKER)
        os.startfile(desktop)  # type: ignore[attr-defined]  # Windows only
        deadline = time.monotonic() + DOCKER_START_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(3.0)
            if docker.server_version():
                return True
        self._set(BackendState.DOCKER_UNAVAILABLE)
        return False

    def _ensure_container(self, docker: DockerCli, image_id: str) -> Connection:
        name = self.config.container_name
        fingerprint = self.config.fingerprint(image_id)
        existing = docker.container(name)
        if (
            existing is not None
            and existing.running
            and existing.labels.get("pyvised.config") == fingerprint
            and existing.env.get(TOKEN_ENV)
        ):
            log.info("reusing running container %s", name)
            port = docker.host_port(name, CONTAINER_PORT)
            return Connection(f"http://127.0.0.1:{port}", existing.env[TOKEN_ENV])
        if existing is not None:
            docker.remove(name)
        workspace = self.config.workspace
        workspace.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        args = [
            "-d",
            "--rm",
            "--name",
            name,
            "--label",
            "pyvised.managed=1",
            "--label",
            f"pyvised.config={fingerprint}",
            "-p",
            f"127.0.0.1::{CONTAINER_PORT}",
            "-e",
            f"{TOKEN_ENV}={token}",
            "-v",
            f"{workspace}:/workspace",
        ]
        if self.config.cpus > 0:
            args += ["--cpus", f"{self.config.cpus:g}"]
        args.append(self.config.image)
        docker.run(args)
        port = docker.host_port(name, CONTAINER_PORT)
        return Connection(f"http://127.0.0.1:{port}", token)

    def _wait_for_health(self, docker: DockerCli, connection: Connection) -> HealthInfo:
        deadline = time.monotonic() + HEALTH_TIMEOUT
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return self._health_probe(connection.base_url)
            except Exception as exc:  # connection refused while uvicorn starts
                last_error = exc
            info = docker.container(self.config.container_name)
            if info is None or not info.running:
                raise DockerError(
                    "the backend container stopped during start-up",
                    docker.logs(self.config.container_name, 50),
                )
            time.sleep(0.5)
        raise DockerError(
            f"no answer from the backend within {HEALTH_TIMEOUT:.0f} s", str(last_error or "")
        )

    def _stop_container(self, *, force: bool) -> None:
        docker = self._docker_or_none()
        if docker is None:
            return
        if self.state is BackendState.READY or force:
            self._set(BackendState.STOPPING)
        if force:
            docker.remove(self.config.container_name)
        else:
            docker.stop(self.config.container_name, timeout=3)
        self.connection = None
        self.health = None
        self._set(BackendState.STOPPED)
