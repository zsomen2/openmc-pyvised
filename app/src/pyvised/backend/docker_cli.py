"""Thin wrapper around the `docker` command line.

The CLI (rather than the Docker SDK) follows the user's Docker context, needs no
pywin32, and every call can be reproduced by hand when something goes wrong.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlencode

# keep console windows from flashing up when the GUI runs docker.exe
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

Runner = Callable[..., subprocess.CompletedProcess[str]]

DOCKER_DESKTOP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Docker Desktop"
"""Uninstall entry of Docker Desktop: in HKCU for per-user installs, in HKLM for all users."""


class DockerError(RuntimeError):
    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output


class Cancelled(DockerError):
    """A pull or load stopped by `cancel`."""

    def __init__(self) -> None:
        super().__init__("cancelled")


@dataclass
class ContainerInfo:
    name: str
    running: bool
    image_id: str
    labels: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)


def _program_files() -> Path:
    return Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))


def _local_app_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))


def _docker_desktop_entry(value: str) -> list[str]:
    """`value` of Docker Desktop's Uninstall entries (current user first)."""
    if sys.platform != "win32":
        return []
    import winreg

    found = []
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, DOCKER_DESKTOP_KEY) as key:
                data, _ = winreg.QueryValueEx(key, value)
        except OSError:
            continue
        if data:
            found.append(str(data))
    return found


def docker_desktop_folders() -> list[Path]:
    """Folders Docker Desktop may be installed in: its Uninstall entry, then the defaults for
    all-users and per-user (`--user`) installs."""
    folders = [Path(folder) for folder in _docker_desktop_entry("InstallLocation")]
    folders += [
        _program_files() / "Docker" / "Docker",
        _local_app_data() / "Programs" / "DockerDesktop",
    ]
    return list(dict.fromkeys(folders))


def docker_desktop_version() -> str | None:
    versions = _docker_desktop_entry("DisplayVersion")
    return versions[0] if versions else None


def find_docker() -> str | None:
    """`docker.exe` on PATH, else in Docker Desktop's install folder (PATH can be stale)."""
    found = shutil.which("docker")
    if found:
        return found
    for folder in docker_desktop_folders():
        candidate = folder / "resources" / "bin" / "docker.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def find_docker_desktop() -> Path | None:
    for folder in docker_desktop_folders():
        candidate = folder / "Docker Desktop.exe"
        if candidate.is_file():
            return candidate
    return None


def docker_data_dir() -> Path:
    """Folder on the drive where Docker Desktop keeps images (its WSL disk), for the free-space
    check: the custom location from its settings, else `%LOCALAPPDATA%\\Docker`."""
    settings = Path(os.environ.get("APPDATA", "")) / "Docker" / "settings-store.json"
    try:
        custom = json.loads(settings.read_text(encoding="utf-8")).get("CustomWslDistroDir")
    except (OSError, ValueError, AttributeError):
        custom = None
    for folder in (Path(custom) if custom else None, _local_app_data() / "Docker"):
        if folder is not None and folder.exists():
            return folder
    return _local_app_data()


def split_reference(reference: str) -> tuple[str, str]:
    """`repository[:tag|@digest]` → (repository, tag or digest); the tag defaults to latest."""
    if "@" in reference:
        repository, digest = reference.split("@", 1)
        return repository, digest
    repository, colon, tag = reference.rpartition(":")
    if colon and "/" not in tag:
        return repository, tag
    return reference, "latest"


@dataclass(frozen=True)
class PullProgress:
    """How far a pull is: bytes of the layers to download, and layers finished."""

    downloaded: int = 0
    total: int = 0
    """Bytes to download (layers already on the PC excluded); 0 while unknown."""
    layers_done: int = 0
    layers: int = 0
    unpacking: bool = False
    """Nothing is left to download and Docker unpacks the layers (also when all of them
    were already on the PC: the containerd image store keeps downloaded layers)."""
    sized: bool = True
    """The size of every layer to download is known, so `total` will not grow."""

    @property
    def fraction(self) -> float | None:
        if not self.sized or self.total <= 0:
            return None
        return min(1.0, self.downloaded / self.total)


