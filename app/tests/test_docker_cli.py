import io
import json
import subprocess
import threading
from pathlib import Path

import pytest

from pyvised.backend import docker_cli
from pyvised.backend.docker_cli import (
    Cancelled,
    DockerCli,
    DockerError,
    PullProgress,
    PullTracker,
    split_reference,
)

LAYERS = {"aaaaaaaaaaaa": 600, "bbbbbbbbbbbb": 300, "cccccccccccc": 100}


class FakeStream:
    """A process's stdout: the given bytes, then (with `block`) nothing until it is killed."""

    def __init__(self, data: bytes, killed: threading.Event, block: bool = False):
        self._data = io.BytesIO(data)
        self._killed = killed
        self._block = block

    def readline(self) -> bytes:
        line = self._data.readline()
        if not line and self._block:
            self._killed.wait(5)
        return line

    def __iter__(self):
        return iter(self.readline, b"")


class Sink(io.BytesIO):
    def close(self) -> None:  # keep what was written readable
        self.closed_by_caller = True


class FakeProc:
    def __init__(self, output: bytes = b"", code: int = 0, block: bool = False):
        self.killed = threading.Event()
        self.stdin = Sink()
        self.stdout = FakeStream(output, self.killed, block)
        self.returncode: int | None = None
        self._code = code

    def kill(self) -> None:
        self.killed.set()
        if self.returncode is None:
            self.returncode = -9

    def wait(self, timeout=None) -> int:
        if self.returncode is None:
            self.returncode = self._code
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode


class FakePopen:
    """Hands out prepared processes by docker subcommand ("system" = dial-stdio)."""

    def __init__(self, procs: dict[str, FakeProc]):
        self.procs = procs
        self.started: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.started.append(cmd[1:])
        return self.procs[cmd[1]]


def api_response(messages: list[dict], status: str = "200 OK") -> bytes:
    head = f"HTTP/1.0 {status}\r\nContent-Type: application/json\r\n\r\n".encode()
    return head + b"".join(json.dumps(m).encode() + b"\r\n" for m in messages)


def manifest_entries() -> str:
    def entry(os_name, arch, layers):
        return {
            "Descriptor": {"platform": {"os": os_name, "architecture": arch}},
            "OCIManifest": {
                "layers": [{"digest": f"sha256:{d}{'0' * 52}", "size": s} for d, s in layers]
            },
        }

    return json.dumps(
        [
            entry("linux", "amd64", list(LAYERS.items())),
            entry("unknown", "unknown", [("dddddddddddd", 5)]),
            entry("linux", "arm64", [("eeeeeeeeeeee", 7)]),
        ]
    )


PULL_MESSAGES = [
    {"status": "Pulling from zsomen2/pyvised-backend", "id": "1.0.0"},
    {"status": "Pulling fs layer", "id": "aaaaaaaaaaaa"},
    {"status": "Pulling fs layer", "id": "bbbbbbbbbbbb"},
    {"status": "Already exists", "id": "cccccccccccc"},
    {
        "status": "Downloading",
        "id": "aaaaaaaaaaaa",
        "progressDetail": {"current": 300, "total": 600},
    },
    {"status": "Downloading", "id": "ffffffffffff", "progressDetail": {"current": 5, "total": 9}},
    {"status": "Download complete", "id": "ffffffffffff"},  # the config blob, not a layer
    {
        "status": "Downloading",
        "id": "bbbbbbbbbbbb",
        "progressDetail": {"current": 300, "total": 300},
    },
    {"status": "Download complete", "id": "bbbbbbbbbbbb"},
    {"status": "Download complete", "id": "aaaaaaaaaaaa"},
    {"status": "Pull complete", "id": "aaaaaaaaaaaa"},
    {"status": "Pull complete", "id": "bbbbbbbbbbbb"},
    {"status": "Digest: sha256:0123"},
    {"status": "Status: Downloaded newer image for ghcr.io/zsomen2/pyvised-backend:1.0.0"},
]


