"""Reading a tracked run: ``python -m pyvised_backend.trackfile RUN_DIR``.

Runs in a subprocess, because h5py may crash on a damaged file. It reads
OpenMC's `TRACKS_FILE` and the run's context (`CONTEXT_FILE`, written by the
track service), keeps whole histories up to `MAX_STATES`, works out the event
at every state and the parent of every split copy and secondary, and writes
the answer frame to `FRAME_FILE`; the summary follows `RESULT_MARKER` on stdout.

OpenMC writes no event types. A state is written at birth, before every flight
(so after each collision and each surface or lattice crossing) and at death,
and the rules follow from that:

- an inner state with the direction and energy of the previous one is a
  crossing, a changed one a collision, or a reflection when only the direction
  changed on a reflective or white boundary; a state that was not reached along
  the previous direction was moved by a periodic boundary;
- the last state is a leak on a vacuum boundary, an absorption when nothing
  changed (or the energy is 0: photons), else a collision followed by Russian
  roulette or the energy cutoff;
- a split copy or a secondary starts exactly at a state of an earlier track of
  its history, which is its parent; a copy of the same particle with the
  parent's energy and direction is a split.

Boundaries are evaluated in global coordinates as quadrics (tori are skipped),
which suits boundaries of the root universe. Times count from the start of each
history: source particles of a later generation carry the time of the fission
that made them.
"""

import itertools
import json
import sys
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from pyvised_protocol.tracks import (
    MAX_STATES,
    TrackData,
    TrackEvent,
    TrackSummary,
    encode_tracks,
    particle_name,
)

TRACKS_FILE = "tracks.h5"
CONTEXT_FILE = "pyvised_tracks.json"
FRAME_FILE = "tracks.pva"
RESULT_MARKER = "PYVISED_TRACKS "
SURFACE_TOLERANCE = 1e-6
"""[cm] a point this close to a surface lies on it."""
PARALLEL_TOLERANCE = 1e-6
"""1 - cos of the angle between a flight and the direction that counts as parallel."""


# -- boundaries ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Boundary:
    kind: str
    """OpenMC's boundary type: "vacuum", "reflective", "white" or "periodic"."""
    quadric: np.ndarray
    """A B C D E F G H J K of A x² + B y² + C z² + D xy + E yz + F xz + G x + H y + J z + K."""


def quadric(surface_type: str, c: list[float]) -> np.ndarray | None:
    """Quadric coefficients of an OpenMC surface, None for the types that are no quadric."""
    q = np.zeros(10)
    if surface_type in ("x-plane", "y-plane", "z-plane"):
        q[6 + "xyz".index(surface_type[0])] = 1.0
        q[9] = -c[0]
    elif surface_type == "plane":
        q[6:9] = c[:3]
        q[9] = -c[3]
    elif surface_type in ("x-cylinder", "y-cylinder", "z-cylinder"):
        axis = "xyz".index(surface_type[0])
        others = [i for i in range(3) if i != axis]
        for i, centre in zip(others, c[:2], strict=True):
            q[i] = 1.0
            q[6 + i] = -2.0 * centre
        q[9] = c[0] ** 2 + c[1] ** 2 - c[2] ** 2
    elif surface_type == "sphere":
        q[0:3] = 1.0
        q[6:9] = [-2.0 * v for v in c[:3]]
        q[9] = c[0] ** 2 + c[1] ** 2 + c[2] ** 2 - c[3] ** 2
    elif surface_type in ("x-cone", "y-cone", "z-cone"):
        axis = "xyz".index(surface_type[0])
        r2 = c[3]
        for i in range(3):
            scale = -r2 if i == axis else 1.0
            q[i] = scale
            q[6 + i] = -2.0 * scale * c[i]
        q[9] = sum((-r2 if i == axis else 1.0) * c[i] ** 2 for i in range(3))
    elif surface_type == "quadric":
        q[:] = c[:10]
    else:
        return None
    return q