class PullTracker:
    """Folds Docker's pull messages into one `PullProgress`.

    A message names a layer by the first 12 hex digits of its digest. Once the layer sizes
    from the registry manifest are known (`use_sizes`, which may come from another thread
    while the pull runs), the total is exact; until then it grows as layers start
    downloading.
    """

    def __init__(self, sizes: dict[str, int] | None = None) -> None:
        self._lock = threading.Lock()
        self._fixed = False
        self._sizes: dict[str, int] = {}
        self._layers: set[str] = set()
        self._current: dict[str, int] = {}
        self._present: set[str] = set()
        self._downloaded: set[str] = set()
        self._done: set[str] = set()
        if sizes:
            self.use_sizes(sizes)

    def use_sizes(self, sizes: dict[str, int]) -> None:
        with self._lock:
            self._fixed = True
            self._sizes = dict(sizes)  # drops sizes learned for config or manifest blobs
            self._layers |= set(sizes)

    def feed(self, message: dict[str, Any]) -> PullProgress:
        layer = str(message.get("id") or "")
        status = str(message.get("status") or "")
        detail = message.get("progressDetail") or {}
        with self._lock:
            if (
                layer
                and not status.startswith("Pulling from")
                and not (self._fixed and layer not in self._sizes)
            ):  # not the tag line or a config/manifest blob
                self._record(layer, status, detail)
        return self.progress()

    def _record(self, layer: str, status: str, detail: dict[str, Any]) -> None:
        match status:
            case "Pulling fs layer" | "Waiting":
                self._layers.add(layer)
            case "Already exists":
                self._layers.add(layer)
                self._present.add(layer)
                self._done.add(layer)
            case "Downloading":
                if detail.get("total") and not self._fixed:
                    self._sizes[layer] = int(detail["total"])
                if detail.get("current") is not None:
                    self._current[layer] = int(detail["current"])
            case "Verifying Checksum" | "Download complete" | "Extracting":
                self._downloaded.add(layer)
            case "Pull complete":
                self._layers.add(layer)
                self._downloaded.add(layer)
                self._done.add(layer)

    def progress(self) -> PullProgress:
        with self._lock:
            return self._progress()

    def _progress(self) -> PullProgress:
        fetch = [layer for layer in self._sizes if layer not in self._present]
        downloaded = 0
        for layer in fetch:
            size = self._sizes[layer]
            current = size if layer in self._downloaded else self._current.get(layer, 0)
            downloaded += min(current, size)
        total = sum(self._sizes[layer] for layer in fetch)
        unknown = self._layers - self._present - self._downloaded - set(self._sizes)
        return PullProgress(
            downloaded=downloaded,
            total=total,
            layers_done=len(self._done & self._layers),
            layers=len(self._layers),
            # "Already exists" does not mean unpacked in the containerd store, so this stays
            # true until the pull ends
            unpacking=bool(self._layers) and not unknown and downloaded >= total,
            sized=bool(self._layers) and not unknown,
        )


def _cli_message(line: str) -> dict[str, str]:
    """A `docker pull` output line ("<layer>: <status>") as an API-like message."""
    layer, colon, status = line.partition(": ")
    if colon and len(layer) == 12 and all(c in "0123456789abcdef" for c in layer):
        return {"id": layer, "status": status}
    return {"status": line}


def _http_head(stream: IO[bytes]) -> tuple[int, str]:
    """Status code and reason of an HTTP response; the headers are skipped."""
    status = stream.readline().decode("latin-1").strip()
    while stream.readline() not in (b"\r\n", b"\n", b""):
        pass
    parts = status.split(" ", 2)
    try:
        return int(parts[1]), parts[2] if len(parts) > 2 else ""
    except (IndexError, ValueError):
        return 0, status


