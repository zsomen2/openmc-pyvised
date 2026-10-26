"""Lifecycle of the backend container: find Docker, get the image, start, health-check, stop.

Every step runs in a worker thread; progress is reported through `stateChanged` and,
while the image is downloaded or loaded from a file, `transferChanged` (queued into the
GUI thread by Qt). `facts` collects what the checks found for the setup dialog.
"""

import hashlib
import logging
import os
import re
import secrets
import shutil
import threading
import time
from collections import deque
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
from .docker_cli import (
    Cancelled,
    DockerCli,
    DockerError,
    PullProgress,
    docker_data_dir,
    docker_desktop_version,
    find_docker,
    find_docker_desktop,
    split_reference,
)

log = logging.getLogger(__name__)

CONTAINER_NAME = "openmc-pyvised"
IMAGE_ENV = "PYVISED_BACKEND_IMAGE"
"""Overrides the image, e.g. `pyvised-backend:dev` for development (see the justfile)."""

DOCKER_START_TIMEOUT = 180.0
HEALTH_TIMEOUT = 90.0
IMAGE_SPACE_NEEDED = 6 * 1024**3
"""Free space wanted for the image: Docker keeps the downloaded layers (~1 GB) next to the
unpacked image (3.4 GB); the rest is room for runs."""


class BackendState(StrEnum):
    IDLE = "idle"
    CHECKING = "checking"
    DOCKER_MISSING = "docker_missing"
    STARTING_DOCKER = "starting_docker"
    DOCKER_UNAVAILABLE = "docker_unavailable"
    IMAGE_MISSING = "image_missing"
    PULLING = "pulling"
    LOADING = "loading"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class Step(StrEnum):
    """The setup check a state belongs to; an error belongs to the step that was running."""

    DOCKER = "docker"
    ENGINE = "engine"
    IMAGE = "image"
    BACKEND = "backend"


STEP_OF = {
    BackendState.CHECKING: Step.DOCKER,
    BackendState.DOCKER_MISSING: Step.DOCKER,
    BackendState.STARTING_DOCKER: Step.ENGINE,
    BackendState.DOCKER_UNAVAILABLE: Step.ENGINE,
    BackendState.IMAGE_MISSING: Step.IMAGE,
    BackendState.PULLING: Step.IMAGE,
    BackendState.LOADING: Step.IMAGE,
    BackendState.STARTING: Step.BACKEND,
    BackendState.READY: Step.BACKEND,
    BackendState.STOPPING: Step.BACKEND,
    BackendState.STOPPED: Step.BACKEND,
}

CUSTOM_DATA = "/data/custom"
"""Where added libraries appear in the container (read-only), one folder per library."""
_LIBRARY_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def library_mount_name(name: str) -> str:
    """A library's folder name inside the container (it becomes part of its id)."""
    return _LIBRARY_NAME.sub("-", name).strip("-.") or "library"


@dataclass(frozen=True)
class BackendConfig:
    image: str
    workspace: Path
    container_name: str
    cpus: float = 0.0
    keep_running: bool = False
    libraries: tuple[tuple[str, Path], ...] = ()
    """Added nuclear-data libraries: (name, host folder), mounted read-only."""
    remove_old_images: bool = True
    """After a successful start, remove other tags of the image's repository (old versions)."""

    def fingerprint(self, image_id: str) -> str:
        """Label value that tells whether a running container matches this configuration."""
        libraries = ";".join(f"{name}={folder}" for name, folder in self.libraries)
        text = f"{PROTOCOL_VERSION}|{image_id}|{self.workspace}|{self.cpus:g}|{libraries}"
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def mounts(self) -> list[str]:
        """`docker run -v` values: the workspace and every added library."""
        volumes = [f"{self.workspace}:/workspace"]
        seen = set()
        for name, folder in self.libraries:
            mount = library_mount_name(name)
            if mount in seen:
                continue
            seen.add(mount)
            volumes.append(f"{folder}:{CUSTOM_DATA}/{mount}:ro")
        return volumes

    @classmethod
    def from_settings(cls, settings: AppSettings) -> "BackendConfig":
        return cls(
            image=resolve_image(settings.backend_image),
            workspace=settings.workspace,
            container_name=CONTAINER_NAME,
            cpus=settings.backend_cpus,
            keep_running=settings.keep_backend_running,
            libraries=tuple(settings.added_libraries),
            remove_old_images=settings.remove_old_images,
        )


def default_image() -> str:
    return f"{BACKEND_IMAGE_REPOSITORY}:{__version__}"


def resolve_image(configured: str) -> str:
    """Environment override, then the user's setting, then the image of this version."""
    return os.environ.get(IMAGE_ENV, "").strip() or configured.strip() or default_image()


