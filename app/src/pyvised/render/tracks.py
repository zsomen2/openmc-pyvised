"""Particle tracks on a view, without Qt: filters, colours, the cut-plane slab and playback.

A `TrackScene` wraps the tracks of one tracked run with what every frame needs
(the flights, playback keys, the energy scale). `view_frame` turns it into
what one view draws at a playback position: the states reached so far as
points (by shape and colour), the flown part of every flight as line pairs
(by colour) and the particles in flight. Positions are projected onto the
view's plane (u, v); with a slab only what lies within ±slab of the cut plane
is kept. The timeline is the event step (all histories advance one event per
step, and a particle moves along its current flight in between) or the time of
flight.
"""

import math
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

from pyvised_protocol.tracks import TrackData, TrackEvent

from . import palette
from .palette import RGB
from .scales import Scale


class Timeline(StrEnum):
    EVENTS = "events"
    TIME = "time"


class TrackColor(StrEnum):
    ENERGY = "energy"
    PARTICLE = "particle"
    EVENT = "event"
    """Particles only: what happened at the state."""
    HISTORY = "history"
    FIXED = "fixed"


class MarkerGroup(StrEnum):
    BIRTHS = "births"
    COLLISIONS = "collisions"
    CROSSINGS = "crossings"
    ENDS = "ends"


GROUP_OF_EVENT = {
    TrackEvent.SOURCE: MarkerGroup.BIRTHS,
    TrackEvent.SECONDARY: MarkerGroup.BIRTHS,
    TrackEvent.SPLIT: MarkerGroup.BIRTHS,
    TrackEvent.CROSSING: MarkerGroup.CROSSINGS,
    TrackEvent.COLLISION: MarkerGroup.COLLISIONS,
    TrackEvent.REFLECTION: MarkerGroup.COLLISIONS,
    TrackEvent.JUMP: MarkerGroup.CROSSINGS,
    TrackEvent.LEAKED: MarkerGroup.ENDS,
    TrackEvent.ABSORBED: MarkerGroup.ENDS,
    TrackEvent.KILLED: MarkerGroup.ENDS,
}
GROUPS = list(MarkerGroup)
SHAPE_OF_EVENT = {
    TrackEvent.SOURCE: "ring",
    TrackEvent.SECONDARY: "ring",
    TrackEvent.SPLIT: "ring",
    TrackEvent.CROSSING: "small",
    TrackEvent.COLLISION: "dot",
    TrackEvent.REFLECTION: "dot",
    TrackEvent.JUMP: "small",
    TrackEvent.LEAKED: "triangle",
    TrackEvent.ABSORBED: "cross",
    TrackEvent.KILLED: "square",
}
"""How the item draws each event: dots and small dots, or outlined symbols."""
SHAPES = ["ring", "small", "dot", "triangle", "cross", "square"]
DEFAULT_MARKERS = frozenset({MarkerGroup.BIRTHS, MarkerGroup.COLLISIONS, MarkerGroup.ENDS})

ENERGY_BINS = 16
ENERGY_MAP = "thermal"
"""Blue (thermal) to red (fast)."""
PARTICLE_COLORS: dict[int, RGB] = {
    2112: (30, 110, 255),
    22: (240, 170, 0),
    11: (0, 170, 90),
    -11: (220, 0, 160),
}
OTHER_PARTICLE: RGB = (128, 128, 128)
EVENT_COLORS: dict[TrackEvent, RGB] = {
    TrackEvent.SOURCE: (0, 190, 70),
    TrackEvent.SECONDARY: (0, 180, 200),
    TrackEvent.SPLIT: (150, 90, 255),
    TrackEvent.CROSSING: (140, 140, 140),
    TrackEvent.COLLISION: (255, 140, 0),
    TrackEvent.REFLECTION: (230, 200, 0),
    TrackEvent.JUMP: (140, 140, 140),
    TrackEvent.LEAKED: (230, 0, 120),
    TrackEvent.ABSORBED: (200, 30, 30),
    TrackEvent.KILLED: (130, 80, 40),
}
HISTORY_COLORS: list[RGB] = [palette.spread_color(i) for i in range(24)]
DEFAULT_PARTICLE_RGB: RGB = (255, 140, 0)
DEFAULT_LINE_RGB: RGB = (30, 110, 255)
TIME_MARGIN = 0.25
"""Decades before the first positive time where the time-of-flight timeline starts."""