def test_split_reference():
    assert split_reference("ghcr.io/x/pyvised-backend:1.0.0") == (
        "ghcr.io/x/pyvised-backend",
        "1.0.0",
    )
    assert split_reference("localhost:5000/pyvised-backend") == (
        "localhost:5000/pyvised-backend",
        "latest",
    )
    assert split_reference("localhost:5000/b:2") == ("localhost:5000/b", "2")
    assert split_reference("ghcr.io/x/b@sha256:abc") == ("ghcr.io/x/b", "sha256:abc")


def test_tracker_with_manifest_sizes_counts_only_what_is_downloaded():
    tracker = PullTracker(LAYERS)
    seen = [tracker.feed(message) for message in PULL_MESSAGES]
    after_partial = seen[4]
    assert (after_partial.downloaded, after_partial.total) == (300, 900), "c already exists"
    assert after_partial.fraction == pytest.approx(1 / 3)
    assert seen[6].total == 900, "the config blob does not count"
    assert not seen[8].unpacking, "layer a is still downloading"
    downloaded = seen[9]
    assert downloaded.fraction == 1.0 and downloaded.unpacking
    final = seen[-1]
    assert (final.layers_done, final.layers) == (3, 3)


def test_manifest_sizes_arriving_during_the_pull_make_the_total_exact():
    tracker = PullTracker()
    for message in PULL_MESSAGES[:6]:
        tracker.feed(message)
    assert tracker.progress().total == 609, "a's total and the config blob's, learned so far"
    tracker.use_sizes(LAYERS)
    assert (tracker.progress().downloaded, tracker.progress().total) == (300, 900)
    for message in PULL_MESSAGES[6:]:
        progress = tracker.feed(message)
    assert progress.fraction == 1.0 and (progress.layers_done, progress.layers) == (3, 3)


def test_layers_kept_by_the_containerd_store_are_only_unpacked():
    """Docker's containerd image store keeps downloaded layers after `docker rmi`: they all
    "already exist", and the pull only unpacks them ("Extracting" counts seconds)."""
    tracker = PullTracker()
    for layer in ("aaaaaaaaaaaa", "bbbbbbbbbbbb"):
        tracker.feed({"status": "Pulling fs layer", "id": layer})
    assert not tracker.progress().unpacking, "nothing known yet"
    for layer in ("aaaaaaaaaaaa", "bbbbbbbbbbbb"):
        tracker.feed({"status": "Already exists", "id": layer})
    progress = tracker.feed(
        {
            "status": "Extracting",
            "id": "aaaaaaaaaaaa",
            "progressDetail": {"current": 1, "units": "s"},
        }
    )
    assert progress.unpacking and progress.fraction is None and progress.downloaded == 0


def test_tracker_without_sizes_learns_them_while_downloading():
    tracker = PullTracker()
    tracker.feed({"status": "Pulling fs layer", "id": "aaaaaaaaaaaa"})
    tracker.feed({"status": "Pulling fs layer", "id": "bbbbbbbbbbbb"})
    assert tracker.progress().fraction is None
    progress = tracker.feed(
        {
            "status": "Downloading",
            "id": "aaaaaaaaaaaa",
            "progressDetail": {"current": 50, "total": 200},
        }
    )
    assert (progress.downloaded, progress.total, progress.layers) == (50, 200, 2)
    assert progress.fraction is None, "b's size is unknown: no percentage that could drop"
    progress = tracker.feed(
        {
            "status": "Downloading",
            "id": "bbbbbbbbbbbb",
            "progressDetail": {"current": 0, "total": 800},
        }
    )
    assert progress.fraction == pytest.approx(0.05)


def test_cli_lines_become_messages():
    assert docker_cli._cli_message("0123456789ab: Pull complete") == {
        "id": "0123456789ab",
        "status": "Pull complete",
    }
    assert docker_cli._cli_message("Digest: sha256:0123") == {"status": "Digest: sha256:0123"}


