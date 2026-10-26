"""Simulation runs: settings, status, live progress, results and server-sent events.

A run lives in its own folder next to the model,
``<model folder>/runs/<YYYYMMDD-HHMMSS>[_label]/``. The folder holds the
patched copy of the exported model, OpenMC's output and PyVised's files:
`RUN_FILE` (`RunInfo`), `RESULTS_FILE` (`RunResults`, once a statepoint
exists) and `TALLIES_FILE` (the tally arrays). `INPUT_FOLDER` keeps copies of
the model's own code (its `.py`/`.ipynb` files) as they were when the run
started; runs made before it have none. A run id is the folder's
workspace-relative path in URL-safe base64, so the backend finds a run from
its id alone, also after a restart.

Live progress comes from OpenMC's log: one entry per generation (k, Shannon
entropy, running mean ± σ of k from the first active batch on). Events over
``GET /events`` carry only what is new; ``first`` tells where it belongs.
"""

import base64
import binascii
import json
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

RUNS_FOLDER = "runs"
RUN_FILE = "pyvised_run.json"
RESULTS_FILE = "pyvised_results.json"
TALLIES_FILE = "pyvised_tallies.npz"
LOG_FILE = "openmc.log"
INPUT_FOLDER = "input"

RunState = Literal["preparing", "running", "summarising", "finished", "stopped", "failed"]
FINAL_STATES: frozenset[str] = frozenset({"finished", "stopped", "failed"})
StopReason = Literal["completed", "triggers", "user", "time_limit", "interrupted", "error"]
"""Why a run ended: all batches done, triggers satisfied, Stop, the wall-time limit, the backend
went away during the run, or an error."""


def run_id_for(path: str) -> str:
    """Id of the run folder at the workspace-relative `path` (forward slashes)."""
    return base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")


def run_path_for(run_id: str) -> str:
    """Workspace-relative folder of `run_id`; raises ValueError for a malformed id."""
    try:
        padded = run_id + "=" * (-len(run_id) % 4)
        path = base64.b64decode(padded.encode(), altchars=b"-_", validate=True).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"not a run id: {run_id!r}") from exc
    if not path:
        raise ValueError("empty run id")
    return path


# -- requests --------------------------------------------------------------------------------


class RunSettings(BaseModel):
    """What a run changes in its copy of the model; None keeps the model's own value."""

    particles: int | None = Field(default=None, gt=0)
    batches: int | None = Field(default=None, gt=0)
    """Batches to run; with a trigger (`keff_std`, `tally_rel_err`) the most batches."""
    inactive: int | None = Field(default=None, ge=0)
    seed: int | None = Field(default=None, gt=0)
    threads: int | None = Field(default=None, gt=0)
    """OpenMP threads (``openmc -s``); None uses every CPU of the container."""
    time_limit: float | None = Field(default=None, gt=0)
    """[s] of wall time; the run then stops and keeps its latest statepoint."""
    keff_std: float | None = Field(default=None, gt=0)
    """Stop once σ(k-eff) is this small (OpenMC's k-eff trigger)."""
    tally_rel_err: float | None = Field(default=None, gt=0)
    """Stop once every tally bin's relative error is this small (OpenMC's tally triggers)."""
    statepoint_every: int | None = Field(default=None, gt=0)
    """Batches between statepoints; None: about 5 % of the batches."""
    entropy_mesh: bool = True
    """Add a Shannon entropy mesh over the bounding box to eigenvalue runs that have none."""


class RunRequest(BaseModel):
    """Body of `POST /runs`: the model is opened (exported) with `params` and `library` first."""

    path: str
    """Workspace-relative model source, as for `POST /models/open`."""
    library: str | None = None
    params: dict[str, float] = Field(default_factory=dict)
    label: str = Field(default="", max_length=60)
    settings: RunSettings = Field(default_factory=RunSettings)
    created: datetime | None = None
    """The client's local time; it names the run folder (YYYYMMDD-HHMMSS). The backend's
    clock (UTC) is used without it."""