class DockerCli:
    def __init__(
        self,
        executable: str,
        runner: Runner = subprocess.run,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self.executable = executable
        self._runner = runner
        self._popen_impl = popen

    def _run(
        self, args: Sequence[str], *, timeout: float = 60.0, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        try:
            proc = self._runner(
                [self.executable, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DockerError(f"cannot run {self.executable}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerError(f"'docker {args[0]}' did not finish within {timeout:.0f} s") from exc
        if check and proc.returncode != 0:
            raise DockerError(
                f"'docker {' '.join(args[:2])}' failed", (proc.stderr or proc.stdout).strip()
            )
        return proc

    def _popen(self, args: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        try:
            return self._popen_impl(
                [self.executable, *args], creationflags=CREATE_NO_WINDOW, **kwargs
            )
        except OSError as exc:
            raise DockerError(f"cannot run {self.executable}: {exc}") from exc

    def server_version(self) -> str | None:
        """Version of the Docker engine, or None if it does not answer."""
        proc = self._run(["version", "--format", "{{.Server.Version}}"], timeout=20, check=False)
        version = proc.stdout.strip()
        return version if proc.returncode == 0 and version else None

    def client_version(self) -> str | None:
        proc = self._run(["version", "--format", "{{.Client.Version}}"], timeout=20, check=False)
        return proc.stdout.strip() or None

    def server_platform(self) -> str:
        """`os/arch` of the engine, e.g. linux/amd64."""
        proc = self._run(
            ["version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"], timeout=20, check=False
        )
        return proc.stdout.strip() if proc.returncode == 0 else "linux/amd64"

    def image_id(self, reference: str) -> str | None:
        proc = self._run(["image", "inspect", "--format", "{{.Id}}", reference], check=False)
        image_id = proc.stdout.strip()
        return image_id if proc.returncode == 0 and image_id else None

    def image_tags(self, repository: str) -> list[str]:
        """Local `repository:tag` references of a repository (untagged images left out)."""
        proc = self._run(
            ["image", "ls", "--format", "{{.Repository}}:{{.Tag}}", repository], check=False
        )
        lines = proc.stdout.split() if proc.returncode == 0 else []
        return [line for line in dict.fromkeys(lines) if not line.endswith(":<none>")]

    def remove_image(self, reference: str) -> bool:
        return self._run(["image", "rm", reference], timeout=120, check=False).returncode == 0

    def remote_layer_sizes(self, reference: str, platform: str) -> dict[str, int] | None:
        """Sizes of the image's layers in the registry by short id (12 hex digits), for the
        `platform` the engine pulls; None when the registry cannot be asked."""
        try:
            proc = self._run(["manifest", "inspect", "-v", reference], timeout=30, check=False)
            entries = json.loads(proc.stdout) if proc.returncode == 0 else None
        except (DockerError, ValueError):
            return None
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            return None
        wanted = tuple(platform.split("/", 1))
        for entry in entries:
            target = (entry.get("Descriptor") or {}).get("platform") or {}
            if len(entries) > 1 and (target.get("os"), target.get("architecture")) != wanted:
                continue
            manifest = entry.get("OCIManifest") or entry.get("SchemaV2Manifest") or {}
            layers = manifest.get("layers") or []
            sizes = {
                str(layer["digest"]).split(":", 1)[-1][:12]: int(layer["size"])
                for layer in layers
                if layer.get("digest") and layer.get("size")
            }
            if sizes:
                return sizes
        return None

    def pull(
        self,
        reference: str,
        on_progress: Callable[[PullProgress], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        """Pull `reference`, reporting progress. Byte counts come from the Engine API (reached
        through `docker system dial-stdio`, so the Docker context still applies); if the API
        refuses, e.g. for a registry that needs the CLI's stored login, `docker pull` runs
        instead, which reports finished layers only. The layer sizes from the registry
        (which takes seconds to ask) arrive while the pull already runs."""
        tracker = PullTracker()
        report = on_progress or (lambda progress: None)

        def lookup() -> None:
            sizes = self.remote_layer_sizes(reference, self.server_platform())
            if sizes:
                tracker.use_sizes(sizes)

        threading.Thread(target=lookup, daemon=True, name="pyvised-docker-manifest").start()
        if not self._api_pull(reference, tracker, report, cancel):
            self._cli_pull(reference, tracker, report, cancel)

    def _api_pull(
        self,
        reference: str,
        tracker: PullTracker,
        report: Callable[[PullProgress], None],
        cancel: threading.Event | None,
    ) -> bool:
        """`POST /images/create` over `docker system dial-stdio`; False if the API cannot be
        used. Closing the connection cancels the pull in the engine."""
        repository, tag = split_reference(reference)
        query = urlencode({"fromImage": repository, "tag": tag})
        request = (
            f"POST /images/create?{query} HTTP/1.0\r\nHost: docker\r\nContent-Length: 0\r\n\r\n"
        )
        proc = self._popen(
            ["system", "dial-stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stop = self._kill_on_cancel(proc, cancel)
        try:
            assert proc.stdin is not None and proc.stdout is not None
            try:
                proc.stdin.write(request.encode("ascii"))
                proc.stdin.flush()
            except OSError:
                return False
            code, _ = _http_head(proc.stdout)
            if code != 200:
                if cancel is not None and cancel.is_set():
                    raise Cancelled
                return False
            for raw in proc.stdout:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if message.get("error") or message.get("errorDetail"):
                    error = message.get("error") or message["errorDetail"].get("message", "")
                    raise DockerError(f"'docker pull {reference}' failed", str(error))
                report(tracker.feed(message))
            if cancel is not None and cancel.is_set():
                raise Cancelled
            return True
        finally:
            stop.set()
            proc.kill()
            proc.wait()

    def _cli_pull(
        self,
        reference: str,
        tracker: PullTracker,
        report: Callable[[PullProgress], None],
        cancel: threading.Event | None,
    ) -> None:
        proc = self._popen(["pull", reference], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        stop = self._kill_on_cancel(proc, cancel)
        lines = []
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    lines.append(line)
                    report(tracker.feed(_cli_message(line)))
            code = proc.wait()
        finally:
            stop.set()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if cancel is not None and cancel.is_set():
            raise Cancelled
        if code != 0:
            raise DockerError(f"'docker pull {reference}' failed", "\n".join(lines[-20:]))

    @staticmethod
    def _kill_on_cancel(
        proc: subprocess.Popen[bytes], cancel: threading.Event | None
    ) -> threading.Event:
        """Kill `proc` as soon as `cancel` is set; set the returned event once done waiting."""
        stop = threading.Event()
        if cancel is not None:

            def watch() -> None:
                while not stop.wait(0.2):
                    if cancel.is_set():
                        proc.kill()
                        return

            threading.Thread(target=watch, daemon=True, name="pyvised-docker-cancel").start()
        return stop

    def load(
        self,
        path: Path,
        on_progress: Callable[[int, int], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> list[str]:
        """`docker load` of an image archive (`docker save`, optionally gzip-compressed). The
        file is fed through stdin, so the bytes sent give the progress. Returns the image
        references in the archive."""
        size = path.stat().st_size
        proc = self._popen(
            ["load"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        output: list[str] = []

        def read() -> None:
            assert proc.stdout is not None
            output.extend(raw.decode("utf-8", "replace") for raw in proc.stdout)

        reader = threading.Thread(target=read, daemon=True, name="pyvised-docker-load")
        reader.start()
        sent = 0
        try:
            assert proc.stdin is not None
            with path.open("rb") as file:
                while chunk := file.read(1 << 20):
                    if cancel is not None and cancel.is_set():
                        raise Cancelled
                    try:
                        proc.stdin.write(chunk)
                    except OSError:  # docker gave up early; its output says why
                        break
                    sent += len(chunk)
                    if on_progress is not None:
                        on_progress(sent, size)
            proc.stdin.close()
            code = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            reader.join(timeout=5)
        text = "".join(output).strip()
        if code != 0:
            raise DockerError(f"'docker load' of {path.name} failed", text[-2000:])
        return [
            line.split(":", 1)[1].strip()
            for line in text.splitlines()
            if line.startswith("Loaded image:")
        ]

    def container(self, name: str) -> ContainerInfo | None:
        proc = self._run(["container", "inspect", name], check=False)
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)[0]
        config = data.get("Config") or {}
        env = dict(item.split("=", 1) for item in config.get("Env") or [] if "=" in item)
        return ContainerInfo(
            name=name,
            running=bool(data.get("State", {}).get("Running")),
            image_id=data.get("Image", ""),
            labels=config.get("Labels") or {},
            env=env,
        )

    def run(self, args: Sequence[str]) -> str:
        """`docker run ARGS`; returns the new container id."""
        return self._run(["run", *args], timeout=120).stdout.strip()

    def host_port(self, name: str, container_port: int) -> int:
        """Host port published for `container_port` (first mapping)."""
        output = self._run(["port", name, f"{container_port}/tcp"]).stdout.strip()
        first = output.splitlines()[0] if output else ""
        try:
            return int(first.rsplit(":", 1)[1])
        except (IndexError, ValueError) as exc:
            raise DockerError(f"unexpected 'docker port' output: {output!r}") from exc

    def stop(self, name: str, timeout: int = 5) -> None:
        self._run(["stop", "-t", str(timeout), name], timeout=timeout + 30, check=False)

    def remove(self, name: str) -> None:
        self._run(["rm", "-f", name], check=False)

    def logs(self, name: str, tail: int = 300) -> str:
        proc = self._run(["logs", "--tail", str(tail), name], check=False)
        return (proc.stdout + proc.stderr).strip()

    def info_text(self) -> str:
        proc = self._run(["info"], timeout=30, check=False)
        return (proc.stdout or proc.stderr).strip()