def is_pullable(reference: str) -> bool:
    """Registry references contain a '/', local development tags do not."""
    return "/" in reference.split("@", 1)[0]


@dataclass
class DockerFacts:
    """What the start sequence found out, for the setup dialog and the diagnostics."""

    docker: str | None = None
    """The docker.exe in use."""
    desktop: Path | None = None
    desktop_version: str | None = None
    engine: str | None = None
    """Version of the Docker engine, once it answers."""
    data_dir: Path | None = None
    free_bytes: int | None = None
    """Free space on the drive where Docker keeps its images."""
    image_id: str | None = None


@dataclass(frozen=True)
class Transfer:
    """Progress of getting the image: a download ("pull") or a load from a file ("load")."""

    kind: str
    done: int = 0
    total: int = 0
    """Bytes; 0 while unknown."""
    layers_done: int = 0
    layers: int = 0
    rate: float = 0.0
    """Bytes per second over the last few seconds."""
    unpacking: bool = False
    sized: bool = True
    """`total` is final (a download learns it layer by layer)."""

    @property
    def fraction(self) -> float | None:
        if not self.sized or self.total <= 0:
            return None
        return min(1.0, self.done / self.total)

    @property
    def remaining_s(self) -> float | None:
        if self.rate <= 0 or self.total <= 0 or self.unpacking:
            return None
        return max(0.0, (self.total - self.done) / self.rate)


class _Meter:
    """Turns progress callbacks into `Transfer` reports, at most every `interval` seconds,
    with the rate over the last `window` seconds."""

    def __init__(
        self,
        kind: str,
        emit: Callable[[Transfer], None],
        interval: float = 0.25,
        window: float = 5.0,
    ) -> None:
        self._emit = emit
        self._interval = interval
        self._window = window
        self._samples: deque[tuple[float, int]] = deque()
        self._last = 0.0
        self.latest = Transfer(kind)

    def update(
        self,
        done: int,
        total: int,
        layers_done: int = 0,
        layers: int = 0,
        unpacking: bool = False,
        sized: bool = True,
    ) -> None:
        now = time.monotonic()
        self._samples.append((now, done))
        while len(self._samples) > 2 and now - self._samples[0][0] > self._window:
            self._samples.popleft()
        start, start_done = self._samples[0]
        rate = (done - start_done) / (now - start) if now - start > 0.5 else self.latest.rate
        self.latest = Transfer(
            self.latest.kind, done, total, layers_done, layers, max(0.0, rate), unpacking, sized
        )
        if now - self._last >= self._interval:
            self._last = now
            self._emit(self.latest)

    def pull(self, progress: PullProgress) -> None:
        self.update(
            progress.downloaded,
            progress.total,
            progress.layers_done,
            progress.layers,
            progress.unpacking,
            progress.sized,
        )

    def finish(self) -> None:
        self._emit(self.latest)