def test_remote_layer_sizes_pick_the_engine_platform():
    docker = DockerCli("docker", FakeRunner({("manifest", "inspect"): (0, manifest_entries())}))
    assert docker.remote_layer_sizes("ghcr.io/x/b:1", "linux/amd64") == LAYERS
    assert docker.remote_layer_sizes("ghcr.io/x/b:1", "linux/arm64") == {"eeeeeeeeeeee": 7}
    offline = DockerCli("docker", FakeRunner({("manifest", "inspect"): (1, "no such host")}))
    assert offline.remote_layer_sizes("ghcr.io/x/b:1", "linux/amd64") is None


def test_pull_through_the_engine_api_reports_bytes():
    runner = FakeRunner(
        {
            ("version",): (0, "linux/amd64"),
            ("manifest", "inspect"): (0, manifest_entries()),
        }
    )
    api = FakeProc(api_response(PULL_MESSAGES))
    popen = FakePopen({"system": api})
    docker = DockerCli("docker", runner, popen)
    reports: list[PullProgress] = []
    docker.pull("ghcr.io/zsomen2/pyvised-backend:1.0.0", reports.append)
    assert popen.started == [["system", "dial-stdio"]]
    request = api.stdin.getvalue().decode()
    assert request.startswith("POST /images/create?fromImage=ghcr.io%2Fzsomen2%2Fpyvised-backend")
    assert "&tag=1.0.0 HTTP/1.0" in request
    assert reports[-1].fraction == 1.0 and reports[-1].layers_done == 3


def test_an_error_message_in_the_stream_fails_the_pull():
    error = {"errorDetail": {"message": "no space left on device"}, "error": "no space left"}
    popen = FakePopen({"system": FakeProc(api_response([PULL_MESSAGES[1], error]))})
    docker = DockerCli("docker", FakeRunner({}), popen)
    with pytest.raises(DockerError) as raised:
        docker.pull("ghcr.io/x/b:1")
    assert "no space left" in raised.value.output


def test_a_refused_api_falls_back_to_docker_pull():
    refused = FakeProc(api_response([], status="401 Unauthorized"))
    cli = FakeProc(
        b"1.0.0: Pulling from x/b\naaaaaaaaaaaa: Pulling fs layer\naaaaaaaaaaaa: Pull complete\n"
    )
    popen = FakePopen({"system": refused, "pull": cli})
    docker = DockerCli("docker", FakeRunner({}), popen)
    reports: list[PullProgress] = []
    docker.pull("registry.example/x/b:1", reports.append)
    assert popen.started[-1] == ["pull", "registry.example/x/b:1"]
    assert (reports[-1].layers_done, reports[-1].layers) == (1, 1)
    failing = FakePopen({"system": refused, "pull": FakeProc(b"denied: access\n", code=1)})
    with pytest.raises(DockerError):
        DockerCli("docker", FakeRunner({}), failing).pull("registry.example/x/b:1")


def test_cancel_stops_a_pull():
    api = FakeProc(api_response(PULL_MESSAGES[:3]), block=True)
    docker = DockerCli("docker", FakeRunner({}), FakePopen({"system": api}))
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    with pytest.raises(Cancelled):
        docker.pull("ghcr.io/x/b:1", cancel=cancel)
    assert api.killed.is_set()


def test_load_feeds_the_file_and_reports_progress(tmp_path: Path):
    archive = tmp_path / "pyvised-backend-1.0.0.tar.gz"
    archive.write_bytes(b"x" * (3 * 1024 * 1024 + 5))
    load = FakeProc(b"Loaded image: ghcr.io/zsomen2/pyvised-backend:1.0.0\n")
    docker = DockerCli("docker", FakeRunner({}), FakePopen({"load": load}))
    progress: list[tuple[int, int]] = []
    loaded = docker.load(archive, lambda sent, size: progress.append((sent, size)))
    assert loaded == ["ghcr.io/zsomen2/pyvised-backend:1.0.0"]
    assert load.stdin.getvalue() == archive.read_bytes()
    assert progress[-1] == (archive.stat().st_size, archive.stat().st_size)
    broken = FakePopen({"load": FakeProc(b"invalid tar header\n", code=1)})
    with pytest.raises(DockerError) as raised:
        DockerCli("docker", FakeRunner({}), broken).load(archive)
    assert "invalid tar header" in raised.value.output


