import math

import numpy as np
import pytest
from track_fakes import sample_tracks

from pyvised.render.tracks import (
    ENERGY_BINS,
    PARTICLE_COLORS,
    MarkerGroup,
    Timeline,
    TrackColor,
    TrackFilter,
    TrackScene,
    TrackStyle,
    key_at,
    nearest_state,
    position_of,
    view_frame,
)

XY = (0, 1, 2)
XZ = (0, 2, 1)
LINES = TrackStyle(lines=True, line_color=TrackColor.FIXED, particle_color=TrackColor.FIXED)


def pairs(frame) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    out = []
    for _rgb, uv in frame.lines:
        for k in range(0, len(uv), 2):
            out.append((tuple(uv[k].round(6).tolist()), tuple(uv[k + 1].round(6).tolist())))
    return sorted(out)


def heads(frame) -> list[tuple[float, float]]:
    return sorted(tuple(p.round(6).tolist()) for _rgb, uv in frame.heads for p in uv)


def shapes(frame) -> dict[str, int]:
    out: dict[str, int] = {}
    for shape, _rgb, uv in frame.points:
        out[shape] = out.get(shape, 0) + len(uv)
    return out


@pytest.fixture
def scene() -> TrackScene:
    return TrackScene(sample_tracks())


def test_everything_at_the_end(scene: TrackScene):
    frame = view_frame(scene, LINES, TrackFilter(), XY, 0.0)
    assert frame.segments == 5 and frame.heads == []
    assert len(frame.states) == 8
    # births as rings, collisions as dots, the three ends; no crossings by default
    assert shapes(frame) == {"ring": 3, "dot": 1, "cross": 1, "triangle": 1, "square": 1}
    particles_only = view_frame(scene, TrackStyle(), TrackFilter(), XY, 0.0)
    assert particles_only.lines == [] and shapes(particles_only) == shapes(frame)
    crossings = TrackStyle(markers=frozenset({MarkerGroup.CROSSINGS}))
    assert shapes(view_frame(scene, crossings, TrackFilter(), XY, 0.0)) == {"small": 1}


def test_event_steps_move_the_particles_along_their_flights(scene: TrackScene):
    frame = view_frame(scene, LINES, TrackFilter(), XY, 0.0, Timeline.EVENTS, 1.5)
    assert pairs(frame) == [
        ((0.0, 0.0), (0.0, 0.0)),  # history 2, along z: a point in the XY view
        ((0.0, 0.0), (2.0, 0.0)),
        ((2.0, 0.0), (2.0, 1.0)),  # half of the second flight
    ]
    assert heads(frame) == [(2.0, 1.0)]  # history 2 ended at step 1; the photon is not born
    assert sorted(frame.states.tolist()) == [0, 1, 6, 7]
    at_two = view_frame(scene, LINES, TrackFilter(), XY, 0.0, Timeline.EVENTS, 2.0)
    # the photon starts with its parent's collision: both are at (2, 2) now
    assert heads(at_two) == [(2.0, 2.0), (2.0, 2.0)]


def test_time_of_flight(scene: TrackScene):
    frame = view_frame(scene, LINES, TrackFilter(), XY, 0.0, Timeline.TIME, 1.5e-9)
    assert heads(frame) == [(2.0, 1.0)]
    later = view_frame(scene, LINES, TrackFilter(), XY, 0.0, Timeline.TIME, 2.05e-9)
    assert (4.0, 2.0) in heads(later)  # the photon is half way to (6, 2)


def test_slab_around_the_cut_plane(scene: TrackScene):
    style = TrackStyle(lines=True, slab=1.0)
    frame = view_frame(scene, style, TrackFilter(), XY, 0.0)
    assert set(frame.states.tolist()) == {0, 1, 2, 4, 5}  # history 2 lies at z = 5 … 8
    # the flight along z from (2, 2, 0) keeps its first cm: a point in the XY view
    assert ((2.0, 2.0), (2.0, 2.0)) in pairs(frame)
    side = view_frame(scene, style, TrackFilter(), XZ, 2.0)  # around the plane y = 2
    assert ((2.0, 0.0), (2.0, 4.0)) in pairs(side)
    assert ((0.0, 0.0), (2.0, 0.0)) not in pairs(side)  # y = 0 is outside the slab