class BackendManager(QObject):
    stateChanged = Signal(str, str)  # BackendState value, detail text
    transferChanged = Signal(object)  # Transfer
    operationFinished = Signal()
    """The worker is done (`busy` turned false), e.g. after cleaning up old images."""

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
        self.step = Step.DOCKER
        self.facts = DockerFacts()
        self.transfer: Transfer | None = None
        self.connection: Connection | None = None
        self.health: HealthInfo | None = None
        self._docker_factory = docker_factory
        self._health_probe = health_probe
        self._docker: DockerCli | None = None
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._running = False
        self._cancel = threading.Event()

    # -- public API (GUI thread) ----------------------------------------------

    @property
    def busy(self) -> bool:
        """An operation (start, download, load, removal) is running in the worker."""
        return self._running

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

    def load_image(self, path: Path) -> None:
        """Load the image from a `docker save` archive (offline PCs), then start."""
        self._spawn(lambda: self._load_sequence(path))

    def cancel_transfer(self) -> None:
        """Stop a running download or load; the state becomes IMAGE_MISSING."""
        self._cancel.set()

    def remove_image(self) -> None:
        """Remove the container and the backend image from Docker (frees ~3.4 GB)."""

        def sequence() -> None:
            docker = self._docker_or_none()
            if docker is None:
                self._set(BackendState.DOCKER_MISSING)
                return
            self._stop_container(force=True)
            image = self.config.image
            if docker.image_id(image) is not None and not docker.remove_image(image):
                raise DockerError(f"Docker did not remove {image}")
            self.facts.image_id = None
            self._set(BackendState.IMAGE_MISSING, image)

        self._spawn(sequence)

    def shutdown(self) -> None:
        """Called when the app exits: stop the container unless it should keep running."""
        self._cancel.set()
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
            if self._running:
                log.info("backend operation already running; request ignored")
                return
            self._running = True
            self._cancel.clear()
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
        finally:
            self._running = False
            self.operationFinished.emit()

    def _set(self, state: BackendState, detail: str = "") -> None:
        self.state = state
        self.detail = detail
        self.step = STEP_OF.get(state, self.step)
        if state not in (BackendState.PULLING, BackendState.LOADING):
            self.transfer = None
        log.info("backend %s %s", state.value, detail.splitlines()[0] if detail else "")
        self.stateChanged.emit(state.value, detail)

    def _report(self, transfer: Transfer) -> None:
        self.transfer = transfer
        self.transferChanged.emit(transfer)

    def _docker_or_none(self) -> DockerCli | None:
        if self._docker is None:
            executable = find_docker()
            if executable:
                self._docker = self._docker_factory(executable)
        return self._docker

    def _check_docker(self) -> DockerCli | None:
        """Docker installed and its engine running; None (with the state set) otherwise."""
        docker = self._docker_or_none()
        self.facts = DockerFacts(
            docker=docker.executable if docker else None,
            desktop=find_docker_desktop(),
            desktop_version=docker_desktop_version(),
        )
        if docker is None:
            self._set(BackendState.DOCKER_MISSING)
            return None
        if not self._ensure_engine(docker):
            return None
        data_dir = docker_data_dir()
        self.facts.data_dir = data_dir
        try:
            self.facts.free_bytes = shutil.disk_usage(data_dir).free
        except OSError:
            self.facts.free_bytes = None
        return docker

    def _start_sequence(self) -> None:
        self.connection = None
        self.health = None
        self._set(BackendState.CHECKING)
        docker = self._check_docker()
        if docker is None:
            return
        image = self.config.image
        image_id = docker.image_id(image)
        if image_id is None:
            if not is_pullable(image):
                self._set(BackendState.IMAGE_MISSING, image)
                return
            image_id = self._pull(docker, image)
            if image_id is None:
                return
        self.facts.image_id = image_id
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
        self._remove_old_images(docker)

    def _pull(self, docker: DockerCli, image: str) -> str | None:
        """Download `image`; its id, or None when the download was cancelled."""
        meter = _Meter("pull", self._report)
        self._set(BackendState.PULLING, image)
        try:
            docker.pull(image, on_progress=meter.pull, cancel=self._cancel)
        except Cancelled:
            self._set(BackendState.IMAGE_MISSING, image)
            return None
        meter.finish()
        image_id = docker.image_id(image)
        if image_id is None:
            raise DockerError(f"image {image} is still missing after the pull")
        return image_id

    def _load_sequence(self, path: Path) -> None:
        self._set(BackendState.CHECKING)
        docker = self._check_docker()
        if docker is None:
            return
        meter = _Meter("load", self._report)
        self._set(BackendState.LOADING, path.name)

        def progress(sent: int, size: int) -> None:  # Docker imports the layers at the end
            meter.update(sent, size, unpacking=sent >= size)

        try:
            loaded = docker.load(path, on_progress=progress, cancel=self._cancel)
        except Cancelled:
            self._set(BackendState.IMAGE_MISSING, self.config.image)
            return
        meter.finish()
        if docker.image_id(self.config.image) is None:
            found = ", ".join(loaded) or "no tagged image"
            raise DockerError(f"{path.name} holds {found}, not {self.config.image}")
        self._start_sequence()

    def _remove_old_images(self, docker: DockerCli) -> None:
        """Remove the other tags of the image's repository (the versions before an update)."""
        image = self.config.image
        if not self.config.remove_old_images or not is_pullable(image) or "@" in image:
            return
        repository, tag = split_reference(image)
        for reference in docker.image_tags(repository):
            if reference != f"{repository}:{tag}" and docker.remove_image(reference):
                log.info("removed the old backend image %s", reference)

    def _ensure_engine(self, docker: DockerCli) -> bool:
        version = docker.server_version()
        if version is None:
            desktop = self.facts.desktop
            if desktop is None:
                self._set(BackendState.DOCKER_UNAVAILABLE)
                return False
            self._set(BackendState.STARTING_DOCKER)
            os.startfile(desktop)  # type: ignore[attr-defined]  # Windows only
            deadline = time.monotonic() + DOCKER_START_TIMEOUT
            while version is None and time.monotonic() < deadline:
                if self._cancel.wait(3.0):
                    break
                version = docker.server_version()
            if version is None:
                self._set(BackendState.DOCKER_UNAVAILABLE)
                return False
        self.facts.engine = version
        return True

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
        ]
        for volume in self.config.mounts():
            args += ["-v", volume]
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
