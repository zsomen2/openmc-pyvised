"""Events, parents and steps derived from synthetic tracks in OpenMC's layout."""

import json
from pathlib import Path

import numpy as np
import pytest

from pyvised_backend import trackfile
from pyvised_backend.trackfile import (
    Boundary,
    classify,
    on_surface,
    quadric,
    raw_tracks,
    read_boundaries,
    track_data,
)
from pyvised_protocol.tracks import TrackEvent, decode_tracks

E = TrackEvent
VECTOR = np.dtype([("x", "<f8"), ("y", "<f8"), ("z", "<f8")])
STATE = np.dtype(
    [
        ("r", VECTOR),
        ("u", VECTOR),
        ("E", "<f8"),
        ("time", "<f8"),
        ("wgt", "<f8"),
        ("cell_id", "<i4"),
        ("cell_instance", "<i4"),
        ("material_id", "<i4"),
    ]
)


def states(rows: list[tuple]) -> np.ndarray:
    """rows of (r, u, E[, time]) -> OpenMC's state records (weight 1, cell 1)."""
    out = np.zeros(len(rows), dtype=STATE)
    for i, row in enumerate(rows):
        r, u, energy = row[:3]
        out[i]["r"] = tuple(float(v) for v in r)
        out[i]["u"] = tuple(float(v) for v in u)
        out[i]["E"] = energy
        out[i]["time"] = row[3] if len(row) > 3 else 1e-9 * i
        out[i]["wgt"] = 1.0
        out[i]["cell_id"] = 1
        out[i]["material_id"] = 1
    out[-1]["wgt"] = 0.0
    return out


X, Y, Z, MY = (1, 0, 0), (0, 1, 0), (0, 0, 1), (0, -1, 0)
NEUTRON = [
    ((0, 0, 0), X, 2e6),  # source
    ((2, 0, 0), X, 2e6),  # crossing
    ((4, 0, 0), MY, 1e6),  # collision, splits here
    ((4, -5, 0), Y, 1e6),  # reflected by y = -5
    ((4, -1, 0), Y, 1e6),  # absorbed
]
COPY = [
    ((4, 0, 0), MY, 1e6),  # split copy of the neutron's collision state
    ((4, -2, 0), X, 5e5),  # collision
    ((10, -2, 0), X, 5e5),  # leaks through x = 10
]
PHOTON = [((4, -1, 0), Z, 2.2e6), ((4, -1, 3), Z, 0.0)]  # capture photon, absorbed (E = 0)
SECOND = [
    ((0, 0, 0), Z, 1e6),  # source
    ((0, 0, 1), Y, 5e5),  # collision
    ((5, 7, 1), Y, 5e5),  # moved by a periodic boundary
    ((5, 8, 1), X, 3e5),  # collided, then killed
]
BOUNDARIES = [
    Boundary("vacuum", quadric("x-plane", [10.0])),  # type: ignore[arg-type]
    Boundary("reflective", quadric("y-plane", [-5.0])),  # type: ignore[arg-type]
]


def history(particle_id: int, *tracks: tuple[int, list]) -> tuple:
    records = np.concatenate([states(rows) for _, rows in tracks])
    offsets = np.cumsum([0] + [len(rows) for _, rows in tracks])
    return particle_id, records, offsets, np.array([code for code, _ in tracks])


def sample():
    return raw_tracks(
        [
            history(1, (2112, NEUTRON), (2112, COPY), (22, PHOTON)),
            history(2, (2112, SECOND)),
        ]
    )


def test_events_parents_and_steps():
    raw = sample()
    event, step, parent = classify(raw, BOUNDARIES)
    assert [E(e) for e in event] == [
        E.SOURCE, E.CROSSING, E.COLLISION, E.REFLECTION, E.ABSORBED,
        E.SPLIT, E.COLLISION, E.LEAKED,
        E.SECONDARY, E.ABSORBED,
        E.SOURCE, E.COLLISION, E.JUMP, E.KILLED,
    ]  # fmt: skip
    assert parent.tolist() == [-1, 0, 0, -1]
    # copies and secondaries count on from their parent's state
    assert step.tolist() == [0, 1, 2, 3, 4, 2, 3, 4, 4, 5, 0, 1, 2, 3]


def test_track_data_keeps_the_weight_at_death_and_counts():
    data = track_data(sample(), BOUNDARIES, {"model_id": "m1", "seed": 3})
    assert data.weight[4] == 1.0 and data.weight[7] == 1.0  # OpenMC writes 0 there
    summary = data.summary
    assert summary.model_id == "m1" and summary.seed == 3
    assert summary.histories == 2 and summary.tracks == 4 and summary.states == 14
    assert summary.particles == {"neutron": 3, "photon": 1}
    assert summary.count(E.SPLIT) == 1 and summary.count(E.LEAKED) == 1
    assert summary.count(E.ABSORBED) == 2 and summary.max_step == 5
    assert summary.energy_range == (3e5, 2.2e6)
    assert data.history_id.tolist() == [1, 2] and data.track_history.tolist() == [0, 0, 0, 1]