def test_image_tags_skip_untagged_images():
    out = "ghcr.io/x/b:1.0.0\nghcr.io/x/b:0.9.0\nghcr.io/x/b:<none>\n"
    docker = DockerCli("docker", FakeRunner({("image", "ls"): (0, out)}))
    assert docker.image_tags("ghcr.io/x/b") == ["ghcr.io/x/b:1.0.0", "ghcr.io/x/b:0.9.0"]


def test_a_per_user_docker_desktop_is_found(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(docker_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(docker_cli, "_docker_desktop_entry", lambda value: [])
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "pf"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert docker_cli.find_docker() is None and docker_cli.find_docker_desktop() is None
    folder = tmp_path / "local" / "Programs" / "DockerDesktop"
    (folder / "resources" / "bin").mkdir(parents=True)
    (folder / "resources" / "bin" / "docker.exe").write_bytes(b"")
    (folder / "Docker Desktop.exe").write_bytes(b"")
    assert docker_cli.find_docker() == str(folder / "resources" / "bin" / "docker.exe")
    assert docker_cli.find_docker_desktop() == folder / "Docker Desktop.exe"
    custom = tmp_path / "elsewhere" / "Docker"
    custom.mkdir(parents=True)
    monkeypatch.setattr(docker_cli, "_docker_desktop_entry", lambda value: [str(custom)])
    (custom / "Docker Desktop.exe").write_bytes(b"")
    assert docker_cli.find_docker_desktop() == custom / "Docker Desktop.exe"


def test_docker_data_dir_follows_a_custom_disk_location(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    (tmp_path / "local" / "Docker").mkdir(parents=True)
    assert docker_cli.docker_data_dir() == tmp_path / "local" / "Docker"
    custom = tmp_path / "D" / "DockerData"
    custom.mkdir(parents=True)
    store = tmp_path / "roaming" / "Docker" / "settings-store.json"
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps({"CustomWslDistroDir": str(custom)}), encoding="utf-8")
    assert docker_cli.docker_data_dir() == custom


class FakeRunner:
    """Records docker invocations and answers from a table keyed by the first arguments."""

    def __init__(self, answers: dict[tuple[str, ...], tuple[int, str]]):
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        args = cmd[1:]
        self.calls.append(args)
        for prefix, (code, out) in self.answers.items():
            if tuple(args[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(cmd, code, out, "" if code == 0 else out)
        return subprocess.CompletedProcess(cmd, 1, "", "unknown command")


def test_server_version_none_when_engine_down():
    docker = DockerCli("docker", FakeRunner({("version",): (1, "cannot connect")}))
    assert docker.server_version() is None


def test_container_inspect_parses_labels_and_env():
    inspect = [
        {
            "Image": "sha256:abc",
            "State": {"Running": True},
            "Config": {
                "Labels": {"pyvised.config": "f00"},
                "Env": ["PYVISED_TOKEN=secret", "PATH=/bin"],
            },
        }
    ]
    docker = DockerCli("docker", FakeRunner({("container", "inspect"): (0, json.dumps(inspect))}))
    info = docker.container("pyvised-backend-x")
    assert info is not None and info.running
    assert info.labels == {"pyvised.config": "f00"}
    assert info.env["PYVISED_TOKEN"] == "secret"


def test_missing_container_is_none():
    docker = DockerCli("docker", FakeRunner({("container", "inspect"): (1, "No such object")}))
    assert docker.container("nope") is None


def test_host_port_parses_first_mapping():
    docker = DockerCli("docker", FakeRunner({("port",): (0, "127.0.0.1:55001\n")}))
    assert docker.host_port("c", 8765) == 55001


def test_failed_run_raises_with_output():
    docker = DockerCli("docker", FakeRunner({("run",): (125, "port is already allocated")}))
    with pytest.raises(DockerError) as raised:
        docker.run(["-d", "img"])
    assert "already allocated" in raised.value.output
