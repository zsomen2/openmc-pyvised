"""Tracked mini-runs: a few histories of the opened model with OpenMC's track output.

A request copies the model's cached ``model.xml`` into a folder of the
container cache (tracks never enter the workspace or the run history),
patches the copy (`patch_tracks_xml`), runs ``openmc --track`` there and reads
the tracks in a subprocess (`trackfile`), whose frame is the answer. One
generation of an eigenvalue model runs as a fixed-source run without fission
neutrons (a fission ends the history either way, and a leaky model may bank no
fission site in one batch, which OpenMC treats as an error). The fission-source
start stays an eigenvalue run: `FISSION_BATCHES` inactive batches, then
particles of the next batch through OpenMC's explicit ``<track>`` list. One tracked run
goes at a time: a new request (or `cancel`) ends the running one, whose
request then fails with ``cancelled``. Runs are small, so they do not wait for
a simulation and use at most `MAX_THREADS` threads.
"""

import contextlib
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pyvised_protocol.tracks import (
    FISSION_BATCHES,
    FISSION_PARTICLES,
    PointSource,
    TrackRequest,
)

from .config import Config
from .errors import ServiceError
from .models import ModelService
from .trackfile import CONTEXT_FILE, FRAME_FILE, RESULT_MARKER, TRACKS_FILE

log = logging.getLogger(__name__)

MAX_THREADS = 4
TIME_LIMIT = 120.0
"""[s] a tracked run (OpenMC and reading its tracks) may take."""
POLL = 0.05
LOG_FILE = "openmc.log"
WARNINGS_KEPT = 10
IGNORED_WARNINGS = (
    "Could not compute uncertainties",  # one active batch, as planned
    "Shared secondary bank is not supported",  # eigenvalue runs: OpenMC's own choice
)
REMOVED_SETTINGS = (
    "particles",
    "batches",
    "inactive",
    "generations_per_batch",
    "max_tracks",
    "track",
    "seed",
    "state_point",
    "source_point",
    "entropy_mesh",
    "ufs_mesh",
    "trigger",
    "keff_trigger",
    "output",
    "surf_source_write",
    "collision_track",
    "event_based",
)
"""Settings a tracked run sets itself or does without (files, statistics, event mode)."""


@dataclass(frozen=True)
class TrackPlan:
    run_mode: str
    photon_transport: bool
    weight_windows: bool
    all_tracks: bool
    """``openmc --track`` (every particle up to `max_tracks`) instead of a ``<track>`` list."""
    warnings: list[str] = field(default_factory=list)


def _text(parent: ET.Element, tag: str) -> str:
    return (parent.findtext(tag) or "").strip()


def _set(parent: ET.Element, tag: str, value: object) -> None:
    for child in parent.findall(tag):
        parent.remove(child)
    ET.SubElement(parent, tag).text = str(value)


def point_source_element(point: PointSource) -> ET.Element:
    source = ET.Element(
        "source", {"type": "independent", "strength": "1.0", "particle": point.particle}
    )
    space = ET.SubElement(source, "space", {"type": "point"})
    ET.SubElement(space, "parameters").text = " ".join(repr(float(v)) for v in point.position)
    ET.SubElement(source, "angle", {"type": "isotropic"})
    energy = ET.SubElement(source, "energy", {"type": "discrete"})
    ET.SubElement(energy, "parameters").text = f"{float(point.energy)!r} 1.0"
    return source


