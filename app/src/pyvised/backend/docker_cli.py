"""Thin wrapper around the `docker` command line.

The CLI (rather than the Docker SDK) follows the user's Docker context, needs no
pywin32, and every call can be reproduced by hand when something goes wrong.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# keep console windows from flashing up when the GUI runs docker.exe
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

Runner = Callable[..., subprocess.CompletedProcess[str]]


class DockerError(RuntimeError):
    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output


@dataclass
class ContainerInfo:
    name: str
    running: bool
    image_id: str
    labels: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)


def _program_files() -> Path:
    return Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))


def find_docker() -> str | None:
    """`docker.exe` on PATH, else Docker Desktop's install folder (PATH can be stale)."""
    found = shutil.which("docker")
    if found:
        return found
    candidate = _program_files() / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"
    return str(candidate) if candidate.is_file() else None


def find_docker_desktop() -> Path | None:
    candidate = _program_files() / "Docker" / "Docker" / "Docker Desktop.exe"
    return candidate if candidate.is_file() else None


class DockerCli:
    def __init__(self, executable: str, runner: Runner = subprocess.run) -> None:
        self.executable = executable
        self._runner = runner

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

    def server_version(self) -> str | None:
        """Version of the Docker engine, or None if it does not answer."""
        proc = self._run(["version", "--format", "{{.Server.Version}}"], timeout=20, check=False)
        version = proc.stdout.strip()
        return version if proc.returncode == 0 and version else None

    def image_id(self, reference: str) -> str | None:
        proc = self._run(["image", "inspect", "--format", "{{.Id}}", reference], check=False)
        image_id = proc.stdout.strip()
        return image_id if proc.returncode == 0 and image_id else None

    def pull(self, reference: str, on_line: Callable[[str], None] | None = None) -> None:
        """`docker pull`, reporting each output line (layer progress) to `on_line`."""
        proc = subprocess.Popen(
            [self.executable, "pull", reference],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        lines = []
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if line:
                lines.append(line)
                if on_line is not None:
                    on_line(line)
        if proc.wait() != 0:
            raise DockerError(f"'docker pull {reference}' failed", "\n".join(lines[-20:]))

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
