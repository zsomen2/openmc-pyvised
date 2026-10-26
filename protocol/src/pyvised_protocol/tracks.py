"""Particle tracks: the request of a tracked mini-run, event codes and the track frame.

`POST /models/{id}/tracks` runs OpenMC for a few histories with track output
and answers with one frame. A history is everything one source particle
started: its own particle track and those of its split copies and secondary
particles. A particle track is a run of consecutive states; a state is where
the particle was born, crossed a surface, collided or ended, with the energy,
weight and cell it had from there on. The backend works out what happened at
each state (`TrackEvent`) and its event step: the index of the state in its
track, counted on from the parent's state for split copies and secondaries, so
that a playback in steps moves them along with their parent. Times come from
OpenMC (copies and secondaries inherit their parent's time).
"""

from dataclasses import dataclass
from enum import IntEnum
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from . import frames

MAX_HISTORIES = 2000
MAX_STATES = 500_000
"""States per answer at most; whole histories beyond it are dropped."""
FISSION_BATCHES = 20
"""Inactive batches before the tracked batch when tracks start from the fission source."""
FISSION_PARTICLES = 1000
"""Particles per batch (at least) while the fission source converges."""


class TrackEvent(IntEnum):
    """What happened at a state."""

    SOURCE = 0
    """Birth of the source particle of a history."""
    SECONDARY = 1
    """Birth of a secondary particle (e.g. a photon from a neutron capture)."""
    SPLIT = 2
    """Birth of a copy made by weight-window splitting."""
    CROSSING = 3
    """Crossed a surface or a lattice boundary."""
    COLLISION = 4
    """Collided (direction or energy changed)."""
    REFLECTION = 5
    """Reflected by a reflective or white boundary."""
    JUMP = 6
    """Moved by a periodic boundary: there is no flight from the previous state."""
    LEAKED = 7
    """Left the geometry through a vacuum boundary."""
    ABSORBED = 8
    """Absorbed (capture or fission; cannot be told apart from the track data)."""
    KILLED = 9
    """Collided, then killed by Russian roulette or the energy cutoff."""


BIRTH_EVENTS = frozenset({TrackEvent.SOURCE, TrackEvent.SECONDARY, TrackEvent.SPLIT})
END_EVENTS = frozenset({TrackEvent.LEAKED, TrackEvent.ABSORBED, TrackEvent.KILLED})

PARTICLE_CODES = {"neutron": 2112, "photon": 22, "electron": 11, "positron": -11}
"""PDG codes, as OpenMC 0.16 writes them."""
PARTICLE_NAMES = {code: name for name, code in PARTICLE_CODES.items()}


def particle_name(code: int) -> str:
    return PARTICLE_NAMES.get(int(code), str(int(code)))


TrackStart = Literal["model", "fission", "point"]
"""Where the histories start: the model's source, the fission source after
`FISSION_BATCHES` inactive batches (eigenvalue models), or a point source."""


class PointSource(BaseModel):
    """An isotropic, mono-energetic point source for demonstrations."""

    particle: Literal["neutron", "photon"] = "neutron"
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    """[cm]"""
    energy: float = Field(default=2.0e6, gt=0)
    """[eV]"""


class TrackRequest(BaseModel):
    """Body of `POST /models/{id}/tracks`."""

    histories: int = Field(default=100, gt=0, le=MAX_HISTORIES)
    start: TrackStart = "model"
    point: PointSource = Field(default_factory=PointSource)
    """The source when `start` is "point"."""
    seed: int | None = Field(default=None, gt=0)
    """None: the backend picks one (the summary says which)."""
    weight_windows: bool = True
    """Keep the model's weight windows (splitting and roulette) on."""
    photon_transport: bool | None = None
    """None keeps the model's setting; a photon point source always turns it on."""