def tracked_ids(n: int, particles: int) -> list[int]:
    """`n` particle ids spread over a batch of `particles`: OpenMC gives consecutive ids
    copies of the same fission site when it has fewer sites than particles."""
    return [1 + (k * particles) // n for k in range(n)]


def patch_tracks_xml(model_xml: Path, request: TrackRequest, seed: int) -> TrackPlan:
    """Turn the copy `model_xml` into a tracked run of `request` (in place)."""
    tree = ET.parse(model_xml)
    root = tree.getroot()
    settings = root.find("settings") if root.tag == "model" else None
    if settings is None:
        raise ServiceError("bad_model", "the exported model has no <settings>")
    tallies = root.find("tallies")
    if tallies is not None:
        # keep the meshes: weight windows may refer to one written with the tallies
        for mesh in tallies.findall("mesh"):
            settings.append(mesh)
        root.remove(tallies)
    for plots in root.findall("plots"):
        root.remove(plots)
    for tag in REMOVED_SETTINGS:
        for child in settings.findall(tag):
            settings.remove(child)

    run_mode = _text(settings, "run_mode") or "eigenvalue"
    eigenvalue = run_mode == "eigenvalue"
    if request.start == "fission" and not eigenvalue:
        raise ServiceError(
            "bad_track_start",
            "Only eigenvalue models have a fission source; start from the model's source "
            "or a point source.",
        )
    warnings: list[str] = []
    n = request.histories
    if request.start == "fission":
        batches = FISSION_BATCHES + 1
        particles = max(n, FISSION_PARTICLES)
        _set(settings, "particles", particles)
        _set(settings, "batches", batches)
        _set(settings, "inactive", FISSION_BATCHES)
        _set(settings, "track", " ".join(f"{batches} 1 {i}" for i in tracked_ids(n, particles)))
    else:
        _set(settings, "particles", n)
        _set(settings, "batches", 1)
        if eigenvalue:
            # one generation as a fixed-source run: a fission ends the history, as in the
            # eigenvalue run, and a batch that banks no fission site is no error
            _set(settings, "run_mode", "fixed source")
            _set(settings, "create_fission_neutrons", "false")
    _set(settings, "max_tracks", n)
    _set(settings, "seed", seed)
    output = ET.SubElement(settings, "output")
    ET.SubElement(output, "summary").text = "false"
    ET.SubElement(output, "tallies").text = "false"

    photon = _text(settings, "photon_transport").lower() == "true"
    if request.photon_transport is not None:
        photon = request.photon_transport
    if request.start == "point":
        for source in settings.findall("source"):
            settings.remove(source)
        settings.append(point_source_element(request.point))
        if request.point.particle == "photon" and not photon:
            photon = True
            warnings.append("photon transport turned on for the photon source")
    _set(settings, "photon_transport", "true" if photon else "false")

    windows = _text(settings, "weight_windows_on").lower() == "true"
    windows = windows and request.weight_windows
    if settings.find("weight_windows_on") is not None or windows:
        _set(settings, "weight_windows_on", "true" if windows else "false")

    ET.indent(tree, space="  ")
    tree.write(model_xml, encoding="utf-8", xml_declaration=True)
    return TrackPlan(
        run_mode=run_mode,
        photon_transport=photon,
        weight_windows=windows,
        all_tracks=request.start != "fission",
        warnings=warnings,
    )


def openmc_messages(log_text: str) -> tuple[str, list[str]]:
    """OpenMC's ERROR block and its WARNING lines from a log."""
    lines = log_text.splitlines()
    error = ""
    for index, line in enumerate(lines):
        if "ERROR" in line:
            block = [line.strip()]
            for more in lines[index + 1 : index + 8]:
                if not more.startswith("        "):  # continuation lines are indented
                    break
                block.append(more.strip())
            error = " ".join(block)
            break
    warnings = []
    for line in lines:
        text = line.strip()
        message = text[len("WARNING:") :].strip()
        ignored = any(message.startswith(start) for start in IGNORED_WARNINGS)
        if text.startswith("WARNING:") and message not in warnings and not ignored:
            warnings.append(message)
    return error, warnings[:WARNINGS_KEPT]


class _Job:
    def __init__(self) -> None:
        self.cancelled = False
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def start(self, args: list[str], **kwargs) -> subprocess.Popen:
        with self.lock:
            if self.cancelled:
                raise ServiceError("cancelled", "The tracked run was cancelled.", status=409)
            self.process = subprocess.Popen(args, **kwargs)
            return self.process

    def cancel(self) -> None:
        with self.lock:
            self.cancelled = True
            if self.process is not None and self.process.poll() is None:
                with contextlib.suppress(OSError):
                    self.process.kill()


class TrackService:
    def __init__(
        self,
        config: Config,
        models: ModelService,
        *,
        command: Sequence[str] = ("openmc",),
        python: str = sys.executable,
        time_limit: float = TIME_LIMIT,
    ) -> None:
        self.config = config
        self.models = models
        self.command = list(command)
        self.python = python
        self.time_limit = time_limit
        self._run_lock = threading.Lock()
        self._lock = threading.Lock()
        self._job: _Job | None = None

    @property
    def root(self) -> Path:
        return self.config.cache_dir / "tracks"

    def generate(self, model_id: str, request: TrackRequest) -> bytes:
        """Run `request` on the opened model `model_id`; the answer is a track frame."""
        model = self.models.get(model_id)
        library = self.models.library(model.info.library)
        job = _Job()
        with self._lock:
            previous, self._job = self._job, job
        if previous is not None:
            previous.cancel()
        with self._run_lock:
            if job.cancelled:
                raise ServiceError("cancelled", "The tracked run was cancelled.", status=409)
            folder = self.root / uuid.uuid4().hex[:12]
            folder.mkdir(parents=True)
            try:
                return self._generate(job, folder, model.directory, model_id, request, library)
            finally:
                with self._lock:
                    if self._job is job:
                        self._job = None
                shutil.rmtree(folder, ignore_errors=True)

    def cancel(self) -> None:
        with self._lock:
            job = self._job
        if job is not None:
            job.cancel()

    def _generate(self, job, folder, source_dir, model_id, request, library) -> bytes:
        shutil.copyfile(source_dir / "model.xml", folder / "model.xml")
        seed = request.seed or secrets.randbelow(2**31 - 2) + 1
        plan = patch_tracks_xml(folder / "model.xml", request, seed)
        threads = min(MAX_THREADS, self.config.cpus)
        env = dict(os.environ, OMP_NUM_THREADS=str(threads))
        if library is not None:
            env["OPENMC_CROSS_SECTIONS"] = library.path
        args = [*self.command, *(["--track"] if plan.all_tracks else []), "-s", str(threads)]
        deadline = time.monotonic() + self.time_limit
        start = time.monotonic()
        with (folder / LOG_FILE).open("w", encoding="utf-8") as out:
            process = job.start(
                args, cwd=folder, env=env, stdout=out, stderr=subprocess.STDOUT, text=True
            )
            code = self._wait(job, process, deadline)
        seconds = time.monotonic() - start
        text = (folder / LOG_FILE).read_text(encoding="utf-8", errors="replace")
        error, warnings = openmc_messages(text)
        if code != 0 or not (folder / TRACKS_FILE).is_file():
            tail = "\n".join(text.strip().splitlines()[-12:])
            raise ServiceError(
                "tracks_failed", error or tail or f"OpenMC ended with exit code {code}"
            )
        context = {
            "model_id": model_id,
            "request": request.model_dump(mode="json"),
            "seed": seed,
            "run_mode": plan.run_mode,
            "photon_transport": plan.photon_transport,
            "weight_windows": plan.weight_windows,
            "seconds": seconds,
            "warnings": plan.warnings + warnings,
        }
        (folder / CONTEXT_FILE).write_text(json.dumps(context), encoding="utf-8")
        process = job.start(
            [self.python, "-m", "pyvised_backend.trackfile", str(folder)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
        code = self._wait(job, process, deadline)
        stdout, stderr = process.communicate()
        if code != 0 or RESULT_MARKER not in stdout:
            detail = "\n".join((stderr or stdout).strip().splitlines()[-12:])
            raise ServiceError("tracks_failed", f"The tracks could not be read.\n{detail}")
        log.info("tracked %d histories of %s in %.1f s", request.histories, model_id, seconds)
        return (folder / FRAME_FILE).read_bytes()

    def _wait(self, job: _Job, process: subprocess.Popen, deadline: float) -> int:
        while True:
            try:
                code = process.wait(POLL)
            except subprocess.TimeoutExpired:
                if time.monotonic() > deadline:
                    with contextlib.suppress(OSError):
                        process.kill()
                    process.wait()
                    raise ServiceError(
                        "tracks_timeout",
                        f"The tracked run took longer than {self.time_limit:.0f} s; "
                        "ask for fewer histories.",
                    ) from None
                continue
            if job.cancelled:
                raise ServiceError("cancelled", "The tracked run was cancelled.", status=409)
            return code