@dataclass(frozen=True)
class TrackStyle:
    lines: bool = False
    """Draw the flights as lines too, not only the particles."""
    particle_color: TrackColor = TrackColor.ENERGY
    particle_rgb: RGB = DEFAULT_PARTICLE_RGB
    line_color: TrackColor = TrackColor.ENERGY
    line_rgb: RGB = DEFAULT_LINE_RGB
    markers: frozenset[MarkerGroup] = DEFAULT_MARKERS
    slab: float | None = None
    """Half-width [cm] of the slab around the cut plane; None projects whole tracks."""


@dataclass(frozen=True)
class TrackFilter:
    first: int = 1
    """First and last history shown (1-based numbers, inclusive)."""
    last: int | None = None
    particles: frozenset[int] | None = None
    """PDG codes shown; None: all."""
    energy_min: float | None = None
    energy_max: float | None = None
    """[eV] states and flights outside are hidden."""
    history: int | None = None
    """Only this history (1-based); it overrides the range."""


@dataclass
class ViewFrame:
    """What one view draws: everything in view coordinates (u, v) [cm], float64."""

    lines: list[tuple[RGB, np.ndarray]] = field(default_factory=list)
    """(2k, 2) start and end points of k line pieces."""
    points: list[tuple[str, RGB, np.ndarray]] = field(default_factory=list)
    """Shape (see `SHAPE_OF_EVENT`), colour, (k, 2) positions."""
    heads: list[tuple[RGB, np.ndarray]] = field(default_factory=list)
    """Particles in flight."""
    states: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    """The reached states on this view (for hovering), and their positions (k, 2)."""
    state_uv: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))

    @property
    def segments(self) -> int:
        return sum(len(uv) // 2 for _, uv in self.lines)


def energy_scale(energies: np.ndarray) -> Scale:
    """A log scale over whole decades around the positive `energies` [eV]."""
    positive = energies[energies > 0]
    if positive.size == 0:
        return Scale(1e-5, 2e7, log=True)
    low = 10.0 ** math.floor(math.log10(float(positive.min())))
    high = 10.0 ** math.ceil(math.log10(float(positive.max())))
    return Scale(low, high if high > low else low * 10, log=True)


def energy_colors() -> list[RGB]:
    colors = palette.color_map(ENERGY_MAP)
    index = ((np.arange(ENERGY_BINS) + 0.5) / ENERGY_BINS * (len(colors) - 1)).round()
    return [tuple(int(c) for c in colors[int(i)]) for i in index]  # type: ignore[misc]


class TrackScene:
    """The tracks of one run with the arrays every frame uses (computed once)."""

    def __init__(self, data: TrackData) -> None:
        self.data = data
        self.position = data.position.astype(np.float64)
        self.state_track = data.state_track()
        self.state_history = data.track_history[self.state_track].astype(np.int64)
        self.state_particle = data.track_particle[self.state_track]
        n = data.n_states
        last = np.zeros(n, dtype=bool)
        last[data.track_start[1:] - 1] = True
        flown = np.flatnonzero(~last)
        self.flights = flown[data.event[flown + 1] != TrackEvent.JUMP]
        """Start states of the flights (the flight ends at the next state)."""
        self.steps = data.step.astype(np.float64)
        self.max_step = int(data.step.max(initial=0))
        positive = data.time[data.time > 0]
        self.time_low = float(positive.min()) if positive.size else 1e-12
        self.time_high = max(float(data.time.max(initial=0.0)), self.time_low * 10)
        self.energy_scale = energy_scale(data.energy)
        self.energy_colors = energy_colors()
        events = list(TrackEvent)
        groups = np.array([GROUPS.index(GROUP_OF_EVENT[e]) for e in events], dtype=np.int8)
        shapes = np.array([SHAPES.index(SHAPE_OF_EVENT[e]) for e in events], dtype=np.int8)
        self.group = groups[data.event]
        self.shape = shapes[data.event]
        self.particles = sorted({int(p) for p in data.track_particle})
        t = np.nan_to_num(self.energy_scale.normalise(data.energy), nan=0.0)
        particle_index = np.searchsorted(np.array(self.particles), self.state_particle)
        self._color_index = {
            TrackColor.ENERGY: np.clip(t * ENERGY_BINS, 0, ENERGY_BINS - 1).astype(np.int16),
            TrackColor.PARTICLE: particle_index.astype(np.int16),
            TrackColor.EVENT: data.event.astype(np.int16),
            TrackColor.HISTORY: (self.state_history % len(HISTORY_COLORS)).astype(np.int16),
        }
        """A colour index per state for every colour mode but the fixed colour."""
        self._palettes: dict[TrackColor, list[RGB]] = {
            TrackColor.ENERGY: self.energy_colors,
            TrackColor.PARTICLE: [PARTICLE_COLORS.get(c, OTHER_PARTICLE) for c in self.particles],
            TrackColor.EVENT: [EVENT_COLORS[e] for e in events],
            TrackColor.HISTORY: HISTORY_COLORS,
        }
        self._uv: dict[tuple[int, int], np.ndarray] = {}
        self._filter: tuple[TrackFilter, np.ndarray, np.ndarray] | None = None

    @property
    def model_id(self) -> str:
        return self.data.summary.model_id

    @property
    def histories(self) -> int:
        return self.data.n_histories

    def keys(self, timeline: Timeline) -> np.ndarray:
        return self.steps if timeline is Timeline.EVENTS else self.data.time

    def timeline_range(self, timeline: Timeline) -> tuple[float, float]:
        """Range of the playback position: steps, or log10 of the time [s]."""
        if timeline is Timeline.EVENTS:
            return 0.0, float(max(self.max_step, 1))
        return math.log10(self.time_low) - TIME_MARGIN, math.log10(self.time_high)

    def uv(self, h: int, v: int) -> np.ndarray:
        """(N, 2) positions on a plane with world axes h (horizontal) and v (vertical)."""
        found = self._uv.get((h, v))
        if found is None:
            found = self._uv[(h, v)] = np.ascontiguousarray(self.position[:, [h, v]])
        return found

    def mask(self, filt: TrackFilter) -> np.ndarray:
        """States that pass the filter."""
        return self.filtered(filt)[0]

    def filtered(self, filt: TrackFilter) -> tuple[np.ndarray, np.ndarray]:
        """The states that pass the filter (a mask) and the flights that start at one."""
        if self._filter is not None and self._filter[0] == filt:
            return self._filter[1], self._filter[2]
        number = self.state_history + 1
        if filt.history is not None:
            mask = number == filt.history
        else:
            mask = number >= filt.first
            if filt.last is not None:
                mask &= number <= filt.last
        if filt.particles is not None:
            mask &= np.isin(self.state_particle, np.fromiter(filt.particles, dtype=np.int64))
        energy = self.data.energy
        if filt.energy_min is not None:
            mask &= energy >= filt.energy_min
        if filt.energy_max is not None:
            mask &= energy <= filt.energy_max
        flights = self.flights[mask[self.flights]]
        self._filter = (filt, mask, flights)
        return mask, flights

    def history_states(self, history: int) -> np.ndarray:
        """The states of a history (1-based), track after track."""
        return np.flatnonzero(self.state_history == history - 1)

    def colors(
        self, mode: TrackColor, rgb: RGB, states: np.ndarray
    ) -> tuple[np.ndarray, list[RGB]]:
        """A colour index per state and the colours it points to."""
        if mode is TrackColor.FIXED:
            return np.zeros(len(states), dtype=np.int16), [rgb]
        return self._color_index[mode][states], self._palettes[mode]


def key_at(scene: TrackScene, timeline: Timeline, position: float | None) -> float:
    """The key (step or time [s]) of a playback position; None or the end: everything."""
    if position is None or position >= scene.timeline_range(timeline)[1]:
        return math.inf
    return position if timeline is Timeline.EVENTS else 10.0**position


def position_of(scene: TrackScene, timeline: Timeline, state: int) -> float:
    """The playback position where `state` is reached."""
    if timeline is Timeline.EVENTS:
        return float(scene.steps[state])
    low = scene.timeline_range(timeline)[0]
    t = float(scene.data.time[state])
    return math.log10(t) if t > 0 else low


def _by_color(index: np.ndarray, colors: list[RGB]) -> list[tuple[RGB, np.ndarray]]:
    """Positions into `index` grouped by colour."""
    if index.size == 0:
        return []
    first = int(index[0])
    if np.all(index == first):
        return [(colors[first], np.arange(index.size))]
    order = np.argsort(index.astype(np.int16), kind="stable")  # a radix sort
    cuts = np.flatnonzero(np.diff(index[order])) + 1
    return [(colors[int(index[g[0]])], g) for g in np.split(order, cuts)]


def view_frame(
    scene: TrackScene,
    style: TrackStyle,
    filt: TrackFilter,
    axes: tuple[int, int, int],
    cut: float,
    timeline: Timeline = Timeline.EVENTS,
    key: float = math.inf,
) -> ViewFrame:
    """What a view with world axes (horizontal, vertical, normal) and its cut plane at `cut`
    draws when the playback has reached `key`."""
    h, v, n = axes
    uv = scene.uv(h, v)
    w = scene.position[:, n]
    keys = scene.keys(timeline)
    mask, flights = scene.filtered(filt)
    everything = math.isinf(key)
    frame = ViewFrame()

    reached = mask if everything else mask & (keys <= key)
    if style.slab is not None:
        reached = reached & (np.abs(w - cut) <= style.slab)
    states = np.flatnonzero(reached)
    frame.states, frame.state_uv = states, uv[states]
    shown = np.zeros(len(GROUPS), dtype=bool)
    shown[[GROUPS.index(g) for g in style.markers]] = True
    marked = states[shown[scene.group[states]]]
    shapes = scene.shape[marked]
    for code in np.unique(shapes).tolist():
        chosen = marked[shapes == code]
        index, colors = scene.colors(style.particle_color, style.particle_rgb, chosen)
        for rgb, group in _by_color(index, colors):
            frame.points.append((SHAPES[code], rgb, uv[chosen[group]]))

    if not everything:
        flights = flights[keys[flights] <= key]
    a, b = uv[flights], uv[flights + 1]
    lo = np.zeros(len(flights))
    hi = np.ones(len(flights))
    if style.slab is not None:
        w0, w1 = w[flights] - cut, w[flights + 1] - cut
        dw = w1 - w0
        flat = dw == 0
        with np.errstate(divide="ignore", invalid="ignore"):
            t0 = np.where(flat, -np.inf, (-style.slab - w0) / dw)
            t1 = np.where(flat, np.inf, (style.slab - w0) / dw)
        lo = np.maximum(lo, np.minimum(t0, t1))
        hi = np.minimum(hi, np.maximum(t0, t1))
        hi[flat & (np.abs(w0) > style.slab)] = -1.0
    if everything:
        progress = np.ones(len(flights))
    else:
        k0, k1 = keys[flights], keys[flights + 1]
        span = k1 - k0
        with np.errstate(divide="ignore", invalid="ignore"):
            progress = np.clip(np.where(span > 0, (key - k0) / span, 1.0), 0.0, 1.0)
    if style.lines:
        top = np.minimum(hi, progress)
        drawn = np.flatnonzero(top > lo)
        step = b[drawn] - a[drawn]
        start = a[drawn] + lo[drawn, None] * step
        end = a[drawn] + top[drawn, None] * step
        index, colors = scene.colors(style.line_color, style.line_rgb, flights[drawn])
        for rgb, group in _by_color(index, colors):
            pairs = np.empty((2 * len(group), 2))
            pairs[0::2] = start[group]
            pairs[1::2] = end[group]
            frame.lines.append((rgb, pairs))
    if not everything:
        flying = np.flatnonzero((keys[flights + 1] > key) & (progress >= lo) & (progress <= hi))
        if flying.size:
            at = a[flying] + progress[flying, None] * (b[flying] - a[flying])
            index, colors = scene.colors(style.particle_color, style.particle_rgb, flights[flying])
            for rgb, group in _by_color(index, colors):
                frame.heads.append((rgb, at[group]))
    return frame


def nearest_state(frame: ViewFrame, u: float, v: float, radius: float) -> int | None:
    """The reached state closest to (u, v), if within `radius` [cm]."""
    if frame.states.size == 0:
        return None
    d2 = (frame.state_uv[:, 0] - u) ** 2 + (frame.state_uv[:, 1] - v) ** 2
    best = int(np.argmin(d2))
    return int(frame.states[best]) if d2[best] <= radius * radius else None


_PREFIXES = ((1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p"))


def with_prefix(value: float, unit: str) -> str:
    """`value` with an SI prefix and 3 significant digits, e.g. "2.5 MeV", "35.1 µs"."""
    if value == 0 or not math.isfinite(value):
        return f"{value:g} {unit}"
    for scale, prefix in _PREFIXES:
        if abs(value) >= scale * 0.9995:
            return f"{value / scale:.3g} {prefix}{unit}"
    return f"{value:.3g} {unit}"