class TrackSummary(BaseModel):
    """What the tracked run did; the meta of a track frame."""

    model_id: str = ""
    request: TrackRequest = Field(default_factory=TrackRequest)
    seed: int = 0
    run_mode: str = ""
    photon_transport: bool = False
    weight_windows: bool = False
    """Weight windows were on in the run."""
    histories: int = 0
    dropped: int = 0
    """Histories left out because the answer would exceed `MAX_STATES`."""
    tracks: int = 0
    states: int = 0
    particles: dict[str, int] = Field(default_factory=dict)
    """Particle tracks per particle name."""
    events: dict[str, int] = Field(default_factory=dict)
    """States per event name (`TrackEvent` names in lower case)."""
    max_step: int = 0
    energy_range: tuple[float, float] | None = None
    """Smallest positive and largest energy of the states [eV]."""
    time_range: tuple[float, float] | None = None
    """Smallest positive and largest time of the states [s]."""
    seconds: float = 0.0
    """Wall time of OpenMC [s]."""
    warnings: list[str] = Field(default_factory=list)

    def count(self, event: TrackEvent) -> int:
        return self.events.get(event.name.lower(), 0)


STATE_ARRAYS = (
    "position",
    "energy",
    "time",
    "weight",
    "cell",
    "instance",
    "material",
    "event",
    "step",
)
TRACK_ARRAYS = ("track_start", "track_history", "track_particle", "track_parent")
DTYPES = {
    "position": np.float32,
    "energy": np.float32,
    "time": np.float64,
    "weight": np.float32,
    "cell": np.int32,
    "instance": np.int32,
    "material": np.int32,
    "event": np.int8,
    "step": np.int32,
    "track_start": np.int64,
    "track_history": np.int32,
    "track_parent": np.int32,
    "track_particle": np.int32,
    "history_id": np.int64,
}


@dataclass
class TrackData:
    """The tracks of a tracked run: N states, M particle tracks, H histories."""

    position: np.ndarray
    """(N, 3) [cm]"""
    energy: np.ndarray
    """(N,) energy from this state on [eV] (before death at the last state)."""
    time: np.ndarray
    """(N,) [s] since the history started."""
    weight: np.ndarray
    """(N,) weight from this state on (the last state keeps the weight it died with)."""
    cell: np.ndarray
    instance: np.ndarray
    material: np.ndarray
    """(N,) the deepest cell, its instance and material (-1: void)."""
    event: np.ndarray
    """(N,) `TrackEvent` codes."""
    step: np.ndarray
    """(N,) event step in the history."""
    track_start: np.ndarray
    """(M + 1,) first state of each track; the tracks of a history are consecutive."""
    track_history: np.ndarray
    """(M,) history index (0-based)."""
    track_particle: np.ndarray
    """(M,) PDG code."""
    track_parent: np.ndarray
    """(M,) the track a split copy or secondary came from, -1 for source particles."""
    history_id: np.ndarray
    """(H,) OpenMC's particle id of each history."""
    summary: TrackSummary

    @property
    def n_states(self) -> int:
        return len(self.energy)

    @property
    def n_tracks(self) -> int:
        return len(self.track_history)

    @property
    def n_histories(self) -> int:
        return len(self.history_id)

    def state_track(self) -> np.ndarray:
        """(N,) the track of each state."""
        counts = np.diff(self.track_start)
        return np.repeat(np.arange(self.n_tracks, dtype=np.int32), counts)


def encode_tracks(data: TrackData) -> bytes:
    names = (*STATE_ARRAYS, *TRACK_ARRAYS, "history_id")
    arrays = {name: np.asarray(getattr(data, name), dtype=DTYPES[name]) for name in names}
    return frames.encode(arrays, data.summary.model_dump(mode="json"))


def decode_tracks(data: bytes) -> TrackData:
    frame = frames.decode(data)
    a = frame.arrays
    names = (*STATE_ARRAYS, *TRACK_ARRAYS, "history_id")
    return TrackData(
        **{name: a[name] for name in names},
        summary=TrackSummary.model_validate(frame.meta),
    )