# -- status -----------------------------------------------------------------------------------


class RunPlan(BaseModel):
    """The settings the run's copy of the model ended up with."""

    run_mode: str
    particles: int
    batches: int
    """Planned batches; with triggers the most batches."""
    inactive: int = 0
    generations_per_batch: int = 1
    first_check: int | None = None
    """With triggers: the batch of the first trigger check (OpenMC's `batches`)."""
    seed: int | None = None
    threads: int
    statepoints: list[int] = Field(default_factory=list)
    entropy: bool = False
    entropy_added: bool = False
    photon_transport: bool = False
    triggers: list[str] = Field(default_factory=list)


class RunProgress(BaseModel):
    """Live state of a run: the whole history (`first` = 0) or what is new since the last event.

    The lists hold one entry per generation; `mean` and `std` are OpenMC's running average of
    k (None during the inactive batches).
    """

    first: int = 0
    k: list[float] = Field(default_factory=list)
    entropy: list[float] = Field(default_factory=list)
    mean: list[float | None] = Field(default_factory=list)
    std: list[float | None] = Field(default_factory=list)
    batch: int = 0
    """Batches finished."""
    batches: int = 0
    inactive: int = 0
    generations_per_batch: int = 1
    elapsed: float = 0.0
    """[s] since OpenMC started."""
    eta: float | None = None
    """[s] still needed for the planned batches."""
    rate: float | None = None
    """Particles per second over the last batches."""
    statepoint: int | None = None
    """Batch of the latest statepoint written."""
    trigger: str = ""
    """OpenMC's last trigger message."""
    phase: str = ""
    """What OpenMC does now, e.g. "Reading cross sections" or "active batches"."""


class RunInfo(BaseModel):
    """A run as `RUN_FILE` stores it and the API reports it."""

    id: str
    path: str
    """Workspace-relative run folder."""
    model: str
    """Workspace-relative model source."""
    label: str = ""
    state: RunState
    stop_reason: StopReason | None = None
    created: datetime
    started: datetime | None = None
    finished: datetime | None = None
    request: RunSettings = Field(default_factory=RunSettings)
    plan: RunPlan | None = None
    params: dict[str, float] = Field(default_factory=dict)
    library: str | None = None
    library_name: str = ""
    model_id: str = ""
    versions: dict[str, str] = Field(default_factory=dict)
    exit_code: int | None = None
    error: str = ""
    """OpenMC's ERROR block, or why the run could not start."""
    warnings: list[str] = Field(default_factory=list)
    batches_done: int = 0
    seconds: float = 0.0
    """Wall time of the OpenMC process [s]."""
    k_eff: float | None = None
    k_std: float | None = None
    has_results: bool = False

    @property
    def active(self) -> bool:
        return self.state not in FINAL_STATES


class RunStatus(BaseModel):
    """Answer of `GET /runs/{id}`: the run and, while it is the active run, its progress."""

    run: RunInfo
    progress: RunProgress | None = None


class LogChunk(BaseModel):
    """Answer of `GET /runs/{id}/log`: the log text from `offset` (bytes) on."""

    offset: int
    text: str
    next: int
    """Offset to ask for next time."""
    size: int


# -- results ----------------------------------------------------------------------------------


class Estimate(BaseModel):
    value: float
    std: float | None = None
    """None without an estimate (fewer than two realizations)."""


class KeffResults(BaseModel):
    combined: Estimate | None = None
    collision: Estimate | None = None
    track_length: Estimate | None = None
    absorption: Estimate | None = None


class MeshInfo(BaseModel):
    id: int
    kind: str
    """"regular", "rectilinear", "cylindrical" or "spherical"."""
    dimension: list[int] = Field(default_factory=list)
    lower_left: list[float] | None = None
    upper_right: list[float] | None = None
    grids: list[list[float]] = Field(default_factory=list)
    """Bin edges along the three axes (x, y, z; r, φ, z; r, θ, φ)."""
    origin: list[float] | None = None