def test_times_count_from_the_start_of_each_history():
    later = [((0, 0, 0), X, 2e6, 1.5e-3), ((2, 0, 0), X, 2e6, 1.5e-3 + 1e-9)]
    raw = raw_tracks([history(1, (2112, NEUTRON)), history(2, (2112, later))])
    data = track_data(raw, BOUNDARIES, {})
    assert data.time[5:].tolist() == pytest.approx([0.0, 1e-9])  # born at 1.5 ms (fission)
    assert data.time[1] == pytest.approx(1e-9)


def test_a_secondary_without_a_matching_parent_state_starts_at_step_zero():
    orphan = [((9, 9, 9), Z, 1e5), ((9, 9, 8), Z, 1e5)]
    raw = raw_tracks([history(1, (2112, NEUTRON), (22, orphan))])
    event, step, parent = classify(raw, BOUNDARIES)
    assert E(event[5]) is E.SECONDARY and parent.tolist() == [-1, -1]
    assert step[5:].tolist() == [0, 1]


@pytest.mark.parametrize(
    ("kind", "coefficients", "on", "off"),
    [
        ("x-plane", [2.0], (2, 5, -1), (2.1, 0, 0)),
        ("plane", [1, 1, 0, 2.0], (1, 1, 7), (1, 1.5, 0)),
        ("z-cylinder", [1, 2, 3.0], (4, 2, 9), (1, 2, 0)),
        ("x-cylinder", [0, 0, 120.0], (5, 120, 0), (5, 119.9, 0)),
        ("sphere", [0, 0, 1, 10.0], (0, 0, 11), (0, 0, 10)),
        ("z-cone", [0, 0, 1, 0.25], (1, 0, 3), (1, 0, 2)),
        ("quadric", [1, 1, 1, 0, 0, 0, 0, 0, 0, -4.0], (0, 2, 0), (0, 0, 0)),
    ],
)
def test_points_on_quadric_surfaces(kind, coefficients, on, off):
    q = quadric(kind, coefficients)
    assert q is not None
    assert on_surface(np.array([on, off], dtype=float), q).tolist() == [True, False]


def test_tori_are_no_quadrics():
    assert quadric("z-torus", [0, 0, 0, 5, 1, 1]) is None


def test_boundaries_from_model_xml(tmp_path: Path):
    path = tmp_path / "model.xml"
    path.write_text(
        """<model><geometry>
        <surface id="1" type="sphere" boundary="vacuum" coeffs="0 0 0 10"/>
        <surface id="2" type="x-plane" boundary="reflective" coeffs="-5"/>
        <surface id="3" type="z-cylinder" coeffs="0 0 1"/>
        <surface id="4" type="z-torus" boundary="vacuum" coeffs="0 0 0 5 1 1"/>
        </geometry></model>""",
        encoding="utf-8",
    )
    found = read_boundaries(path)
    assert [b.kind for b in found] == ["vacuum", "reflective"]


def test_reading_an_openmc_track_file(tmp_path: Path):
    h5py = pytest.importorskip("h5py")
    with h5py.File(tmp_path / "tracks.h5", "w") as f:
        f.attrs["filetype"] = np.bytes_(b"track")
        for particle_id, records, offsets, codes in (
            history(10, (2112, SECOND)),
            history(2, (2112, NEUTRON), (2112, COPY), (22, PHOTON)),
        ):
            dataset = f.create_dataset(f"track_1_1_{particle_id}", data=records)
            dataset.attrs["n_particles"] = len(codes)
            dataset.attrs["offsets"] = offsets
            dataset.attrs["particles"] = codes
    raw = trackfile.read_tracks(tmp_path / "tracks.h5")
    assert raw.history_id.tolist() == [2, 10]  # particle-id order, not name order
    assert raw.track_start.tolist() == [0, 5, 8, 10, 14] and raw.dropped == 0
    small = trackfile.read_tracks(tmp_path / "tracks.h5", max_states=12)
    assert small.history_id.tolist() == [2] and small.dropped == 1

    (tmp_path / "model.xml").write_text(
        '<model><geometry><surface id="1" type="x-plane" boundary="vacuum" coeffs="10"/>'
        "</geometry></model>",
        encoding="utf-8",
    )
    (tmp_path / trackfile.CONTEXT_FILE).write_text(json.dumps({"seed": 5}), encoding="utf-8")
    assert trackfile.main([str(tmp_path)]) == 0
    data = decode_tracks((tmp_path / trackfile.FRAME_FILE).read_bytes())
    assert data.summary.seed == 5 and data.summary.count(E.LEAKED) == 1