def test_slab_clips_oblique_flights():
    data = sample_tracks()
    data.position[5] = (6, 2, 4)  # the photon flies up at 45°
    scene = TrackScene(data)
    style = TrackStyle(lines=True, slab=1.0, line_color=TrackColor.PARTICLE)
    frame = view_frame(scene, style, TrackFilter(particles=frozenset({22})), XY, 0.0)
    assert pairs(frame) == [((2.0, 2.0), (3.0, 2.0))]  # z = 0 … 1 of z = 0 … 4
    assert frame.states.tolist() == [4]  # its end at z = 4 is outside
    halfway = view_frame(
        scene, style, TrackFilter(particles=frozenset({22})), XY, 0.0, Timeline.EVENTS, 2.125
    )
    assert pairs(halfway) == [((2.0, 2.0), (2.5, 2.0))] and heads(halfway) == [(2.5, 2.0)]
    beyond = view_frame(
        scene, style, TrackFilter(particles=frozenset({22})), XY, 0.0, Timeline.EVENTS, 2.5
    )
    assert heads(beyond) == []  # flying above the slab now


def test_filters(scene: TrackScene):
    neutrons = TrackFilter(particles=frozenset({2112}))
    frame = view_frame(scene, LINES, neutrons, XY, 0.0)
    assert 4 not in frame.states and 5 not in frame.states
    second = view_frame(scene, LINES, TrackFilter(history=2), XY, 0.0)
    assert sorted(second.states.tolist()) == [6, 7]
    ranged = view_frame(scene, LINES, TrackFilter(first=2), XY, 0.0)
    assert sorted(ranged.states.tolist()) == [6, 7]
    fast = view_frame(scene, LINES, TrackFilter(energy_min=1e6), XY, 0.0)
    assert sorted(fast.states.tolist()) == [0, 1, 4, 5, 6]


def test_colours(scene: TrackScene):
    states = np.arange(8)
    index, colors = scene.colors(TrackColor.ENERGY, (0, 0, 0), states)
    assert len(colors) == ENERGY_BINS and index.min() >= 0 and index.max() < ENERGY_BINS
    assert index[0] > index[2]  # 2 MeV is redder than 100 keV
    index, colors = scene.colors(TrackColor.PARTICLE, (0, 0, 0), states)
    assert colors[index[4]] == PARTICLE_COLORS[22] and colors[index[0]] == PARTICLE_COLORS[2112]
    index, colors = scene.colors(TrackColor.FIXED, (1, 2, 3), states)
    assert colors == [(1, 2, 3)] and set(index.tolist()) == {0}
    style = TrackStyle(lines=True, line_color=TrackColor.HISTORY)
    frame = view_frame(scene, style, TrackFilter(), XY, 0.0)
    assert len(frame.lines) == 2  # one colour per history


def test_timeline_positions(scene: TrackScene):
    assert scene.timeline_range(Timeline.EVENTS) == (0.0, 3.0)
    low, high = scene.timeline_range(Timeline.TIME)
    assert high == pytest.approx(-6.0) and low == pytest.approx(-9.25)
    assert key_at(scene, Timeline.TIME, -9.0) == pytest.approx(1e-9)
    assert key_at(scene, Timeline.EVENTS, 3.0) == math.inf and key_at(scene, Timeline.EVENTS, None)
    assert position_of(scene, Timeline.EVENTS, 5) == 3.0
    assert position_of(scene, Timeline.TIME, 0) == low  # born at t = 0
    assert position_of(scene, Timeline.TIME, 3) == pytest.approx(-6.0)


def test_nearest_state_and_history_states(scene: TrackScene):
    frame = view_frame(scene, TrackStyle(), TrackFilter(), XY, 0.0)
    assert nearest_state(frame, 5.9, 2.1, 0.5) == 5
    assert nearest_state(frame, 4.0, 4.0, 0.5) is None
    assert scene.history_states(1).tolist() == [0, 1, 2, 3, 4, 5]
