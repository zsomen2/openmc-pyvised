"""View geometry without Qt: plane bases, view windows, slice requests and pixels.

A view shows a plane of the model with the horizontal axis u to the right and
the vertical axis v up; the cut position w is the coordinate along the normal.
"""

import math
from dataclasses import dataclass

from pyvised_protocol.schemas import (
    MAX_SLICE_PIXELS,
    BoundingBox,
    SliceRequest,
    WeightWindowLayer,
)
from pyvised_protocol.sidecar import Basis

AXIS_NAMES = ("x", "y", "z")
BASES: dict[Basis, tuple[int, int, int]] = {
    "xy": (0, 1, 2),
    "xz": (0, 2, 1),
    "yz": (1, 2, 0),
}
"""Basis -> world axis indices of (horizontal, vertical, normal)."""


def axis_names(basis: Basis) -> tuple[str, str, str]:
    h, v, n = BASES[basis]
    return AXIS_NAMES[h], AXIS_NAMES[v], AXIS_NAMES[n]


def world_point(basis: Basis, u: float, v: float, cut: float) -> tuple[float, float, float]:
    point = [0.0, 0.0, 0.0]
    h, vert, n = BASES[basis]
    point[h], point[vert], point[n] = u, v, cut
    return point[0], point[1], point[2]


def plane_extent(
    box: BoundingBox, basis: Basis
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:
    """Ranges of (u, v, w) covered by the bounding box."""
    lo, hi = box.lower_left, box.upper_right
    h, v, n = BASES[basis]
    return (lo[h], hi[h]), (lo[v], hi[v]), (lo[n], hi[n])


@dataclass(frozen=True)
class SliceLayers:
    """The optional maps a view asks for besides the deepest cells."""

    level: int | None = None
    """Universe level of the `level_cells` layer (universe colours at that level)."""
    temperature: bool = False
    weight_windows: WeightWindowLayer | None = None


@dataclass(frozen=True)
class Window:
    """A rectangle of the plane in view coordinates [cm]."""

    u0: float
    v0: float
    width: float
    height: float

    @property
    def center(self) -> tuple[float, float]:
        return self.u0 + self.width / 2, self.v0 + self.height / 2

    def contains(self, u: float, v: float) -> bool:
        return self.u0 <= u <= self.u0 + self.width and self.v0 <= v <= self.v0 + self.height


def slice_request(
    basis: Basis,
    window: Window,
    cut: float,
    pixels: tuple[int, int],
    *,
    overlaps: bool = True,
    layers: SliceLayers | None = None,
) -> SliceRequest:
    cu, cv = window.center
    layers = layers or SliceLayers()
    return SliceRequest(
        basis=basis,
        origin=world_point(basis, cu, cv, cut),
        width=(window.width, window.height),
        pixels=pixels,
        show_overlaps=overlaps,
        level=layers.level,
        properties=layers.temperature,
        weight_windows=layers.weight_windows,
    )


def pixel_index(
    window: Window, shape: tuple[int, int], u: float, v: float
) -> tuple[int, int] | None:
    """(row, col) of the pixel under (u, v) in a slice of `shape` (rows, cols); row 0 is the top."""
    if not window.contains(u, v):
        return None
    rows, cols = shape
    col = int((u - window.u0) / window.width * cols)
    row = int((window.v0 + window.height - v) / window.height * rows)
    return min(max(row, 0), rows - 1), min(max(col, 0), cols - 1)


def device_pixels(
    width: float, height: float, ratio: float, quality: float, limit: int = MAX_SLICE_PIXELS
) -> tuple[int, int]:
    """Slice size in pixels for a view of `width` x `height` logical pixels."""
    w = max(1.0, width * ratio * quality)
    h = max(1.0, height * ratio * quality)
    scale = min(1.0, limit / max(w, h))
    return max(1, round(w * scale)), max(1, round(h * scale))


def nice_step(span: float, steps: int = 1000) -> float:
    """A 1-2-5 step close to span / steps (for spin boxes)."""
    if span <= 0:
        return 0.1
    raw = span / steps
    exponent = math.floor(math.log10(raw))
    for mantissa in (1, 2, 5, 10):
        step = mantissa * 10**exponent
        if step >= raw:
            return step
    return 10 ** (exponent + 1)