def on_surface(points: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Which of the (n, 3) `points` lie on the quadric `q` (distance ≈ |f| / |∇f|)."""
    x, y, z = points[:, 0], points[:, 1], points[:, 2]
    a, b, c, d, e, f, g, h, j, k = q
    value = a * x * x + b * y * y + c * z * z + d * x * y + e * y * z + f * x * z
    value += g * x + h * y + j * z + k
    gx = 2 * a * x + d * y + f * z + g
    gy = 2 * b * y + d * x + e * z + h
    gz = 2 * c * z + e * y + f * x + j
    gradient = np.sqrt(gx * gx + gy * gy + gz * gz)
    return (gradient > 0) & (np.abs(value) <= SURFACE_TOLERANCE * gradient)


def read_boundaries(model_xml: Path) -> list[Boundary]:
    """The surfaces of a model with a boundary condition."""
    root = ET.parse(model_xml).getroot()
    geometry = root if root.tag == "geometry" else root.find("geometry")
    found = []
    for surface in [] if geometry is None else geometry.iter("surface"):
        kind = (surface.get("boundary") or "transmission").strip()
        if kind == "transmission":
            continue
        text = surface.get("coeffs") or surface.get("coefficients") or ""
        try:
            coefficients = [float(v) for v in text.split()]
            q = quadric((surface.get("type") or "").strip(), coefficients)
        except (ValueError, IndexError):
            continue
        if q is not None:
            found.append(Boundary(kind, q))
    return found


def on_boundary(points: np.ndarray, boundaries: list[Boundary], kinds: set[str]) -> np.ndarray:
    hit = np.zeros(len(points), dtype=bool)
    for boundary in boundaries:
        if boundary.kind in kinds and len(points):
            hit |= on_surface(points, boundary.quadric)
    return hit


# -- reading -----------------------------------------------------------------------------------


@dataclass
class RawTracks:
    """The states of the kept histories in file order, as OpenMC wrote them."""

    r: np.ndarray
    u: np.ndarray
    energy: np.ndarray
    time: np.ndarray
    weight: np.ndarray
    cell: np.ndarray
    instance: np.ndarray
    material: np.ndarray
    track_start: np.ndarray
    track_particle: np.ndarray
    track_history: np.ndarray
    history_id: np.ndarray
    dropped: int = 0


def history_key(name: str) -> tuple[int, ...]:
    """(batch, generation, particle id) of a dataset named ``track_<b>_<g>_<id>``."""
    return tuple(int(part) for part in name.split("_")[1:])


def raw_tracks(chunks: list[tuple[int, np.ndarray, np.ndarray, np.ndarray]], dropped: int = 0):
    """Join (particle id, states, offsets, particle codes) of each history into `RawTracks`."""
    starts = [0]
    particles: list[int] = []
    histories: list[int] = []
    total = 0
    for index, (_id, states, offsets, codes) in enumerate(chunks):
        for i in range(len(offsets) - 1):
            starts.append(total + int(offsets[i + 1]))
            particles.append(int(codes[i]))
            histories.append(index)
        total += len(states)
    states = np.concatenate([c[1] for c in chunks])

    def vectors(name: str) -> np.ndarray:
        field = states[name]
        return np.stack([field["x"], field["y"], field["z"]], axis=1).astype(np.float64)

    return RawTracks(
        r=vectors("r"),
        u=vectors("u"),
        energy=states["E"].astype(np.float64),
        time=states["time"].astype(np.float64),
        weight=states["wgt"].astype(np.float64),
        cell=states["cell_id"].astype(np.int32),
        instance=states["cell_instance"].astype(np.int32),
        material=states["material_id"].astype(np.int32),
        track_start=np.array(starts, dtype=np.int64),
        track_particle=np.array(particles, dtype=np.int32),
        track_history=np.array(histories, dtype=np.int32),
        history_id=np.array([c[0] for c in chunks], dtype=np.int64),
        dropped=dropped,
    )


def read_tracks(path: Path, max_states: int = MAX_STATES) -> RawTracks:
    """The histories of a `tracks.h5`, in particle-id order, up to `max_states` states."""
    import h5py  # pyright: ignore[reportMissingImports]  (container only)

    chunks = []
    total = 0
    dropped = 0
    with h5py.File(path, "r") as f:
        names = sorted((k for k in f if k.startswith("track_")), key=history_key)
        for index, name in enumerate(names):
            dataset = f[name]
            if chunks and total + dataset.shape[0] > max_states:
                dropped = len(names) - index
                break
            states = dataset[()]
            offsets = np.asarray(dataset.attrs["offsets"], dtype=np.int64)
            codes = np.asarray(dataset.attrs["particles"]).astype(np.int64)
            chunks.append((history_key(name)[-1], states, offsets, codes))
            total += len(states)
    if not chunks:
        raise ValueError("the track file holds no tracks")
    return raw_tracks(chunks, dropped)


# -- events ----------------------------------------------------------------------------------


def classify(raw: RawTracks, boundaries: list[Boundary]) -> tuple[np.ndarray, ...]:
    """(event, step, parent) of the states and tracks of `raw`."""
    n = len(raw.energy)
    m = len(raw.track_particle)
    starts = raw.track_start
    first, last = starts[:-1], starts[1:] - 1
    is_first = np.zeros(n, dtype=bool)
    is_first[first] = True
    is_last = np.zeros(n, dtype=bool)
    is_last[last] = True
    event = np.full(n, TrackEvent.CROSSING, dtype=np.int8)

    later = np.flatnonzero(~is_first)  # states with a previous state in their track
    prev = later - 1
    same_u = np.all(raw.u[later] == raw.u[prev], axis=1)
    same_e = raw.energy[later] == raw.energy[prev]
    same = same_u & same_e
    inner = ~is_last[later]
    event[later[inner & ~same]] = TrackEvent.COLLISION
    turned = later[inner & ~same_u & same_e]
    reflected = on_boundary(raw.r[turned], boundaries, {"reflective", "white"})
    event[turned[reflected]] = TrackEvent.REFLECTION
    flight = raw.r[later] - raw.r[prev]
    length = np.linalg.norm(flight, axis=1)
    moved = length > 0
    cos = np.ones(len(later))
    cos[moved] = np.einsum("ij,ij->i", flight[moved], raw.u[prev[moved]]) / length[moved]
    event[later[inner & moved & (cos < 1.0 - PARALLEL_TOLERANCE)]] = TrackEvent.JUMP

    ends = later[~inner]
    leaked = on_boundary(raw.r[ends], boundaries, {"vacuum"})
    absorbed = ~leaked & (same[~inner] | (raw.energy[ends] == 0))
    event[ends[leaked]] = TrackEvent.LEAKED
    event[ends[absorbed]] = TrackEvent.ABSORBED
    event[ends[~leaked & ~absorbed]] = TrackEvent.KILLED

    counts = np.diff(starts)
    step = np.arange(n, dtype=np.int64) - np.repeat(first, counts)
    parent = np.full(m, -1, dtype=np.int32)
    event[first] = TrackEvent.SECONDARY
    history_first = np.flatnonzero(np.r_[True, raw.track_history[1:] != raw.track_history[:-1]])
    event[first[history_first]] = TrackEvent.SOURCE
    keys = np.ascontiguousarray(raw.r).view(np.dtype((np.void, 24))).ravel()
    bounds = np.r_[history_first, m]
    for t0, t1 in itertools.pairwise(bounds):
        if t1 - t0 > 1:
            _link_history(raw, int(t0), int(t1), keys, event, step, parent)
    return event, step.astype(np.int32), parent


def _link_history(
    raw: RawTracks,
    t0: int,
    t1: int,
    keys: np.ndarray,
    event: np.ndarray,
    step: np.ndarray,
    parent: np.ndarray,
) -> None:
    """Parents, splits and steps of the tracks t0 … t1-1 of one history (parents come first:
    OpenMC follows the secondaries once the particle that made them is done)."""
    starts = raw.track_start
    s0 = int(starts[t0])
    rows = keys[s0 : int(starts[t1])].tolist()  # positions as bytes
    inner: dict[bytes, int] = {}
    births: dict[bytes, int] = {}
    for k in range(t0, t1):
        a, b = int(starts[k]) - s0, int(starts[k + 1]) - s0
        if k > t0:
            found = inner.get(rows[a], births.get(rows[a], -1))
            if found >= 0:
                at, birth = s0 + found, s0 + a
                parent[k] = int(np.searchsorted(starts, at, side="right")) - 1
                step[birth : s0 + b] += step[at]
                if (
                    raw.track_particle[k] == raw.track_particle[parent[k]]
                    and raw.energy[birth] == raw.energy[at]
                    and np.array_equal(raw.u[birth], raw.u[at])
                ):
                    event[birth] = TrackEvent.SPLIT
        births.setdefault(rows[a], a)
        for i in range(a + 1, b):
            if rows[i] not in inner:
                inner[rows[i]] = i


# -- the answer --------------------------------------------------------------------------------


def track_data(raw: RawTracks, boundaries: list[Boundary], context: dict) -> TrackData:
    event, step, parent = classify(raw, boundaries)
    weight = raw.weight.copy()
    starts = raw.track_start
    long = np.diff(starts) > 1
    last = starts[1:][long] - 1
    weight[last] = weight[last - 1]  # OpenMC writes 0 at death
    history_first = np.flatnonzero(np.r_[True, raw.track_history[1:] != raw.track_history[:-1]])
    born = raw.time[starts[history_first]]  # the time of each history's source particle
    state_history = np.repeat(raw.track_history, np.diff(starts))
    time = raw.time - born[state_history]
    names, counts = np.unique(raw.track_particle, return_counts=True)
    kinds = np.bincount(event, minlength=len(TrackEvent))
    positive_e = raw.energy[raw.energy > 0]
    positive_t = time[time > 0]
    summary = TrackSummary.model_validate(
        {
            **context,
            "histories": len(raw.history_id),
            "dropped": raw.dropped,
            "tracks": len(raw.track_particle),
            "states": len(raw.energy),
            "particles": {particle_name(c): int(n) for c, n in zip(names, counts, strict=True)},
            "events": {e.name.lower(): int(kinds[e]) for e in TrackEvent if kinds[e]},
            "max_step": int(step.max(initial=0)),
            "energy_range": (float(positive_e.min()), float(positive_e.max()))
            if positive_e.size
            else None,
            "time_range": (float(positive_t.min()), float(positive_t.max()))
            if positive_t.size
            else None,
        }
    )
    return TrackData(
        position=raw.r,
        energy=raw.energy,
        time=time,
        weight=weight,
        cell=raw.cell,
        instance=raw.instance,
        material=raw.material,
        event=event,
        step=step,
        track_start=raw.track_start,
        track_history=raw.track_history,
        track_particle=raw.track_particle,
        track_parent=parent,
        history_id=raw.history_id,
        summary=summary,
    )


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m pyvised_backend.trackfile RUN_DIR", file=sys.stderr)
        return 2
    folder = Path(argv[0])
    try:
        context = json.loads((folder / CONTEXT_FILE).read_text(encoding="utf-8"))
        raw = read_tracks(folder / TRACKS_FILE)
        data = track_data(raw, read_boundaries(folder / "model.xml"), context)
        (folder / FRAME_FILE).write_bytes(encode_tracks(data))
    except Exception:
        traceback.print_exc()
        return 1
    print(RESULT_MARKER + data.summary.model_dump_json(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
