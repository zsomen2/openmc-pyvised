import json
import subprocess

import pytest

from pyvised.backend.docker_cli import DockerCli, DockerError


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