class TallyFilterResult(BaseModel):
    kind: str
    """OpenMC's filter type, e.g. "cell", "surface", "energy", "mesh", "energyfunction"."""
    bins: int
    ids: list[int] = Field(default_factory=list)
    """Ids of cell, surface, material, universe … bins."""
    edges: list[float] = Field(default_factory=list)
    """Bin edges of energy (eV), time … filters."""
    labels: list[str] = Field(default_factory=list)
    """Other bins, e.g. particle names."""
    mesh: MeshInfo | None = None
    summary: str = ""


class TallyResult(BaseModel):
    """A tally of the results; its arrays come from `GET /runs/{id}/tallies/{tid}`.

    The arrays have the shape (bins of each filter …, nuclides, scores); the last filter
    varies fastest, and mesh bins run with the first mesh index fastest.
    """

    id: int
    name: str = ""
    scores: list[str] = Field(default_factory=list)
    nuclides: list[str] = Field(default_factory=list)
    filters: list[TallyFilterResult] = Field(default_factory=list)
    shape: list[int] = Field(default_factory=list)
    realizations: int = 0
    estimator: str | None = None
    max_rel_err: float | None = None
    """Largest relative error among the bins with a result."""
    zero_bins: int = 0
    totals: list[Estimate] = Field(default_factory=list)
    """Per (nuclide, score), in that order: the sum over all filter bins; its σ treats the bins
    as independent."""


class Convergence(BaseModel):
    """A quantity at each statepoint the run wrote."""

    batches: list[int] = Field(default_factory=list)
    seconds: list[float] = Field(default_factory=list)
    """[s] of active batches up to each statepoint (for the figure of merit)."""
    mean: list[list[float | None]] = Field(default_factory=list)
    std: list[list[float | None]] = Field(default_factory=list)
    """None where a statepoint had no estimate (no realizations yet, or only one)."""
    binned: bool = True
    """True: one value per bin; False: the totals per (nuclide, score) of a large tally."""


class RunResults(BaseModel):
    """Summary of a run's latest statepoint (`RESULTS_FILE`)."""

    run_id: str
    statepoint: str
    run_mode: str
    batches: int
    """Batches in the statepoint."""
    inactive: int = 0
    particles: int = 0
    generations_per_batch: int = 1
    realizations: int = 0
    keff: KeffResults | None = None
    leakage: Estimate | None = None
    k_generation: list[float] = Field(default_factory=list)
    entropy: list[float] = Field(default_factory=list)
    runtime: dict[str, float] = Field(default_factory=dict)
    """OpenMC's timers [s], e.g. "active batches", "total"."""
    rate_active: float | None = None
    rate_inactive: float | None = None
    """Particles per second."""
    tallies: list[TallyResult] = Field(default_factory=list)
    convergence: dict[int, Convergence] = Field(default_factory=dict)
    """Per tally id."""
    keff_convergence: Convergence | None = None
    warnings: list[str] = Field(default_factory=list)

    @property
    def active_seconds(self) -> float | None:
        return self.runtime.get("active batches")


# -- events -----------------------------------------------------------------------------------

EventType = Literal["run.state", "run.progress", "run.log", "run.finished", "backend.warning"]


class Event(BaseModel):
    """One server-sent event: `data` is a `RunInfo` (state, finished), a `RunProgress`
    (progress) or ``{"text", "offset"}`` (log)."""

    type: str
    run_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


def format_sse(event: Event) -> bytes:
    data = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
    return f"event: {event.type}\ndata: {data}\n\n".encode()


def parse_sse(lines: Iterable[str]) -> Iterator[Event]:
    """Events from the lines of an event stream (comments and unknown fields are skipped)."""
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data:
                yield Event.model_validate_json("\n".join(data))
                data = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if field == "data":
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        yield Event.model_validate_json("\n".join(data))
